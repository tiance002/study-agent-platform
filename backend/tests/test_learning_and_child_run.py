"""学习证据、掌握投影与子任务运行时的不变量证明。

对应 01 号规格 §5/§6/§7 与不变量 #10/#14/#15/#16。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.clock import ClockViolation, FixedClock, SystemClock, projection_scope, utc
from app.core.errors import ErrorCode, PlatformError
from app.execution.child_run import (
    ArtifactRef,
    ChildEnvelope,
    ChildPurpose,
    ChildRunRuntime,
    Claim,
    ClaimKind,
    DisplayPolicy,
    EnvelopeStatus,
)
from app.learning.evidence import (
    ComponentVerdict,
    Confidence,
    Direction,
    EvidenceKind,
    EvidenceLog,
    IndependenceLevel,
    ObservationStrength,
    Validity,
)
from app.learning.projector import Projector
from app.policy.token import TokenIssuer
from app.workflow.catalog import build_registry

NOW = utc(2026, 9, 18, 10, 0)


def verdict(
    component_id: str = "llm.tool_calling",
    *,
    level: IndependenceLevel = IndependenceLevel.PRACTICED,
    group: str = "g1",
    validity: Validity = Validity.VALID,
    direction: Direction = Direction.POSITIVE,
    observation: ObservationStrength = ObservationStrength.OBS_4,
) -> ComponentVerdict:
    return ComponentVerdict(
        component_id=component_id,
        assessment_validity=validity,
        observation_strength=observation,
        source_reliability_ok=True,
        independence_level=level,
        direction=direction,
        independence_group=group,
    )


def append(log: EvidenceLog, verdicts, **overrides):
    params = dict(
        tenant_id="tenant_a",
        project_id="proj_a",
        kind=EvidenceKind.LEARNING,
        task_id="task_1",
        contract_id="ctr_1",
        mapping_version="map_v1",
        graph_version="graph/v1",
        occurred_at=NOW,
        verdicts=tuple(verdicts),
    )
    params.update(overrides)
    return log.append(**params)


# ------------------------------------------------------------------ 证据写入


@pytest.mark.invariant
def test_learning_evidence_requires_frozen_contract():
    """01 号规格 §4：普通练习不能事后追认为 assessment。"""
    log = EvidenceLog()
    with pytest.raises(PlatformError) as exc:
        append(log, [verdict()], contract_id=None)
    assert exc.value.code is ErrorCode.EVIDENCE_UNMAPPED


@pytest.mark.invariant
def test_learning_evidence_requires_valid_mapping():
    """无有效 mapping 的证据不能进入掌握投影。"""
    log = EvidenceLog()
    with pytest.raises(PlatformError) as exc:
        append(log, [verdict()], mapping_version=None)
    assert exc.value.code is ErrorCode.EVIDENCE_UNMAPPED


@pytest.mark.invariant
def test_evidence_log_has_no_update_or_delete():
    """01 号规格 §11：EvidenceEvent 只能 INSERT。"""
    log = EvidenceLog()
    for name in ("update", "delete", "remove", "rewrite", "purge"):
        assert not hasattr(log, name), f"证据日志不应提供 {name}"


@pytest.mark.invariant
def test_failed_fact_survives_correction():
    """01 号规格 §7：复测通过只 supersede 贡献，不删除失败事实。"""
    log = EvidenceLog()
    failure = append(log, [verdict(level=IndependenceLevel.UNKNOWN, direction=Direction.NEGATIVE)])
    log.append_correction(
        target_event_id=failure.event_id,
        component_id="llm.tool_calling",
        supersedes=True,
        reason="复测通过",
        issued_at=NOW,
    )
    assert any(e.event_id == failure.event_id for e in log.events())
    assert len(log.corrections()) == 1


# ------------------------------------------------------------------ 投影性质


@pytest.mark.invariant
def test_projection_is_order_independent():
    """投影必须与事件到达顺序无关。"""
    log = EvidenceLog()
    first = append(log, [verdict(group="g1")])
    second = append(log, [verdict(group="g2", level=IndependenceLevel.DEMONSTRATED)])

    projector = Projector()
    forward = projector.project_from(events=[first, second], corrections=[], graph_version="g/v1")
    backward = projector.project_from(events=[second, first], corrections=[], graph_version="g/v1")

    assert forward.input_set_hash == backward.input_set_hash
    assert forward.components == backward.components


@pytest.mark.invariant
def test_projection_forbids_reading_current_time():
    """01 号规格 §6：投影算法禁止使用当前时间。"""
    with projection_scope():
        with pytest.raises(ClockViolation):
            SystemClock().now()


@pytest.mark.invariant
def test_fixed_clock_is_allowed_inside_projection():
    """投影重放可以用固定时钟，因此结果可复现。"""
    fixed = FixedClock(NOW)
    with projection_scope():
        assert fixed.now() == NOW


@pytest.mark.invariant
def test_product_evidence_does_not_form_mastery():
    """不变量 #14：跑通代码不等于掌握。产品证据不进入掌握投影。"""
    log = EvidenceLog()
    append(
        log,
        [verdict(level=IndependenceLevel.DEMONSTRATED)],
        kind=EvidenceKind.PROJECT,
        contract_id=None,
        mapping_version=None,
    )
    projection = Projector().project(log, graph_version="g/v1")
    assert projection.components == ()
    assert projection.level_of("llm.tool_calling") is IndependenceLevel.UNKNOWN


