"""项目 CRUD 的 HTTP 入口（任务 4）。

四条边界与认证流共享（`authenticate_request` 唯一入口），请求体里
没有也不允许有 `tenant_id` / `project_id` 以外的归属字段——
归属由服务端成员关系决定，身份由已验签的 cookie 声明决定。

**创建即授予**是一次不可分的行为：响应返回的那一刻，创建者必然能在
自己的列表里看到这个项目。PATCH 的乐观锁语义：`expected_version`
不匹配 → 409（VERSION_CONFLICT），客户端应重新读取后基于最新版本编辑。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.ids import new_id
from app.identity.models import Principal

router = APIRouter()

#: 命名长度上界。产品输入一律设界：不设界就是客户端控制的存储放大入口。
NAME_MAX_CHARS = 200
GOAL_MAX_CHARS = 2000


def _state(request: Request):
    return request.app.state.platform


class ProjectCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    goal: str = Field(default="", max_length=GOAL_MAX_CHARS)


class ProjectUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_CHARS)
    goal: str | None = Field(default=None, max_length=GOAL_MAX_CHARS)
    # 乐观锁：必须携带客户端看到的版本号。
    expected_version: int = Field(ge=1)


@router.post("/projects", status_code=201, response_model=None)
def create_project(request: Request, body: ProjectCreateBody) -> dict | JSONResponse:
    """创建学习项目。创建者立即获得访问权（同一行为，不可分）。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        project = state.membership.create_project_for(
            guard.principal,
            project_id=new_id("proj"),
            name=body.name,
            goal=body.goal,
        )
        result = project.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects")
def list_projects(request: Request) -> dict:
    """当前用户被授予的项目列表。成员感知：别人的项目根本不可见。"""
    state = _state(request)
    principal: Principal = _authenticate_and_tenant(request)
    return {
        "projects": [p.to_dict() for p in state.membership.list_for(principal)]
    }


@router.get("/projects/{project_id}")
def get_project(request: Request, project_id: str) -> dict:
    state = _state(request)
    principal: Principal = _authenticate_and_tenant(request)
    return state.membership.get(principal, project_id).to_dict()


@router.patch("/projects/{project_id}", response_model=None)
def update_project(request: Request, project_id: str, body: ProjectUpdateBody) -> dict | JSONResponse:
    """乐观锁更新。`name` / `goal` 至少提供一个 —— 全空等于没说要改什么。"""
    if body.name is None and body.goal is None:
        from app.core.errors import ErrorCode, deny

        raise deny(
            ErrorCode.PARAMS_INVALID,
            "name 与 goal 至少提供一个；空的更新请求不产生新版本",
        )
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        updated = state.membership.update(
            guard.principal,
            project_id,
            name=body.name,
            goal=body.goal,
            expected_version=body.expected_version,
        )
        result = updated.to_dict()
        guard.complete(200, result)
        return result


def _authenticate_and_tenant(request: Request) -> Principal:
    """项目路由的统一认证入口。**薄封装**：真实逻辑只在
    `api.auth_routes.authenticate_request` —— 这里不重复认证规则。
    """
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)
