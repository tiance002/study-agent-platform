"""Read only aggregate snapshots for the protected Web metrics endpoint."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.db.session import connect


class PostgresMetricsStore:
    """Call the restricted aggregate function; never select source tables directly."""

    def __init__(
        self, *, dsn: str | None = None,
        cache_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dsn = dsn
        self._cache_seconds = cache_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._monotonic()
            if self._cached is not None and now - self._cached_at < self._cache_seconds:
                return self._cached
            with connect(self._dsn) as conn:
                row = conn.execute("SELECT public.study_metrics_snapshot()").fetchone()
            if row is None or not isinstance(row[0], dict):
                raise RuntimeError("metrics snapshot unavailable")
            self._cached = row[0]
            self._cached_at = self._monotonic()
            return self._cached
