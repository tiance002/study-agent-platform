"""契约统一后的回归测试：证据问题闭集、四级状态、taint 派生语义、ChildRun 信封。

这一组测试守的是**协议漂移**：
规格已经前进到结构化 `issues[] + EvidenceIssueCode`，运行时却还输出自然语言
`unresolved[]`；规格要求模型派生显式增加 `MODEL_OUTPUT`，代码却没有那个能力。
这类问题的共同点是「两边分别看都对，放在一起才矛盾」—— 只有机械比对能发现。
"""

from __future__ import annotations

import pytest
from app.core.evidence_issues import (
    EvidenceIssue,
    EvidenceIssueCode,
    EvidenceState,
)
from app.execution.child_run import ChildEnvelope, EnvelopeStatus
from app.knowledge.evidence_state import FetchFailure, RetrievalSignals, assess_retrieval
from app.policy.taint import TaintSource, derive, derive_model_output, mark_tainted
from app.workflow.runtime import InteractionRequest

# 02 号规格 §4 列出的首批闭集，逐字抄自规格。
SPEC_ISSUE_CODES = {
    "NO_CANDIDATES",
    "LOW_RELEVANCE",
    "MISSING_SUPPORT",
    "SOURCE_CONFLICT",
    "FRESHNESS_UNKNOWN",
    "SOURCE_FETCH_FAILED",
    "TOOL_RESULT_UNKNOWN",
    "SCOPE_BLOCKED",
}


# --------------------------------------------------------------- 闭集与规格一致性


@pytest.mark.invariant
def test_issue_code_closed_set_matches_spec():
    """枚举必须与规格逐字一致。

    这类「规格与代码必须一致」的检查应该机械化：不一致时两边都可能"看起来对"，
    只有比对才能发现协议漂移。规格新增码而代码没跟上（或反之）都会在此失败。
    """
    assert {str(code) for code in EvidenceIssueCode} == SPEC_ISSUE_CODES


@pytest.mark.invariant
def test_issue_code_does_not_reuse_platform_error_codes():
    """`EvidenceIssueCode` 不得复用 `core.ErrorCode`。

    「没有候选」和「证据冲突」不是平台执行错误。混用会让指标把"检索不到"
    统计成"系统故障"，也会让 SCOPE_BLOCKED 被当作错误抛出后泄露存在性。
    """
    from app.core.errors import ErrorCode

    platform_codes = {str(code) for code in ErrorCode}
    assert not (SPEC_ISSUE_CODES & platform_codes), "证据问题码与平台错误码发生了重叠"


@pytest.mark.invariant
def test_issue_serialization_field_names_match_spec():
    """`issues[]` 的字段名必须与 02 号规格 §4 一致。"""
    issue = EvidenceIssue(code=EvidenceIssueCode.NO_CANDIDATES, detail="x")
    assert set(issue.to_dict()) == {
        "code",
        "claim_refs",
        "source_refs",
        "detail",
        "retryable",
        "next_action",
    }


# --------------------------------------------------------------- 状态判定


@pytest.mark.invariant
def test_no_issues_means_supported():
    assessment = assess_retrieval(RetrievalSignals(candidate_count=3, top_score=2))
    assert assessment.state is EvidenceState.SUPPORTED
    assert assessment.issues == ()
    assert assessment.has_blocking_issue is False


@pytest.mark.invariant
def test_any_issue_prevents_supported():
    """硬规则：只要存在影响核心结论的缺口，就不能是 `supported`。

    这是本次改造的核心 —— 原实现 `"supported" if hits else "insufficient"`
    让抓取失败、低相关、步骤未完成全都不影响状态。
    """
    scenarios = [
        RetrievalSignals(candidate_count=3, top_score=2, fetch_failures=(
            FetchFailure(source_ref="u", error_code="X", retryable=True),
        )),
        RetrievalSignals(candidate_count=3, top_score=2, required_steps_completed=False),
        RetrievalSignals(candidate_count=3, top_score=2, scope_blocked=True),
        RetrievalSignals(candidate_count=3, top_score=2, tool_result_unknown=True),
        RetrievalSignals(candidate_count=0),
    ]
    for signals in scenarios:
        assessment = assess_retrieval(signals)
        assert assessment.state is not EvidenceState.SUPPORTED, signals
        assert assessment.issues, signals


