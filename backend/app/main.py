"""应用入口：装配各层组件并暴露 HTTP 接口。

运行：

```bash
# 在仓库根目录
uvicorn app.main:app --app-dir backend --reload
# 然后打开 http://127.0.0.1:8000/
```

⚠️ 本版是「批次一最小闭环的**开发适配器版**」：核心边界逻辑完整且可测，
   但持久化、隔离沙箱、真实检索管线、模型调用、KMS 与 PostgreSQL RLS
   均**未实现**。详见 README 的「未实现项」一节。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.auth_routes import router as auth_router
from app.api.routes import error_response, router
from app.audit.sink import AuditSink
from app.budget.ledger import BudgetLedger
from app.core.clock import SystemClock
from app.core.errors import PlatformError
from app.core.ids import new_request_id
from app.core.request_context import bind_request_id, reset_request_id
from app.execution.confirmation import ConfirmationStore
from app.execution.state_machine import ActionStateMachine
from app.identity.auth import AuthProvider, BearerSessionAuthProvider
from app.identity.cookie_auth import CookieAuth
from app.identity.membership import MembershipStore
from app.identity.memory_store import (
    InMemoryInvitationRepository,
    InMemorySessionRepository,
)
from app.identity.ports import (
    InvitationRepository,
    MembershipRepository,
    SessionRepository,
    SystemContext,
)
from app.identity.session import SessionIssuer
from app.knowledge.retrieval import ChunkIndex
from app.learning.evidence import EvidenceLog
from app.learning.projector import Projector
from app.policy.gateway import PolicyGateway
from app.policy.token import TokenIssuer
from app.registry.registry import Registry
from app.workflow.catalog import build_registry
from app.workflow.runtime import InteractionRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"
VAR_DIR = REPO_ROOT / "var"

# 开箱可用的演示租户与项目。
# 注意：这是**服务端种子数据**，不是客户端可以声称的值 —— 调用方必须持有
# 该租户成员的有效会话令牌才能访问。
DEMO_TENANT = "tenant_demo"
DEMO_PRINCIPAL = "user_demo"
DEMO_PROJECT = "proj_demo"

#: 会话 cookie 的默认有效期。
DEFAULT_SESSION_TTL = timedelta(hours=8)


@dataclass
class PlatformState:
    registry: Registry
    gateway: PolicyGateway
    ledger: BudgetLedger
    audit: AuditSink
    machine: ActionStateMachine
    tokens: TokenIssuer
    projector: Projector
    clock: SystemClock
    chunk_index: ChunkIndex
    evidence_log: EvidenceLog
    runtime: InteractionRuntime
    # 身份与授权
    sessions: SessionIssuer
    membership: MembershipRepository
    auth: AuthProvider
    invitations: InvitationRepository
    session_store: SessionRepository
    cookie_auth: CookieAuth
    # 服务端确认记录
    confirmations: ConfirmationStore
    #: 会话 cookie 的有效期（也是兑换出的数据库会话的过期时间）。
    session_ttl: timedelta = DEFAULT_SESSION_TTL
    #: 生产环境置 True（HTTPS-only cookie）。测试与本机开发保持 False：
    #: TestClient 走 http，Secure cookie 不会被回传，等于开了箱就坏。
    cookie_secure: bool = False


def build_platform(
    *,
    audit_available: bool = True,
    var_dir: Path | None = None,
) -> PlatformState:
    """装配平台。参数用于故障注入与测试隔离。

    ⚠️ 本装配函数默认全部使用**内存适配器**。PostgreSQL 适配器
    （`db.identity_store` / `db.confirmation_store`）由契约测试直接驱动，
    切换生产装配属于任务 7（重启恢复验收）的范围。
    """
    base = var_dir or VAR_DIR
    registry = build_registry()
    gateway = PolicyGateway()
    ledger = BudgetLedger()
    machine = ActionStateMachine()
    clock = SystemClock()
    tokens = TokenIssuer(
        # 生产密钥必须来自 KMS / Secret Manager，并与数据、审计密钥分离。
        secret=os.environ.get("STUDY_PLATFORM_TOKEN_SECRET", "dev-only-placeholder-change-me")
    )
    projector = Projector()
    chunk_index = ChunkIndex()
    evidence_log = EvidenceLog()
    audit = AuditSink(base / "audit", available=audit_available)
    confirmations = ConfirmationStore()

    # 身份与授权。签名密钥生产必须来自 KMS/Secret Manager，且互相分离。
    session_secret = os.environ.get(
        "STUDY_PLATFORM_SESSION_SECRET", "dev-only-session-secret-change-me"
    )
    sessions = SessionIssuer(secret=session_secret)
    membership = MembershipStore()
    session_store = InMemorySessionRepository(clock=clock)
    invitations = InMemoryInvitationRepository(clock=clock, sessions=session_store)
    cookie_auth = CookieAuth(
        # cookie 密钥缺省复用会话密钥：开发环境少一个要配的变量；
        # 生产必须显式提供独立密钥（见 .env.example 的说明）。
        secret=os.environ.get("STUDY_PLATFORM_COOKIE_SECRET", session_secret),
        clock=clock,
    )
    _seed_demo_membership(membership)
    auth = BearerSessionAuthProvider(issuer=sessions, clock=clock)

    runtime = InteractionRuntime(
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        audit=audit,
        machine=machine,
        tokens=tokens,
        projector=projector,
        clock=clock,
        chunk_index=chunk_index,
        evidence_log=evidence_log,
        confirmations=confirmations,
    )
    return PlatformState(
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        audit=audit,
        machine=machine,
        tokens=tokens,
        projector=projector,
        clock=clock,
        chunk_index=chunk_index,
        evidence_log=evidence_log,
        runtime=runtime,
        sessions=sessions,
        membership=membership,
        auth=auth,
        invitations=invitations,
        session_store=session_store,
        cookie_auth=cookie_auth,
        cookie_secure=os.environ.get("STUDY_PLATFORM_COOKIE_SECURE", "") in {"1", "true"},
        confirmations=confirmations,
    )


def _seed_demo_membership(membership: MembershipStore) -> None:
    """建立演示租户、项目与成员关系。

    这是**服务端种子**：它决定"谁属于哪个项目"。
    客户端无法通过任何请求字段改变这些关系 —— 这正是与「自报租户」的本质区别。
    """
    context = SystemContext(DEMO_TENANT, "演示种子数据")
    membership.create_project(context, project_id=DEMO_PROJECT, name="演示学习项目")
    membership.grant_project(context, principal_id=DEMO_PRINCIPAL, project_id=DEMO_PROJECT)


def create_app(*, platform: PlatformState | None = None) -> FastAPI:
    app = FastAPI(
        title="Agent 工程学习规划平台（批次一开发适配器版）",
        version="0.1.0",
        description=(
            "核心边界逻辑的可用实现：策略网关、capability token、taint/endorsement、"
            "预算树、幂等执行状态机、审计哈希链、证据与掌握投影、子任务运行时。"
            "持久化与沙箱为开发适配器，生产适配器未实现。"
        ),
    )
    app.state.platform = platform or build_platform()
    app.include_router(router)
    app.include_router(auth_router)

    @app.middleware("http")
    async def _bind_request_id(request: Request, call_next):
        """给每个请求绑定一个追踪 id，并回写到响应头。

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
    async def _platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return error_response(exc)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
