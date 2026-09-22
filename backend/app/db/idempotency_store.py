"""PostgreSQL HTTP idempotency adapter wiring."""

from app.api.http_idempotency import PostgresHttpIdempotencyStore as _StoreCore
from app.db.session import connect


class PostgresHttpIdempotencyStore(_StoreCore):
    def __init__(self, dsn: str | None = None) -> None:
        super().__init__(dsn, connect_factory=connect)

__all__ = ["PostgresHttpIdempotencyStore"]
