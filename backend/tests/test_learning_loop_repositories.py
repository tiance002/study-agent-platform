# -*- coding: utf-8 -*-
"""学习闭环跨表命令契约：内存锁与 PostgreSQL 事务必须同语义。"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.learning.ports import TaskAssessment
from app.product.models import (
    LearningPlan,
    LearningTask,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)

MIGRATION_DSN = os.environ.get(
    "STUDY_PLATFORM_MIGRATION_DSN",
    "postgresql://postgres@127.0.0.1:5432/study_platform",
)
TENANT = "t_loop_pg"
ALICE = "u_loop_pg_alice"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(MIGRATION_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _actor() -> Principal:
    return Principal(principal_id=ALICE, tenant_id=TENANT)


@pytest.fixture(scope="module")
def pg_seed():
    if not _postgres_reachable():
        return
    with psycopg.connect(MIGRATION_DSN) as conn, conn.transaction():
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


@pytest.fixture(params=["memory", "postgres"])
def loop_env(request, pg_seed):
    if request.param == "postgres" and not _postgres_reachable():
        pytest.skip("本地 PostgreSQL 未运行")
    if request.param == "memory":
        from app.identity.membership import MembershipStore
        from app.learning.loop_store import InMemoryLearningLoopRepository
        from app.learning.memory_store import InMemoryEvidenceRepository
        from app.product.memory_store import InMemoryProductRepository

        membership = MembershipStore()
        products = InMemoryProductRepository(membership=membership)
        evidence = InMemoryEvidenceRepository()
        loop = InMemoryLearningLoopRepository(products, evidence)
    else:
        from app.db.evidence_store import PostgresEvidenceRepository
        from app.db.identity_store import PostgresMembershipRepository
        from app.db.learning_store import PostgresLearningLoopRepository
        from app.db.product_store import PostgresProductRepository

        membership = PostgresMembershipRepository()
        products = PostgresProductRepository(membership=membership)
        evidence = PostgresEvidenceRepository(None)
        loop = PostgresLearningLoopRepository(products=products, evidence=evidence)
    return loop, products, evidence, membership


def _bundle(project_id: str) -> PlanBundle:
    plan_id, milestone_id, task_id = _id("plan"), _id("mile"), _id("task")
    return PlanBundle(
        plan=LearningPlan(
            plan_id, TENANT, project_id, 1, "学会事务",
            PlanStatus.ACTIVE, datetime.now(timezone.utc),
        ),
        milestones=(
            Milestone(
                milestone_id, TENANT, project_id, plan_id, 0, "理解", "",
            ),
        ),
        tasks=(
            LearningTask(
                task_id, TENANT, project_id, milestone_id, 0,
                "解释原子性", TaskStatus.PENDING,
            ),
        ),
    )


def _create_project(env) -> str:
    _, _, _, membership = env
    project_id = _id("proj")
    membership.create_project_for(
        _actor(), project_id=project_id, name="闭环", goal="学会事务"
    )
    return project_id


def _assessment(task_id: str) -> TaskAssessment:
    return TaskAssessment(_id("asm"), task_id, "concept", "contract/v1", "map/v1")


@pytest.mark.invariant
def test_generated_plan_freezes_assessment_and_submission_appends_evidence(loop_env):
    loop, products, evidence, _ = loop_env
    project_id = _create_project(loop_env)
    bundle = _bundle(project_id)
    task_id = bundle.tasks[0].task_id
    saved = loop.install_generated_plan(
        _actor(), project_id, bundle, (_assessment(task_id),)
    )
    products.transition_task(
        _actor(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )

    submission, event = loop.submit_self_report(
        _actor(), project_id, task_id,
        submission_id=_id("sub"), content="我可以解释事务回滚。",
    )

    assert saved.plan.version == 1
    assert submission.task_id == task_id
    assert event.contract_id == "contract/v1"
    assert event.verdicts[0].component_id == "concept"
    assert event.verdicts[0].independence_group == submission.submission_id
    assert len(loop.submissions_for_task(_actor(), project_id, task_id)) == 1
    assert [item.event_id for item in evidence.events_for(_actor(), project_id)] == [
        event.event_id
    ]


@pytest.mark.invariant
def test_submission_requires_in_progress_and_frozen_mapping(loop_env):
    loop, products, evidence, _ = loop_env
    project_id = _create_project(loop_env)
    bundle = _bundle(project_id)
    task_id = bundle.tasks[0].task_id
    products.replace_plan(_actor(), project_id, bundle)

    with pytest.raises(PlatformError) as excinfo:
        loop.submit_self_report(
            _actor(), project_id, task_id,
            submission_id=_id("sub"), content="过早提交",
        )
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION

    products.transition_task(
        _actor(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )
    with pytest.raises(PlatformError) as excinfo:
        loop.submit_self_report(
            _actor(), project_id, task_id,
            submission_id=_id("sub"), content="没有映射",
        )
    assert excinfo.value.code is ErrorCode.EVIDENCE_UNMAPPED
    assert loop.submissions_for_task(_actor(), project_id, task_id) == ()
    assert evidence.events_for(_actor(), project_id) == ()


@pytest.mark.invariant
def test_submission_and_evidence_roll_back_together(loop_env, monkeypatch):
    loop, products, evidence, _ = loop_env
    project_id = _create_project(loop_env)
    bundle = _bundle(project_id)
    task_id = bundle.tasks[0].task_id
    loop.install_generated_plan(_actor(), project_id, bundle, (_assessment(task_id),))
    products.transition_task(
        _actor(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )

    method = (
        "append_learning_in_transaction"
        if hasattr(evidence, "append_learning_in_transaction")
        else "append_learning"
    )

    def fail(*args, **kwargs):
        raise RuntimeError("forced evidence failure")

    monkeypatch.setattr(evidence, method, fail)
    with pytest.raises(RuntimeError, match="forced evidence failure"):
        loop.submit_self_report(
            _actor(), project_id, task_id,
            submission_id=_id("sub"), content="触发回滚",
        )
    assert loop.submissions_for_task(_actor(), project_id, task_id) == ()


@pytest.mark.invariant
def test_latest_diagnosis_is_append_only(loop_env):
    loop, _, _, _ = loop_env
    project_id = _create_project(loop_env)
    loop.record_diagnosis(
        _actor(), project_id, diagnosis_id=_id("diag"),
        answers={"confidence": 1}, summary="起步",
    )
    second = loop.record_diagnosis(
        _actor(), project_id, diagnosis_id=_id("diag"),
        answers={"confidence": 3}, summary="已有基础",
    )
    assert loop.latest_diagnosis(_actor(), project_id) == second


@pytest.mark.invariant
@pytest.mark.parametrize("content", ["", "x" * 20_001])
def test_submission_content_limits_match_across_adapters(loop_env, content):
    loop, products, evidence, _ = loop_env
    project_id = _create_project(loop_env)
    bundle = _bundle(project_id)
    task_id = bundle.tasks[0].task_id
    loop.install_generated_plan(_actor(), project_id, bundle, (_assessment(task_id),))
    products.transition_task(
        _actor(), project_id, task_id,
        expected_status=TaskStatus.PENDING, next_status=TaskStatus.IN_PROGRESS,
    )
    with pytest.raises(PlatformError) as excinfo:
        loop.submit_self_report(
            _actor(), project_id, task_id,
            submission_id=_id("sub"), content=content,
        )
    assert excinfo.value.code is ErrorCode.PARAMS_INVALID
    assert loop.submissions_for_task(_actor(), project_id, task_id) == ()
    assert evidence.events_for(_actor(), project_id) == ()
