from datetime import datetime, timezone

import pytest
from app.budget.platform import PlatformPaidBudget, PlatformReservationState
from app.core.errors import ErrorCode, PlatformError

AT = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)


def test_reservation_snapshots_period_provider_and_price_and_keeps_inflight_across_month():
    budget = PlatformPaidBudget(monthly_cap_micro=100, paid_dispatch_enabled=True)
    item = budget.reserve(provider="p1", price_version="v7", max_spend_micro=60, at=AT)
    assert item.billing_period == "2026-09"
    assert item.provider == "p1"
    assert item.price_version == "v7"
    budget.mark_in_flight(item.reservation_id)
    budget.set_cap(1)
    assert budget.snapshot(datetime(2026, 10, 1, tzinfo=timezone.utc))["in_flight_micro"] == 60
    with pytest.raises(PlatformError):
        budget.reserve(provider="p2", price_version="v1", max_spend_micro=1, at=AT)


def test_disabled_switch_blocks_new_dispatch_but_does_not_erase_reservation():
    budget = PlatformPaidBudget(monthly_cap_micro=100, paid_dispatch_enabled=True)
    item = budget.reserve(provider="p1", price_version="v1", max_spend_micro=10, at=AT)
    budget.paid_dispatch_enabled = False
    assert item.state is PlatformReservationState.HELD
    with pytest.raises(PlatformError) as exc:
        budget.reserve(provider="p2", price_version="v1", max_spend_micro=1, at=AT)
    assert exc.value.code is ErrorCode.BUDGET_EXCEEDED


def test_inflight_without_authoritative_usage_requires_reconciliation():
    budget = PlatformPaidBudget(monthly_cap_micro=100, paid_dispatch_enabled=True)
    item = budget.reserve(provider="p1", price_version="v1", max_spend_micro=10, at=AT)
    budget.mark_in_flight(item.reservation_id)
    with pytest.raises(PlatformError) as exc:
        budget.settle(item.reservation_id, actual_spend_micro=None)
    assert exc.value.code is ErrorCode.RECONCILIATION_REQUIRED


def test_reservation_retry_uses_stable_id_without_double_counting_exposure():
    budget = PlatformPaidBudget(monthly_cap_micro=100, paid_dispatch_enabled=True)
    first = budget.reserve(
        provider="p1",
        price_version="v1",
        max_spend_micro=60,
        at=AT,
        reservation_id="paid_stable",
    )
    retried = budget.reserve(
        provider="p1",
        price_version="v1",
        max_spend_micro=60,
        at=AT,
        reservation_id="paid_stable",
    )
    assert retried == first
    assert budget.snapshot(AT)["reserved_micro"] == 60
    assert budget.mark_in_flight(first.reservation_id) == budget.mark_in_flight(
        first.reservation_id
    )
    settled = budget.settle(first.reservation_id, actual_spend_micro=40)
    assert budget.settle(first.reservation_id, actual_spend_micro=40) == settled

    with pytest.raises(PlatformError) as exc:
        budget.reserve(
            provider="p1",
            price_version="v2",
            max_spend_micro=60,
            at=AT,
            reservation_id="paid_stable",
        )
    assert exc.value.code is ErrorCode.BUDGET_TREE_INVALID


def test_none_monthly_cap_keeps_accounting_without_rejecting_dispatch():
    budget = PlatformPaidBudget(monthly_cap_micro=None, paid_dispatch_enabled=True)
    first = budget.reserve(
        provider="p1", price_version="v1", max_spend_micro=10**12, at=AT
    )
    budget.mark_in_flight(first.reservation_id)
    settled = budget.settle(first.reservation_id, actual_spend_micro=7)

    assert settled.actual_spend_micro == 7
    snapshot = budget.snapshot(AT)
    assert snapshot["monthly_cap_micro"] is None
    assert snapshot["spent_micro"] == 7
    assert snapshot["paid_dispatch_enabled"] is True
