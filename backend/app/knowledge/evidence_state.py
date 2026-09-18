"""检索证据状态的确定性判定。

设计依据：02 号规格 §4。

**这是对 `"supported" if hits else "insufficient"` 的替换。**

原实现把「有没有命中」当成「证据是否充分」。后果是：外部抓取失败、候选相关度
极低、必需检索步骤没跑完 —— 这些都被记录下来了，但都不影响状态判定，
系统照样声称 `supported`。

「关键证据失败，系统却说已支持」是最危险的一类错误：它让用户以为结论有据。
错误的 `insufficient` 会被用户追问后修正；错误的 `supported` 会被直接采信。

## 首版各码的启用情况（诚实标注）

| 码 | 首版是否产生 | 触发条件 / 缺什么 |
|---|---|---|
| `NO_CANDIDATES` | ✅ | 候选项数为 0 |
| `LOW_RELEVANCE` | ✅ | 最高候选低于相关度下限 |
| `SOURCE_FETCH_FAILED` | ✅ | 外部抓取失败（可重试性由调用方传入） |
| `SCOPE_BLOCKED` | ✅ | 检索范围被权限截断 |
| `TOOL_RESULT_UNKNOWN` | ✅ | 工具结果未知（强制不可重试，须先对账） |
| `MISSING_SUPPORT` | 🔶 部分 | 当前用「必需步骤未完成」触发；完整的**核心结论覆盖率**判定依赖标注集 |
| `SOURCE_CONFLICT` | ❌ 待接入 | 需要来源一致性与权威性比较 |
| `FRESHNESS_UNKNOWN` | ❌ 待接入 | 需要文档时间戳与新鲜度策略 |

后两个**不是"还没写"，是"输入还不存在"** —— 它们的判定需要冻结的核心结论
标注集。在此之前不产生它们，也不假装产生了：一个永不触发的码比缺失的码更糟，
因为它看起来已经实现。
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


def _derive_state(signals: RetrievalSignals, issues: list[EvidenceIssue]) -> EvidenceState:
    """状态是 issues 的函数，不是独立判断。"""
    if not issues:
        return EvidenceState.SUPPORTED
    if signals.candidate_count > 0:
        # 有候选、也有缺口 —— 能隔离「已支持结论」与「缺口」。
        return EvidenceState.PARTIALLY_SUPPORTED
    # 一条候选都没有。
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
                # ⚠️ 只说"范围受限"，绝不说"该资源存在但你无权访问"。
                # 后者会泄露未授权资源的存在性，把权限边界变成信息探针。
                detail="本次检索范围受权限限制，未能覆盖全部候选来源",
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

    return EvidenceAssessment(state=_derive_state(signals, issues), issues=tuple(issues))
