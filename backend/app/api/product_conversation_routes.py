"""会话、消息与人工计划路由（产品域之一）。

归属与认证边界与项目路由共享（`authenticate_request` 唯一入口）。

**服务端负责分配一切标识**：conversation_id / message_id / plan_id /
milestone_id 全部由服务端生成并随响应返回——客户端不提供 id，
提供 id 就等于提供了一种"猜中别人的资源"的方式（会被 404 拦，但没必要开门）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.api.product_schemas import ConversationBody, MessageBody, PlanBody
from app.core.ids import new_id
from app.identity.models import Principal
from app.learning.plan_builders import build_manual_bundle

router = APIRouter()


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request) -> Principal:
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


@router.post("/projects/{project_id}/conversations", status_code=201, response_model=None)
def create_conversation(request: Request, project_id: str, body: ConversationBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
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
            return guard.replay_response()
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
def list_messages(request: Request, project_id: str, conversation_id: str) -> dict:
    state = _state(request)
    rows = state.products.list_messages(_actor(request), project_id, conversation_id)
    return {"messages": [m.to_dict() for m in rows]}


@router.put("/projects/{project_id}/plan", response_model=None)
def replace_plan(request: Request, project_id: str, body: PlanBody) -> dict | JSONResponse:
    """整版替换：返回实现分配版本后的完整 bundle。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
        state = _state(request)
        bundle = state.products.replace_plan(
            guard.principal,
            project_id,
            build_manual_bundle(
                guard.principal, project_id, body.goal,
                [(m.title, m.description, [t.title for t in m.tasks]) for m in body.milestones],
                now=state.clock.now(),
            ),
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
        return Response(status_code=204)
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
