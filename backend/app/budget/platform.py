"""Platform-wide paid-provider budget with an explicit reservation lifecycle."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from app.core.errors import ErrorCode, PlatformError


def reservation_id_for_run(run_id: str) -> str:
    """Derive a stable, opaque reservation id for crash-safe worker recovery."""

    if not run_id:
        raise ValueError("run_id must not be empty")
    return f"paid_{sha256(run_id.encode('utf-8')).hexdigest()[:24]}"


def billing_period(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m")


class PlatformReservationState(StrEnum):
    HELD = "held"
    IN_FLIGHT = "in_flight"
    SETTLED = "settled"
    RELEASED = "released"


@dataclass(frozen=True)
class PlatformReservation:
    reservation_id: str
    billing_period: str
    provider: str
    price_version: str
    max_spend_micro: int
    state: PlatformReservationState = PlatformReservationState.HELD
    actual_spend_micro: int | None = None


class PlatformPaidBudgetPort(Protocol):
    """The small lifecycle surface used by the teaching worker.

    Keeping this port separate lets the worker use the in-memory implementation
    in development and a durable PostgreSQL implementation in production without
    weakening the reservation state machine.
    """

    def reserve(
        self,
        *,
        provider: str,
        price_version: str,
        max_spend_micro: int,
        at: datetime,
        reservation_id: str | None = None,
    ) -> PlatformReservation: ...

    def mark_in_flight(self, reservation_id: str) -> PlatformReservation: ...

    def settle(
        self, reservation_id: str, *, actual_spend_micro: int | None
    ) -> PlatformReservation: ...

    def release_before_dispatch(self, reservation_id: str) -> PlatformReservation: ...

    def release_dispatch_failed(self, reservation_id: str) -> PlatformReservation: ...


class PlatformPaidBudget:
    """Shared paid-provider accounting for each UTC calendar month.

    ``monthly_cap_micro=None`` means accounting remains enabled but the
    platform does not reject dispatches based on a monthly monetary cap.
    Per-request token limits and provider timeouts remain enforced by the
    teaching service/provider boundary.
    """

    def __init__(
        self, *, monthly_cap_micro: int | None = 0, paid_dispatch_enabled: bool = False
    ) -> None:
        if monthly_cap_micro is not None and monthly_cap_micro < 0:
            raise ValueError("monthly cap must be non-negative")
        self.monthly_cap_micro = monthly_cap_micro
        self.paid_dispatch_enabled = paid_dispatch_enabled
        self._reservations: dict[str, PlatformReservation] = {}
        self._spent: dict[str, int] = {}
        self._lock = threading.RLock()

    def set_cap(self, monthly_cap_micro: int | None) -> None:
        if monthly_cap_micro is not None and monthly_cap_micro < 0:
            raise ValueError("monthly cap must be non-negative")
        with self._lock:
            self.monthly_cap_micro = monthly_cap_micro

    def reserve(
        self,
        *,
        provider: str,
        price_version: str,
        max_spend_micro: int,
        at: datetime,
        reservation_id: str | None = None,
    ) -> PlatformReservation:
        if max_spend_micro <= 0:
            raise ValueError("max_spend_micro must be positive")
        period = billing_period(at)
        with self._lock:
            if not self.paid_dispatch_enabled:
                raise PlatformError(ErrorCode.BUDGET_EXCEEDED, "付费派发已由管理员暂停")
            stable_id = reservation_id or reservation_id_for_run(
                f"anonymous:{provider}:{price_version}:{period}:{len(self._reservations)}"
            )
            existing = self._reservations.get(stable_id)
            if existing is not None:
                if (
                    existing.billing_period != period
                    or existing.provider != provider
                    or existing.price_version != price_version
                    or existing.max_spend_micro != max_spend_micro
                    or existing.state
                    not in {PlatformReservationState.HELD, PlatformReservationState.IN_FLIGHT}
                ):
                    raise PlatformError(
                        ErrorCode.BUDGET_TREE_INVALID,
                        "平台预留幂等键与既有参数或终态冲突",
                    )
                return existing
            exposure = self._exposure(period)
            if (
                self.monthly_cap_micro is not None
                and exposure + max_spend_micro > self.monthly_cap_micro
            ):
                raise PlatformError(ErrorCode.BUDGET_EXCEEDED, "平台月度付费额度不足")
            item = PlatformReservation(
                reservation_id=stable_id,
                billing_period=period,
                provider=provider,
                price_version=price_version,
                max_spend_micro=max_spend_micro,
            )
            self._reservations[item.reservation_id] = item
            return item

    def mark_in_flight(self, reservation_id: str) -> PlatformReservation:
        with self._lock:
            item = self._get(reservation_id)
            if item.state is PlatformReservationState.IN_FLIGHT:
                return item
            if item.state is not PlatformReservationState.HELD:
                raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "只能派发 held 预留")
            updated = PlatformReservation(**{**item.__dict__, "state": PlatformReservationState.IN_FLIGHT})
            self._reservations[reservation_id] = updated
            return updated

    def settle(self, reservation_id: str, *, actual_spend_micro: int | None) -> PlatformReservation:
        with self._lock:
            item = self._get(reservation_id)
            if (
                item.state is PlatformReservationState.SETTLED
                and item.actual_spend_micro == actual_spend_micro
            ):
                return item
            if item.state is not PlatformReservationState.IN_FLIGHT:
                raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "只能结算 in_flight 预留")
            if actual_spend_micro is None:
                raise PlatformError(ErrorCode.RECONCILIATION_REQUIRED, "provider 用量未知，必须对账")
            if actual_spend_micro < 0 or actual_spend_micro > item.max_spend_micro:
                raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "实际费用超出预留上界")
            updated = PlatformReservation(**{**item.__dict__, "state": PlatformReservationState.SETTLED, "actual_spend_micro": actual_spend_micro})
            self._reservations[reservation_id] = updated
            self._spent[item.billing_period] = self._spent.get(item.billing_period, 0) + actual_spend_micro
            return updated

    def release_before_dispatch(self, reservation_id: str) -> PlatformReservation:
        with self._lock:
            item = self._get(reservation_id)
            if item.state is PlatformReservationState.RELEASED:
                return item
            if item.state is not PlatformReservationState.HELD:
                raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "已派发预留不得自动释放")
            updated = PlatformReservation(**{**item.__dict__, "state": PlatformReservationState.RELEASED})
            self._reservations[reservation_id] = updated
            return updated

    def release_dispatch_failed(self, reservation_id: str) -> PlatformReservation:
        """Release an in-flight reservation only when delivery was proven absent."""
        with self._lock:
            item = self._get(reservation_id)
            if item.state is PlatformReservationState.RELEASED:
                return item
            if item.state is not PlatformReservationState.IN_FLIGHT:
                raise PlatformError(
                    ErrorCode.BUDGET_TREE_INVALID,
                    "只有已确认未送达的 in_flight 预留才能释放",
                )
            updated = PlatformReservation(
                **{**item.__dict__, "state": PlatformReservationState.RELEASED}
            )
            self._reservations[reservation_id] = updated
            return updated

    def snapshot(self, at: datetime) -> dict[str, int | str | bool | None]:
        period = billing_period(at)
        with self._lock:
            return {
                "billing_period": period,
                "monthly_cap_micro": self.monthly_cap_micro,
                "spent_micro": self._spent.get(period, 0),
                "reserved_micro": sum(x.max_spend_micro for x in self._reservations.values() if x.billing_period == period and x.state is PlatformReservationState.HELD),
                # In-flight exposure is deliberately not partitioned by month:
                # a stale provider result remains an obligation after UTC rolls
                # over until reconciliation settles it.
                "in_flight_micro": sum(
                    x.max_spend_micro
                    for x in self._reservations.values()
                    if x.state is PlatformReservationState.IN_FLIGHT
                ),
                "paid_dispatch_enabled": self.paid_dispatch_enabled,
            }

    def _exposure(self, period: str) -> int:
        return self._spent.get(period, 0) + sum(
            x.max_spend_micro
            for x in self._reservations.values()
            if x.state is PlatformReservationState.IN_FLIGHT
            or (x.billing_period == period and x.state is PlatformReservationState.HELD)
        )

    def _get(self, reservation_id: str) -> PlatformReservation:
        try:
            return self._reservations[reservation_id]
        except KeyError as exc:
            raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "付费预留不存在") from exc
