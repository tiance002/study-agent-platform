# -*- coding: utf-8 -*-
"""学习闭环仓储的**契约测试**（任务 9）：证据仓储 + 任务状态流转。

内存与 PostgreSQL 适配器跑同一批断言。重点守四条语义：

1. **证据回放一致**：append 后 events_for 返回的事件与写入语义一致
   （payload 往返不丢裁决、不丢契约引用）；
2. **append-only 的校验透传**：学习证据缺契约 / 缺裁决必须被拒
   （内存与 PG 共用 EvidenceLog 的校验，此处确认校验真的生效）;
3. **项目隔离**：A 项目的证据不会出现在 B 项目的回放里；
4. **状态流转**：合法路径通过、非法迁移同码拒绝、过期 expected 冲突、
   终态无出边、重放幂等。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.learning.evidence import (
    ComponentVerdict,
    Direction,
    IndependenceLevel,
    ObservationStrength,
    Validity,
)
from app.learning.memory_store import InMemoryEvidenceRepository
from app.product.memory_store import InMemoryProductRepository
from app.product.models import (
    LearningPlan,
    LearningTask,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)

TENANT = "t_learn_pg"
ALICE = "u_learn_pg_alice"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def pg_seed() -> None:
    if not _postgres_reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (TENANT, TENANT),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                " ON CONFLICT (principal_id) DO NOTHING",
                (ALICE, TENANT),
            )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _alice() -> Principal:
    return Principal(principal_id=ALICE, tenant_id=TENANT)


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not _postgres_reachable(),
                    reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
                ),
            ],
            id="postgres",
        ),
    ]
)
def env(request, pg_seed):
    """返回 (evidence_repo, products_repo, membership)。"""
    if request.param == "memory":
        from app.identity.membership import MembershipStore

        membership = MembershipStore()
        products = InMemoryProductRepository(membership=membership)
        evidence = InMemoryEvidenceRepository()
        return evidence, products, membership

    from app.db.evidence_store import PostgresEvidenceRepository
    from app.db.identity_store import PostgresMembershipRepository
    from app.db.product_store import PostgresProductRepository

    membership = PostgresMembershipRepository()
    products = PostgresProductRepository(membership=membership)
    evidence = PostgresEvidenceRepository(None)
    return evidence, products, membership


def _project_with_task(env) -> tuple[str, str]:
    """建项目 + 一版计划（含两个任务），返回 (project_id, task_id)。"""
    evidence, products, membership = env
    project_id = _unique("proj")
    membership.create_project_for(_alice(), project_id=project_id, name="学习", goal="")

    plan_id = _unique("plan")
    m1 = _unique("mile")
    t1, t2 = _unique("task"), _unique("task")
    now = datetime.now(timezone.utc)
    bundle = PlanBundle(
        plan=LearningPlan(
            plan_id=plan_id, tenant_id=TENANT, project_id=project_id,
            version=99, goal="目标", status=PlanStatus.ACTIVE, created_at=now,
        ),
        milestones=(
            Milestone(
                milestone_id=m1, tenant_id=TENANT, project_id=project_id,
                plan_id=plan_id, order_index=0, title="第一周", description="",
            ),
        ),
        tasks=(
            LearningTask(
                task_id=t1, tenant_id=TENANT, project_id=project_id,
                milestone_id=m1, order_index=0, title="读第一章",
                status=TaskStatus.PENDING,
            ),
            LearningTask(
                task_id=t2, tenant_id=TENANT, project_id=project_id,
                milestone_id=m1, order_index=1, title="做练习",
                status=TaskStatus.PENDING,
            ),
        ),
    )
    products.replace_plan(_alice(), project_id, bundle)
    return project_id, t1


def _verdict(component_id: str = "concept") -> ComponentVerdict:
    """MVP 自报证据的裁决：有效、OBS_1、正向、INTRODUCED。"""
    return ComponentVerdict(
        component_id=component_id,
        assessment_validity=Validity.VALID,
        observation_strength=ObservationStrength.OBS_1,
        source_reliability_ok=True,
        independence_level=IndependenceLevel.INTRODUCED,
        direction=Direction.POSITIVE,
        independence_group=_unique("grp"),
    )


# ---------------------------------------------------------------- 证据回放


@pytest.mark.invariant
def test_evidence_roundtrip_preserves_verdicts(env):
    evidence, _, _ = env
    project_id, task_id = _project_with_task(env)
    verdict = _verdict()

    evidence.append_learning(
        _alice(), project_id=project_id, task_id=task_id,
        contract_id="contract_test", mapping_version="v1", verdicts=(verdict,),
    )
    replayed = evidence.events_for(_alice(), project_id)

    assert len(replayed) == 1
    got = replayed[0]
    assert got.task_id == task_id
    assert got.contract_id == "contract_test"
    assert got.graph_version == "graph/v1"
    assert got.verdicts[0].component_id == verdict.component_id
    assert got.verdicts[0].direction is Direction.POSITIVE
    assert got.verdicts[0].assessment_validity is Validity.VALID


@pytest.mark.invariant
def test_evidence_requires_contract_and_verdicts(env):
    """append-only 校验透传：学习证据缺契约 / 缺裁决必须被拒。"""
    evidence, _, _ = env
    project_id, task_id = _project_with_task(env)

    with pytest.raises(PlatformError) as excinfo:
        evidence.append_learning(
            _alice(), project_id=project_id, task_id=task_id,
            contract_id="", mapping_version="v1", verdicts=(_verdict(),),
        )
    assert excinfo.value.code is ErrorCode.EVIDENCE_UNMAPPED

    with pytest.raises(PlatformError):
        evidence.append_learning(
            _alice(), project_id=project_id, task_id=task_id,
            contract_id="c1", mapping_version="v1", verdicts=(),
        )


@pytest.mark.invariant
def test_evidence_scoped_by_project(env):
    """项目隔离：A 项目的证据不进入 B 项目的回放。"""
    evidence, _, _ = env
    project_a, task_a = _project_with_task(env)
    project_b, _ = _project_with_task(env)

    evidence.append_learning(
        _alice(), project_id=project_a, task_id=task_a,
        contract_id="c", mapping_version="v1", verdicts=(_verdict(),),
    )
    assert evidence.events_for(_alice(), project_b) == ()
    assert len(evidence.events_for(_alice(), project_a)) == 1


# ---------------------------------------------------------------- 状态流转


@pytest.mark.invariant
def test_transition_legal_path(env):
    evidence, products, _ = env
    project_id, task_id = _project_with_task(env)

    task = products.transition_task(
        _alice(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )
    assert task.status is TaskStatus.IN_PROGRESS
    task = products.transition_task(
        _alice(), project_id, task_id,
        expected_status=TaskStatus.IN_PROGRESS, next_status=TaskStatus.DONE,
    )
    assert task.status is TaskStatus.DONE


@pytest.mark.invariant
def test_transition_illegal_is_rejected(env):
    evidence, products, _ = env
    project_id, task_id = _project_with_task(env)

    with pytest.raises(PlatformError) as excinfo:
        products.transition_task(
            _alice(), project_id, task_id,
            expected_status=TaskStatus.PENDING, next_status=TaskStatus.DONE,
        )
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    # 拒绝后状态不变。
    assert products.get_task(_alice(), project_id, task_id).status is TaskStatus.PENDING


@pytest.mark.invariant
def test_transition_terminal_has_no_exit(env):
    evidence, products, _ = env
    project_id, task_id = _project_with_task(env)

    products.transition_task(
        _alice(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.SKIPPED,
    )
    with pytest.raises(PlatformError) as excinfo:
        products.transition_task(
            _alice(), project_id, task_id,
            expected_status=TaskStatus.SKIPPED, next_status=TaskStatus.IN_PROGRESS,
        )
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


@pytest.mark.invariant
def test_transition_stale_expected_conflicts(env):
    evidence, products, _ = env
    project_id, task_id = _project_with_task(env)

    products.transition_task(
        _alice(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )
    with pytest.raises(PlatformError) as excinfo:
        products.transition_task(
            _alice(), project_id, task_id,
            expected_status=TaskStatus.PENDING, next_status=TaskStatus.SKIPPED,
        )
    assert excinfo.value.code is ErrorCode.VERSION_CONFLICT


@pytest.mark.invariant
def test_transition_replay_is_idempotent(env):
    """同目标状态的重试返回现状（幂等成功），不报冲突 —— 重试安全的应有语义。"""
    evidence, products, _ = env
    project_id, task_id = _project_with_task(env)

    products.transition_task(
        _alice(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )
    task = products.transition_task(
        _alice(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )
    assert task.status is TaskStatus.IN_PROGRESS
