"""应用入口：装配各层组件并暴露 HTTP 接口。

运行：

```bash
# 在仓库根目录
uvicorn app.main:app --app-dir backend --reload
# 然后打开 http://127.0.0.1:8000/
```

## 部署形态（第 1 轮安全收口）

``STUDY_PLATFORM_ENV`` 决定装配，**生产配置缺失时启动直接失败**，
而不是静默退回内存适配器：

- ``development``（默认）：内存适配器 + Bearer 兜底 + 演示种子，零配置本机开发；
  显式 ``STUDY_PLATFORM_PERSISTENCE=postgres`` 时改用 PostgreSQL（本机演练）。
- ``production``：PostgreSQL 持久化、仅 cookie 认证（Bearer 关闭）、
  密钥显式注入且互不相同、Secure cookie、可信 Origin 白名单、限流，
  启动时连接数据库核对迁移版本。

安全配置集中在 ``app.core.deployment``，本文件只负责装配。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.auth_routes import router as auth_router
from app.api.http_idempotency import (
    HttpIdempotencyStore,
    InMemoryHttpIdempotencyStore,
)
from app.api.product_routes import router as product_router
from app.api.projects_routes import router as projects_router
from app.api.routes import error_response, router
from app.audit.sink import AuditSink
from app.budget.ledger import BudgetLedger
from app.core.clock import SystemClock
from app.core.errors import PlatformError
from app.core.ids import new_request_id
from app.core.request_context import bind_request_id, reset_request_id
from app.db.confirmation_store import PostgresConfirmationStore
from app.db.idempotency_store import PostgresHttpIdempotencyStore
from app.db.identity_store import (
    PostgresInvitationRepository,
    PostgresMembershipRepository,
    PostgresSessionRepository,
)
from app.db.product_store import PostgresProductRepository
from app.db.rate_limit_store import PostgresRateLimiter
from app.db.settings import DEFAULT_APP_DSN
from app.deployment import DeploymentSettings
from app.execution.confirmation import ConfirmationRepository, ConfirmationStore
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
from app.identity.rate_limit import InMemoryRateLimiter, RateLimiter
from app.identity.session import SessionIssuer
from app.knowledge.retrieval import ChunkIndex
from app.learning.evidence import EvidenceLog
from app.learning.projector import Projector
from app.policy.gateway import PolicyGateway
from app.policy.token import TokenIssuer
from app.product.memory_store import InMemoryProductRepository
from app.product.ports import ProductRepository
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

#: 代码预期的数据库迁移版本。启动自检核对它；新增迁移必须同步更新。
EXPECTED_SCHEMA_VERSION = "0004"


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
    confirmations: ConfirmationRepository
    # --- 第 1 轮安全收口新增 ---
    # rate_limiter 刻意**必填**：限流是认证引导端点的前置守卫，
    # 装配遗漏必须在构造 PlatformState 时就报错，而不是等第一个请求 AttributeError。
    # （必填字段必须排在有默认值的字段之前 —— dataclass 的硬规则。）
    rate_limiter: RateLimiter
    # 产品仓储（会话/消息/计划/资料）。必填同理：产品端点不能等第一个请求才暴露装配缺失。
    products: ProductRepository
    # HTTP 命令幂等的存储。None = 幂等关闭（客户端不带 Idempotency-Key 时无感）。
    http_idempotency: HttpIdempotencyStore | None = None
    #: 会话 cookie 的有效期（也是兑换出的数据库会话的过期时间）。
    session_ttl: timedelta = DEFAULT_SESSION_TTL
    #: 生产环境置 True（HTTPS-only cookie）。测试与本机开发保持 False：
    #: TestClient 走 http，Secure cookie 不会被回传，等于开了箱就坏。
    cookie_secure: bool = False
    trusted_origins: tuple[str, ...] = ()
    behind_proxy: bool = False
    #: 是否保留 Bearer 兼容通道。生产形态必须为 False。
    bearer_enabled: bool = True
    settings: DeploymentSettings | None = None
    persistence_backend: str = "in_memory_adapter"
    rls_label: str = "not_implemented"
    auth_mode_label: str = "cookie_session_bearer_compat"


def _verify_database_ready(dsn: str, *, expected_version: str) -> None:
    """启动时数据库自检：连得上 + 迁移版本是代码预期值，否则拒绝启动。

    应用角色在 0004 起对 ``alembic_version`` 有 SELECT 权限。
    """
    from app.db.session import connect

    with connect(dsn) as conn:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    versions = [row[0] for row in rows]
    if len(versions) != 1:
        raise RuntimeError(
            f"启动自检失败：alembic_version 应有且仅有一行，实际为 {versions!r}；"
            "请先运行 alembic upgrade head。"
        )
    if versions[0] != expected_version:
        raise RuntimeError(
            f"启动自检失败：数据库迁移版本为 {versions[0]!r}，代码预期 {expected_version!r}；"
            "请先运行 alembic upgrade head（或回退代码到匹配版本）。"
        )


def build_platform(
    var_dir: Path | None = None,
    audit_available: bool = True,
    settings: DeploymentSettings | None = None,
) -> PlatformState:
    """组合根：在这里选择适配器实现。

    生产形态（``STUDY_PLATFORM_ENV=production``）配置不完整时**直接抛错**，
    由进程启动失败暴露问题 —— 绝不静默退回内存实现。
    """
    loaded = settings or DeploymentSettings.load()
    loaded.validate_for_startup()

    base = var_dir or VAR_DIR
    registry = build_registry()
    gateway = PolicyGateway()
    ledger = BudgetLedger()
    machine = ActionStateMachine()
    clock = SystemClock()
    tokens = TokenIssuer(secret=loaded.token_secret)
    projector = Projector()
    chunk_index = ChunkIndex()
    evidence_log = EvidenceLog()
    audit = AuditSink(base / "audit", available=audit_available)

    # 身份与授权。签名密钥生产必须来自 KMS/Secret Manager，且互相分离。
    sessions = SessionIssuer(secret=loaded.session_secret)
    cookie_auth = CookieAuth(
        loaded.cookie_secret,
        clock,
        previous_secrets=loaded.cookie_previous_secrets,
    )
    auth = BearerSessionAuthProvider(issuer=sessions, clock=clock)

    # 适配器选择：先把变量声明成**端口类型**，再在各分支里赋具体实现。
    # 不声明的话 mypy 会拿第一个分支的具体类当变量类型，第二个分支的赋值
    # 就报"类型不兼容"—— 而这两个适配器本来就该可以互换，
    # 那个报错说明的是类型标注写错了，不是代码写错了。
    membership: MembershipRepository
    session_store: SessionRepository
    invitations: InvitationRepository
    confirmations: ConfirmationRepository
    products: ProductRepository
    http_idempotency: HttpIdempotencyStore | None

    if loaded.use_postgres:
        dsn = loaded.dsn or DEFAULT_APP_DSN
        # 无论是生产还是开发态显式选择 postgres：连不上 / 版本不对都必须
        # 在启动时暴露，而不是等第一个请求 500。
        _verify_database_ready(dsn, expected_version=EXPECTED_SCHEMA_VERSION)

        membership = PostgresMembershipRepository(clock, dsn)
        pg_sessions = PostgresSessionRepository(clock, dsn)
        session_store = pg_sessions
        invitations = PostgresInvitationRepository(clock, dsn, sessions=pg_sessions)
        confirmations = PostgresConfirmationStore(dsn)
        products = PostgresProductRepository(membership=membership, clock=clock, dsn=dsn)
        http_idempotency = PostgresHttpIdempotencyStore(dsn)
        rate_limiter: RateLimiter = PostgresRateLimiter(
            limit=loaded.exchange_limit,
            window_seconds=loaded.exchange_window_seconds,
            dsn=dsn,
        )
        runtime = _build_runtime(
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
        # 生产形态不做演示种子：租户/主体由管理员邀请流程建立。
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
            confirmations=confirmations,
            products=products,
            http_idempotency=http_idempotency,
            session_ttl=loaded.session_ttl,
            cookie_secure=loaded.cookie_secure,
            rate_limiter=rate_limiter,
            trusted_origins=loaded.trusted_origins,
            behind_proxy=loaded.behind_proxy,
            # 生产形态关闭 bearer 兜底；开发态用 PG 演练时保留它方便测试工具。
            bearer_enabled=not loaded.is_production,
            settings=loaded,
            persistence_backend="postgresql",
            rls_label="postgresql_row_level_security",
            auth_mode_label=(
                "cookie_session" if loaded.is_production else "cookie_session_bearer_compat"
            ),
        )

    # ---------------------------------------------------------- 开发内存形态
    membership = MembershipStore()
    memory_sessions = InMemorySessionRepository(clock=clock)
    session_store = memory_sessions
    invitations = InMemoryInvitationRepository(clock=clock, sessions=memory_sessions)
    confirmations = ConfirmationStore()
    products = InMemoryProductRepository(membership=membership)
    http_idempotency = InMemoryHttpIdempotencyStore()
    rate_limiter = InMemoryRateLimiter(
        limit=loaded.exchange_limit,
        window_seconds=loaded.exchange_window_seconds,
    )
    runtime = _build_runtime(
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
    state = PlatformState(
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
        confirmations=confirmations,
        products=products,
        http_idempotency=http_idempotency,
        session_ttl=loaded.session_ttl,
        cookie_secure=loaded.cookie_secure,
        rate_limiter=rate_limiter,
        trusted_origins=loaded.trusted_origins,
        behind_proxy=loaded.behind_proxy,
        bearer_enabled=True,
        settings=loaded,
    )
    _seed_demo_membership(membership)
    return state


def _build_runtime(
    *,
    registry: Registry,
    gateway: PolicyGateway,
    ledger: BudgetLedger,
    audit: AuditSink,
    machine: ActionStateMachine,
    tokens: TokenIssuer,
    projector: Projector,
    clock: SystemClock,
    chunk_index: ChunkIndex,
    evidence_log: EvidenceLog,
    confirmations: ConfirmationRepository,
) -> InteractionRuntime:
    return InteractionRuntime(
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


def _seed_demo_membership(membership: MembershipRepository) -> None:
    """建立演示租户、项目与成员关系。

    这是**服务端种子**：它决定"谁属于哪个项目"。
    客户端无法通过任何请求字段改变这些关系 —— 这正是与「自报租户」的本质区别。
    """
    context = SystemContext(DEMO_TENANT, "演示种子数据")
    membership.create_project(context, project_id=DEMO_PROJECT, name="演示学习项目")
    membership.grant_project(context, principal_id=DEMO_PRINCIPAL, project_id=DEMO_PROJECT)


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
    app.state.platform = platform or build_platform()
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(product_router)

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
