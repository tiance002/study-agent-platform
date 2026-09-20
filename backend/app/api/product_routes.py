"""会话、消息、计划与资料登记的 HTTP 入口（任务 5）。

归属与认证边界与项目路由共享（`authenticate_request` 唯一入口）。

**服务端负责分配一切标识**：conversation_id / message_id / plan_id /
milestone_id / source_id 全部由服务端生成并随响应返回——客户端不提供 id，
提供 id 就等于提供了一种"猜中别人的资源"的方式（会被 404 拦，但没必要开门）。

**资料去重**：`identity_hash` 由服务端从 `acquisition` 规范化 JSON 派生 ——
同一项目内同一获取方式重复登记返回既有记录（幂等成功）。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.hashing import canonical_json
from app.core.ids import new_id
from app.identity.models import Principal
from app.knowledge.models import MAX_DOCUMENT_BYTES, require_document_content
from app.learning.evidence import Direction, EvidenceKind, Validity
from app.learning.ports import COMPONENTS_V1, TaskAssessment
from app.product.models import (
    LearningPlan,
    LearningTask,
    MessageRole,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)

router = APIRouter()

CONTENT_MAX_CHARS = 32_000
TITLE_MAX_CHARS = 200
GOAL_MAX_CHARS = 2000
#: 资料标题上限。比会话标题宽松：文献标题常带编号与副标题，200 字容易不够。
DOCUMENT_TITLE_MAX_CHARS = 300


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request) -> Principal:
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


# ---------------------------------------------------------------- 请求模型


class ConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=TITLE_MAX_CHARS)


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = Field(min_length=1, max_length=CONTENT_MAX_CHARS)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)


class MilestoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tasks: list[TaskInput] = Field(default_factory=list)


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=GOAL_MAX_CHARS)
    milestones: list[MilestoneInput] = Field(min_length=1)


class TaskTransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Literal 让非法状态名在请求校验层就 422，不进业务层。
    expected_status: Literal["pending", "in_progress", "done", "skipped"]
    next_status: Literal["pending", "in_progress", "done", "skipped"]


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(default="", max_length=100)
    acquisition: dict = Field(default_factory=dict)


class SourceContentBody(BaseModel):
    """上传一版原文并把摄取任务入队。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=DOCUMENT_TITLE_MAX_CHARS)
    #: ⚠️ 这里的 `max_length` 是**字符**上限，只是字节上限的**粗筛**：
    #: UTF-8 每个字符至少一字节，所以字符数 ≤ 字节数 —— 超过字节上限的输入
    #: 必然也超过字符上限，粗筛不会漏（它的作用是不把巨量输入解码进内存）。
    #: 反方向不成立（中文一字三字节），因此真正的判定按字节做，
    #: 由下面的校验器调用 `require_document_content` —— **同一个出口**，
    #: 否则 40 万字中文能过应用层校验、却在数据库 CHECK 上炸成 500。
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_BYTES)
    media_type: Literal["text/plain", "text/markdown"]
    language: str = Field(default="zh", pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        return require_document_content(value)


class DiagnosisBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experience_level: Literal["beginner", "familiar", "experienced"]
    weekly_hours: int = Field(ge=1, le=40)
    preferred_style: Literal["reading", "practice", "mixed"]


class GeneratePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["self_report"]
    content: str = Field(min_length=1, max_length=20_000)


# ---------------------------------------------------------------- 会话与消息


@router.post("/projects/{project_id}/conversations", status_code=201, response_model=None)
def create_conversation(
    request: Request, project_id: str, body: ConversationBody
) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        conversation = state.products.create_conversation(
            guard.principal, project_id, conversation_id=new_id("conv"), title=body.title
        )
        result = conversation.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/conversations")
def list_conversations(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.products.list_conversations(_actor(request), project_id)
    return {"conversations": [c.to_dict() for c in rows]}


@router.post(
    "/projects/{project_id}/conversations/{conversation_id}/messages",
    status_code=201,
    response_model=None,
)
def append_message(
    request: Request, project_id: str, conversation_id: str, body: MessageBody
) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        message = state.products.append_message(
            guard.principal,
            project_id,
            conversation_id,
            message_id=new_id("msg"),
            role=body.role,
            content=body.content,
        )
        result = message.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/conversations/{conversation_id}/messages")
def list_messages(
    request: Request, project_id: str, conversation_id: str
) -> dict:
    state = _state(request)
    rows = state.products.list_messages(_actor(request), project_id, conversation_id)
    return {"messages": [m.to_dict() for m in rows]}


# ---------------------------------------------------------------- 计划


def _build_bundle(actor: Principal, project_id: str, body: PlanBody, *, now) -> PlanBundle:
    """把用户输入翻译成**完整契约**：所有 id 服务端生成（任务 1 端口约定：
    写入方法接收已生成的 id）。version 由仓储分配，这里填占位值 0……
    不行，契约要求正整数 —— 填 1，replace 会忽略它。"""
    plan_id = new_id("plan")
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    for order, milestone_input in enumerate(body.milestones):
        milestone_id = new_id("mile")
        milestones.append(
            Milestone(
                milestone_id=milestone_id,
                tenant_id=actor.tenant_id,
                project_id=project_id,
                plan_id=plan_id,
                order_index=order,
                title=milestone_input.title,
                description=milestone_input.description,
            )
        )
        for task_order, task_input in enumerate(milestone_input.tasks):
            tasks.append(
                LearningTask(
                    task_id=new_id("task"),
                    tenant_id=actor.tenant_id,
                    project_id=project_id,
                    milestone_id=milestone_id,
                    order_index=task_order,
                    title=task_input.title,
                    status=TaskStatus.PENDING,
                )
            )
    return PlanBundle(
        plan=LearningPlan(
            plan_id=plan_id,
            tenant_id=actor.tenant_id,
            project_id=project_id,
            version=1,  # 占位：replace 的契约是版本由实现分配
            goal=body.goal,
            status=PlanStatus.ACTIVE,
            created_at=now,
        ),
        milestones=tuple(milestones),
        tasks=tuple(tasks),
    )


@router.put("/projects/{project_id}/plan", response_model=None)
def replace_plan(request: Request, project_id: str, body: PlanBody) -> dict | JSONResponse:
    """整版替换：返回实现分配版本后的完整 bundle。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        bundle = state.products.replace_plan(
            guard.principal,
            project_id,
            _build_bundle(guard.principal, project_id, body, now=state.clock.now()),
        )
        result = {
            "plan": bundle.plan.to_dict(),
            "milestones": [m.to_dict() for m in bundle.milestones],
            "tasks": [t.to_dict() for t in bundle.tasks],
        }
        guard.complete(200, result)
        return result


@router.get("/projects/{project_id}/plan")
def current_plan(request: Request, project_id: str):
    state = _state(request)
    bundle = state.products.current_plan(_actor(request), project_id)
    if bundle is None:
        # 没有计划不是错误 —— 204 说明"还没有"，与"找不到"（404）分开。
        return JSONResponse(status_code=204, content=None)
    return {
        "plan": bundle.plan.to_dict(),
        "milestones": [m.to_dict() for m in bundle.milestones],
        "tasks": [t.to_dict() for t in bundle.tasks],
    }


@router.get("/projects/{project_id}/plan/history")
def plan_history(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.products.plan_history(_actor(request), project_id)
    return {"plans": [p.to_dict() for p in rows]}


# ---------------------------------------------------------------- 资料


def _identity_hash(acquisition: dict) -> str:
    """从获取方式派生稳定标识。**由服务端计算**：客户端声称的"同一资料"
    不算数，规范化 JSON 的哈希才算——同一 acquisition 必得同一哈希。"""
    digest = sha256(canonical_json(acquisition).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@router.post("/projects/{project_id}/sources", status_code=201, response_model=None)
def register_source(request: Request, project_id: str, body: SourceBody) -> JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        record = state.products.register_source(
            guard.principal,
            project_id,
            source_id=new_id("src"),
            display_name=body.display_name,
            media_type=body.media_type,
            identity_hash=_identity_hash(body.acquisition),
            acquisition=body.acquisition,
        )
        guard.complete(201, record.to_dict())
        return JSONResponse(status_code=201, content=record.to_dict())


@router.get("/projects/{project_id}/sources")
def list_sources(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.products.list_sources(_actor(request), project_id)
    return {"sources": [s.to_dict() for s in rows]}


@router.post(
    "/projects/{project_id}/sources/{source_id}/content",
    status_code=202,
    response_model=None,
)
def upload_source_content(
    request: Request, project_id: str, source_id: str, body: SourceContentBody
) -> dict | JSONResponse:
    """登记一版原文并入队一个摄取任务。**返回 202，请求内不切块。**

    切块是 worker 的重活，写在请求里的话：一次 1 MiB 的 Markdown 解析会占住
    一个 web worker 若干秒，客户端超时重试又压一份进来 —— 而重试本该是安全的。
    真正的进度看 `GET /projects/{project_id}/ingestion-jobs/{job_id}`。

    原文与任务在**一次调用**里写入（`enqueue` 的契约）：拆成两次会留下
    "原文在库里、没人处理"的半完成状态，而它看起来完全合法 ——
    用户看到的是"上传成功但永远搜不到"。
    """
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        document, job = state.ingestion.enqueue(
            guard.principal,
            project_id,
            source_id,
            document_id=new_id("doc"),
            job_id=new_id("job"),
            title=body.title,
            content=body.content,
            media_type=body.media_type,
            language=body.language,
        )
        result = {"document": document.to_dict(), "job": job.to_dict()}
        guard.complete(202, result)
        return result


@router.get("/projects/{project_id}/ingestion-jobs/{job_id}")
def get_ingestion_job(request: Request, project_id: str, job_id: str) -> dict:
    """摄取任务的稳定状态。只含状态、尝试次数、安全错误码与时间戳。

    **不含原文与片段**：这个端点会被客户端轮询，把原文放进来就是一个
    客户端可控的响应体放大入口（同一条理由让 `SourceDocument.to_dict()`
    不含 `content`）。原文回读走引用（`source_id + span + content_hash`）。
    """
    state = _state(request)
    job = state.ingestion.get_job(_actor(request), project_id, job_id)
    return job.to_dict()


# ------------------------------------------------- 任务流转 / 详情 / 掌握度


@router.get("/projects/{project_id}/tasks/{task_id}")
def get_task(request: Request, project_id: str, task_id: str) -> dict:
    """任务详情。`verified` 是**计算结论**：存在 >=1 条有效正向学习证据。

    它不存库 —— 存库就需要第二处写入者去维护它，而"已验证"的本质是
    对证据的聚合，不是一个新的可变状态。
    """
    state = _state(request)
    actor = _actor(request)
    task = state.products.get_task(actor, project_id, task_id)
    events = state.evidence.events_for(actor, project_id)
    return {**task.to_dict(), "verified": _task_verified(events, task_id)}


def _task_verified(events, task_id: str) -> bool:
    """verified 的唯一定义：该任务存在 kind=LEARNING 且含有效正向裁决的证据。

    无效裁决（INCONCLUSIVE/VOIDED）与负向证据都不能让任务变绿 ——
    否则"掌握度只由合法证据更新"就会在这里漏一个口子。
    """
    for event in events:
        if event.task_id != task_id or event.kind is not EvidenceKind.LEARNING:
            continue
        for verdict in event.verdicts:
            if (
                verdict.assessment_validity is Validity.VALID
                and verdict.direction is Direction.POSITIVE
            ):
                return True
    return False


@router.post(
    "/projects/{project_id}/tasks/{task_id}/transition",
    response_model=None,
)
def transition_task(
    request: Request, project_id: str, task_id: str, body: TaskTransitionBody
) -> dict | JSONResponse:
    """任务状态流转。非法迁移与并发过期都返回 409，但错误码不同：

    ILLEGAL_STATE_TRANSITION（迁移本身不合法）与 VERSION_CONFLICT
    （迁移合法但状态已被别人改掉）混在一起，客户端就无法区分
    "我的请求写错了"和"该刷新重试"。
    """
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        task = state.products.transition_task(
            guard.principal,
            project_id,
            task_id,
            expected_status=TaskStatus(body.expected_status),
            next_status=TaskStatus(body.next_status),
        )
        result = {
            **task.to_dict(),
            "verified": _task_verified(
                state.evidence.events_for(guard.principal, project_id), task_id
            ),
        }
        guard.complete(200, result)
        return result


# ----------------------------------------------------- 诊断 / 生成 / 提交闭环


def _diagnosis_summary(body: DiagnosisBody) -> str:
    levels = {
        "beginner": "初学",
        "familiar": "有一些基础",
        "experienced": "已有实践经验",
    }
    styles = {"reading": "阅读", "practice": "动手实践", "mixed": "混合方式"}
    return (
        f"{levels[body.experience_level]}；每周可投入 {body.weekly_hours} 小时；"
        f"偏好{styles[body.preferred_style]}。"
    )


@router.post("/projects/{project_id}/diagnosis", status_code=201, response_model=None)
def create_diagnosis(
    request: Request, project_id: str, body: DiagnosisBody
) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        row = _state(request).learning_loop.record_diagnosis(
            guard.principal,
            project_id,
            diagnosis_id=new_id("diag"),
            answers=body.model_dump(mode="json"),
            summary=_diagnosis_summary(body),
        )
        result = row.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/diagnosis")
def latest_diagnosis(request: Request, project_id: str):
    row = _state(request).learning_loop.latest_diagnosis(_actor(request), project_id)
    if row is None:
        return JSONResponse(status_code=204, content=None)
    return row.to_dict()


def _generated_bundle(
    actor: Principal, project_id: str, goal: str, summary: str, *, now
) -> tuple[PlanBundle, tuple[TaskAssessment, ...]]:
    plan_id = new_id("plan")
    # 标题有模型层 200 字上限；保留目标与诊断的首段，完整值仍在项目/诊断记录中。
    context = f"{goal.strip() or '完成学习目标'}（{summary}）"[:120]
    stage_specs = (
        ("理解核心概念", f"理解并解释：{context}"),
        ("完成实践练习", f"动手完成一个练习：{context}"),
        ("复盘与迁移", f"总结并迁移到新情境：{context}"),
    )
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    assessments: list[TaskAssessment] = []
    for order, ((milestone_title, task_title), component) in enumerate(
        zip(stage_specs, COMPONENTS_V1, strict=True)
    ):
        milestone_id = new_id("mile")
        task_id = new_id("task")
        milestones.append(
            Milestone(
                milestone_id, actor.tenant_id, project_id, plan_id,
                order, milestone_title, "",
            )
        )
        tasks.append(
            LearningTask(
                task_id, actor.tenant_id, project_id, milestone_id,
                0, task_title, TaskStatus.PENDING,
            )
        )
        assessments.append(
            TaskAssessment(
                new_id("asm"), task_id, component,
                "self-report/v1", "graph-v1/task-v1",
            )
        )
    bundle = PlanBundle(
        LearningPlan(
            plan_id, actor.tenant_id, project_id, 1, goal,
            PlanStatus.ACTIVE, now,
        ),
        tuple(milestones),
        tuple(tasks),
    )
    return bundle, tuple(assessments)


@router.post("/projects/{project_id}/plan/generate", status_code=201, response_model=None)
def generate_plan(
    request: Request, project_id: str, body: GeneratePlanBody
) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write
    from app.core.errors import ErrorCode, deny

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        diagnosis = state.learning_loop.latest_diagnosis(guard.principal, project_id)
        if diagnosis is None:
            raise deny(ErrorCode.PARAMS_INVALID, "请先完成基础诊断，再生成学习计划")
        project = state.membership.get(guard.principal, project_id)
        bundle, assessments = _generated_bundle(
            guard.principal, project_id, project.goal, diagnosis.summary,
            now=state.clock.now(),
        )
        saved = state.learning_loop.install_generated_plan(
            guard.principal, project_id, bundle, assessments
        )
        result = {
            "generator": "template/graph-v1",
            "plan": saved.plan.to_dict(),
            "milestones": [item.to_dict() for item in saved.milestones],
            "tasks": [item.to_dict() for item in saved.tasks],
        }
        guard.complete(201, result)
        return result


@router.post(
    "/projects/{project_id}/tasks/{task_id}/submissions",
    status_code=201,
    response_model=None,
)
def submit_task(
    request: Request, project_id: str, task_id: str, body: SubmissionBody
) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        submission, event = _state(request).learning_loop.submit_self_report(
            guard.principal,
            project_id,
            task_id,
            submission_id=new_id("sub"),
            content=body.content,
        )
        verdict = event.verdicts[0]
        result = {
            "submission": submission.to_dict(),
            "evidence": {
                "event_id": event.event_id,
                "component_id": verdict.component_id,
                "observation_strength": int(verdict.observation_strength),
                "independence_level": str(verdict.independence_level),
                "confidence_note": "self_report_low_observation",
            },
        }
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/tasks/{task_id}/submissions")
def list_task_submissions(request: Request, project_id: str, task_id: str) -> dict:
    rows = _state(request).learning_loop.submissions_for_task(
        _actor(request), project_id, task_id
    )
    return {"submissions": [row.to_dict() for row in rows]}
