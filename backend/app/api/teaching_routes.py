"""教学运行的 HTTP 入口（第五轮任务 5）。

## 三条冻结的接口纪律

1. **客户端只提供 `question`**：role、tenant、provider、model、prompt、
   usage、tool schema 一律服务端决定。请求模型 `extra="forbid"` ——
   多出来的字段直接拒绝，不给"先用着，以后再校验"留门。
2. **创建必须带 `Idempotency-Key`**（复用全局幂等守卫）：同键同体返回
   同一个 run（`X-Idempotent-Replay: true`），冲突体拒绝 ——
   客户端超时重试不会创建第二个 run、扣第二笔预算。
3. **SSE 是"批量重放"模型**：每次连接重新认证授权，回放持久化事件
   （有界批次）后结束；客户端凭 `Last-Event-ID` 续传、按 event id 去重。
   断开只结束订阅 —— worker 继续处理，重连不触发 generate。
   首版只发**状态与已校验的最终结果**事件；未校验文本不得冒充
   已引用结论（02 号规格）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ErrorCode, PlatformError, deny
from app.knowledge.retrieval import RANKING_VERSION
from app.teaching.prompts import SYSTEM_PROMPT_VERSION
from app.teaching.runs import RunStatus

router = APIRouter()

#: SSE 单次连接回放的事件批次上限。无界 JSON 流是慢客户端的
#: 资源放大入口；批次有界，客户端凭 Last-Event-ID 分批续传。
MAX_EVENTS_PER_BATCH = 200

#: 一次教学运行的下一动作（按状态推导，闭集）。
_NEXT_ACTIONS = {
    RunStatus.QUEUED: "poll",
    RunStatus.RUNNING: "poll",
    RunStatus.SUCCEEDED: "read_answer",
    RunStatus.FAILED: "none",
    RunStatus.RECONCILIATION_REQUIRED: "contact_support",
}


class TeachingRunBody(BaseModel):
    """创建教学运行的请求体。**只有 question** —— 其余全是服务端决策。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request):
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


def _require_provider(state) -> None:
    """教学功能显式开关：没有 provider 就明确 503，绝不静默降级。"""
    if state.teaching_provider is None:
        raise PlatformError(
            ErrorCode.TEACHING_PROVIDER_DISABLED,
            "教学功能未启用（STUDY_PLATFORM_TEACHING_PROVIDER）；请联系管理员",
        )


def _budget_limits(state) -> tuple[int, int, int]:
    """预算上限（部署配置；settings 为 None 的测试环境用默认值）。"""
    from app.deployment import (
        DEFAULT_TEACHING_MAX_INPUT_TOKENS,
        DEFAULT_TEACHING_MAX_OUTPUT_TOKENS,
        DEFAULT_TEACHING_PROJECT_BUDGET_MICRO,
    )

    settings = state.settings
    if settings is None:
        return (
            DEFAULT_TEACHING_PROJECT_BUDGET_MICRO,
            DEFAULT_TEACHING_MAX_INPUT_TOKENS,
            DEFAULT_TEACHING_MAX_OUTPUT_TOKENS,
        )
    return (
        settings.teaching_project_budget_micro,
        settings.teaching_max_input_tokens,
        settings.teaching_max_output_tokens,
    )


@router.post(
    "/projects/{project_id}/conversations/{conversation_id}/teaching-runs",
    response_model=None,
    status_code=202,
)
def create_teaching_run(
    request: Request,
    project_id: str,
    conversation_id: str,
    body: TeachingRunBody,
) -> dict | JSONResponse:  # noqa: UP007  (FastAPI 需要 response_model=None，见装饰器)
    """原子创建教学运行：用户消息 + 运行 + 预算预留（202）。"""
    from app.api.http_idempotency import idempotent_write
    from app.core.ids import new_id

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        _require_provider(state)
        budget_micro, max_input, max_output = _budget_limits(state)
        run = state.teaching.start_run(
            guard.principal,
            project_id,
            conversation_id,
            run_id=new_id("run"),
            question=body.question,
            # 模型与 prompt 版本是服务端决策；输入估计按逐字符上界，
            # 输出估计取承诺上限 —— 预算在派发前就把最坏情况算进去。
            model_id=_approved_model(state),
            prompt_version=SYSTEM_PROMPT_VERSION,
            ranking_version=RANKING_VERSION,
            estimated_input_tokens=len(body.question),
            estimated_output_tokens=max_output,
            budget_total_micro=budget_micro,
            budget_max_input_tokens=max_input,
            budget_max_output_tokens=max_output,
        )
        result = {
            "run_id": run.run_id,
            "status": str(run.status),
            "status_url": f"/projects/{project_id}/teaching-runs/{run.run_id}",
            "events_url": f"/projects/{project_id}/teaching-runs/{run.run_id}/events",
        }
        guard.complete(202, result)
        return result


@router.get("/projects/{project_id}/teaching-runs/{run_id}")
def get_teaching_run(request: Request, project_id: str, run_id: str) -> dict:
    """运行状态 + 安全错误 + 下一动作。不可见/不存在统一 404。"""
    state = _state(request)
    actor = _actor(request)
    run = state.teaching.get_run(actor, project_id, run_id)
    return {
        **run.to_dict(),
        "next_action": _NEXT_ACTIONS[run.status],
    }


@router.get("/projects/{project_id}/teaching-runs/{run_id}/events")
def stream_teaching_events(request: Request, project_id: str, run_id: str):
    """SSE 事件回放（批量重放模型，见模块 docstring）。

    - `Last-Event-ID`：客户端上次收到的 event id（seq）。非法值明确 400，
      不静默当 0 —— 静默重放会让"去重"变成客户端的猜测。
    - 每次连接重新认证授权（get_run 内的成员判定）；撤销授权后
      重连即 404，内容不再外发。
    """
    state = _state(request)
    actor = _actor(request)
    state.teaching.get_run(actor, project_id, run_id)  # 授权：不可见即 404

    raw = request.headers.get("Last-Event-ID")
    after_seq = 0
    if raw is not None:
        try:
            after_seq = int(raw)
        except ValueError:
            raise deny(
                ErrorCode.PARAMS_INVALID,
                "Last-Event-ID 必须是事件序号（正整数）",
            ) from None
        if after_seq < 0:
            raise deny(ErrorCode.PARAMS_INVALID, "Last-Event-ID 不能为负")

    events = state.teaching.list_events(actor, project_id, run_id, after_seq=after_seq)
    batch = events[:MAX_EVENTS_PER_BATCH]
    truncated = len(events) > MAX_EVENTS_PER_BATCH

    def generate():
        # retry 提示客户端断线后的重连间隔（SSE 标准字段，非业务消息）。
        yield "retry: 3000\n\n"
        for event in batch:
            yield event.sse_lines() + "\n\n"
        if truncated:
            # 显式的"还有更多"：客户端继续带 Last-Event-ID 续传。
            yield "event: batch.truncated\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _approved_model(state) -> str:
    """批准的模型 id：部署配置决定；测试环境（无 settings）用占位。"""
    settings = state.settings
    if settings is not None and settings.teaching_model:
        return settings.teaching_model
    return "test-model-v1"
