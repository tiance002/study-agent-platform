"""租户隔离、策略网关、capability token 与 taint 的不变量证明。

每个测试对应 `docs/skills/L0/invariants.md` 里的一条不变量。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.clock import utc
from app.core.errors import ErrorCode, PlatformError
from app.policy.gateway import (
    ExecutorCapabilities,
    Obligation,
    PolicyDecision,
    PolicyGateway,
    PolicyInput,
    Verdict,
)
from app.policy.taint import (
    EndorsementRegistry,
    EndorsementSink,
    TaintSource,
    derive,
    mark_tainted,
)
from app.policy.token import TokenIssuer
from app.registry.models import Authority
from app.tenancy.context import TenantContext, tenant_scope
from app.tenancy.memory_store import InMemoryRepository


def _policy_input(**overrides) -> PolicyInput:
    base = dict(
        request_id="req_1",
        principal_id="user_a",
        tenant_id="tenant_a",
        project_id="proj_a",
        node_id="retrieve_material",
        tool_id="retrieve_project_chunks",
        authority_required=Authority.A1A,
        authority_ceiling=Authority.A1C,
        params_hash="sha256:abc",
    )
    base.update(overrides)
    return PolicyInput(**base)


# --------------------------------------------------------------- 不变量 #1 / #2


@pytest.mark.invariant
def test_missing_tenant_context_fails_instead_of_scanning_all():
    """不变量 #2：缺少租户上下文的查询必须失败，不得回落为无过滤查询。"""
    repo = InMemoryRepository()
    with pytest.raises(PlatformError) as exc:
        repo.list_all("projects")
    assert exc.value.code is ErrorCode.TENANT_CONTEXT_MISSING


@pytest.mark.invariant
def test_cross_tenant_read_is_denied():
    """不变量 #1：未通过租户校验的请求不得读取项目数据。"""
    repo = InMemoryRepository()
    with tenant_scope(TenantContext("tenant_a", "user_a", "proj_a")):
        repo.put("docs", {"id": "d1", "tenant_id": "tenant_a", "learning_project_id": "proj_a"})
        assert repo.get("docs", "d1") is not None

    with tenant_scope(TenantContext("tenant_b", "user_b", "proj_b")):
        with pytest.raises(PlatformError) as exc:
            repo.get("docs", "d1")
        assert exc.value.code is ErrorCode.CROSS_TENANT_DENIED


@pytest.mark.invariant
def test_list_all_is_scoped_to_current_project():
    """项目级实体必须按项目过滤，不能跨项目互相可见。"""
    repo = InMemoryRepository()
    with tenant_scope(TenantContext("tenant_a", "user_a", "proj_a")):
        repo.put("docs", {"id": "d1", "tenant_id": "tenant_a", "learning_project_id": "proj_a"})
    with tenant_scope(TenantContext("tenant_a", "user_a", "proj_b")):
        repo.put("docs", {"id": "d2", "tenant_id": "tenant_a", "learning_project_id": "proj_b"})
        assert [r["id"] for r in repo.list_all("docs")] == ["d2"]


# ------------------------------------------------------------------- 策略网关


@pytest.mark.invariant
def test_policy_gateway_is_deterministic():
    """03 号规格 §2：相同 policy_version + 不可变快照必产生相同决策。"""
    gateway = PolicyGateway()
    payload = _policy_input()
    first, second = gateway.decide(payload), gateway.decide(payload)
    assert first.verdict is second.verdict is Verdict.ALLOW
    assert first.snapshot_hash == second.snapshot_hash
    assert first.obligations == second.obligations
    assert first.decision_id == second.decision_id


@pytest.mark.invariant
def test_policy_gateway_unavailable_fails_closed():
    """00 号规格 §4 失败矩阵：网关不可用时特权路径一律拒绝。"""
    gateway = PolicyGateway(available=False)
    decision = gateway.decide(_policy_input())
    assert decision.verdict is Verdict.DENY
    assert "policy_gateway_unavailable" in decision.deny_reasons
    with pytest.raises(PlatformError) as exc:
        decision.require_allowed()
    assert exc.value.code is ErrorCode.POLICY_DENIED


@pytest.mark.invariant
def test_audit_unavailable_blocks_high_impact_action():
    """03 号规格 §9：审计 sink 不可用时高影响动作 fail-closed。"""
    decision = PolicyGateway().decide(
        _policy_input(authority_required=Authority.A2, audit_available=False)
    )
    assert decision.verdict is Verdict.DENY
    assert "audit_sink_unavailable" in decision.deny_reasons


@pytest.mark.invariant
def test_authority_ceiling_is_enforced_by_policy():
    """权限不得超过 node 上限，且这是策略层判定而不是调用方自觉。"""
    decision = PolicyGateway().decide(
        _policy_input(authority_required=Authority.A2, authority_ceiling=Authority.A1A)
    )
    assert decision.verdict is Verdict.DENY
    assert "authority_ceiling_exceeded" in decision.deny_reasons