@pytest.mark.invariant
def test_no_candidates_is_insufficient():
    assessment = assess_retrieval(RetrievalSignals(candidate_count=0))
    assert assessment.state is EvidenceState.INSUFFICIENT
    assert assessment.issues[0].code is EvidenceIssueCode.NO_CANDIDATES
    assert assessment.issues[0].retryable is True


@pytest.mark.invariant
def test_low_relevance_is_reported():
    assessment = assess_retrieval(
        RetrievalSignals(candidate_count=2, top_score=0, relevance_floor=1)
    )
    assert assessment.issues[0].code is EvidenceIssueCode.LOW_RELEVANCE


@pytest.mark.invariant
def test_candidates_with_gap_is_partially_supported():
    """有候选、也有缺口 → partially_supported（能隔离已支持结论与缺口）。"""
    assessment = assess_retrieval(
        RetrievalSignals(
            candidate_count=2,
            top_score=3,
            fetch_failures=(FetchFailure(source_ref="u", error_code="EGRESS", retryable=True),),
        )
    )
    assert assessment.state is EvidenceState.PARTIALLY_SUPPORTED
    assert assessment.issues[0].code is EvidenceIssueCode.SOURCE_FETCH_FAILED


@pytest.mark.invariant
def test_incomplete_required_steps_blocks_supported():
    """必需步骤没完成 → 至少不能让状态是 supported。"""
    assessment = assess_retrieval(
        RetrievalSignals(candidate_count=5, top_score=9, required_steps_completed=False)
    )
    assert assessment.state is EvidenceState.PARTIALLY_SUPPORTED
    assert assessment.issues[0].code is EvidenceIssueCode.MISSING_SUPPORT


# --------------------------------------------------------------- 两个安全细节


@pytest.mark.invariant
def test_scope_blocked_does_not_leak_existence():
    """`SCOPE_BLOCKED` 不得泄露未授权资源是否存在。

    这是权限边界与信息边界的交界处：说"该资源存在但你无权访问"
    等于把权限系统变成存在性探针。
    """
    assessment = assess_retrieval(
        RetrievalSignals(candidate_count=0, scope_blocked=True)
    )
    blocked = [i for i in assessment.issues if i.code is EvidenceIssueCode.SCOPE_BLOCKED]
    assert blocked, "SCOPE_BLOCKED 必须被报出"

    detail = blocked[0].detail
    for leak in ("存在", "该资源", "exists", "但不", "无权"):
        assert leak not in detail, f"detail 泄露了存在性信息：{leak!r}"


@pytest.mark.invariant
def test_unknown_result_is_not_auto_retryable():
    """`TOOL_RESULT_UNKNOWN` 在对账完成前必须不可自动重试。

    结果未知时重试会产生第二次副作用 —— 那不是恢复动作，是放大。
    """
    assessment = assess_retrieval(
        RetrievalSignals(candidate_count=1, top_score=1, tool_result_unknown=True)
    )
    unknown = [i for i in assessment.issues if i.code is EvidenceIssueCode.TOOL_RESULT_UNKNOWN]
    assert unknown
    assert unknown[0].retryable is False
    assert unknown[0].next_action == "reconcile"


@pytest.mark.invariant
def test_fetch_failure_retryability_comes_from_caller():
    """抓取失败的可重试性由调用方传入，不在这里统一推导。"""
    retryable_case = assess_retrieval(
        RetrievalSignals(
            candidate_count=1,
            top_score=1,
            fetch_failures=(FetchFailure(source_ref="u", error_code="TIMEOUT", retryable=True),),
        )
    )
    final_case = assess_retrieval(
        RetrievalSignals(
            candidate_count=1,
            top_score=1,
            fetch_failures=(FetchFailure(source_ref="u", error_code="BLOCKED", retryable=False),),
        )
    )
    assert retryable_case.issues[0].retryable is True
    assert final_case.issues[0].retryable is False


# --------------------------------------------------------------- taint 派生语义


@pytest.mark.invariant
def test_deterministic_derive_does_not_add_sources():
    """确定性派生只继承父来源，**不新增** —— 默认安全。"""
    parent = mark_tainted("v1", {"a": 1}, TaintSource.UPLOADED_SOURCE)
    child = derive([parent], "v2", {"a": 2})

    assert child.sources == (TaintSource.UPLOADED_SOURCE,)
    assert TaintSource.MODEL_OUTPUT not in child.sources


