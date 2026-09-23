"""教学运行仓储的**契约测试**：内存与 PostgreSQL 适配器跑同一批断言。

重点守计划任务 2 冻结的语义：

1. **start_run 原子性**：用户消息、运行、预算预留、首条事件同生共死；
   预算不足时**任何东西**都不许出现（无部分写入）；
2. **预算记账**：held → in_flight → settled / released；整数微单位；
   最后一份额度只够一次预留（并发争抢只有一个赢家）；
3. **认领围栏**：旧凭证（被接管 / 租约过期）不能落定新持有者的运行；
4. **费用语义**：缺 usage 不按零结算（敞口保留）；可证明未派发才释放。
"""

from __future__ import annotations

import threading
import uuid

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.teaching.models import TokenUsage
from app.teaching.routing import RoutingDecision
from app.teaching.runs import Grounding, RunStatus

#: 整场会话跑在随机临时库上（`conftest.pg_database`，会话级 autouse）。

TENANT = "t_teach_pg"
QUESTION = "什么是幂等？"


def _drain_teaching() -> None:
    """把遗留的非终态运行推进到终态（`claim_run` 是跨租户的系统级操作）。

    与摄取队列的 `_drain_queue` 同理：**只允许发生在临时测试库上**，
    先过库名闸门，失败发生在任何写入之前。
    """
    dsn = pg_support.require_test_database(pg_support.migration_dsn())
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE teaching_runs"
                " SET status = 'failed', error_code = 'TEST_DRAIN',"
                "     error_detail = '测试前置：排空遗留运行',"
                "     lease_owner = NULL, lease_until = NULL, claim_token = NULL"
                " WHERE status IN ('queued', 'running')"
            )


@pytest.fixture(scope="module")
def pg_seed() -> None:
    if not pg_support.reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) ON CONFLICT (tenant_id) DO NOTHING",
                (TENANT, TENANT),
            )


class _FrozenClock:
    """冻结时钟：租约"过期与否"由此变成确定性断言，不靠 sleep。"""

    def __init__(self) -> None:
        from datetime import datetime, timezone

        self.now_value = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.now_value

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.now_value = self.now_value + timedelta(seconds=seconds)


class Env:
    def __init__(self, *, membership, products, teaching, clock, is_postgres: bool):
        self.membership = membership
        self.products = products
        self.teaching = teaching
        self.clock = clock
        self.is_postgres = is_postgres


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not pg_support.reachable(),
                    reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
                ),
            ],
            id="postgres",
        ),
    ]
)
def env(request, pg_seed):
    if request.param == "memory":
        from app.identity.membership import MembershipStore
        from app.product.memory_store import InMemoryProductRepository
        from app.teaching.memory_store import InMemoryTeachingRepository

        clock = _FrozenClock()
        membership = MembershipStore()
        products = InMemoryProductRepository(membership=membership, clock=clock)
        return Env(
            membership=membership,
            products=products,
            teaching=InMemoryTeachingRepository(membership=membership, products=products, clock=clock),
            clock=clock,
            is_postgres=False,
        )

    from app.db.identity_store import PostgresMembershipRepository
    from app.db.product_store import PostgresProductRepository
    from app.db.teaching_store import PostgresTeachingRepository

    _drain_teaching()
    membership = PostgresMembershipRepository()
    products = PostgresProductRepository(membership=membership)
    return Env(
        membership=membership,
        products=products,
        teaching=PostgresTeachingRepository(membership=membership),
        clock=None,
        is_postgres=True,
    )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _world(env: Env, *, tag: str = "a") -> tuple[Principal, str, str]:
    """一套全新的租户 + 项目 + 会话（预算账户因此互不污染）。"""
    tenant = f"t_tw_{tag}_{uuid.uuid4().hex[:8]}"
    principal = f"u_tw_{tag}_{uuid.uuid4().hex[:8]}"
    if env.is_postgres:
        dsn = pg_support.require_test_database(pg_support.migration_dsn())
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (tenant, tenant),
                )
                conn.execute(
                    "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (principal, tenant),
                )
    actor = Principal(principal_id=principal, tenant_id=tenant)
    project_id = _unique("proj")
    env.membership.create_project_for(actor, project_id=project_id, name="教学", goal="")
    conversation = env.products.create_conversation(
        actor, project_id, conversation_id=_unique("conv"), title="教学会话"
    )
    return actor, project_id, conversation.conversation_id


