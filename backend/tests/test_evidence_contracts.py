"""契约统一后的回归测试：证据问题闭集、四级状态、taint 派生语义、ChildRun 信封。

这一组测试守的是**协议漂移**：
规格已经前进到结构化 `issues[] + EvidenceIssueCode`，运行时却还输出自然语言
`unresolved[]`；规格要求模型派生显式增加 `MODEL_OUTPUT`，代码却没有那个能力。
这类问题的共同点是「两边分别看都对，放在一起才矛盾」—— 只有机械比对能发现。
"""

from __future__ import annotations

import pytest
from app.core.errors import ErrorCode, PlatformError, deny
from app.core.evidence_issues import (
    EvidenceAssessment,
    EvidenceIssue,
    EvidenceIssueCode,
    EvidenceState,
)
from app.execution.child_run import ChildEnvelope, EnvelopeStatus
from app.knowledge.evidence_state import (
    FetchFailure,
    RetrievalHealth,
    RetrievalSignals,
    assess_retrieval,
    assess_retrieval_health,
)
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
def test_supported_requires_full_claim_coverage():
    """`supported` 需要**正向**的结论覆盖证明，不是"没发现问题"。

    规格的定义是「核心结论有充分且一致的证据」。曾经只要 issues 为空就返回它 ——
    那检查的其实是"检索过程没发现异常"，而过程无异常绝不等于结论有据。
    """
    covered = assess_retrieval(
        RetrievalSignals(
            candidate_count=3,
            top_score=2,
            required_claim_refs=("claim.a", "claim.b"),
            supported_claim_refs=("claim.a", "claim.b"),
        )
    )
    assert covered.state is EvidenceState.SUPPORTED
    assert covered.issues == ()
    assert covered.has_blocking_issue is False

    # 覆盖信息缺失（首版的常态）→ 不得 supported，且必须给出 MISSING_SUPPORT。
    unknown_coverage = assess_retrieval(RetrievalSignals(candidate_count=3, top_score=2))
    assert unknown_coverage.state is EvidenceState.INSUFFICIENT
    assert [
        i.code for i in unknown_coverage.issues
    ] == [EvidenceIssueCode.MISSING_SUPPORT]


@pytest.mark.invariant
def test_missing_claim_coverage_does_not_upgrade_by_having_candidates():
    """有候选、相关度也好，只要覆盖未知就必须 insufficient。

    这是审查点出的核心：`candidate_count > 0` 不能代替结论覆盖。
    """
    assessment = assess_retrieval(RetrievalSignals(candidate_count=9, top_score=99))
    assert assessment.state is EvidenceState.INSUFFICIENT
    assert assessment.supported_claim_refs == ()


@pytest.mark.invariant
def test_partial_claim_coverage_is_partially_supported():
    """能指名"哪几条站住了、哪几条没站住"时才是部分支持。"""
    assessment = assess_retrieval(
        RetrievalSignals(
            candidate_count=3,
            top_score=2,
            required_claim_refs=("claim.a", "claim.b"),
            supported_claim_refs=("claim.a",),
        )
    )
    assert assessment.state is EvidenceState.PARTIALLY_SUPPORTED
    assert assessment.supported_claim_refs == ("claim.a",)
    missing = [
        i for i in assessment.issues if i.code is EvidenceIssueCode.MISSING_SUPPORT
    ]
    assert missing, "缺少的结论必须以 MISSING_SUPPORT 列出"
    assert missing[0].claim_refs == ("claim.b",), "缺口要能指到具体是哪条结论"


@pytest.mark.invariant
def test_missing_support_retryable_matches_its_next_action():
    """`retryable` 必须与 `next_action` 自洽：**重试能修好的才标可重试**。

    「没给必需结论集合」重试一万次也一样 —— 它要的是补标注集，不是重试。
    标成可重试会让编排层白白重跑一遍检索，还把真实原因（缺输入）盖过去。
    一个字段说"重试"，另一个字段说"去补数据"，两者不可能都对。
    """
    unknown_coverage = assess_retrieval(RetrievalSignals(candidate_count=3, top_score=2))
    issue = unknown_coverage.issues[0]
    assert issue.next_action == "supply_required_claims"
    assert issue.retryable is False, "补标注集不是重试能解决的"

    expandable = assess_retrieval(
        RetrievalSignals(
            candidate_count=3,
            top_score=2,
            required_claim_refs=("claim.a", "claim.b"),
            supported_claim_refs=("claim.a",),
        )
    )
    partial = [
        i for i in expandable.issues if i.code is EvidenceIssueCode.MISSING_SUPPORT
    ][0]
    assert partial.next_action == "expand_query"
    assert partial.retryable is True, "换查询能补上缺口 —— 这时才该标可重试"


