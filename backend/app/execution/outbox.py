"""工具派发：intent → 幂等 dispatch → outcome → reconciliation。

设计依据：不变量 #6、03 号规格 §6/§9。

派发前的固定顺序（**不得调换**）：
1. 策略决策必须放行；
2. 执行器必须声明支持该决策的全部 obligation；
3. token 必须验签通过、未过期、未被撤销；
4. token 必须持有该工具；
5. 审计必须已写入 intent（不变量 #6）；高影响动作在审计不可用时拒绝；
6. 预算预留必须进入 `in_flight`；
7. 到此才允许真正调用外部工具。

顺序若被打乱，就会出现「先产生副作用、再发现没权限/没审计」——这正是本项目
要机械排除的失败模式。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from app.audit.sink import AuditSink, RiskLevel
from app.budget.ledger import BudgetLedger
from app.core.clock import Clock
from app.core.errors import ErrorCode, deny
from app.execution.state_machine import ActionState, ActionStateMachine, LogicalAction
from app.policy.gateway import ExecutorCapabilities, PolicyDecision
from app.policy.token import CapabilityToken, TokenIssuer
from app.registry.registry import Registry


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class UncertainFailure(Exception):
    """工具适配器在「无法确定是否已生效」时抛出。

    这是最需要小心的一类失败：调用可能已经产生副作用，但结果不可知。
    处理方式是保留预算与 intent 并进入对账，绝不能当作没发生过。
    """


@dataclass(frozen=True)
class ToolOutcome:
    logical_action_id: str
    tool_id: str
    status: OutcomeStatus
    attempt_id: str
    idempotency_key: str
    payload: dict
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "logical_action_id": self.logical_action_id,
            "tool_id": self.tool_id,
            "status": str(self.status),
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "error": self.error,
        }


class ToolDispatcher:
    """受策略、预算与审计约束的工具派发器。"""

    def __init__(
        self,
        *,
        registry: Registry,
        ledger: BudgetLedger,
        audit: AuditSink,
        machine: ActionStateMachine,
        tokens: TokenIssuer,
        capabilities: ExecutorCapabilities,
        clock: Clock,
        revocation_epoch: int = 0,
    ) -> None:
        self._registry = registry
        self._ledger = ledger
        self._audit = audit
        self._machine = machine
        self._tokens = tokens
        self._capabilities = capabilities
        self._clock = clock
        self._revocation_epoch = revocation_epoch

    @property
    def revocation_epoch(self) -> int:
        return self._revocation_epoch

    def revoke_all(self) -> int:
        """广播撤销：所有早期 token 立即作废（03 号规格 §3）。"""
        self._revocation_epoch += 1
        return self._revocation_epoch

    # ------------------------------------------------------------------ 派发

    def dispatch(
        self,
        *,
        token: CapabilityToken,
        decision: PolicyDecision,
        action: LogicalAction,
        reservation_id: str,
        tool: Callable[[dict], dict],
        params: dict,
        is_high_impact: bool = False,
        request_id: str | None = None,
    ) -> ToolOutcome:
        """执行一次工具调用。任何前置条件不满足都在**产生副作用之前**失败。

        `request_id` 会写进本次派发产生的每一条审计事件。这不是可有可无的装饰：
        **没有它就无法从一次错误响应反查到对应的审计事件** ——
        而全链路追踪正是追踪 id 存在的唯一理由。
        """
        # 1) 策略必须放行
        decision.require_allowed()
        # 2) 执行器必须支持全部 obligation
        self._capabilities.assert_supported(decision)
        # 3) token 验签、时效、撤销
        self._tokens.verify(
            token,
            now=self._clock.now(),
            current_revocation_epoch=self._revocation_epoch,
        )
        # 4) token 必须持有该工具，且工具必须已注册
        spec = self._registry.tool(action.tool_id)
        if not token.holds(action.tool_id):
            raise deny(
                ErrorCode.CAPABILITY_NOT_HELD,
                f"当前 token 未持有工具 {action.tool_id}；"
                f"扩权必须结束当前授权域重新评估，不能就地放宽",
                tool_id=action.tool_id,
            )
        # 重复投递：同幂等键且已到终态的动作不再执行。
        if action.terminal:
            return ToolOutcome(
                logical_action_id=action.logical_action_id,
                tool_id=action.tool_id,
                status=OutcomeStatus.SUCCEEDED if action.state is ActionState.ACKNOWLEDGED else OutcomeStatus.FAILED,
                attempt_id="(none)",
                idempotency_key=action.idempotency_key,
                payload=action.outcome or {},
                error=None if action.state is ActionState.ACKNOWLEDGED else "动作已处于终态",
            )
        if action.state is ActionState.UNKNOWN:
            raise deny(
                ErrorCode.RECONCILIATION_REQUIRED,
                f"动作 {action.logical_action_id} 状态未知，必须先对账再决定是否重试",
                action_id=action.logical_action_id,
            )

        # 5) intent 必须先落审计。审计不可用时高影响动作在这里就失败，尚未产生副作用。
        self._audit.append(
            "action_intent",
            {
                "logical_action_id": action.logical_action_id,
                "idempotency_key": action.idempotency_key,
                "tool_id": action.tool_id,
                "tenant_id": token.tenant_id,
                "project_id": token.project_id,
                "run_id": token.run_id,
                "node_instance_id": action.node_instance_id,
                "policy_decision_id": decision.decision_id,
                "policy_version": decision.policy_version,
                "registry_version": token.registry_version,
                "params_hash": decision.snapshot_hash,
            },
            risk=RiskLevel.HIGH if (is_high_impact or spec.requires_audit) else RiskLevel.LOW,
            tenant_id=token.tenant_id,
            project_id=token.project_id,
            request_id=request_id,
        )
        self._machine.transition(action, ActionState.INTENT_PERSISTED)

        # 6) 预算进入 in_flight：此后不可因 TTL 释放。
        self._ledger.mark_in_flight(reservation_id)

        attempt_id = self._machine.begin_attempt(action)
        self._machine.transition(action, ActionState.DISPATCHED)

        # 7) 真正调用。
        try:
            payload = tool(params)
        except UncertainFailure as exc:
            self._machine.transition(action, ActionState.UNKNOWN)
            self._machine.set_outcome(action, {"error": str(exc), "status": "unknown"})
            self._audit.append(
                "action_unknown",
                {"logical_action_id": action.logical_action_id, "reason": str(exc)},
                risk=RiskLevel.HIGH,
                tenant_id=token.tenant_id,
                project_id=token.project_id,
                request_id=request_id,
            )
            return ToolOutcome(
                logical_action_id=action.logical_action_id,
                tool_id=action.tool_id,
                status=OutcomeStatus.UNKNOWN,
                attempt_id=attempt_id,
                idempotency_key=action.idempotency_key,
                payload={},
                error=str(exc),
            )
        except Exception as exc:  # 明确的失败，可安全结算为 0
            self._machine.transition(action, ActionState.FAILED)
            self._machine.set_outcome(action, {"error": str(exc), "status": "failed"})
            self._ledger.settle(reservation_id, 0)
            self._audit.append(
                "action_failed",
                {"logical_action_id": action.logical_action_id, "error": str(exc)},
                risk=RiskLevel.LOW,
                tenant_id=token.tenant_id,
                project_id=token.project_id,
                request_id=request_id,
            )
            return ToolOutcome(
                logical_action_id=action.logical_action_id,
                tool_id=action.tool_id,
                status=OutcomeStatus.FAILED,
                attempt_id=attempt_id,
                idempotency_key=action.idempotency_key,
                payload={},
                error=str(exc),
            )

        actual = int(payload.get("cost_units", 0))
        self._machine.transition(action, ActionState.ACKNOWLEDGED)
        self._machine.set_outcome(action, dict(payload))
        self._ledger.settle(reservation_id, actual)
        self._audit.append(
            "action_outcome",
            {
                "logical_action_id": action.logical_action_id,
                "status": "succeeded",
                "cost_units": actual,
                "result_keys": sorted(payload.keys()),
            },
            risk=RiskLevel.LOW,
            tenant_id=token.tenant_id,
            project_id=token.project_id,
            request_id=request_id,
        )
        return ToolOutcome(
            logical_action_id=action.logical_action_id,
            tool_id=action.tool_id,
            status=OutcomeStatus.SUCCEEDED,
            attempt_id=attempt_id,
            idempotency_key=action.idempotency_key,
            payload=dict(payload),
        )

    # ------------------------------------------------------------------ 对账

    def reconcile(
        self,
        action: LogicalAction,
        *,
        reservation_id: str,
        resolved: OutcomeStatus,
        actual: int = 0,
        request_id: str | None = None,
    ) -> LogicalAction:
        """对账 `unknown` 状态。只有对账之后才可能再次执行。

        `request_id` 通常由对账作业（而非 HTTP 请求）传入：对账是异步重活，
        它处理的动作来自**更早的某次请求**，因此不能靠请求上下文兜底 ——
        显式传入才是正确的关联方式。
        """
        if action.state is not ActionState.UNKNOWN:
            raise deny(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"只有 unknown 动作需要对账，当前为 {action.state}",
                action_id=action.logical_action_id,
            )
        if resolved is OutcomeStatus.UNKNOWN:
            raise deny(
                ErrorCode.RECONCILIATION_REQUIRED,
                "对账结果不能仍是未知；需查明外部系统的真实状态",
                action_id=action.logical_action_id,
            )
        self._ledger.settle(reservation_id, actual)
        target = (
            ActionState.ACKNOWLEDGED
            if resolved is OutcomeStatus.SUCCEEDED
            else ActionState.FAILED
        )
        self._machine.transition(action, target)
        self._machine.transition(action, ActionState.RECONCILED)
        self._audit.append(
            "action_reconciled",
            {
                "logical_action_id": action.logical_action_id,
                "resolved": str(resolved),
                "actual_cost_units": actual,
            },
            risk=RiskLevel.HIGH,
            tenant_id=action.tenant_id,
            project_id=action.project_id,
            request_id=request_id,
        )
        return action
