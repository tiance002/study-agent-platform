"""学习闭环路由：诊断、任务流转与提交证据（产品域之三）。

任务详情里的 `verified` 是对学习证据的聚合结论（计算值，不落库）；
诊断 → 生成计划 → 提交证据构成最小学习闭环。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.api.product_schemas import (
    DiagnosisBody,
    GeneratePlanBody,
    SubmissionBody,
    TaskTransitionBody,
)
from app.core.ids import new_id
from app.identity.models import Principal
from app.learning.evidence import Direction, EvidenceKind, Validity
from app.learning.plan_builders import build_generated_bundle
from app.product.models import TaskStatus

router = APIRouter()


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request) -> Principal:
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


def _task_verified(events, task_id: str) -> bool:
    """verified 的唯一定义：该任务存在 kind=LEARNING 且含有效正向裁决的证据。

    无效裁决（INCONCLUSIVE/VOIDED）与负向证据都不能让任务变绿 ——
    否则"掌握度只由合法证据更新"就会在这里漏一个口子。
    """
    for event in events:
        if event.task_id != task_id or event.kind is not EvidenceKind.LEARNING:
            continue
        for verdict in event.verdicts:
            if verdict.assessment_validity is Validity.VALID and verdict.direction is Direction.POSITIVE:
                return True
    return False


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
            return guard.replay_response()
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
            "verified": _task_verified(state.evidence.events_for(guard.principal, project_id), task_id),
        }
        guard.complete(200, result)
        return result


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
def create_diagnosis(request: Request, project_id: str, body: DiagnosisBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
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
        return Response(status_code=204)
    return row.to_dict()


@router.post("/projects/{project_id}/plan/generate", status_code=201, response_model=None)
def generate_plan(request: Request, project_id: str, body: GeneratePlanBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write
    from app.core.errors import ErrorCode, deny

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
        state = _state(request)
        diagnosis = state.learning_loop.latest_diagnosis(guard.principal, project_id)
        if diagnosis is None:
            raise deny(ErrorCode.PARAMS_INVALID, "请先完成基础诊断，再生成学习计划")
        project = state.membership.get(guard.principal, project_id)
        bundle, assessments = build_generated_bundle(
            guard.principal,
            project_id,
            project.goal,
            diagnosis.summary,
            now=state.clock.now(),
        )
        saved = state.learning_loop.install_generated_plan(guard.principal, project_id, bundle, assessments)
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
def submit_task(request: Request, project_id: str, task_id: str, body: SubmissionBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
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
    rows = _state(request).learning_loop.submissions_for_task(_actor(request), project_id, task_id)
    return {"submissions": [row.to_dict() for row in rows]}
