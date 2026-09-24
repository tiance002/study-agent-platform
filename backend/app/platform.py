"""平台组合根：装配各层适配器，产出 ``PlatformState``。

本模块是**进程级装配**的唯一出口：Web 入口（``app.main``）、worker CLI
（``app.workers.*``）与测试都从这里取得平台实例，而不是各自重复装配，
也不是反向依赖 HTTP 入口（``app.main`` 在导入期就会创建应用实例）。

职责边界：

- 这里只有装配（选择适配器实现、演示种子、启动自检），**没有 HTTP 语义**：
  ``create_app`` 与路由挂载留在 ``app.main``。
- 生产配置缺失时**直接抛错**，由进程启动失败暴露问题 —— 绝不静默退回内存实现。
- PostgreSQL 适配器全部延迟导入：内存模式导入本模块不需要 psycopg。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from app.audit.outbox import (
    AuditOutbox,
    InMemoryAuditOutbox,
)
from app.audit.sink import AuditSink
from app.budget.ledger import BudgetLedger
from app.budget.platform import PlatformPaidBudget, PlatformPaidBudgetPort
from app.core.clock import SystemClock
from app.deployment import DeploymentSettings
from app.execution.confirmation import ConfirmationRepository, ConfirmationStore
from app.execution.state_machine import ActionStateMachine
from app.identity.account_store import InMemoryAccountRepository
from app.identity.argon2_pool import Argon2WorkPool
from app.identity.auth import AuthProvider, BearerSessionAuthProvider
from app.identity.cookie_auth import CookieAuth
from app.identity.idempotency import (
    HttpIdempotencyStore,
    InMemoryHttpIdempotencyStore,
)
from app.identity.membership import MembershipStore
from app.identity.memory_store import (
    InMemoryInvitationRepository,
    InMemorySessionRepository,
)
from app.identity.ports import (
    AccountRepository,
    InvitationRepository,
    MembershipRepository,
    SessionRepository,
    SystemContext,
)
from app.identity.rate_limit import InMemoryRateLimiter, RateLimiter
from app.identity.session import SessionIssuer
from app.knowledge.acquisition_ports import AcquisitionRepository
from app.knowledge.discovery import SourceSearchProvider, TavilySearchProvider
from app.knowledge.memory_acquisition_store import InMemoryAcquisitionRepository
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
from app.teaching.local_router import build_local_query_rewriter
from app.teaching.memory_store import InMemoryTeachingRepository
from app.teaching.ports import LocalQueryRewriter, TeachingProvider, TeachingRunRepository
from app.teaching.providers_factory import build_teaching_provider
from app.workflow.catalog import build_registry
from app.workflow.runtime import InteractionRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
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
EXPECTED_SCHEMA_VERSION = "0018"


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
    # 显式候选与下载任务：web 只登记/授权，外网访问留给独立 worker。
    acquisition: AcquisitionRepository
    source_search_provider: SourceSearchProvider | None
    local_query_rewriter: LocalQueryRewriter | None
    # 教学运行仓储（第五轮）。必填同理：教学端点不能等第一个请求才暴露装配缺失。
    teaching: TeachingRunRepository
    # 教学 provider。None = 功能显式关闭（无凭据/未配置）——
    # 教学端点据此明确报"功能未启用"，绝不静默退回模拟器。
    teaching_provider: TeachingProvider | None
    accounts: AccountRepository | None = None
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
    registration_enabled: bool = False
    password_login_enabled: bool = False
    paid_dispatch_enabled: bool = False
    auth_argon2_max_concurrency: int = 2
    auth_argon2_queue_limit: int = 16
    auth_argon2_wait_seconds: float = 2.0
    argon2_pool: Argon2WorkPool | None = None
    platform_paid_budget: PlatformPaidBudgetPort | None = None
    metrics_reader: object | None = None


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
    acquisition: AcquisitionRepository
    teaching: TeachingRunRepository
    audit_outbox: AuditOutbox
    accounts: AccountRepository

    if loaded.use_postgres:
        from app.db.account_store import PostgresAccountRepository
        from app.db.acquisition_store import PostgresAcquisitionRepository
        from app.db.audit_store import PostgresAuditOutbox
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
        from app.db.metrics_store import PostgresMetricsStore
        from app.db.platform_budget_store import PostgresPlatformPaidBudget
        from app.db.product_store import PostgresProductRepository
        from app.db.rate_limit_store import PostgresRateLimiter
        from app.db.settings import app_dsn
        from app.db.settings import worker_dsn as worker_role_dsn
        from app.db.teaching_store import PostgresTeachingRepository

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
        accounts = PostgresAccountRepository(sessions=pg_sessions, dsn=dsn, clock=clock)
        audit_outbox = PostgresAuditOutbox(sink=audit, dsn=dsn)
        invitations = PostgresInvitationRepository(clock, dsn, sessions=pg_sessions, outbox=audit_outbox)
        confirmations = PostgresConfirmationStore(dsn)
        products = PostgresProductRepository(membership=membership, clock=clock, dsn=dsn)
        acquisition = PostgresAcquisitionRepository(
            membership=membership,
            products=products,
            dsn=dsn,
            worker_dsn=loaded.worker_dsn or worker_role_dsn(),
            clock=clock,
        )
        http_idempotency = PostgresHttpIdempotencyStore(dsn)
        evidence = PostgresEvidenceRepository(clock, dsn)
        learning_loop = PostgresLearningLoopRepository(products=products, evidence=evidence, dsn=dsn)
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
        local_query_rewriter = build_local_query_rewriter(loaded)
        source_search_provider = (
            TavilySearchProvider(api_key=loaded.source_search_api_key)
            if loaded.source_search_provider == "tavily"
            else None
        )
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
            acquisition=acquisition,
            source_search_provider=source_search_provider,
            local_query_rewriter=local_query_rewriter,
            teaching=teaching,
            teaching_provider=teaching_provider,
            accounts=accounts,
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
            auth_mode_label=("cookie_session" if loaded.is_production else "cookie_session_bearer_compat"),
            registration_enabled=loaded.registration_enabled,
            password_login_enabled=loaded.password_login_enabled,
            paid_dispatch_enabled=loaded.paid_dispatch_enabled,
            auth_argon2_max_concurrency=loaded.auth_argon2_max_concurrency,
            auth_argon2_queue_limit=loaded.auth_argon2_queue_limit,
            auth_argon2_wait_seconds=loaded.auth_argon2_wait_seconds,
            argon2_pool=Argon2WorkPool(
                loaded.auth_argon2_max_concurrency,
                loaded.auth_argon2_queue_limit,
                loaded.auth_argon2_wait_seconds,
            ),
            # The paid-provider cap is durable and shared across worker
            # processes.  Its live switch/cap are held in the migration-0013
            # singleton and changed through the restricted transition surface.
            platform_paid_budget=PostgresPlatformPaidBudget(
                dsn=loaded.worker_dsn or worker_role_dsn(),
            ),
            metrics_reader=PostgresMetricsStore(dsn=dsn),
        )

    # ---------------------------------------------------------- 开发内存形态
    membership = MembershipStore()
    memory_sessions = InMemorySessionRepository(clock=clock)
    session_store = memory_sessions
    audit_outbox = InMemoryAuditOutbox(sink=audit)
    invitations = InMemoryInvitationRepository(clock=clock, sessions=memory_sessions, outbox=audit_outbox)
    accounts = InMemoryAccountRepository(
        membership=membership,
        sessions=memory_sessions,
        clock=clock,
        outbox=audit_outbox,
    )
    confirmations = ConfirmationStore()
    memory_products = InMemoryProductRepository(membership=membership)
    products = memory_products
    http_idempotency = InMemoryHttpIdempotencyStore()
    evidence = InMemoryEvidenceRepository(clock=clock)
    learning_loop = InMemoryLearningLoopRepository(memory_products, evidence)
    ingestion = InMemoryIngestionRepository(membership=membership, products=products, clock=clock)
    knowledge = KnowledgeRepository(ingestion=ingestion)
    acquisition = InMemoryAcquisitionRepository(membership=membership, products=products, clock=clock)
    teaching = InMemoryTeachingRepository(membership=membership, products=memory_products, clock=clock)
    teaching_provider = build_teaching_provider(loaded)
    local_query_rewriter = build_local_query_rewriter(loaded)
    source_search_provider = (
        TavilySearchProvider(api_key=loaded.source_search_api_key)
        if loaded.source_search_provider == "tavily"
        else None
    )
    rate_limiter = InMemoryRateLimiter(
        limit=loaded.exchange_limit,
        window_seconds=loaded.exchange_window_seconds,
        capacity=loaded.auth_rate_limit_capacity,
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
        acquisition=acquisition,
        source_search_provider=source_search_provider,
        local_query_rewriter=local_query_rewriter,
        teaching=teaching,
        teaching_provider=teaching_provider,
        accounts=accounts,
        session_ttl=loaded.session_ttl,
        cookie_secure=loaded.cookie_secure,
        rate_limiter=rate_limiter,
        trusted_origins=loaded.trusted_origins,
        trusted_proxies=loaded.trusted_proxies,
        behind_proxy=loaded.behind_proxy,
        audit_outbox=audit_outbox,
        bearer_enabled=True,
        settings=loaded,
        registration_enabled=loaded.registration_enabled,
        password_login_enabled=loaded.password_login_enabled,
        paid_dispatch_enabled=loaded.paid_dispatch_enabled,
        auth_argon2_max_concurrency=loaded.auth_argon2_max_concurrency,
        auth_argon2_queue_limit=loaded.auth_argon2_queue_limit,
        auth_argon2_wait_seconds=loaded.auth_argon2_wait_seconds,
        argon2_pool=Argon2WorkPool(
            loaded.auth_argon2_max_concurrency,
            loaded.auth_argon2_queue_limit,
            loaded.auth_argon2_wait_seconds,
        ),
        platform_paid_budget=PlatformPaidBudget(
            monthly_cap_micro=loaded.platform_monthly_cap_micro,
            paid_dispatch_enabled=loaded.paid_dispatch_enabled,
        ),
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
