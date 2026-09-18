"""稳定错误码与平台错误基类。

设计依据：
- 总设计 §8 —— 所有错误使用稳定错误码、`request_id`、可重试标记和用户可理解的状态。
- 03 号规格 §1 —— 权限轴 A0-A3 与失败方向。

约束：
- 错误码一经发布不得改变语义，只能追加。
- 调用方必须比较 `ErrorCode` 枚举，不得用字符串比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ErrorCode(StrEnum):
    """稳定错误码。新增只能追加，不得改写既有语义。"""

    # ---- 鉴权与隔离 ----
    AUTH_REQUIRED = "AUTH_REQUIRED"
    TENANT_CONTEXT_MISSING = "TENANT_CONTEXT_MISSING"
    CROSS_TENANT_DENIED = "CROSS_TENANT_DENIED"
    CROSS_PROJECT_DENIED = "CROSS_PROJECT_DENIED"

    # ---- 策略（L2 边界层）----
    POLICY_DENIED = "POLICY_DENIED"
    OBLIGATION_UNSUPPORTED = "OBLIGATION_UNSUPPORTED"
    POLICY_GATEWAY_UNAVAILABLE = "POLICY_GATEWAY_UNAVAILABLE"
    CAPABILITY_NOT_HELD = "CAPABILITY_NOT_HELD"
    CAPABILITY_ESCALATION_DENIED = "CAPABILITY_ESCALATION_DENIED"

    # ---- node / tool 注册表 ----
    TOOL_NOT_REGISTERED = "TOOL_NOT_REGISTERED"
    NODE_NOT_REGISTERED = "NODE_NOT_REGISTERED"
    TOOL_NOT_ALLOWED_FOR_NODE = "TOOL_NOT_ALLOWED_FOR_NODE"
    TOOL_DECLARATION_INVALID = "TOOL_DECLARATION_INVALID"
    TOOL_INTENT_CONFLICT = "TOOL_INTENT_CONFLICT"

    # ---- 预算 ----
    BUDGET_RESERVATION_FAILED = "BUDGET_RESERVATION_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    BUDGET_TREE_INVALID = "BUDGET_TREE_INVALID"

    # ---- 执行 ----
    PARAMS_INVALID = "PARAMS_INVALID"
    IDEMPOTENCY_VIOLATION = "IDEMPOTENCY_VIOLATION"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    # ---- 审计 ----
    AUDIT_SINK_UNAVAILABLE = "AUDIT_SINK_UNAVAILABLE"

    # ---- taint 与 endorsement ----
    TAINT_REQUIRES_ENDORSEMENT = "TAINT_REQUIRES_ENDORSEMENT"
    ENDORSEMENT_EXPIRED = "ENDORSEMENT_EXPIRED"
    ENDORSEMENT_SINK_MISMATCH = "ENDORSEMENT_SINK_MISMATCH"
    ENDORSEMENT_REPLAY_DENIED = "ENDORSEMENT_REPLAY_DENIED"

    # ---- 子任务（ChildRun）----
    CHILD_RUN_DEPTH_EXCEEDED = "CHILD_RUN_DEPTH_EXCEEDED"
    CHILD_RUN_AUTHORITY_ESCALATION = "CHILD_RUN_AUTHORITY_ESCALATION"
    CHILD_RUN_ENVELOPE_INVALID = "CHILD_RUN_ENVELOPE_INVALID"

    # ---- 学习证据 ----
    EVIDENCE_IMMUTABLE = "EVIDENCE_IMMUTABLE"
    PROJECTION_WRITE_DENIED = "PROJECTION_WRITE_DENIED"
    EVIDENCE_UNMAPPED = "EVIDENCE_UNMAPPED"


# 平台层 next_action 词汇表。
# 目前只登记真实会被产生的取值 —— 永不触发的取值比缺失的取值更糟，
# 因为它看起来已经实现（这条教训来自 `EvidenceIssueCode` 的诚实标注）。
NEXT_ACTION_RECONCILE = "reconcile"


@dataclass
class PlatformError(Exception):
    """平台错误基类。必须携带稳定错误码与可重试标记。

    `retryable` 只对明确的瞬时错误为真；业务拒绝一律为假，
    否则调用方会重试一个永远不会成功的请求。

    ⚠️ 「结果未知」（`RECONCILIATION_REQUIRED`）**不是**可自动重试的瞬时错误。
    把它标成 `retryable=True` 会让客户端直接重放整个请求，在没有对账的情况下
    产生第二次副作用 —— 那不是恢复，是放大。这条由 `__post_init__` 强制，
    并要求调用方改走 `next_action`。
    """

    code: ErrorCode
    message: str
    retryable: bool = False
    request_id: str | None = None
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code is ErrorCode.RECONCILIATION_REQUIRED and self.retryable:
            raise ValueError(
                "RECONCILIATION_REQUIRED 不得标记为可自动重试："
                "结果未知时重试会产生第二次副作用，必须先到外部系统对账"
            )

    @property
    def next_action(self) -> str:
        """调用方接下来应当做什么。**由 code 推导，不由 raise 点手工填写。**

        推导而非传参，是为了让它不可能被遗漏：新增一个 RECONCILIATION_REQUIRED
        的 raise 点却忘了写 next_action 的情况，在结构上不会发生。
        """
        if self.code is ErrorCode.RECONCILIATION_REQUIRED:
            return NEXT_ACTION_RECONCILE
        return ""

    def __str__(self) -> str:
        text = f"[{self.code}] {self.message}"
        if self.request_id:
            text += f" (request_id={self.request_id})"
        return text

    def to_payload(self) -> dict:
        """转换为 API 响应体。只暴露稳定码与可理解描述，不泄露内部细节。"""
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
            "next_action": self.next_action,
        }


def deny(code: ErrorCode, message: str, **details) -> PlatformError:
    """构造一个不可重试的拒绝错误。特权路径失败一律用这个。"""
    return PlatformError(code=code, message=message, retryable=False, details=details)
