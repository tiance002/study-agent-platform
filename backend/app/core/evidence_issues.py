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


@dataclass(frozen=True)
class EvidenceIssue:
    """一条结构化的证据问题。

    `retryable` **必须由构造方显式给出**，不能统一从工具 outcome 推导
    （02 号规格 §4）：执行类问题按 `ToolOutcome.status` 与错误策略映射，
    检索类问题按各自的恢复规则判定。特别是 ——
    `TOOL_RESULT_UNKNOWN` 在对账完成前**必须**为 `retryable=False`：
    结果未知时重试会产生第二次副作用，那不是恢复，是放大。

    `detail` 只用于展示。⚠️ `SCOPE_BLOCKED` 的 detail **不得**透露
    未授权资源是否存在 —— 「你的权限不足」可以写，「该资源存在但你不能看」不行。
    """

    code: EvidenceIssueCode
    detail: str = ""
    claim_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    retryable: bool = False
    next_action: str = ""

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
    """状态 + 全部结构化问题。

    「状态」不是独立于 issues 的另一个判断 —— 它是 issues 的函数。
    把两者放在同一个对象里，是为了让「状态说 supported 但 issues 非空」这种
    自相矛盾在结构上就难以产生。
    """

    state: EvidenceState
    issues: tuple[EvidenceIssue, ...] = field(default_factory=tuple)

    @property
    def has_blocking_issue(self) -> bool:
        """是否存在影响核心结论的缺口（即状态不可能是 supported）。"""
        return bool(self.issues)

    def to_dict(self) -> dict:
        return {
            "state": str(self.state),
            "issues": [issue.to_dict() for issue in self.issues],
        }