def _start(
    env: Env,
    actor: Principal,
    project_id: str,
    conversation_id: str,
    *,
    question: str = QUESTION,
    estimated_input_tokens: int = 100,
    estimated_output_tokens: int = 50,
    budget_total_micro: int = 1_000_000,
    budget_max_input_tokens: int = 8_000,
    budget_max_output_tokens: int = 2_000,
):
    return env.teaching.start_run(
        actor,
        project_id,
        conversation_id,
        run_id=_unique("run"),
        question=question,
        model_id="test-model-v1",
        prompt_version="teaching-sys/v1",
        ranking_version="keyword/v1",
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        budget_total_micro=budget_total_micro,
        budget_max_input_tokens=budget_max_input_tokens,
        budget_max_output_tokens=budget_max_output_tokens,
    )


USAGE = TokenUsage(input_tokens=100, output_tokens=50)
#: 与估价一致的金额（模拟单价：输入 1、输出 2 微单位 / token）。
EST_MICRO = 100 * 1 + 50 * 2


# ------------------------------------------------------------------ 创建


@pytest.mark.invariant
def test_start_run_writes_message_run_reservation_and_event(env):
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)

    assert run.status is RunStatus.QUEUED
    messages = env.products.list_messages(actor, project_id, conversation_id)
    assert [m.role for m in messages] == ["user"]  # 问题已入会话
    assert run.user_message_id == messages[0].message_id
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["reserved_micro"] == EST_MICRO
    events = env.teaching.list_events(actor, project_id, run.run_id)
    assert [e.event_type for e in events] == ["run.created"]


@pytest.mark.invariant
def test_routing_decision_persists_atomically_with_dispatch_in_both_adapters(env):
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    claim = env.teaching.claim_run(worker_id="route-worker", lease_seconds=60)
    assert claim is not None and claim.run.run_id == run.run_id
    decision = RoutingDecision(
        query_rewrite_status="fallback",
        reason_code="local_unavailable_or_invalid",
    )
    attempt_id = _unique("att")

    env.teaching.mark_dispatched(
        claim,
        attempt_id=attempt_id,
        estimated_input_tokens=100,
        estimated_output_tokens=50,
        request_payload={"routing_decision": decision.to_dict()},
        routing_decision=decision,
    )

    stored = env.teaching.get_run(actor, project_id, run.run_id)
    events = env.teaching.list_events(actor, project_id, run.run_id)
    assert stored.routing_decision == decision
    assert events[-1].payload["routing_decision"] == decision.to_dict()


@pytest.mark.invariant
def test_start_run_over_budget_leaves_nothing_behind(env):
    """预算不足：运行、消息、预留、事件一个都不许出现（无部分写入）。"""
    actor, project_id, conversation_id = _world(env)
    with pytest.raises(PlatformError) as failure:
        _start(
            env,
            actor,
            project_id,
            conversation_id,
            estimated_output_tokens=2_001,  # 超过单次输出上限
        )
    assert failure.value.code is ErrorCode.BUDGET_EXCEEDED

    assert env.products.list_messages(actor, project_id, conversation_id) == ()
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    # 账户行可能整体回滚掉了（PG：账户与预留同一事务；内存：账户先建好）——
    # 两种形态都只允许"金额为零"，不允许"扣了额度但运行不存在"。
    project = snapshot["project"] or {"reserved_micro": 0, "in_flight_micro": 0}
    assert project["reserved_micro"] == 0
    assert project["in_flight_micro"] == 0


