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
    # cookie 认证的跨站请求伪造拦截。仅作用于"凭 cookie 认证的不安全方法"；
    # bearer 兼容路径不经过它（凭据是显式的，不存在环境凭证被利用的问题）。
    CSRF_DENIED = "CSRF_DENIED"
    # 认证尝试（注册/登录）触发限流：这是**可重试**拒绝，
    # 响应带 Retry-After。
    RATE_LIMITED = "RATE_LIMITED"
    ACCOUNT_ALREADY_AUTHENTICATED = "already_authenticated"
    USERNAME_TAKEN = "USERNAME_TAKEN"
    PASSWORD_LOGIN_DISABLED = "PASSWORD_LOGIN_DISABLED"
    REGISTRATION_DISABLED = "REGISTRATION_DISABLED"
    AUTH_POOL_SATURATED = "AUTH_POOL_SATURATED"

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
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_VIOLATION = "IDEMPOTENCY_VIOLATION"
    # 同一幂等键的请求正在处理中。**与 VIOLATION 是两回事**：
    # 前者是"等一下再来"，后者是"你这把钥匙用错了"。混为一谈会让客户端
    # 把并发重试当成参数冲突处理，从而放弃一个本来会成功的请求。
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    # 乐观锁冲突：`expected_version` 与当前版本不符。**可重试但必须先读最新状态**，
    # 与"资源不存在"（404）严格区分 —— 混在一起客户端会误判成权限问题。
    VERSION_CONFLICT = "VERSION_CONFLICT"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    # ---- 模型教学（第五轮）----
    # provider 未启用是**配置事实**，不是瞬时错误：明确 503 + 这个码，
    # 绝不静默退回模拟器假装可用。
    TEACHING_PROVIDER_DISABLED = "TEACHING_PROVIDER_DISABLED"
    SOURCE_SEARCH_DISABLED = "SOURCE_SEARCH_DISABLED"
    SOURCE_SEARCH_UNAVAILABLE = "SOURCE_SEARCH_UNAVAILABLE"

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

    # ---- 审计存储 ----
    AUDIT_LOG_CORRUPTED = "AUDIT_LOG_CORRUPTED"

    # ---- 内部一致性 ----
    # 服务端自己的不变量被破坏（例如注册函数声称建了会话、回读却不可见，
    # 或服务端传入了超出硬上限的会话期限）。**绝不能用 assert 守这类不变量**：
    # python -O 会剥掉断言。对外只呈现通用 500，不暴露内部细节。
    INTERNAL_CONSISTENCY_ERROR = "INTERNAL_CONSISTENCY_ERROR"


# 平台层 next_action 词汇表。
# 目前只登记真实会被产生的取值 —— 永不触发的取值比缺失的取值更糟，
# 因为它看起来已经实现（这条教训来自 `EvidenceIssueCode` 的诚实标注）。
NEXT_ACTION_RECONCILE = "reconcile"

# 对外错误响应体的**唯一**形状定义。
#
# 为什么需要它：同一条语义曾经有四个出口 —— `PlatformError.to_payload()`、
# API 层两处手写 dict（401 与 404 的脱敏替换）、runtime 的拒绝分支 ——
# 于是新增一个字段时只落到了其中一部分：`/interactions` 在不同失败路径上
# 返回不同形状的错误体，同一个端点自己就不一致。
#
# 形状不一致不是"小瑕疵"：客户端只能按"有就取、没有就跳过"来写，
# 最终等于该字段不存在。收敛成单一构造点后，新增字段不可能只落一半。
ERROR_PAYLOAD_KEYS = ("code", "message", "retryable", "request_id", "next_action")


def public_error_payload(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
    next_action: str = "",
) -> dict:
    """构造对外错误响应体。**这是唯一允许出现错误字段名的地方。**"""
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id,
        "next_action": next_action,
    }


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
        return public_error_payload(
            self.code.value,
            self.message,
            retryable=self.retryable,
            request_id=self.request_id,
            next_action=self.next_action,
        )


def deny(
    code: ErrorCode,
    message: str,
    *,
    request_id: str | None = None,
    **details,
) -> PlatformError:
    """构造一个不可重试的拒绝错误。特权路径失败一律用这个。

    `request_id` 是显式参数，不落进 `details`。此前它只能靠 `**details` 传递，
    结果是 `deny(code, msg, request_id="r1")` 把 id 塞进了 `details`
    ——一个不会出现在错误响应里的字典——然后**静默丢失**：
    调用方以为写了追踪 id，实际响应里是 null。
    把参数提到签名上，这种写法就会绑到正确的字段。
    """
    return PlatformError(
        code=code,
        message=message,
        retryable=False,
        request_id=request_id,
        details=details,
    )