@pytest.mark.invariant
def test_invalid_verdicts_do_not_enter_projection():
    """筛选层结论为 voided / attribution_pending 的裁决不进入投影。"""
    log = EvidenceLog()
    append(log, [verdict(validity=Validity.VOIDED, level=IndependenceLevel.DEMONSTRATED)])
    append(log, [verdict(validity=Validity.ATTRIBUTION_PENDING)])
    projection = Projector().project(log, graph_version="g/v1")
    assert projection.components == ()


@pytest.mark.invariant
def test_negative_evidence_does_not_raise_level():
    """失败事实保留为负向贡献，但不提升独立水平。"""
    log = EvidenceLog()
    append(log, [verdict(direction=Direction.NEGATIVE, level=IndependenceLevel.DEMONSTRATED)])
    projection = Projector().project(log, graph_version="g/v1")
    component = projection.component("llm.tool_calling")
    assert component is not None
    assert component.independence_level is IndependenceLevel.UNKNOWN
    assert component.negative_evidence_count == 1


@pytest.mark.invariant
def test_confidence_uses_four_discrete_levels():
    """01 号规格 §1：首版不输出概率数值，只给四档。"""
    log = EvidenceLog()
    append(log, [verdict(group="g1")])
    one_group = Projector().project(log, graph_version="g/v1")
    assert one_group.component("llm.tool_calling").confidence is Confidence.LOW

    append(log, [verdict(group="g2")])
    two_groups = Projector().project(log, graph_version="g/v1")
    assert two_groups.component("llm.tool_calling").confidence is Confidence.MEDIUM

    append(log, [verdict(group="g3")])
    three_groups = Projector().project(log, graph_version="g/v1")
    assert three_groups.component("llm.tool_calling").confidence is Confidence.HIGH


@pytest.mark.invariant
def test_projection_refuses_mixed_projects():
    """跨项目证据混入投影即拒绝，不做「各自投影」。"""
    from dataclasses import replace

    log = EvidenceLog()
    append(log, [verdict()])
    foreign = replace(append(EvidenceLog(), [verdict()]), project_id="proj_b")

    with pytest.raises(PlatformError) as exc:
        Projector().project_from(
            events=[*log.events(), foreign], corrections=[], graph_version="g/v1"
        )
    assert exc.value.code is ErrorCode.CROSS_PROJECT_DENIED


@pytest.mark.invariant
def test_same_input_produces_identical_projection():
    """相同输入集合必得相同投影（可复现）。"""
    log = EvidenceLog()
    append(log, [verdict(group="g1")])
    append(log, [verdict(group="g2")])
    projector = Projector()
    first = projector.project(log, graph_version="g/v1")
    second = projector.project(log, graph_version="g/v1")
    assert first.to_dict() == second.to_dict()


# ---------------------------------------------------------------- ChildRun


def _runtime() -> ChildRunRuntime:
    return ChildRunRuntime(
        tokens=TokenIssuer(secret="test-secret"),
        clock=FixedClock(NOW),
        registry=build_registry(),
    )