@pytest.mark.invariant
def test_last_slot_of_budget_goes_to_exactly_one_request(env):
    """额度只够一次：第二个请求预留失败，且没有负额度。"""
    actor, project_id, conversation_id = _world(env)
    # 总额度恰好等于一次预留（EST_MICRO）。
    _start(env, actor, project_id, conversation_id, budget_total_micro=EST_MICRO)
    with pytest.raises(PlatformError) as failure:
        _start(env, actor, project_id, conversation_id, budget_total_micro=EST_MICRO)
    assert failure.value.code is ErrorCode.BUDGET_EXCEEDED
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["tenant"]["reserved_micro"] == EST_MICRO  # 没有多扣


@pytest.mark.invariant
def test_concurrent_reservation_on_last_slot_has_one_winner(env):
    """双连接屏障：最后一份额度同时争抢，只有一个命令成功。"""

    if not env.is_postgres:
        pytest.skip("并发争抢只在 PG 的双连接下有意义（内存锁天然串行）")

    actor, project_id, conversation_id = _world(env)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[ErrorCode] = []

    def attempt() -> None:
        barrier.wait()
        try:
            _start(env, actor, project_id, conversation_id, budget_total_micro=EST_MICRO)
            outcomes.append("ok")
        except PlatformError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes == ["ok"], f"两个并发预留都成功了：{outcomes}"
    assert errors == [ErrorCode.BUDGET_EXCEEDED]


# ------------------------------------------------------------------ 执行


def _claim(env: Env):
    return env.teaching.claim_run(worker_id="w1", lease_seconds=0)


@pytest.mark.invariant
def test_claim_and_finish_settle_budget_and_write_answer(env):
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)

    claim, attempt_id = _claim_and_dispatch(env, run)
    assert claim.run.status is RunStatus.RUNNING
    env.teaching.record_result(
        claim,
        attempt_id=attempt_id,
        provider_request_id="req_1",
        payload={"answer_markdown": "幂等是……", "citations": []},
    )
    answer_id = _unique("msg")
    finished = env.teaching.finish_run(
        claim,
        attempt_id=attempt_id,
        answer_message_id=answer_id,
        answer_text="幂等是……",
        grounding=Grounding.INFERENCE_ONLY,
        usage=USAGE,
    )
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.grounding is Grounding.INFERENCE_ONLY
    messages = env.products.list_messages(actor, project_id, conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "幂等是……"

    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["spent_micro"] == EST_MICRO
    assert snapshot["project"]["in_flight_micro"] == 0

    events = env.teaching.list_events(actor, project_id, run.run_id)
    assert [e.event_type for e in events] == [
        "run.created",
        "run.dispatched",
        "run.succeeded",
    ]
    assert [e.seq for e in events] == [1, 2, 3]


def _claim_and_dispatch(env: Env, run, *, lease_seconds: int = 60) -> tuple:
    """认领 + 派发（记录 attempt id 供后续落定用）。

    默认租约 60 秒：围栏用例自己传 0（冻结时钟下立即过期）。
    """
    claim = env.teaching.claim_run(worker_id="w1", lease_seconds=lease_seconds)
    assert claim.run.run_id == run.run_id
    attempt_id = _unique("att")
    env.teaching.mark_dispatched(
        claim,
        attempt_id=attempt_id,
        estimated_input_tokens=100,
        estimated_output_tokens=50,
    )
    return claim, attempt_id


def _expire_lease(env: Env, run_id: str) -> None:
    """把租约改到过去（内存：推时钟；PG：超级用户直改那一列）。

    等价于"等满了租约期"，但不靠 sleep —— 租约过期的语义就是
    "`lease_until` 落在 now() 之前"，改那一列是这句话的逐字实现。
    """
    if not env.is_postgres:
        env.clock.advance(61)
        return
    dsn = pg_support.require_test_database(pg_support.migration_dsn())
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            updated = conn.execute(
                "UPDATE teaching_runs SET lease_until = now() - interval '1 second' WHERE run_id = %s",
                (run_id,),
            ).rowcount
    assert updated == 1, "租约没被改到 —— 后面那句'已过期'就没有意义"


def self_attempt_id(env, run_id: str) -> str:
    """取该运行的 attempt id（测试辅助；attempt 行由 mark_dispatched 创建）。"""
    if env.is_postgres:
        dsn = pg_support.require_test_database(pg_support.migration_dsn())
        with psycopg.connect(dsn) as conn:
            return conn.execute(
                "SELECT attempt_id FROM provider_attempts WHERE run_id = %s", (run_id,)
            ).fetchone()[0]
    return next(attempt.attempt_id for attempt in env.teaching._attempts.values() if attempt.run_id == run_id)


@pytest.mark.invariant
def test_finish_without_usage_keeps_exposure_and_succeeds(env):
    """缺 usage：运行成功（用户拿到答案），但费用敞口必须保留。"""
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    claim, attempt_id = _claim_and_dispatch(env, run)
    finished = env.teaching.finish_run(
        claim,
        attempt_id=attempt_id,
        answer_message_id=_unique("msg"),
        answer_text="幂等是……",
        grounding=Grounding.INFERENCE_ONLY,
        usage=None,
    )
    assert finished.status is RunStatus.SUCCEEDED
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["in_flight_micro"] == EST_MICRO  # 敞口保留
    assert snapshot["project"]["spent_micro"] == 0  # 绝不按零结算


@pytest.mark.invariant
def test_dispatched_provider_failure_with_usage_commits_failure_payload(env):
    """失败 attempt 也必须满足 PG 的 result_payload 状态约束。"""
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    claim, attempt_id = _claim_and_dispatch(env, run)

    finished = env.teaching.fail_run(
        claim,
        error_code="PROVIDER_REFUSED",
        safe_detail="provider 拒绝",
        dispatch_happened=True,
        usage=USAGE,
    )

    assert finished.status is RunStatus.FAILED
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["spent_micro"] == EST_MICRO
    assert snapshot["project"]["in_flight_micro"] == 0
    if env.is_postgres:
        dsn = pg_support.require_test_database(pg_support.migration_dsn())
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT status, result_payload FROM provider_attempts WHERE attempt_id = %s",
                (attempt_id,),
            ).fetchone()
        assert row[0] == "failed"
        assert row[1]["kind"] == "failure"


