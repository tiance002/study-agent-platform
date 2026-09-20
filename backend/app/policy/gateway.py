"""Policy Gateway：确定性授权边界。

设计依据：03 号规格 §2、不变量 #3。

四条性质（均有对应测试）：
1. 相同 `policy_version` + 相同不可变输入快照 → **完全相同的决策内容**；
2. 无模型参与：本模块不导入任何模型或 provider，判定全为确定性规则；
3. 未知或不支持的 obligation 一律拒绝执行（执行器必须声明支持范围）；
4. Gateway 不可用时特权路径 **fail-closed**。

`decision_id` 由快照哈希派生而非随机生成 —— 这是刻意的：
「同一决策」应当有同一个标识，随机 id 会让可重放性验证变得含糊。
审计上的唯一性由审计事件的 `event_id` 承担。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.authority import Authority
from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash

POLICY_VERSION = "policy/v1"


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Obligation(StrEnum):
    """决策附加义务。执行器必须声明支持，否则拒绝执行。"""

    REQUIRE_CONFIRMATION = "requires_confirmation/v1"
    REQUIRE_DUAL_APPROVAL = "requires_dual_approval/v1"
    REQUIRE_SIGNED_AUDIT_BATCH = "requires_signed_audit_batch/v1"
    REQUIRE_EGRESS_ALLOWLIST = "requires_egress_allowlist/v1"
    REQUIRE_TAINT_ENDORSEMENT = "requires_taint_endorsement/v1"


@dataclass(frozen=True)
class PolicyInput:
    """策略输入。所有字段都必须来自不可变快照，不得包含实时可变状态。"""

    request_id: str
    principal_id: str
    tenant_id: str
    project_id: str
    node_id: str
    tool_id: str | None
    authority_required: Authority
    authority_ceiling: Authority
    params_hash: str
    data_labels: frozenset[str] = frozenset()
    budget_available: bool = True
    audit_available: bool = True
    confirmation_recorded: bool = False
    region_allowed: bool = True
    is_high_impact: bool = False
    needs_egress: bool = False

    def snapshot(self) -> dict:
        """规范化快照。**禁止**在此放入随机值、当前时间或实时 ACL。"""
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "node_id": self.node_id,
            "tool_id": self.tool_id,
            "authority_required": int(self.authority_required),
            "authority_ceiling": int(self.authority_ceiling),
            "params_hash": self.params_hash,
            "data_labels": sorted(self.data_labels),
            "budget_available": self.budget_available,
            "audit_available": self.audit_available,
            "confirmation_recorded": self.confirmation_recorded,
            "region_allowed": self.region_allowed,
            "is_high_impact": self.is_high_impact,
            "needs_egress": self.needs_egress,
        }


@dataclass(frozen=True)
class PolicyDecision:
    """显式持久实体。执行前必须记录，且执行器必须声明支持其 obligation。"""

    decision_id: str
    policy_version: str
    snapshot_id: str
    snapshot_hash: str
    verdict: Verdict
    obligations: tuple[str, ...] = ()
    deny_reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def require_allowed(self) -> None:
        """在执行点调用：未放行即拒绝，且拒绝路径不得产生任何外部副作用。"""
        if not self.allowed:
            raise deny(
                ErrorCode.POLICY_DENIED,
                f"策略拒绝：{', '.join(self.deny_reasons) or '未说明原因'}",
                decision_id=self.decision_id,
                deny_reasons=list(self.deny_reasons),
            )


@dataclass(frozen=True)
class ExecutorCapabilities:
    """执行器声明的能力。未声明的 obligation 会导致拒绝，而不是"尽力而为"。"""

    supported_obligations: frozenset[str]

    def assert_supported(self, decision: PolicyDecision) -> None:
        unknown = [o for o in decision.obligations if o not in self.supported_obligations]
        if unknown:
            raise deny(
                ErrorCode.OBLIGATION_UNSUPPORTED,
                f"执行器不支持以下 obligation：{unknown}",
                unsupported=unknown,
                decision_id=decision.decision_id,
            )


class PolicyGateway:
    """无模型的确定性策略服务。

    `available=False` 用于故障注入测试：此时**任何**请求都只得到拒绝，
    以验证 fail-closed 语义（00 号规格 §4 失败矩阵）。
    """

    def __init__(self, *, available: bool = True, policy_version: str = POLICY_VERSION) -> None:
        self._available = available
        self._policy_version = policy_version

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def set_available(self, available: bool) -> None:
        """仅用于测试与运维演练。生产由部署层控制。"""
        self._available = available

    def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        snapshot = policy_input.snapshot()
        snapshot_hash = content_hash({"policy_version": self._policy_version, **snapshot})

        if not self._available:
            return self._build(
                snapshot_hash,
                Verdict.DENY,
                deny_reasons=["policy_gateway_unavailable"],
            )

        reasons: list[str] = []
        obligations: list[str] = []

        # 规则 1：地域与合规硬约束优先，任何其他条件都不能覆盖它。
        if not policy_input.region_allowed:
            reasons.append("region_not_allowed")

        # 规则 2：审计不可用时，高影响动作 fail-closed（03 号规格 §9）。
        if not policy_input.audit_available:
            if policy_input.is_high_impact or policy_input.authority_required >= Authority.A2:
                reasons.append("audit_sink_unavailable")

        # 规则 3：权限不得超过 node 上限。
        if policy_input.authority_required > policy_input.authority_ceiling:
            reasons.append("authority_ceiling_exceeded")

        # 规则 4：预算预留失败不得执行（不变量 #8）。
        if not policy_input.budget_available:
            reasons.append("budget_unavailable")

        # 规则 5：A2 及以上需要用户确认；未确认时带义务放行由调用方决定是否执行，
        # 但高风险动作在这里直接拒绝，避免"先执行再补确认"。
        if policy_input.authority_required >= Authority.A2:
            if policy_input.is_high_impact and not policy_input.confirmation_recorded:
                reasons.append("confirmation_required")
            else:
                obligations.append(str(Obligation.REQUIRE_CONFIRMATION))

        # 规则 6：A3 一律需要双人审批，不因确认而豁免。
        if policy_input.authority_required >= Authority.A3:
            obligations.append(str(Obligation.REQUIRE_DUAL_APPROVAL))

        # 规则 7：带 taint 的数据进入 sink 前必须有 endorsement。
        if policy_input.data_labels:
            obligations.append(str(Obligation.REQUIRE_TAINT_ENDORSEMENT))

        # 规则 8：访问外部网络必须经过出网白名单校验。
        if policy_input.needs_egress:
            obligations.append(str(Obligation.REQUIRE_EGRESS_ALLOWLIST))

        verdict = Verdict.DENY if reasons else Verdict.ALLOW
        return self._build(snapshot_hash, verdict, obligations=obligations, deny_reasons=reasons)

    def _build(
        self,
        snapshot_hash: str,
        verdict: Verdict,
        *,
        obligations: list[str] | None = None,
        deny_reasons: list[str] | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            decision_id=f"dec_{snapshot_hash.split(':')[-1][:24]}",
            policy_version=self._policy_version,
            snapshot_id=f"snap_{snapshot_hash.split(':')[-1][:24]}",
            snapshot_hash=snapshot_hash,
            verdict=verdict,
            obligations=tuple(sorted(obligations or [])),
            deny_reasons=tuple(sorted(deny_reasons or [])),
        )