@pytest.mark.invariant
def test_retrieval_health_is_separate_from_evidence_state():
    """过程健康度独立于证据状态。

    两个问题必须分开回答：核心结论有没有证据（证据状态）／这次检索有没有按预期跑完
    （过程健康度）。曾经前者被后者顶替，结果是外部抓取失败时仍声称证据充分。
    """
    clean_but_unproven = RetrievalSignals(candidate_count=3, top_score=2)
    assert assess_retrieval_health(clean_but_unproven) is RetrievalHealth.CLEAN
    assert assess_retrieval(clean_but_unproven).state is EvidenceState.INSUFFICIENT

    degraded = RetrievalSignals(
        candidate_count=3,
        top_score=2,
        fetch_failures=(FetchFailure(source_ref="u", error_code="X", retryable=True),),
    )
    assert assess_retrieval_health(degraded) is RetrievalHealth.DEGRADED


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
def test_candidates_with_gap_is_not_partially_supported():
    """**只有候选片段、没有结论覆盖关系时，不得返回 `partially_supported`。**

    这是上一版的缺陷：`if signals.candidate_count > 0: return PARTIALLY_SUPPORTED`
    把「检索返回了点东西」当成「部分结论已站住」。02 号规格 §4 的前提是
    「可明确隔离已支持结论与缺口」——候选与结论之间还差一层覆盖关系。

    覆盖不了的时候正确状态是 `insufficient`：宁可说"证据不足"，
    也不要用"部分支持"把不确定性讲小。
    """
    assessment = assess_retrieval(
        RetrievalSignals(
            candidate_count=2,
            top_score=3,
            fetch_failures=(FetchFailure(source_ref="u", error_code="EGRESS", retryable=True),),
        )
    )
    assert assessment.state is EvidenceState.INSUFFICIENT
    assert assessment.issues[0].code is EvidenceIssueCode.SOURCE_FETCH_FAILED


@pytest.mark.invariant
def test_partially_supported_requires_explicit_supported_claims():
    """没有明确的已支持结论时不得 `partially_supported`，有则必须带进结果。

    两个方向都要测：**缺条件时降级**、**满足条件时升级**。
    只测其中一个方向会漏掉"条件其实没生效"这种实现错误。
    """
    gap = (FetchFailure(source_ref="u", error_code="EGRESS", retryable=True),)

    # 方向一：有缺口 + 给不出已支持结论 → 只能 insufficient。
    without_claims = assess_retrieval(
        RetrievalSignals(candidate_count=2, top_score=3, fetch_failures=gap)
    )
    assert without_claims.state is EvidenceState.INSUFFICIENT
    assert without_claims.supported_claim_refs == ()

    # 方向二：有缺口但必需结论全部有支撑 → partially_supported，且结论被带出来。
    with_claims = assess_retrieval(
        RetrievalSignals(
            candidate_count=2,
            top_score=3,
            fetch_failures=gap,
            required_claim_refs=("claim.tool_calling_loop",),
            supported_claim_refs=("claim.tool_calling_loop",),
        )
    )
    assert with_claims.state is EvidenceState.PARTIALLY_SUPPORTED
    assert with_claims.supported_claim_refs == ("claim.tool_calling_loop",)
    assert with_claims.issues, "部分支持必须同时列出缺口，否则等于只报喜"


@pytest.mark.invariant
def test_unknown_and_low_relevance_do_not_upgrade_state_by_having_candidates():
    """审查列出的两个具体场景：有候选也不得升级。

    - 工具结果未知，但缓存里碰巧有候选；
    - 候选相关度低于下限。
    """
    unknown_case = assess_retrieval(
        RetrievalSignals(candidate_count=3, top_score=5, tool_result_unknown=True)
    )
    low_case = assess_retrieval(
        RetrievalSignals(candidate_count=3, top_score=0, relevance_floor=2)
    )
    assert unknown_case.state is EvidenceState.INSUFFICIENT
    assert low_case.state is EvidenceState.INSUFFICIENT