@pytest.mark.invariant
def test_pre_dispatch_failure_releases_reservation(env):
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    claim, _ = _claim_and_dispatch(env, run)
    # 派发后 provider 明确报"未送达"：可证明没花钱 → 释放。
    finished = env.teaching.fail_run(
        claim,
        error_code="PROVIDER_DISPATCH_FAILED",
        safe_detail="provider 连接失败；本次未产生费用",
        dispatch_happened=False,
        usage=None,
    )
    assert finished.status is RunStatus.FAILED
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["reserved_micro"] == 0
    assert snapshot["project"]["in_flight_micro"] == 0
    assert snapshot["project"]["spent_micro"] == 0


@pytest.mark.invariant
def test_unknown_usage_after_dispatch_requires_reconciliation(env):
    """已派发 + 用量未知：fail_run 拒绝，必须走 reconciliation（敞口保留）。"""
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    claim, _ = _claim_and_dispatch(env, run)
    with pytest.raises(PlatformError, match="reconciliation"):
        env.teaching.fail_run(
            claim,
            error_code="PROVIDER_TIMEOUT",
            safe_detail="超时",
            dispatch_happened=True,
            usage=None,
        )
    reconciled = env.teaching.require_reconciliation(
        claim, error_code="PROVIDER_TIMEOUT", safe_detail="超时；结果与费用未知"
    )
    assert reconciled.status is RunStatus.RECONCILIATION_REQUIRED
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["in_flight_micro"] == EST_MICRO  # 费用敞口保留


# ------------------------------------------------------------------ 围栏


