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

## ⚠️ `supported` 在首版的含义比它的名字弱（自查发现的不对称）

规格对 `supported` 的定义是「核心结论有充分且一致的证据」。但本判定器只在
**没有任何 issue** 时返回它 —— 我们检查的是"检索过程没发现异常"（有候选、
相关度达标、必需步骤完成、没有抓取失败），**并不是**"核心结论已被验证"。
后者同样需要那套冻结的核心结论标注集。

这跟 `partially_supported` 是同一类问题，只是方向不同：那里是**把不确定性讲小**
（所以收紧成默认不产生），这里是**把能力的边界讲大**（所以只能诚实标注）。

没有一并收紧的原因很实际：若要求正向的结论覆盖才返回 `supported`，
## `supported` 需要**正向**的结论覆盖证明

规格对 `supported` 的定义是「核心结论有充分且一致的证据」。曾经本判定器在没有
issue 时就返回它 —— 检查的其实是"检索过程没发现异常"（有候选、相关度达标、
必需步骤完成、没有抓取失败），**不是**"核心结论已被验证"。

上一轮我把这一点当作"已知局限"标注了，理由是：要求正向覆盖会让首版每次都判成
`insufficient`，状态退化成常量。**这个理由站不住。** 状态退化成常量不是"问题被掩盖"，
而是"如实反映我们确实还不知道" —— 真正的错误是用一个更强的词去描述一个更弱的判断。

正确做法是二选一，而不是削弱术语：

1. **`supported` 必须有正向覆盖**：给出必需结论集合（`required_claim_refs`）
   与已覆盖集合（`supported_claim_refs`），只有必需结论全部被覆盖才返回 `supported`；
   拿不到覆盖信息时产生 `MISSING_SUPPORT` 并返回 `insufficient`。
2. **"检索过程无异常"另立名目**：它是**过程健康度**，与证据是否充分正交，
   所以放在独立的 `RetrievalHealth` 里，**不占用证据状态**。

于是首版的实际表现是：普通检索会返回 `insufficient` + `MISSING_SUPPORT`
（因为标注集还不存在，无法给出必需结论集合），同时 `retrieval_health`
如实报告过程是否干净。这是正确的：**我们现在确实无法证明核心结论有充分证据。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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
    # **本次检索必须支撑的核心结论**。来自冻结的核心结论标注集。
    # 空表示"我们还不知道该要求哪些结论" —— 那就无法证明覆盖，
    # 必须判 `insufficient`，而不是默认放过。
    required_claim_refs: tuple[str, ...] = ()
    # 已明确站住的核心结论。**不是候选片段** —— 是"哪些结论已有充分支撑"。
    supported_claim_refs: tuple[str, ...] = ()


def _derive_state(
    issues: list[EvidenceIssue], effective_claims: tuple[str, ...]
) -> EvidenceState:
    """状态是 issues 与**可信**已支持结论的函数，不是独立判断。

    `effective_claims` 是调用方已经确认可用的覆盖集合（见 `assess_retrieval`
    的归一化）：覆盖信息拿不到时它为空，状态因此落到 `insufficient`。
    """
    if not issues:
        # 注意：issues 为空**蕴含**覆盖已确认 —— 拿不到必需结论集合时
        # `assess_retrieval` 一定会塞一条 MISSING_SUPPORT 进来。
        # 所以这里的 SUPPORTED 是"必需结论全覆盖且无其他缺口"，不是"没发现异常"。
        return EvidenceState.SUPPORTED
    if effective_claims:
        # 能指名"哪些结论站住了、哪些没站住" —— 这才是部分支持。
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

    # 覆盖判定：这是 `supported` 需要的**正向**证明。
    required = tuple(signals.required_claim_refs)
    covered = set(signals.supported_claim_refs)
    missing: tuple[str, ...] = ()
    support_gaps: list[str] = []
    if not required:
        support_gaps.append("未提供必需结论集合，无法证明核心结论已被覆盖")
    else:
        missing = tuple(sorted(set(required) - covered))
        if missing:
            support_gaps.append(f"{len(missing)} 条必需结论缺少支撑")
    if not signals.required_steps_completed:
        support_gaps.append("必需检索步骤未完成")
    if support_gaps:
        # 「必需结论覆盖未知」与「步骤没跑完」共用 MISSING_SUPPORT：
        # 它们都是"核心结论缺少支撑"，闭集里没有更细的码。
        # 不能因为"没有更精确的码"就放过 —— 那会让 supported 失去正向证明。
        #
        # `retryable` 必须与 `next_action` 自洽：**重试能修好的才标可重试**。
        # 「没给必需结论集合」重试一万次也一样 —— 它要的是补标注集，不是重试。
        # 标成可重试会让编排层白白重跑一遍检索，还把真实原因（缺输入）盖过去。
        retryable = bool(required) and (
            bool(missing) or not signals.required_steps_completed
        )
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.MISSING_SUPPORT,
                detail="；".join(support_gaps),
                claim_refs=missing,
                retryable=retryable,
                next_action=(
                    "run_required_steps"
                    if not signals.required_steps_completed
                    else ("expand_query" if required else "supply_required_claims")
                ),
            )
        )

    # 拿不到必需结论集合时，**不声称任何结论已支持**。
    # 否则会与 `insufficient` 的模型约束（不得携带已支持结论）冲突，
    # 也会把"不知道哪些结论算数"讲成"部分结论已知"。
    effective_claims = tuple(signals.supported_claim_refs) if required else ()

    return EvidenceAssessment(
        state=_derive_state(issues, effective_claims),
        issues=tuple(issues),
        # 必需集合**如实传递**（未知就是空元组）：类型层要据此拒绝
        # "声称 supported 却拿不出必需结论集合"这类构造。
        required_claim_refs=required,
        supported_claim_refs=effective_claims,
    )


class RetrievalHealth(StrEnum):
    """检索**过程**的健康度。

    它与证据状态正交，所以**必须独立命名、独立字段**：

    - 证据状态回答「核心结论有没有足够证据」—— 需要覆盖信息才敢说 `supported`；
    - 过程健康度回答「这次检索有没有按预期跑完」—— 抓取失败、结果未知、范围被截断。

    两者不能互相顶替。曾用证据状态顺带表达"过程无异常"，结果是在外部抓取失败时
    仍然返回 `supported`：一句"检索顺利"被当成了"结论有据"。
    """

    CLEAN = "clean"
    DEGRADED = "degraded"


def assess_retrieval_health(signals: RetrievalSignals) -> RetrievalHealth:
    """过程健康度。

    ⚠️ `CLEAN` **不代表证据充分** —— 那由 `assess_retrieval` 回答。
    首版因为拿不到必需结论集合，正常检索的证据状态就是 `insufficient`，
    而过程健康度很可能是 `CLEAN`。这不是矛盾，是两个问题各归各位。
    """
    if signals.tool_result_unknown or signals.fetch_failures or signals.scope_blocked:
        return RetrievalHealth.DEGRADED
    return RetrievalHealth.CLEAN