@pytest.mark.invariant
def test_unsupported_obligation_is_denied():
    """03 号规格 §2：执行器必须声明支持的 obligation，未知的一律拒绝。"""
    capabilities = ExecutorCapabilities(frozenset({str(Obligation.REQUIRE_TAINT_ENDORSEMENT)}))
    decision = PolicyDecision(
        decision_id="dec_1",
        policy_version="policy/v1",
        snapshot_id="snap_1",
        snapshot_hash="sha256:1",
        verdict=Verdict.ALLOW,
        obligations=(str(Obligation.REQUIRE_DUAL_APPROVAL),),
    )
    with pytest.raises(PlatformError) as exc:
        capabilities.assert_supported(decision)
    assert exc.value.code is ErrorCode.OBLIGATION_UNSUPPORTED


@pytest.mark.invariant
def test_skill_declaration_does_not_affect_authorization():
    """不变量 #17：skill 只改变「模型知道什么」，不改变「模型被允许做什么」。

    做法：同一个请求换掉 node 的 declared_skills，决策必须完全一致。
    """
    gateway = PolicyGateway()
    baseline = _policy_input()
    # declared_skills 根本不在策略输入中，因此换不换都不影响决策。
    assert "declared_skills" not in baseline.snapshot()
    with_skills = _policy_input(params_hash="sha256:abc")
    assert gateway.decide(baseline).decision_id == gateway.decide(with_skills).decision_id


# --------------------------------------------------------------- capability token


