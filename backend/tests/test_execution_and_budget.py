"""预算、执行状态机与审计的不变量证明。

对应 03 号规格 §5/§6/§9 与不变量 #6/#7/#8。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.audit.sink import AuditSink, RiskLevel
from app.budget.ledger import BudgetLedger, Dimension
from app.core.errors import ErrorCode, PlatformError
from app.execution.state_machine import ActionState, ActionStateMachine

MONEY = str(Dimension.CURRENCY_MICROS)
TOOL_CALLS = str(Dimension.TOOL_CALLS)
STEPS = str(Dimension.STEPS)


# --------------------------------------------------------------------- 预算


@pytest.mark.invariant
def test_reservation_failure_blocks_execution():
    """不变量 #8：预算预留失败不得执行。"""
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={MONEY: 100})
    with pytest.raises(PlatformError) as exc:
        ledger.reserve("acct", MONEY, 101)
    assert exc.value.code is ErrorCode.BUDGET_RESERVATION_FAILED
    assert ledger.account("acct").reserved[MONEY] == 0


@pytest.mark.invariant
def test_completion_reserve_cannot_be_lent_out():
    """03 号规格 §5：父账户的 completion_reserve 不可借出。"""
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={MONEY: 100}, completion_reserve={MONEY: 30})
    assert ledger.account("acct").available(MONEY) == 70
    with pytest.raises(PlatformError):
        ledger.reserve("acct", MONEY, 71)
    ledger.reserve("acct", MONEY, 70)


@pytest.mark.invariant
def test_batch_reserve_is_atomic():
    """批量预留要么全成要么全不动。"""
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={MONEY: 100, STEPS: 1})
    with pytest.raises(PlatformError):
        ledger.batch_reserve("acct", {MONEY: 50, STEPS: 5})
    account = ledger.account("acct")
    assert account.reserved[MONEY] == 0
    assert account.reserved[STEPS] == 0


@pytest.mark.invariant
def test_child_account_cannot_use_parent_headroom():
    """子账户只能用父账户授予的额度，父账户余额对它不可见。"""
    ledger = BudgetLedger()
    ledger.open_account("parent", limits={MONEY: 1000})
    ledger.grant_to_child("parent", "child", {MONEY: 100})
    assert ledger.account("child").available(MONEY) == 100
    with pytest.raises(PlatformError):
        ledger.reserve("child", MONEY, 101)


@pytest.mark.invariant
def test_in_flight_reservation_cannot_be_released():
    """03 号规格 §5：已派发的调用不能仅因 TTL 释放。"""
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={MONEY: 100})
    reservation = ledger.reserve("acct", MONEY, 10)
    ledger.mark_in_flight(reservation.reservation_id)
    with pytest.raises(PlatformError) as exc:
        ledger.release(reservation.reservation_id)
    assert exc.value.code is ErrorCode.RECONCILIATION_REQUIRED


@pytest.mark.invariant
def test_settlement_above_reserved_ceiling_is_rejected():
    """预留必须是成本上界；实际用量超出上界说明预留逻辑有缺陷，应当报错而不是静默扣款。"""
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={MONEY: 100})
    reservation = ledger.reserve("acct", MONEY, 10)
    ledger.mark_in_flight(reservation.reservation_id)
    with pytest.raises(PlatformError) as exc:
        ledger.settle(reservation.reservation_id, 11)
    assert exc.value.code is ErrorCode.BUDGET_EXCEEDED


@pytest.mark.invariant
def test_closing_child_account_returns_headroom_to_parent():
    """关闭子账户时只把**实际消耗**计入父账户，其余额度归还。"""
    ledger = BudgetLedger()
    ledger.open_account("parent", limits={MONEY: 1000})
    ledger.grant_to_child("parent", "child", {MONEY: 200})
    reservation = ledger.reserve("child", MONEY, 50)
    ledger.mark_in_flight(reservation.reservation_id)
    ledger.settle(reservation.reservation_id, 20)
    ledger.close_account("child")

    parent = ledger.account("parent")
    assert parent.consumed[MONEY] == 20
    assert parent.reserved[MONEY] == 0
    assert parent.available(MONEY) == 980


@pytest.mark.invariant
def test_closing_account_with_open_reservations_is_refused():
    """还有未结算的调用时不得关闭账户 —— 那属于对账，不能被顺手清理掩盖。"""
    ledger = BudgetLedger()
    ledger.open_account("parent", limits={MONEY: 1000})
    ledger.grant_to_child("parent", "child", {MONEY: 200})
    ledger.reserve("child", MONEY, 50)
    with pytest.raises(PlatformError) as exc:
        ledger.close_account("child")
    assert exc.value.code is ErrorCode.RECONCILIATION_REQUIRED


@pytest.mark.invariant
def test_grant_reservations_are_not_counted_as_exposure():
    """授予预留不算预算敞口，否则会被误判为泄漏。"""
    ledger = BudgetLedger()
    ledger.open_account("parent", limits={MONEY: 1000})
    ledger.grant_to_child("parent", "child", {MONEY: 200})
    assert ledger.grant_reservations()
    assert ledger.open_reservations() == ()


