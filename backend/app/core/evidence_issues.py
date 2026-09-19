"""证据问题的结构化契约（knowledge 域闭集）。

设计依据：02 号规格 §4（四级证据状态与 `issues[]`）、03 号规格 §8、ADR-014、不变量 #16。

## 为什么这些类型住在 `core`

`EvidenceIssue` 同时被 knowledge（检索状态判定）与 execution（ChildRun 回传信封）使用。
按 06 号规格 §2.1，L4 模块之间**禁止横向依赖** —— 所以跨层共享的契约必须住在最底层的
`core`。这与 `ArtifactRef`（谱系引用）是同一个处理：
**共享契约下沉，而不是让某一层去 import 另一层。**

## `EvidenceIssueCode` 为什么不复用 `core.ErrorCode`

「没有候选」和「证据冲突」不是平台执行错误 —— 它们是**证据状态**。
混用会导致两件坏事：指标统计把"检索不到"算成"系统故障"；
以及 `SCOPE_BLOCKED` 这类状态被当成错误抛出后，错误消息里带着
"该资源存在但你无权访问"这种存在性泄露。

## 自然语言的位置

`EvidenceIssue.detail` 允许自然语言，但它**只服务于展示**。
稳定判断、指标与审计一律只读结构化字段（`code` / `claim_refs` / `source_refs` /
`retryable`）。理由很直接：自然语言无法机械比较，一旦拿它做判断，
「状态」就退化成模型措辞的产物。

## 约束落在类型上，不落在注释里

本模块的几条安全约束（unknown 不可重试、SCOPE_BLOCKED 文案固定、
supported 不得带缺口、partially_supported 必须有已支持结论）
全部由 `__post_init__` 强制，而不是写在 docstring 里。

这是一次审查的教训：判定器当时确实不产生非法组合，但**其他调用方和未来的
反序列化路径可以**。「约束写进模型」这句话如果只体现为注释，那就是没有写。
判断一条约束是否真的落地，标准只有一个 —— 构造一个违反它的对象，会不会失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EvidenceIssueCode(StrEnum):
    """knowledge 域闭集。首批八个码，来自 02 号规格 §4。

    新增码必须同时更新 02 号规格与评测集的判定规则 —— 这个枚举不是"随手加的标签"，
    每个码都对应一条确定的检索恢复规则。
    """

    # 检索侧
    NO_CANDIDATES = "NO_CANDIDATES"
    LOW_RELEVANCE = "LOW_RELEVANCE"
    MISSING_SUPPORT = "MISSING_SUPPORT"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    FRESHNESS_UNKNOWN = "FRESHNESS_UNKNOWN"
    # 执行侧
    SOURCE_FETCH_FAILED = "SOURCE_FETCH_FAILED"
    TOOL_RESULT_UNKNOWN = "TOOL_RESULT_UNKNOWN"
    # 权限侧
    SCOPE_BLOCKED = "SCOPE_BLOCKED"


class EvidenceState(StrEnum):
    """四级证据状态（02 号规格 §4）。

    **只要未完成步骤影响核心结论，就不能返回 `supported`。**
    能明确隔离「已支持结论」与「缺口」时才返回 `partially_supported`。
    """

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"


# `SCOPE_BLOCKED` 唯一允许的文案。固定，**不由调用方提供** ——
# 任何自定义文案都有泄露「未授权资源是否存在」的风险，而权限边界的价值
# 恰恰在于「不可探测」。把文案收进模型，就不存在"某处调用方写漏了".
SCOPE_BLOCKED_SAFE_DETAIL = "本次检索范围受权限限制，未能覆盖全部候选来源"

# 对账相关的固定取值。结果未知时**只有**对账一个正确动作，
# 所以它不做成可自由填写的字符串。
NEXT_ACTION_RECONCILE = "reconcile"


@dataclass(frozen=True)
class EvidenceIssue:
    """一条结构化的证据问题。

    `retryable` **必须由构造方显式给出**，不能统一从工具 outcome 推导
    （02 号规格 §4）：执行类问题按 `ToolOutcome.status` 与错误策略映射，
    检索类问题按各自的恢复规则判定。特别是 ——
    `TOOL_RESULT_UNKNOWN` 在对账完成前**必须**为 `retryable=False`：
    结果未知时重试会产生第二次副作用，那不是恢复，是放大。

    `detail` 只用于展示。⚠️ `SCOPE_BLOCKED` **不接受自定义 detail** ——
    任意文案都可能写成「该资源存在但你不能看」，那等于把权限系统变成存在性探针。
    该码的文案由模型固定填入 `SCOPE_BLOCKED_SAFE_DETAIL`。

    ⚠️ 上面这些约束不是注释，而是**由 `__post_init__` 强制**。
    只写在注释里的约束等于没有约束：判定器不会违规，不代表其他调用方和
    未来的反序列化路径不会。约束必须落在类型能拒绝的地方。
    """

    code: EvidenceIssueCode
    detail: str = ""
    claim_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    retryable: bool = False
    next_action: str = ""

    def __post_init__(self) -> None:
        if self.code is EvidenceIssueCode.TOOL_RESULT_UNKNOWN:
            if self.retryable:
                raise ValueError(
                    "TOOL_RESULT_UNKNOWN 不得标记为可重试："
                    "结果未知时重试会产生第二次副作用 —— 那不是恢复，是放大；"
                    "必须先完成对账"
                )
            if self.next_action != NEXT_ACTION_RECONCILE:
                raise ValueError(
                    f"TOOL_RESULT_UNKNOWN 的 next_action 只能是 "
                    f"{NEXT_ACTION_RECONCILE!r}，收到 {self.next_action!r}"
                )

        if self.code is EvidenceIssueCode.SCOPE_BLOCKED:
            if self.detail:
                raise ValueError(
                    "SCOPE_BLOCKED 不接受自定义 detail：任意文案都可能泄露"
                    "未授权资源是否存在；请留空，由模型填入固定安全文案"
                )
            # frozen dataclass 里填入派生值用 object.__setattr__。
            object.__setattr__(self, "detail", SCOPE_BLOCKED_SAFE_DETAIL)

    def to_dict(self) -> dict:
        return {
            "code": str(self.code),
            "detail": self.detail,
            "claim_refs": list(self.claim_refs),
            "source_refs": list(self.source_refs),
            "retryable": self.retryable,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class EvidenceAssessment:
    """状态 + 全部结构化问题 + **必需结论集合**与已覆盖集合。

    「状态」不是独立于 issues 的另一个判断 —— 它是这三者的函数。
    把三者放在同一个对象里，是为了让自相矛盾的组合在**构造时**就失败。

    ## 为什么 `required_claim_refs` 必须在这个类型上

    判定器（`assess_retrieval`）会要求正向覆盖，但这个类型**被多个模块共享**：
    ChildRun 回传、未来的反序列化路径、以及任何直接构造它的调用方。
    约束只存在于判定器里时，`EvidenceAssessment(state=SUPPORTED)` ——
    一个"声称证据充分、却拿不出任何覆盖证明"的对象 —— 仍然构造得出来。

    判断标准与 `EvidenceIssue` 一致：**构造一个违反约束的对象，会不会失败。**
    会失败才算落地；写在判定器里只算"常规路径上没问题"。

    ## 四级状态的不变量

    | state | 必须满足 |
    |---|---|
    | `supported` | 无 issue；必需结论集合非空且**全部**被覆盖 |
    | `partially_supported` | 有 issue；必需结论非空；已覆盖集合非空 |
    | `insufficient` | 有 issue；已覆盖集合为空（说不出哪条结论站得住） |
    | 必需集合未知 | 必须有 issue、必须带 `MISSING_SUPPORT`、不得声称任何结论已支持 |
    """

    state: EvidenceState
    issues: tuple[EvidenceIssue, ...] = field(default_factory=tuple)
    #: 本次检索**必须支撑**的核心结论。来自冻结的标注集；未知时为空元组。
    required_claim_refs: tuple[str, ...] = ()
    #: 已明确站住的核心结论。**不是候选片段** —— 候选与结论之间还差一层覆盖关系。
    supported_claim_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = set(self.required_claim_refs)
        covered = set(self.supported_claim_refs)

        if self.state is EvidenceState.SUPPORTED:
            if self.issues:
                raise ValueError(
                    "state=supported 不得携带任何 issue："
                    "存在影响核心结论的缺口就不能声称已支持"
                )
            if not required:
                raise ValueError(
                    "state=supported 必须给出必需结论集合：没有它就只能证明"
                    "「检索过程没报错」，而那是过程健康度（RetrievalHealth），"
                    "不是「核心结论有充分证据」"
                )
            missing = sorted(required - covered)
            if missing:
                raise ValueError(
                    f"state=supported 要求必需结论全部被覆盖，仍缺少 {missing}"
                )
            return

        # 以下都是非支持状态：必须可解释。
        if not self.issues:
            raise ValueError(
                f"state={self.state} 必须至少携带一条 issue；"
                "无法说明缺口的非支持状态是不可审计的"
            )

        if not required:
            # 覆盖信息不可用：只能如实标注，且不得声称任何结论已支持。
            if self.supported_claim_refs:
                raise ValueError(
                    "必需结论集合未知时不得声称已支持结论；"
                    "不知道「该要求哪些结论」就无法说「哪些结论站住了」"
                )
            if not any(i.code is EvidenceIssueCode.MISSING_SUPPORT for i in self.issues):
                raise ValueError(
                    "必需结论集合未知时必须携带 MISSING_SUPPORT："
                    "否则「覆盖未知」这件事在结果里完全看不出来"
                )
            return

        if self.state is EvidenceState.PARTIALLY_SUPPORTED and not self.supported_claim_refs:
            raise ValueError(
                "state=partially_supported 必须显式列出已支持的核心结论"
                "（supported_claim_refs）：只有候选片段、没有结论覆盖关系时，"
                "正确状态是 insufficient —— 部分支持的说法会夸大证据充分性"
            )

        if self.state is EvidenceState.INSUFFICIENT and self.supported_claim_refs:
            raise ValueError(
                "state=insufficient 不得携带已支持的核心结论；"
                "若确有结论站得住，状态应为 partially_supported"
            )

    @property
    def has_blocking_issue(self) -> bool:
        """是否存在影响核心结论的缺口（即状态不可能是 supported）。"""
        return bool(self.issues)

    def to_dict(self) -> dict:
        return {
            "state": str(self.state),
            "issues": [issue.to_dict() for issue in self.issues],
            "required_claim_refs": list(self.required_claim_refs),
            "supported_claim_refs": list(self.supported_claim_refs),
        }