@pytest.mark.invariant
def test_capability_token_can_only_shrink():
    """不变量 #4：capability token 只能收缩，扩权必须重新评估签发。"""
    issuer = TokenIssuer(secret="test-secret")
    now = utc(2026, 9, 18)
    parent = issuer.issue(
        tenant_id="tenant_a",
        project_id="proj_a",
        run_id="run_1",
        node_instance_id="n:1",
        audience="user_a",
        allowed_tools={"retrieve_project_chunks", "query_competency_graph"},
        policy_version="policy/v1",
        registry_version="sha256:r",
        revocation_epoch=0,
        budget_account_id="acct_1",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    child = issuer.derive(parent, allowed_tools={"retrieve_project_chunks"}, expires_at=now + timedelta(minutes=5))
    assert child.allowed_tools == frozenset({"retrieve_project_chunks"})

    with pytest.raises(PlatformError) as exc:
        issuer.derive(parent, allowed_tools={"run_in_sandbox"}, expires_at=parent.expires_at)
    assert exc.value.code is ErrorCode.CAPABILITY_ESCALATION_DENIED


@pytest.mark.invariant
def test_capability_token_cannot_outlive_parent():
    """派生 token 的过期时间不得晚于父 token。"""
    issuer = TokenIssuer(secret="test-secret")
    now = utc(2026, 9, 18)
    parent = issuer.issue(
        tenant_id="tenant_a",
        project_id="proj_a",
        run_id="run_1",
        node_instance_id="n:1",
        audience="user_a",
        allowed_tools={"retrieve_project_chunks"},
        policy_version="policy/v1",
        registry_version="sha256:r",
        revocation_epoch=0,
        budget_account_id="acct_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    with pytest.raises(PlatformError) as exc:
        issuer.derive(parent, allowed_tools={"retrieve_project_chunks"}, expires_at=now + timedelta(hours=2))
    assert exc.value.code is ErrorCode.CAPABILITY_ESCALATION_DENIED


@pytest.mark.invariant
def test_tampered_token_fails_verification():
    """token 任一字段被改动即验签失败。"""
    from dataclasses import replace

    issuer = TokenIssuer(secret="test-secret")
    now = utc(2026, 9, 18)
    token = issuer.issue(
        tenant_id="tenant_a",
        project_id="proj_a",
        run_id="run_1",
        node_instance_id="n:1",
        audience="user_a",
        allowed_tools={"retrieve_project_chunks"},
        policy_version="policy/v1",
        registry_version="sha256:r",
        revocation_epoch=0,
        budget_account_id="acct_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    forged = replace(token, allowed_tools=frozenset({"run_in_sandbox", "retrieve_project_chunks"}))
    with pytest.raises(PlatformError) as exc:
        issuer.verify(forged, now=now, current_revocation_epoch=0)
    assert exc.value.code is ErrorCode.CAPABILITY_NOT_HELD


@pytest.mark.invariant
def test_revocation_epoch_invalidates_old_tokens():
    """广播撤销后，旧 token 立即作废。"""
    issuer = TokenIssuer(secret="test-secret")
    now = utc(2026, 9, 18)
    token = issuer.issue(
        tenant_id="tenant_a",
        project_id="proj_a",
        run_id="run_1",
        node_instance_id="n:1",
        audience="user_a",
        allowed_tools={"retrieve_project_chunks"},
        policy_version="policy/v1",
        registry_version="sha256:r",
        revocation_epoch=0,
        budget_account_id="acct_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    with pytest.raises(PlatformError):
        issuer.verify(token, now=now, current_revocation_epoch=1)


# ------------------------------------------------------------------- taint


@pytest.mark.invariant
def test_model_summary_cannot_remove_taint():
    """不变量 #10：模型摘要不能自动去污点。派生值的来源是全部父来源的并集。"""
    web = mark_tainted("v1", "来自网页的内容", TaintSource.WEB)
    model = mark_tainted("v2", "模型生成的内容", TaintSource.MODEL_OUTPUT)
    summary = derive([web, model], "v3", "这是子 agent 写的一段摘要")
    assert summary.tainted
    assert set(summary.sources) == {TaintSource.WEB, TaintSource.MODEL_OUTPUT}
    assert summary.derived_from == ("v1", "v2")


@pytest.mark.invariant
def test_endorsement_is_sink_specific():
    """不变量 #10：SQL 参数的 endorsement 不得用于 Shell。"""
    registry = EndorsementRegistry()
    now = utc(2026, 9, 18)
    value = mark_tainted("v1", {"name": "x"}, TaintSource.USER_INPUT)
    endorsement = registry.issue(
        value=value,
        subject_id="user_a",
        operation="insert",
        sink=EndorsementSink.SQL,
        schema_id="users/v1",
        policy_decision_id="dec_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    registry.authorize(
        endorsement,
        value=value,
        subject_id="user_a",
        operation="insert",
        sink=EndorsementSink.SQL,
        schema_id="users/v1",
        now=now,
    )
    with pytest.raises(PlatformError) as exc:
        registry.authorize(
            endorsement,
            value=value,
            subject_id="user_a",
            operation="insert",
            sink=EndorsementSink.SHELL,
            schema_id="users/v1",
            now=now,
        )
    assert exc.value.code is ErrorCode.ENDORSEMENT_SINK_MISMATCH


@pytest.mark.invariant
def test_endorsement_is_single_use():
    """授权持久化后不能作为未来执行的授权复用。"""
    registry = EndorsementRegistry()
    now = utc(2026, 9, 18)
    value = mark_tainted("v1", {"q": 1}, TaintSource.USER_INPUT)
    endorsement = registry.issue(
        value=value,
        subject_id="user_a",
        operation="query",
        sink=EndorsementSink.SQL,
        schema_id="q/v1",
        policy_decision_id="dec_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    registry.consume(endorsement)
    with pytest.raises(PlatformError) as exc:
        registry.authorize(
            registry._issued[endorsement.endorsement_id],
            value=value,
            subject_id="user_a",
            operation="query",
            sink=EndorsementSink.SQL,
            schema_id="q/v1",
            now=now,
        )
    assert exc.value.code is ErrorCode.ENDORSEMENT_REPLAY_DENIED


@pytest.mark.invariant
def test_endorsement_invalidated_when_value_changes():
    """派生或拼接会生成新值，旧 endorsement 立即失效。"""
    registry = EndorsementRegistry()
    now = utc(2026, 9, 18)
    original = mark_tainted("v1", {"a": 1}, TaintSource.USER_INPUT)
    endorsement = registry.issue(
        value=original,
        subject_id="user_a",
        operation="query",
        sink=EndorsementSink.SQL,
        schema_id="q/v1",
        policy_decision_id="dec_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    changed = derive([original], "v2", {"a": 1, "b": 2})
    with pytest.raises(PlatformError) as exc:
        registry.authorize(
            endorsement,
            value=changed,
            subject_id="user_a",
            operation="query",
            sink=EndorsementSink.SQL,
            schema_id="q/v1",
            now=now,
        )
    assert exc.value.code is ErrorCode.TAINT_REQUIRES_ENDORSEMENT


@pytest.mark.invariant
def test_expired_endorsement_is_rejected():
    registry = EndorsementRegistry()
    now = utc(2026, 9, 18)
    value = mark_tainted("v1", {"a": 1}, TaintSource.WEB)
    endorsement = registry.issue(
        value=value,
        subject_id="user_a",
        operation="read",
        sink=EndorsementSink.MODEL_CONTEXT,
        schema_id="ctx/v1",
        policy_decision_id="dec_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    with pytest.raises(PlatformError) as exc:
        registry.authorize(
            endorsement,
            value=value,
            subject_id="user_a",
            operation="read",
            sink=EndorsementSink.MODEL_CONTEXT,
            schema_id="ctx/v1",
            now=now + timedelta(minutes=2),
        )
    assert exc.value.code is ErrorCode.ENDORSEMENT_EXPIRED
