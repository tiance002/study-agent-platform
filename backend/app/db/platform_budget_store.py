"""Durable platform paid-budget adapter for the worker role.

The tables are FORCE RLS and intentionally have no direct application grants.
Only the narrowly-scoped SECURITY DEFINER transition functions from migration
0013 are callable here, so a worker cannot turn the platform budget into a
general-purpose billing-table read/write surface.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.budget.platform import (
    PlatformPaidBudgetPort,
    PlatformReservation,
    PlatformReservationState,
)
from app.core.errors import ErrorCode, PlatformError
from app.db.session import connect_worker


class PostgresPlatformPaidBudget(PlatformPaidBudgetPort):
    """PostgreSQL implementation of the platform reservation state machine."""

    def __init__(self, *, dsn: str | None = None) -> None:
        self._dsn = dsn

    def reserve(
        self,
        *,
        provider: str,
        price_version: str,
        max_spend_micro: int,
        at: datetime,
        reservation_id: str | None = None,
    ) -> PlatformReservation:
        reservation_id = reservation_id or self._new_reservation_id()
        with connect_worker(self._dsn) as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM public.reserve_platform_paid(%s, %s, %s, %s, %s)",
                    (
                        reservation_id,
                        date.fromisoformat(at.astimezone(timezone.utc).strftime("%Y-%m-01")),
                        provider,
                        price_version,
                        max_spend_micro,
                    ),
                ).fetchone()
        if row is None:
            raise PlatformError(ErrorCode.BUDGET_EXCEEDED, "平台月度付费额度不足或已暂停派发")
        return self._from_row(row)

    def mark_in_flight(self, reservation_id: str) -> PlatformReservation:
        return self._transition(
            "mark_platform_paid_in_flight", reservation_id, "预留不在 held 状态"
        )

    def release_before_dispatch(self, reservation_id: str) -> PlatformReservation:
        return self._transition(
            "release_platform_paid_held", reservation_id, "预留不在 held 状态"
        )

    def release_dispatch_failed(self, reservation_id: str) -> PlatformReservation:
        return self._transition(
            "release_platform_paid_failed", reservation_id, "预留不在 in_flight 状态"
        )

    def settle(
        self, reservation_id: str, *, actual_spend_micro: int | None
    ) -> PlatformReservation:
        if actual_spend_micro is None:
            raise PlatformError(
                ErrorCode.RECONCILIATION_REQUIRED,
                "provider 用量未知，必须对账",
            )
        with connect_worker(self._dsn) as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM public.settle_platform_paid(%s, %s)",
                    (reservation_id, actual_spend_micro),
                ).fetchone()
        if row is None:
            raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "预留不在 in_flight 状态或实际费用越界")
        return self._from_row(row)

    def _transition(self, function_name: str, reservation_id: str, message: str) -> PlatformReservation:
        with connect_worker(self._dsn) as conn:
            with conn.transaction():
                row = conn.execute(
                    f"SELECT * FROM public.{function_name}(%s)",
                    (reservation_id,),
                ).fetchone()
        if row is None:
            raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, message)
        return self._from_row(row)

    @staticmethod
    def _from_row(row: tuple) -> PlatformReservation:
        period = row[1]
        period_text = period.strftime("%Y-%m") if hasattr(period, "strftime") else str(period)
        return PlatformReservation(
            reservation_id=row[0],
            billing_period=period_text,
            provider=row[2],
            price_version=row[3],
            max_spend_micro=int(row[4]),
            state=PlatformReservationState(row[5]),
            actual_spend_micro=None if row[6] is None else int(row[6]),
        )

    @staticmethod
    def _new_reservation_id() -> str:
        import uuid

        return f"paid_{uuid.uuid4().hex[:24]}"