# --------------------------------------------------------------- 状态判定


@pytest.mark.invariant
def test_incomplete_required_steps_blocks_supported():
    """必需步骤没完成 → 至少不能让状态是 supported。"""
    assessment = assess_retrieval(
        RetrievalSignals(candidate_count=5, top_score=9, required_steps_completed=False)
    )
    assert assessment.state is EvidenceState.INSUFFICIENT
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
def test_scope_blocked_rejects_custom_detail():
    """`SCOPE_BLOCKED` 不接受自定义 detail —— 文案由模型固定填入。

    上一版把这条写在注释里。只写注释等于没有约束：判定器不违规，
    不代表其他调用方和未来的反序列化路径不违规。
    """
    with pytest.raises(ValueError) as exc:
        EvidenceIssue(
            code=EvidenceIssueCode.SCOPE_BLOCKED,
            detail="资源 X 存在，但你无权访问",
        )
    assert "自定义 detail" in str(exc.value)


@pytest.mark.invariant
def test_scope_blocked_fixed_detail_is_applied_by_model():
    """留空时由模型填入固定安全文案，调用方不需要也不应该自己写。"""
    from app.core.evidence_issues import SCOPE_BLOCKED_SAFE_DETAIL

    issue = EvidenceIssue(code=EvidenceIssueCode.SCOPE_BLOCKED)
    assert issue.detail == SCOPE_BLOCKED_SAFE_DETAIL


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


# --------------------------------------------------------------- 约束由类型强制


@pytest.mark.invariant
def test_supported_state_cannot_carry_issues():
    """`state=supported` 与「有缺口」在结构上不能共存。

    「状态说 supported 但 issues 非空」是一句自相矛盾的话。
    上一版只在 docstring 里说"难以产生"，这里把它变成"构造即失败"。
    """
    issue = EvidenceIssue(code=EvidenceIssueCode.NO_CANDIDATES)
    with pytest.raises(ValueError) as exc:
        EvidenceAssessment(state=EvidenceState.SUPPORTED, issues=(issue,))
    assert "不得携带任何 issue" in str(exc.value)


@pytest.mark.invariant
def test_retrieval_health_clean_does_not_imply_supported_evidence():
    """`retrieval_health=CLEAN` 不得被读成"证据充分"。

    这条**曾经是一条绊线测试**：上一版把 `supported` 的弱语义（"未检测到缺口"）
    固定成断言，并在 docstring 里写明"等标注集就绪、加上正向覆盖校验后，
    这条测试会失败，那时应把它改成断言严格行为"。

    绊线按设计触发了——第六轮审查指出"不要继续弱化术语，而要加正向覆盖"。
    现在它守的是严格行为，并额外钉住那个最容易混淆的推论：
    **过程干净 ≠ 结论有据**（首版正常检索就是 CLEAN + insufficient）。
    """
    signals = RetrievalSignals(candidate_count=5, top_score=9)
    assert assess_retrieval_health(signals) is RetrievalHealth.CLEAN

    assessment = assess_retrieval(signals)
    assert assessment.state is EvidenceState.INSUFFICIENT, (
        "过程干净不代表证据充分；没有结论覆盖就不得 supported"
    )
    assert assessment.supported_claim_refs == ()


@pytest.mark.invariant
def test_unsupported_state_must_cite_at_least_one_issue():
    """非支持状态必须可解释：说不出缺口的"不足"是不可审计的。"""
    with pytest.raises(ValueError) as exc:
        EvidenceAssessment(state=EvidenceState.INSUFFICIENT)
    assert "至少携带一条 issue" in str(exc.value)


@pytest.mark.invariant
def test_partially_supported_requires_supported_claim_refs():
    """`partially_supported` 必须能指名已支持的结论。

    这条是本次审查的核心：`candidate_count > 0` 不能当作"部分结论已支持"。
    """
    issue = EvidenceIssue(code=EvidenceIssueCode.SOURCE_FETCH_FAILED)
    with pytest.raises(ValueError) as exc:
        EvidenceAssessment(state=EvidenceState.PARTIALLY_SUPPORTED, issues=(issue,))
    assert "supported_claim_refs" in str(exc.value)