# --------------------------------------------------------------- 执行状态机


@pytest.mark.invariant
def test_idempotency_key_excludes_attempt_and_time():
    """03 号规格 §6：attempt_id 不得进入幂等键。"""
    machine = ActionStateMachine()
    action = machine.plan(
        tenant_id="t", project_id="p", run_id="r", node_instance_id="n:1",
        logical_action_id="act_1", tool_id="tool_x",
    )
    baseline = action.idempotency_key
    machine.begin_attempt(action)
    machine.begin_attempt(action)
    assert action.idempotency_key == baseline


@pytest.mark.invariant
def test_duplicate_submission_reuses_same_action():
    """重复投递命中同一逻辑动作，不会新建一个。"""
    machine = ActionStateMachine()
    first = machine.plan(
        tenant_id="t", project_id="p", run_id="r", node_instance_id="n:1",
        logical_action_id="act_1", tool_id="tool_x",
    )
    second = machine.plan(
        tenant_id="t", project_id="p", run_id="r", node_instance_id="n:1",
        logical_action_id="act_1", tool_id="tool_x",
    )
    assert first is second


@pytest.mark.invariant
def test_illegal_state_transition_is_rejected():
    """非法状态转移一律拒绝，不做尽力而为。"""
    machine = ActionStateMachine()
    action = machine.plan(
        tenant_id="t", project_id="p", run_id="r", node_instance_id="n:1",
        logical_action_id="act_1", tool_id="tool_x",
    )
    with pytest.raises(PlatformError) as exc:
        machine.transition(action, ActionState.DISPATCHED)  # 必须先 intent_persisted
    assert exc.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


@pytest.mark.invariant
def test_unknown_cannot_go_back_to_dispatched():
    """不变量 #8：状态未知的调用不得盲目重派。"""
    machine = ActionStateMachine()
    action = machine.plan(
        tenant_id="t", project_id="p", run_id="r", node_instance_id="n:1",
        logical_action_id="act_1", tool_id="tool_x",
    )
    machine.transition(action, ActionState.INTENT_PERSISTED)
    machine.transition(action, ActionState.DISPATCHED)
    machine.transition(action, ActionState.UNKNOWN)
    with pytest.raises(PlatformError):
        machine.transition(action, ActionState.DISPATCHED)
    assert machine.actions_needing_reconciliation() == (action,)


# --------------------------------------------------------------------- 审计


@pytest.mark.invariant
def test_audit_sink_unavailable_blocks_high_risk_events(tmp_path):
    """03 号规格 §9：审计不可用时高影响动作 fail-closed。"""
    sink = AuditSink(tmp_path / "audit", available=False)
    with pytest.raises(PlatformError) as exc:
        sink.append("action_intent", {"x": 1}, risk=RiskLevel.HIGH)
    assert exc.value.code is ErrorCode.AUDIT_SINK_UNAVAILABLE


@pytest.mark.invariant
def test_low_risk_events_buffer_with_a_cap(tmp_path):
    """低风险事件进有界缓冲，超限即拒绝（不是无限堆积）。"""
    sink = AuditSink(tmp_path / "audit", available=False, buffer_capacity=2)
    sink.append("tick", {"i": 1}, risk=RiskLevel.LOW)
    sink.append("tick", {"i": 2}, risk=RiskLevel.LOW)
    with pytest.raises(PlatformError) as exc:
        sink.append("tick", {"i": 3}, risk=RiskLevel.LOW)
    assert exc.value.code is ErrorCode.AUDIT_SINK_UNAVAILABLE


@pytest.mark.invariant
def test_buffered_events_are_flushed_on_recovery(tmp_path):
    """sink 恢复后缓冲必须回放，不能静默丢弃。"""
    sink = AuditSink(tmp_path / "audit", available=False)
    sink.append("tick", {"i": 1}, risk=RiskLevel.LOW)
    assert sink.buffered_count == 1
    sink.set_available(True)
    assert sink.buffered_count == 0
    assert len(sink.read_all()) == 1


@pytest.mark.invariant
def test_audit_hash_chain_detects_tampering(tmp_path):
    """审计记录被改动后链校验必须失败。"""
    sink = AuditSink(tmp_path / "audit")
    sink.append("a", {"v": 1})
    sink.append("b", {"v": 2})
    assert sink.verify_chain()

    path = sink.path
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"v":1', '"v":9')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not sink.verify_chain()


@pytest.mark.invariant
def test_audit_sink_has_no_delete_capability(tmp_path):
    """应用层没有删除审计的能力 —— 不是"约定不删"，而是根本没有接口。"""
    sink = AuditSink(tmp_path / "audit")
    for name in ("delete", "truncate", "clear", "remove", "purge"):
        assert not hasattr(sink, name), f"审计 sink 不应提供 {name}"