@pytest.mark.invariant
def test_model_derive_adds_model_output_and_keeps_parents():
    """模型边界产生新内容：必须显式增加 `MODEL_OUTPUT` 并继承全部父来源。

    这是先前的真实缺口：`derive()` 只有"合并父来源"一种行为，于是模型生成的
    内容永远拿不到 `MODEL_OUTPUT` 标记，会被当成普通派生值混进证据链。
    """
    parent = mark_tainted("v1", {"a": 1}, TaintSource.UPLOADED_SOURCE)
    child = derive_model_output([parent], "v2", {"summary": "模型写的摘要"})

    assert TaintSource.MODEL_OUTPUT in child.sources
    assert TaintSource.UPLOADED_SOURCE in child.sources, "父来源必须全部继承"
    assert child.derived_from == ("v1",)


@pytest.mark.invariant
def test_explicit_new_sources_is_honored():
    parent = mark_tainted("v1", {"a": 1}, TaintSource.WEB)
    child = derive([parent], "v2", {}, new_sources=(TaintSource.MCP,))

    assert set(child.sources) == {TaintSource.WEB, TaintSource.MCP}


@pytest.mark.invariant
def test_derive_multiple_parents_merges_sources_without_duplicates():
    a = mark_tainted("a", {"x": 1}, TaintSource.WEB)
    b = mark_tainted("b", {"x": 2}, TaintSource.WEB)
    c = mark_tainted("c", {"x": 3}, TaintSource.REPO)

    child = derive([a, b, c], "d", {"x": 4})

    assert set(child.sources) == {TaintSource.WEB, TaintSource.REPO}
    assert child.derived_from == ("a", "b", "c")


# --------------------------------------------------------------- ChildRun 信封


@pytest.mark.invariant
def test_child_envelope_uses_issues_not_unresolved():
    """信封不得再出现旧的 `unresolved[]` —— 那是被规格替换掉的契约。"""
    envelope = ChildEnvelope(
        child_run_id="child_1",
        status=EnvelopeStatus.PARTIAL,
        issues=(
            EvidenceIssue(
                code=EvidenceIssueCode.NO_CANDIDATES,
                detail="未找到候选",
                retryable=True,
                next_action="expand_query",
            ),
        ),
    )
    payload = envelope.to_dict()

    assert "unresolved" not in payload, "旧的 unresolved 字段必须消失"
    assert payload["issues"][0]["code"] == "NO_CANDIDATES"
    assert payload["issues"][0]["next_action"] == "expand_query"


@pytest.mark.invariant
def test_child_envelope_issues_default_to_empty():
    envelope = ChildEnvelope(child_run_id="child_2", status=EnvelopeStatus.OK)
    assert envelope.issues == ()
    assert envelope.to_dict()["issues"] == []


# --------------------------------------------------------------- 端到端集成


@pytest.mark.invariant
def test_retrieval_with_failed_fetch_is_not_supported(platform, tenant_ctx):
    """有命中但外部抓取失败 → 状态不能是 supported。

    这是原实现的具体缺陷场景：`bool(hits)` 为真，于是抓取失败完全不影响判定，
    系统会声称"已支持"。
    """
    from app.knowledge.retrieval import Chunk
    from app.policy.taint import TaintSource as TS
    from app.tenancy.context import tenant_scope

    with tenant_scope(tenant_ctx):
        platform.chunk_index.add(
            Chunk(
                chunk_id="c1",
                tenant_id=tenant_ctx.tenant_id,
                learning_project_id=tenant_ctx.project_id,
                source_id="s1",
                span=(0, 20),
                text="Agent harness 负责编排工具调用",
                origin=TS.UPLOADED_SOURCE,
            )
        )

    result = platform.runtime.run(
        InteractionRequest(
            request_id="r_ev",
            tenant_id=tenant_ctx.tenant_id,
            principal_id=tenant_ctx.principal_id,
            learning_project_id=tenant_ctx.project_id,
            node_id="retrieve_material",
            user_input="harness",
            # 外部抓取必然失败（域名不在白名单）——正是要验证的场景。
            params={"also_fetch_external": "https://evil.example.com/x"},
        )
    )

    assert result.status == "ok", result.error
    output = result.output
    assert output["hits"], "本地检索应当有命中"
    assert output["evidence_state"] != "supported", (
        "有命中不等于已支持：外部抓取失败必须压低证据状态"
    )
    codes = {issue["code"] for issue in output["issues"]}
    assert "SOURCE_FETCH_FAILED" in codes
    assert "unresolved" not in output, "输出不得再带自然语言 unresolved 字段"