@pytest.mark.invariant
def test_insufficient_cannot_carry_supported_claims():
    """`insufficient` 不得同时声称有结论站住 —— 那是"部分支持"。"""
    issue = EvidenceIssue(code=EvidenceIssueCode.NO_CANDIDATES)
    with pytest.raises(ValueError) as exc:
        EvidenceAssessment(
            state=EvidenceState.INSUFFICIENT,
            issues=(issue,),
            supported_claim_refs=("claim.x",),
        )
    assert "partially_supported" in str(exc.value)


@pytest.mark.invariant
def test_unknown_issue_cannot_be_marked_retryable():
    """`TOOL_RESULT_UNKNOWN` + `retryable=True` 必须被模型拒绝。

    上一版这条只写在注释里，任意调用方仍能构造出"未知但可重试"这条
    会引发第二次副作用的问题。
    """
    with pytest.raises(ValueError) as exc:
        EvidenceIssue(
            code=EvidenceIssueCode.TOOL_RESULT_UNKNOWN,
            retryable=True,
            next_action="reconcile",
        )
    assert "不得标记为可重试" in str(exc.value)


@pytest.mark.invariant
def test_unknown_issue_next_action_must_be_reconcile():
    with pytest.raises(ValueError) as exc:
        EvidenceIssue(
            code=EvidenceIssueCode.TOOL_RESULT_UNKNOWN,
            retryable=False,
            next_action="retry",
        )
    assert "next_action" in str(exc.value)


# --------------------------------------------------------------- 对账不可自动重试


@pytest.mark.invariant
def test_reconciliation_error_cannot_be_marked_retryable():
    """平台错误层同样不允许把「结果未知」标成可自动重试。

    证据层管住了 `TOOL_RESULT_UNKNOWN`，但同一件事在平台错误层还有第二个出口：
    `RECONCILIATION_REQUIRED`。上一版正是这里漏了 —— 证据层 retryable=False，
    平台层却给了 retryable=True，API 客户端据此会直接重放整个请求。
    """
    with pytest.raises(ValueError) as exc:
        PlatformError(
            code=ErrorCode.RECONCILIATION_REQUIRED,
            message="x",
            retryable=True,
        )
    assert "不得标记为可自动重试" in str(exc.value)


@pytest.mark.invariant
def test_reconciliation_error_exposes_reconcile_next_action():
    """`next_action` 由 code 推导，不靠 raise 点手工填写。"""
    error = deny(ErrorCode.RECONCILIATION_REQUIRED, "结果未知")
    assert error.retryable is False
    assert error.next_action == "reconcile"
    assert error.to_payload()["next_action"] == "reconcile"


@pytest.mark.invariant
def test_ordinary_error_has_no_next_action():
    """普通拒绝不编造 next_action —— 只登记真实会产生的取值。"""
    error = deny(ErrorCode.POLICY_DENIED, "策略拒绝")
    assert error.next_action == ""


@pytest.mark.invariant
def test_reconciliation_result_has_distinct_status(platform, tenant_ctx):
    """未知结果既不是 denied、也不是可重试的 failed，必须是独立状态。

    否则客户端只看到 `retryable=false`，会把未知当成终态直接放弃 ——
    永远不会去对账，而未知动作永远悬在那里。

    这里直接驱动失败映射（`_fail`），因为当前没有公共路径能产生
    "工具结果未知"（需要真实外部系统的超时），而这条映射本身必须被守住。
    """
    request = InteractionRequest(
        request_id="r_recon",
        tenant_id=tenant_ctx.tenant_id,
        principal_id=tenant_ctx.principal_id,
        learning_project_id=tenant_ctx.project_id,
        node_id="retrieve_material",
        user_input="x",
    )

    unknown = platform.runtime._fail(  # noqa: SLF001 — 见上方 docstring 说明
        request, deny(ErrorCode.RECONCILIATION_REQUIRED, "工具结果未知")
    )
    assert unknown.status == "reconciliation_required"
    assert unknown.error["retryable"] is False
    assert unknown.error["next_action"] == "reconcile"

    plain = platform.runtime._fail(  # noqa: SLF001
        request, deny(ErrorCode.POLICY_DENIED, "策略拒绝")
    )
    assert plain.status == "denied"
