"""应用入口：装配各层组件并暴露 HTTP 接口。

运行：

```bash
# 在仓库根目录
uvicorn app.main:app --app-dir backend --reload
# 然后打开 http://127.0.0.1:8000/
```

## 职责边界

平台装配（组合根）在 ``app.platform``：``PlatformState``、``build_platform``
与演示种子都从那里取得。本文件只负责 HTTP 语义：路由挂载、中间件、
错误处理与静态资源。worker CLI 与路由模块不得导入本模块 ——
导入期就会创建应用实例（``app = create_app()``）。

这里保留 ``app.platform`` 公共名字的兼容导出（``build_platform``、
``PlatformState``、演示常量、``EXPECTED_SCHEMA_VERSION`` 等），
既有调用方无需改动；新代码应直接从 ``app.platform`` 导入。

## 部署形态（第 1 轮安全收口）

``STUDY_PLATFORM_ENV`` 决定装配，**生产配置缺失时启动直接失败**，
而不是静默退回内存适配器：

- ``development``（默认）：内存适配器 + Bearer 兜底 + 演示种子，零配置本机开发；
  显式 ``STUDY_PLATFORM_PERSISTENCE=postgres`` 时改用 PostgreSQL（本机演练）。
- ``production``：PostgreSQL 持久化、仅 cookie 认证（Bearer 关闭）、
  密钥显式注入且互不相同、Secure cookie、可信 Origin 白名单、限流，
  启动时连接数据库核对迁移版本。

安全配置集中在 ``app.deployment``，本文件只负责装配。
"""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.auth_routes import router as auth_router
from app.api.library_routes import router as library_router
from app.api.product_routes import router as product_router
from app.api.projects_routes import router as projects_router
from app.api.routes import error_response, legacy_router, request_validation_response, router
from app.api.teaching_routes import router as teaching_router
from app.core.errors import PlatformError, public_error_payload
from app.core.ids import new_request_id
from app.core.request_context import bind_request_id, current_request_id, reset_request_id
from app.metrics import render_metrics

# 兼容导出：这些名字历史上由本模块提供。装配实现已移至 app.platform，
# 旧调用方（测试、工具脚本）继续可用；新代码请直接从 app.platform 导入。
from app.platform import (  # noqa: F401
    DEFAULT_SESSION_TTL,
    DEMO_PRINCIPAL,
    DEMO_PROJECT,
    DEMO_TENANT,
    EXPECTED_SCHEMA_VERSION,
    VAR_DIR,
    PlatformState,
    _build_runtime,
    _seed_demo_membership,
    _verify_database_ready,
    build_platform,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"


def create_app(*, platform: PlatformState | None = None) -> FastAPI:
    app = FastAPI(
        title="Agent 工程学习规划平台",
        version="0.1.0",
        description=(
            "核心边界逻辑的可用实现：策略网关、capability token、taint/endorsement、"
            "预算树、幂等执行状态机、审计哈希链、证据与掌握投影、子任务运行时，"
            "以及 cookie 会话认证与 PostgreSQL 持久化装配。"
        ),
    )
    # The first-party workbench is static so the API and browser share one origin.
    # That keeps the HttpOnly cookie and strict same-origin CSRF path intact.
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="frontend-assets")
    app.state.platform = platform or build_platform()
    app.state.metrics_reader = getattr(app.state.platform, "metrics_reader", None)
    app.include_router(router)
    if not app.state.platform.settings.is_production:
        app.include_router(legacy_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(product_router)
    app.include_router(library_router)
    app.include_router(teaching_router)

    @app.middleware("http")
    async def _auth_body_guard(request: Request, call_next):
        """Read and cap unauthenticated JSON bodies before Pydantic parsing."""
        if request.method == "POST" and request.url.path in {
            "/auth/invitations/exchange",
            "/auth/register",
            "/auth/login",
        }:
            content_type = request.headers.get("content-type", "").lower()
            if not content_type.startswith("application/json"):
                response = JSONResponse(
                    status_code=400,
                    content=public_error_payload("PARAMS_INVALID", "请求必须使用 application/json"),
                )
                response.headers["Cache-Control"] = "no-store"
                return response
            length = request.headers.get("content-length")
            try:
                declared_length = int(length) if length is not None else None
            except ValueError:
                declared_length = -1
            if declared_length is not None and (declared_length < 0 or declared_length > 64 * 1024):
                response = JSONResponse(
                    status_code=413 if declared_length > 64 * 1024 else 400,
                    content=public_error_payload(
                        "PARAMS_INVALID",
                        "请求体过大" if declared_length > 64 * 1024 else "请求体长度无效",
                        request_id=current_request_id(),
                    ),
                )
                response.headers["Cache-Control"] = "no-store"
                return response
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 64 * 1024:
                    response = JSONResponse(
                        status_code=413,
                        content=public_error_payload(
                            "PARAMS_INVALID", "请求体过大", request_id=current_request_id()
                        ),
                    )
                    response.headers["Cache-Control"] = "no-store"
                    return response
                chunks.append(chunk)
            # Starlette caches request bodies on this attribute; downstream
            # Pydantic parsing can consume the bounded bytes without rereading
            # the ASGI receive channel.
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.middleware("http")
    async def _bind_request_id(request: Request, call_next):
        """给每个请求绑定一个追踪 id，回写到响应头。

        这是 `request_id` 真正的来源：在此之前它从没有任何 raise 点设置过，
        于是每条错误响应里都是 null —— 字段在、值为空，最容易被误读成已实现。
        id 一律服务端生成：它会进日志，让客户端决定日志内容等于开一个日志注入口。
        """
        rid = new_request_id()
        token = bind_request_id(rid)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(PlatformError)
    async def _platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        response = error_response(exc)
        if request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """请求形状错误。

        必须显式注册：FastAPI 的默认实现返回 `{"detail": [...]}`，
        它既与本项目其它错误响应**形状不同**，又会把请求输入原样回显 ——
        遇到不能编码成 UTF-8 的输入（孤立代理项）时，编码响应本身抛异常，
        422 变成 500。理由详见 `api/routes.py:request_validation_response`。
        """
        response = request_validation_response(exc)
        if request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        settings = getattr(request.app.state.platform, "settings", None)
        configured_token = getattr(settings, "metrics_token", "") if settings is not None else ""
        if not configured_token:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        authorization = request.headers.get("authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if (
            scheme.casefold() != "bearer"
            or not separator
            or not supplied_token
            or not hmac.compare_digest(supplied_token, configured_token)
        ):
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        reader = getattr(request.app.state, "metrics_reader", None)
        if reader is None:
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        try:
            exposition = render_metrics(reader.snapshot())
        except Exception:  # noqa: BLE001 - never expose database or snapshot details
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        return Response(
            content=exposition,
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
