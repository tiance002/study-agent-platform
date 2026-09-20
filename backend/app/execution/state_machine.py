"""执行状态机与幂等键。

设计依据：03 号规格 §6。

```text
planned → intent_persisted → dispatched
                          → acknowledged | failed | unknown
                          → reconciled
```

三条性质（均有测试）：
1. 幂等键为 `(tenant_id, run_id, node_instance_id, logical_action_id, tool_id)`，
   **`attempt_id` 只用于观测，不进入幂等键**；
2. 非法状态转移一律拒绝，不做"尽力而为"；
3. `unknown` 不能回到 `dispatched` —— 已派发的调用不能被当作没发生。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.core.ids import new_id


class ActionState(StrEnum):
    PLANNED = "planned"
    INTENT_PERSISTED = "intent_persisted"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


ALLOWED_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PLANNED: frozenset({ActionState.INTENT_PERSISTED}),
    ActionState.INTENT_PERSISTED: frozenset({ActionState.DISPATCHED}),
    ActionState.DISPATCHED: frozenset(
        {ActionState.ACKNOWLEDGED, ActionState.FAILED, ActionState.UNKNOWN}
    ),
    ActionState.ACKNOWLEDGED: frozenset({ActionState.RECONCILED}),
    ActionState.FAILED: frozenset({ActionState.RECONCILED}),
    ActionState.UNKNOWN: frozenset({ActionState.RECONCILED}),
    ActionState.RECONCILED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {ActionState.ACKNOWLEDGED, ActionState.FAILED, ActionState.RECONCILED}
)


@dataclass
class LogicalAction:
    """一个逻辑动作。同一动作的所有重试共享同一个幂等键。"""

    tenant_id: str
    project_id: str
    run_id: str
    node_instance_id: str
    logical_action_id: str
    tool_id: str
    state: ActionState = ActionState.PLANNED
    attempts: list[str] = field(default_factory=list)
    outcome: dict | None = None

    @property
    def idempotency_key(self) -> str:
        """稳定幂等键。

        **禁止**把 `attempt_id`、当前时间或随机数放进来：
        一旦放入，重试就会被当作新动作，产生重复副作用（03 号规格 §6）。
        """
        return content_hash(
            {
                "tenant_id": self.tenant_id,
                "run_id": self.run_id,
                "node_instance_id": self.node_instance_id,
                "logical_action_id": self.logical_action_id,
                "tool_id": self.tool_id,
            }
        )

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "node_instance_id": self.node_instance_id,
            "logical_action_id": self.logical_action_id,
            "tool_id": self.tool_id,
            "state": str(self.state),
            "attempts": list(self.attempts),
            "idempotency_key": self.idempotency_key,
        }


class ActionStateMachine:
    """动作状态机 + 幂等键唯一约束的持有者。"""

    def __init__(self) -> None:
        self._actions: dict[str, LogicalAction] = {}
        self._by_key: dict[str, str] = {}

    # ------------------------------------------------------------------ 计划

    def plan(
        self,
        *,
        tenant_id: str,
        project_id: str,
        run_id: str,
        node_instance_id: str,
        logical_action_id: str,
        tool_id: str,
    ) -> LogicalAction:
        """登记逻辑动作。

        若同一幂等键已存在，**返回既有动作**而不是新建 —— 这是"重复投递不重复执行"
        的实现点：重复投递命中同一个动作，其状态机不允许再次 dispatch。
        """
        action = LogicalAction(
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            node_instance_id=node_instance_id,
            logical_action_id=logical_action_id,
            tool_id=tool_id,
        )
        existing_id = self._by_key.get(action.idempotency_key)
        if existing_id is not None:
            return self._actions[existing_id]
        self._by_key[action.idempotency_key] = logical_action_id
        self._actions[logical_action_id] = action
        return action

    # ------------------------------------------------------------------ 转移

    def transition(self, action: LogicalAction, target: ActionState) -> LogicalAction:
        allowed = ALLOWED_TRANSITIONS[action.state]
        if target not in allowed:
            raise deny(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"动作 {action.logical_action_id} 不允许从 {action.state} 转移到 {target}；"
                f"允许的目标：{sorted(str(s) for s in allowed)}",
                action_id=action.logical_action_id,
                current=str(action.state),
                target=str(target),
            )
        action.state = target
        return action

    def begin_attempt(self, action: LogicalAction) -> str:
        """开始一次尝试。返回仅用于观测的 `attempt_id`。"""
        attempt_id = new_id("att")
        action.attempts.append(attempt_id)
        return attempt_id

    def set_outcome(self, action: LogicalAction, outcome: dict) -> None:
        action.outcome = outcome

    # ------------------------------------------------------------------ 查询

    def get(self, logical_action_id: str) -> LogicalAction:
        try:
            return self._actions[logical_action_id]
        except KeyError as exc:
            raise deny(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"逻辑动作不存在：{logical_action_id}；"
                f"恢复必须读取持久化的 intent，而不是重新猜测标识",
            ) from exc

    def find_by_key(self, key: str) -> LogicalAction | None:
        action_id = self._by_key.get(key)
        return self._actions[action_id] if action_id else None

    def actions_needing_reconciliation(
        self, *, tenant_id: str | None = None, project_id: str | None = None
    ) -> tuple[LogicalAction, ...]:
        """需要人工或自动对账的动作。`unknown` 有 SLA 与单独看板（03 号规格 §5）。

        可按租户/项目过滤：对账看板同样是项目级视图，不能跨租户聚合。
        """
        actions = [a for a in self._actions.values() if a.state is ActionState.UNKNOWN]
        if tenant_id is not None:
            actions = [a for a in actions if a.tenant_id == tenant_id]
        if project_id is not None:
            actions = [a for a in actions if a.project_id == project_id]
        return tuple(actions)

    def all_actions(self) -> tuple[LogicalAction, ...]:
        return tuple(self._actions.values())
