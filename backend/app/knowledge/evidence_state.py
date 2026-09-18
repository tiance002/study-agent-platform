"""检索证据状态的确定性判定。

设计依据：02 号规格 §4。

**这是对 `"supported" if hits else "insufficient"` 的替换。**

原实现把「有没有命中」当成「证据是否充分」。后果是：外部抓取失败、候选相关度
极低、必需检索步骤没跑完 —— 这些都被记录下来了，但都不影响状态判定，
系统照样声称 `supported`。

「关键证据失败，系统却说已支持」是最危险的一类错误：它让用户以为结论有据。
错误的 `insufficient` 会被用户追问后修正；错误的 `supported` 会被直接采信。

## 首版各码的真实落地程度（诚实标注，三级）

一个码"存在"可以有三种含义，把三者混为一谈会让项目状态失真。
实施计划里曾经笼统写作「已产生 6 个码」，那是过度乐观的表述。

| 码 | 判定器支持 | 生产运行时接线 | 首版触发条件 |
|---|---|---|---|
| `NO_CANDIDATES` | ✅ | ✅ 已接线 | 候选项数为 0 |
| `LOW_RELEVANCE` | ✅ | ✅ 已接线 | 最高候选低于相关度下限 |
| `SOURCE_FETCH_FAILED` | ✅ | ✅ 已接线 | 外部抓取失败（可重试性由调用方传入） |
| `TOOL_RESULT_UNKNOWN` | ✅ | ✅ 已接线 | 工具结果未知（强制不可重试，须先对账） |
| `MISSING_SUPPORT` | ✅ | ✅ 已接线 | 当前由「必需步骤未完成」触发；完整的**核心结论覆盖率**判定依赖标注集 |
| `SCOPE_BLOCKED` | ✅ | ❌ **未接线，仅测试可构造** | 检索路径当前没有按来源的作用域概念（`project_grants` 未被检索读取），项目内过滤只会静默隐藏越界候选 |
| `SOURCE_CONFLICT` | ❌ 待实现 | ❌ | 需要来源一致性与权威性比较 |
| `FRESHNESS_UNKNOWN` | ❌ 待实现 | ❌ | 需要文档时间戳与新鲜度策略 |

后两个**不是"还没写"，是"输入还不存在"**。`SCOPE_BLOCKED` 则是判定规则已就绪、
但生产路径还产生不了它 —— 三者的差别必须写清楚，否则「有判定能力」会被读成
「已经在工作」。一个永不触发的码比缺失的码更糟，因为它看起来已经实现。

## `partially_supported` 为什么默认不出现

02 号规格 §4 的措辞是「**可明确隔离已支持结论与缺口时**返回 `partially_supported`」。
「有候选 + 有问题」并不满足这个条件：候选片段与核心结论之间还差一层覆盖关系，
而覆盖关系来自冻结的核心结论标注集（计划第 9 项），首版并不存在。

因此本判定器要求调用方显式给出 `supported_claim_refs`；给不出时一律
`insufficient`。**宁可保守说"证据不足"，也不要用"部分支持"把不确定性讲小。**
错误的 `insufficient` 会被追问后修正，错误的 `partially_supported` 会被直接采信。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.evidence_issues import (
    EvidenceAssessment,
    EvidenceIssue,
    EvidenceIssueCode,
    EvidenceState,
)


@dataclass(frozen=True)
class FetchFailure:
    """一次外部抓取失败。

    `retryable` 由调用方按 `ToolOutcome.status` 与错误策略判定后传入 ——
    02 号规格 §4 明确要求不能在这里统一推导。
    """

    source_ref: str
    error_code: str
    retryable: bool


@dataclass(frozen=True)
class RetrievalSignals:
    """判定所需的输入。

    **每一项都必须是可机械获得的** —— 这里不能出现"模型认为相关"这类判断，
    否则「状态」就重新变成模型措辞的产物，与这次改造的目的正好相反。
    """

    candidate_count: int
    top_score: float = 0.0
    relevance_floor: float = 0.0
    required_steps_completed: bool = True
    fetch_failures: tuple[FetchFailure, ...] = field(default_factory=tuple)
    scope_blocked: bool = False
    tool_result_unknown: bool = False
    # 已明确站住的核心结论。**不是候选片段** —— 是"哪些结论已有充分支撑"。
    # 首版恒为空（没有冻结的标注集可用），因此有缺口时状态一律 insufficient。
    supported_claim_refs: tuple[str, ...] = ()


def _derive_state(signals: RetrievalSignals, issues: list[EvidenceIssue]) -> EvidenceState:
    """状态是 issues 与已支持结论的函数，不是独立判断。"""
    if not issues:
        return EvidenceState.SUPPORTED
    if signals.supported_claim_refs:
        # 能明确指出"哪些结论站住了、哪些没站住" —— 这才是部分支持。
        return EvidenceState.PARTIALLY_SUPPORTED
    # 有缺口，又说不出哪些核心结论已站住。
    # ⚠️ 这里**不能**因为 candidate_count > 0 就升级成 partially_supported：
    # 有候选片段不代表任何核心结论得到支持，那只是"检索返回了点东西"。
    return EvidenceState.INSUFFICIENT


def assess_retrieval(signals: RetrievalSignals) -> EvidenceAssessment:
    """把检索信号判成四级状态 + 结构化 issues。

    硬规则（02 号规格 §4）：**只要存在影响核心结论的缺口，就不能是 `supported`。**
    这也是本函数唯一不允许"宽松处理"的地方。
    """
    issues: list[EvidenceIssue] = []

    if signals.scope_blocked:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.SCOPE_BLOCKED,
                # detail 刻意留空：文案由 EvidenceIssue 固定填入 SCOPE_BLOCKED_SAFE_DETAIL。
                # 这里若能自由写文案，就会有人写出「该资源存在但你无权访问」——
                # 那等于把权限边界变成存在性探针。
                retryable=False,
                next_action="request_scope",
            )
        )

    if signals.tool_result_unknown:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.TOOL_RESULT_UNKNOWN,
                detail="检索工具结果未知，需先完成对账再进行任何重试",
                # 对账完成前必须不可自动重试：结果未知时重试会产生第二次副作用，
                # 那不是恢复动作，是放大。
                retryable=False,
                next_action="reconcile",
            )
        )

    for failure in signals.fetch_failures:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.SOURCE_FETCH_FAILED,
                detail=f"外部来源未能取回（错误码 {failure.error_code}）",
                source_refs=(failure.source_ref,),
                retryable=failure.retryable,
                next_action="retry_fetch" if failure.retryable else "report_unavailable",
            )
        )

    if signals.candidate_count == 0:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.NO_CANDIDATES,
                detail="本次检索没有返回任何候选片段",
                retryable=True,
                next_action="expand_query",
            )
        )
    elif signals.top_score < signals.relevance_floor:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.LOW_RELEVANCE,
                detail="最高候选的相关度低于可用下限",
                retryable=True,
                next_action="rewrite_query",
            )
        )

    if not signals.required_steps_completed and not issues:
        # 步骤没跑完却没有具体码：仍必须拒绝 supported。
        # 「未完成步骤影响核心结论 → 不得 supported」是规格的硬要求，
        # 不能因为"没有更精确的码"就放过。
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.MISSING_SUPPORT,
                detail="必需检索步骤未完成，核心结论缺少支撑",
                retryable=True,
                next_action="run_required_steps",
            )
        )

    return EvidenceAssessment(
        state=_derive_state(signals, issues),
        issues=tuple(issues),
        supported_claim_refs=signals.supported_claim_refs,
    )
