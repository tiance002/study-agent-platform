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

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app.api.auth_routes import router as auth_router
from app.api.http_idempotency import (
    HttpIdempotencyStore,
    InMemoryHttpIdempotencyStore,
)
from app.api.product_routes import router as product_router
from app.api.projects_routes import router as projects_router
from app.api.routes import error_response, request_validation_response, router
from app.audit.outbox import (
    AuditOutbox,
    InMemoryAuditOutbox,
    PostgresAuditOutbox,
)
from app.audit.sink import AuditSink
from app.budget.ledger import BudgetLedger
from app.core.clock import SystemClock
from app.core.errors import PlatformError
from app.core.ids import new_request_id
from app.core.request_context import bind_request_id, reset_request_id
from app.db.confirmation_store import PostgresConfirmationStore
from app.db.evidence_store import PostgresEvidenceRepository
from app.db.idempotency_store import PostgresHttpIdempotencyStore
from app.db.identity_store import (
    PostgresInvitationRepository,
    PostgresMembershipRepository,
    PostgresSessionRepository,
)
from app.db.ingestion_store import PostgresIngestionRepository
from app.db.learning_store import PostgresLearningLoopRepository
from app.db.product_store import PostgresProductRepository
from app.db.rate_limit_store import PostgresRateLimiter
from app.db.settings import app_dsn
from app.db.settings import worker_dsn as worker_role_dsn
from app.db.teaching_store import PostgresTeachingRepository
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
from app.knowledge.memory_store import InMemoryIngestionRepository
from app.knowledge.ports import IngestionRepository
from app.knowledge.retrieval import ChunkIndex
from app.knowledge.store import KnowledgeRepository
from app.learning.evidence import EvidenceLog
from app.learning.loop_store import InMemoryLearningLoopRepository
from app.learning.memory_store import InMemoryEvidenceRepository
from app.learning.ports import EvidenceRepository, LearningLoopRepository
from app.learning.projector import Projector
from app.policy.gateway import PolicyGateway
from app.policy.token import TokenIssuer
from app.product.memory_store import InMemoryProductRepository
from app.product.ports import ProductRepository
from app.registry.registry import Registry
from app.teaching.memory_store import InMemoryTeachingRepository
from app.teaching.ports import TeachingProvider, TeachingRunRepository
from app.teaching.providers_factory import build_teaching_provider
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
EXPECTED_SCHEMA_VERSION = "0010"


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
    http_idempotency: HttpIdempotencyStore | None
    # 学习证据仓储（append-only）。掌握度投影的唯一事实源入口。
    evidence: EvidenceRepository
    # 跨产品表与证据表的原子学习闭环命令。
    learning_loop: LearningLoopRepository
    # 资料摄取：原文、摄取任务与可引用片段。必填同理 —— 上传端点不能等
    # 第一个请求才暴露装配缺失。
    ingestion: IngestionRepository
    # 项目内检索：作用域收窄 + 确定性排序 + 保守的证据判定。
    # 它没有后端分支（排序是纯函数，隔离由 ingestion 承担），所以
    # 两个分支里构造出来的其实是同一个类 —— 这一点写在装配处，免得
    # 后来的人以为"少装配了一个 PG 版"。
    knowledge: KnowledgeRepository
    # 教学运行仓储（第五轮）。必填同理：教学端点不能等第一个请求才暴露装配缺失。
    teaching: TeachingRunRepository
    # 教学 provider。None = 功能显式关闭（无凭据/未配置）——
    # 教学端点据此明确报"功能未启用"，绝不静默退回模拟器。
    teaching_provider: TeachingProvider | None
    #: 会话 cookie 的有效期（也是兑换出的数据库会话的过期时间）。
    session_ttl: timedelta = DEFAULT_SESSION_TTL
    #: 生产环境置 True（HTTPS-only cookie）。测试与本机开发保持 False：
    #: TestClient 走 http，Secure cookie 不会被回传，等于开了箱就坏。
    cookie_secure: bool = False
    trusted_origins: tuple[str, ...] = ()
    #: 可信代理链（IP/CIDR）。behind_proxy=True 时必须非空（启动自检强制）。
    trusted_proxies: tuple[str, ...] = ()
    behind_proxy: bool = False
    #: 认证审计的可靠中转（transactional outbox）。兑换成功的审计事实
    #: 与业务同事务/同临界区落在这里，再由端点投影进链式 sink。
    audit_outbox: AuditOutbox | None = None
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

    # 文件审计 sink 的哈希链只能由**单一写入进程**维护：多 worker 各自
    # 持有独立的 seq 与链尾哈希，互不衔接，链当场分裂（审查 P2）。
    # worker 数由启动命令传入，应用只能靠运维显式声明来核对 ——
    # 声明 >1 直接拒绝启动；没声明则按 1 处理。
    if loaded.is_production:
        raw_workers = (os.environ.get("STUDY_PLATFORM_WEB_WORKERS") or "1").strip()
        if raw_workers.isdigit() and int(raw_workers) > 1:
            raise RuntimeError(
                "STUDY_PLATFORM_WEB_WORKERS>1 与文件审计 sink 不兼容："
                "多进程会产生分裂的审计哈希链。在审计链落库（单写入者或"
                "数据库存储）之前，请保持 1 个 worker 进程。"
            )

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
    evidence: EvidenceRepository
    learning_loop: LearningLoopRepository
    ingestion: IngestionRepository
    knowledge: KnowledgeRepository
    teaching: TeachingRunRepository
    audit_outbox: AuditOutbox

    if loaded.use_postgres:
        # ⚠️ 回退值必须走 `app_dsn()`（读 `STUDY_PLATFORM_DSN`），不能写死
        # `DEFAULT_APP_DSN`：那样"环境变量说一套、装配连另一套"就成了可能，
        # 而两边看起来都正常 —— 实测后果是测试把数据写进业务库，测试全绿。
        # 同一个事实（应用连哪个库）只能有一个出口。
        dsn = loaded.dsn or app_dsn()
        # 无论是生产还是开发态显式选择 postgres：连不上 / 版本不对都必须
        # 在启动时暴露，而不是等第一个请求 500。
        _verify_database_ready(dsn, expected_version=EXPECTED_SCHEMA_VERSION)

        membership = PostgresMembershipRepository(clock, dsn)
        pg_sessions = PostgresSessionRepository(clock, dsn)
        session_store = pg_sessions
        audit_outbox = PostgresAuditOutbox(sink=audit, dsn=dsn)
        invitations = PostgresInvitationRepository(
            clock, dsn, sessions=pg_sessions, outbox=audit_outbox
        )
        confirmations = PostgresConfirmationStore(dsn)
        products = PostgresProductRepository(membership=membership, clock=clock, dsn=dsn)
        http_idempotency = PostgresHttpIdempotencyStore(dsn)
        evidence = PostgresEvidenceRepository(clock, dsn)
        learning_loop = PostgresLearningLoopRepository(
            products=products, evidence=evidence, dsn=dsn
        )
        ingestion = PostgresIngestionRepository(
            membership=membership,
            clock=clock,
            dsn=dsn,
            # 认领与落定走 **worker 角色**（队列的跨租户策略只授予它）。
            # 回退同样走 `db.settings` 的唯一出口，而不是写死默认值。
            worker_dsn=loaded.worker_dsn or worker_role_dsn(),
        )
        knowledge = KnowledgeRepository(ingestion=ingestion)
        teaching = PostgresTeachingRepository(
            membership=membership,
            dsn=dsn,
            worker_dsn=loaded.worker_dsn or worker_role_dsn(),
            clock=clock,
        )
        teaching_provider = build_teaching_provider(loaded)
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
            evidence=evidence,
            learning_loop=learning_loop,
            ingestion=ingestion,
            knowledge=knowledge,
            teaching=teaching,
            teaching_provider=teaching_provider,
            session_ttl=loaded.session_ttl,
            cookie_secure=loaded.cookie_secure,
            rate_limiter=rate_limiter,
            trusted_origins=loaded.trusted_origins,
            trusted_proxies=loaded.trusted_proxies,
            behind_proxy=loaded.behind_proxy,
            audit_outbox=audit_outbox,
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
    audit_outbox = InMemoryAuditOutbox(sink=audit)
    invitations = InMemoryInvitationRepository(
        clock=clock, sessions=memory_sessions, outbox=audit_outbox
    )
    confirmations = ConfirmationStore()
    memory_products = InMemoryProductRepository(membership=membership)
    products = memory_products
    http_idempotency = InMemoryHttpIdempotencyStore()
    evidence = InMemoryEvidenceRepository(clock=clock)
    learning_loop = InMemoryLearningLoopRepository(memory_products, evidence)
    ingestion = InMemoryIngestionRepository(
        membership=membership, products=products, clock=clock
    )
    knowledge = KnowledgeRepository(ingestion=ingestion)
    teaching = InMemoryTeachingRepository(
        membership=membership, products=memory_products, clock=clock
    )
    teaching_provider = build_teaching_provider(loaded)
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
        evidence=evidence,
        learning_loop=learning_loop,
        ingestion=ingestion,
        knowledge=knowledge,
        teaching=teaching,
        teaching_provider=teaching_provider,
        session_ttl=loaded.session_ttl,
        cookie_secure=loaded.cookie_secure,
        rate_limiter=rate_limiter,
        trusted_origins=loaded.trusted_origins,
        trusted_proxies=loaded.trusted_proxies,
        behind_proxy=loaded.behind_proxy,
        audit_outbox=audit_outbox,
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

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """请求形状错误。

        必须显式注册：FastAPI 的默认实现返回 `{"detail": [...]}`，
        它既与本项目其它错误响应**形状不同**，又会把请求输入原样回显 ——
        遇到不能编码成 UTF-8 的输入（孤立代理项）时，编码响应本身抛异常，
        422 变成 500。理由详见 `api/routes.py:request_validation_response`。
        """
        return request_validation_response(exc)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