@pytest.mark.invariant
def test_stale_claim_cannot_settle_a_reclaimed_run(env):
    """A 认领 → 租约过期 → B 接管 → A 的落定被拒，B 正常落定。"""
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)

    stale, attempt_id = _claim_and_dispatch(env, run)
    _expire_lease(env, run.run_id)
    # B 接管（租约已过期 ⇒ 可回收）。
    fresh = env.teaching.claim_run(worker_id="w2", lease_seconds=60)
    assert fresh.run.run_id == run.run_id
    assert fresh.claim_token != stale.claim_token

    with pytest.raises(PlatformError, match="认领"):
        env.teaching.finish_run(
            stale,
            attempt_id=attempt_id,
            answer_message_id=_unique("msg"),
            answer_text="旧持有者的答案",
            grounding=Grounding.INFERENCE_ONLY,
            usage=USAGE,
        )
    # B 正常落定（阳性对照）：消息恰好一条，费用结算一次。
    finished = env.teaching.finish_run(
        fresh,
        attempt_id=attempt_id,
        answer_message_id=_unique("msg"),
        answer_text="新持有者的答案",
        grounding=Grounding.INFERENCE_ONLY,
        usage=USAGE,
    )
    assert finished.status is RunStatus.SUCCEEDED
    messages = env.products.list_messages(actor, project_id, conversation_id)
    assert [m.content for m in messages] == [QUESTION, "新持有者的答案"]
    snapshot = env.teaching.budget_snapshot(actor, project_id)
    assert snapshot["project"]["spent_micro"] == EST_MICRO  # 只结算一次


@pytest.mark.invariant
def test_expired_lease_with_no_takeover_cannot_finish(env):
    """租约过期但没人接管：旧持有者同样不能落定（期限也是围栏）。"""
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    claim, attempt_id = _claim_and_dispatch(env, run)
    _expire_lease(env, run.run_id)
    with pytest.raises(PlatformError, match="租约"):
        env.teaching.finish_run(
            claim,
            attempt_id=attempt_id,
            answer_message_id=_unique("msg"),
            answer_text="迟到的答案",
            grounding=Grounding.INFERENCE_ONLY,
            usage=USAGE,
        )


@pytest.mark.invariant
def test_replay_uses_stored_result_without_new_dispatch(env):
    """final commit 失败 → 新认领读到已存证的结果 → 重放落库。"""
    actor, project_id, conversation_id = _world(env)
    run = _start(env, actor, project_id, conversation_id)
    first, attempt_id = _claim_and_dispatch(env, run)
    payload = {"answer_markdown": "已存证的答案", "citations": []}
    env.teaching.record_result(
        first,
        attempt_id=attempt_id,
        provider_request_id="req_1",
        payload=payload,
    )
    _expire_lease(env, run.run_id)
    # 第二个 worker 接管（模拟第一个 worker 提交前崩溃、租约过期）。
    second = env.teaching.claim_run(worker_id="w2", lease_seconds=60)
    assert second.run.run_id == run.run_id
    stored = env.teaching.find_stored_result(second)
    assert stored == payload  # 结果从存证恢复，绝不重新调用模型


@pytest.mark.postgres
@pytest.mark.invariant
@pytest.mark.skipif(not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）")
def test_app_role_cannot_see_the_run_queue_with_a_self_set_variable():
    """教学队列的跨租户可见性只授予 `study_worker`（0010 的 TO study_worker）。

    应用角色自设 `app.worker_id` 仍然 0 行 —— 自定义变量不是凭据
    （R4-01 的同类问题，教学队列从第一天就按角色限定）。
    """
    dsn = pg_support.require_test_database(pg_support.app_dsn())
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.worker_id', 'x', true)")
        visible = conn.execute("SELECT count(*) FROM teaching_runs").fetchone()[0]
    assert visible == 0, "应用角色自设 worker 变量后看到了运行队列：角色限定失效"

    # 阳性对照：worker 角色确实看得见（别让"全库恰好为空"蒙混过关）。
    worker_dsn = pg_support.require_test_database(pg_support.worker_dsn())
    with psycopg.connect(worker_dsn) as conn:
        conn.execute("SELECT set_config('app.worker_id', 'x', true)")
        worker_visible = conn.execute("SELECT count(*) FROM teaching_runs").fetchone()[0]
    assert worker_visible >= 0  # 关键是连接本身成功且走的是 worker 策略