def _parent_token(issuer: TokenIssuer):
    return issuer.issue(
        tenant_id="tenant_a",
        project_id="proj_a",
        run_id="run_1",
        node_instance_id="n:1",
        audience="user_a",
        allowed_tools={"query_competency_graph", "retrieve_project_chunks", "run_in_sandbox"},
        policy_version="policy/v1",
        registry_version="sha256:r",
        revocation_epoch=0,
        budget_account_id="acct_1",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


@pytest.mark.invariant
def test_child_run_depth_is_capped_at_one():
    """不变量 #15：子 agent 不可递归 spawn。"""
    runtime = _runtime()
    parent = _parent_token(runtime._tokens)
    with pytest.raises(PlatformError) as exc:
        runtime.spawn(
            parent_token=parent,
            child_run_id="child_1",
            purpose=ChildPurpose.RESEARCH,
            allowed_tools={"retrieve_project_chunks"},
            expires_at=NOW + timedelta(minutes=30),
            parent_depth=1,
        )
    assert exc.value.code is ErrorCode.CHILD_RUN_DEPTH_EXCEEDED


@pytest.mark.invariant
def test_child_purpose_limits_authority():
    """子任务用途决定权限上限：drafting 连只读工具都不该用。"""
    runtime = _runtime()
    parent = _parent_token(runtime._tokens)
    with pytest.raises(PlatformError) as exc:
        runtime.spawn(
            parent_token=parent,
            child_run_id="child_1",
            purpose=ChildPurpose.DRAFTING,
            allowed_tools={"query_competency_graph"},
            expires_at=NOW + timedelta(minutes=30),
        )
    assert exc.value.code is ErrorCode.CHILD_RUN_AUTHORITY_ESCALATION


@pytest.mark.invariant
def test_child_token_can_never_exceed_parent():
    """子 token 从父派生，只减不增：父 token 未持有的工具派生不出来。

    这里刻意选 `read_source_span`：它在 RESEARCH 用途允许的权限范围内（A1a ≤ A1c），
    但不在父 token 的工具集合里 —— 因此触发的是 **token 派生拒绝**。
    两个拒绝点（用途权限 / token 派生）必须分得清，否则测试会掩盖真实缺陷。
    """
    runtime = _runtime()
    parent = _parent_token(runtime._tokens)
    assert "read_source_span" not in parent.allowed_tools
    with pytest.raises(PlatformError) as exc:
        runtime.spawn(
            parent_token=parent,
            child_run_id="child_1",
            purpose=ChildPurpose.RESEARCH,
            allowed_tools={"read_source_span"},
            expires_at=NOW + timedelta(minutes=30),
        )
    assert exc.value.code is ErrorCode.CAPABILITY_ESCALATION_DENIED


@pytest.mark.invariant
def test_child_token_is_narrower_than_parent():
    """正常派生得到的是更窄的 token。"""
    runtime = _runtime()
    parent = _parent_token(runtime._tokens)
    child = runtime.spawn(
        parent_token=parent,
        child_run_id="child_1",
        purpose=ChildPurpose.RESEARCH,
        allowed_tools={"retrieve_project_chunks"},
        expires_at=NOW + timedelta(hours=2),
    )
    assert child.token.allowed_tools == frozenset({"retrieve_project_chunks"})
    assert child.token.expires_at <= parent.expires_at
    assert child.depth == 1


@pytest.mark.invariant
def test_envelope_requires_evidence_for_sourced_claim():
    """不变量 #16：散文摘要必须降级为 inference，不能冒充有来源的结论。"""
    runtime = _runtime()
    with pytest.raises(PlatformError) as exc:
        runtime.validate_envelope(
            ChildEnvelope(
                child_run_id="child_1",
                status=EnvelopeStatus.OK,
                artifacts=(ArtifactRef("s1", (0, 10), "sha256:x", "p/v1", DisplayPolicy.FULL),),
                claims=(Claim(text="我读完了这些资料，结论是……", evidence_refs=(), kind=ClaimKind.SOURCED),),
            )
        )
    assert exc.value.code is ErrorCode.CHILD_RUN_ENVELOPE_INVALID


@pytest.mark.invariant
def test_envelope_requires_verifiable_content_hash():
    """谱系必须可校验：artifact 必须带 sha256 指纹。"""
    runtime = _runtime()
    with pytest.raises(PlatformError):
        runtime.validate_envelope(
            ChildEnvelope(
                child_run_id="child_1",
                status=EnvelopeStatus.OK,
                artifacts=(ArtifactRef("s1", (0, 10), "not-a-hash", "p/v1"),),
            )
        )


@pytest.mark.invariant
def test_unknown_status_envelope_is_rejected():
    """状态未知时不得回传结果，必须先对账。"""
    runtime = _runtime()
    with pytest.raises(PlatformError) as exc:
        runtime.validate_envelope(
            ChildEnvelope(child_run_id="child_1", status=EnvelopeStatus.UNKNOWN)
        )
    assert exc.value.code is ErrorCode.CHILD_RUN_ENVELOPE_INVALID


@pytest.mark.invariant
def test_inference_claims_are_not_learning_evidence():
    """不变量 #16：无来源的推断不得进入学习证据。"""
    runtime = _runtime()
    envelope = ChildEnvelope(
        child_run_id="child_1",
        status=EnvelopeStatus.OK,
        artifacts=(ArtifactRef("s1", (0, 10), "sha256:x", "p/v1"),),
        claims=(
            Claim(text="有来源的结论", evidence_refs=(0,), kind=ClaimKind.SOURCED),
            Claim(text="我的推测", evidence_refs=(), kind=ClaimKind.INFERENCE),
        ),
    )
    runtime.validate_envelope(envelope)
    eligible = runtime.evidence_eligible_claims(envelope)
    assert [c.text for c in eligible] == ["有来源的结论"]


@pytest.mark.invariant
def test_envelope_rejects_dangling_artifact_reference():
    """claim 不得引用不存在的 artifact 下标。"""
    runtime = _runtime()
    with pytest.raises(PlatformError):
        runtime.validate_envelope(
            ChildEnvelope(
                child_run_id="child_1",
                status=EnvelopeStatus.OK,
                artifacts=(),
                claims=(Claim(text="x", evidence_refs=(3,), kind=ClaimKind.SOURCED),),
            )
        )
