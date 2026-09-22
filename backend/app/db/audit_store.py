"""PostgreSQL transactional audit outbox wiring."""

from psycopg.types.json import Json

from app.audit.outbox import PostgresAuditOutbox as _OutboxCore
from app.audit.sink import AuditSink
from app.db.session import connect


class PostgresAuditOutbox(_OutboxCore):
    def __init__(self, sink: AuditSink, dsn: str | None = None) -> None:
        super().__init__(
            sink,
            dsn,
            connect_factory=connect,
            json_factory=Json,
        )


__all__ = ["PostgresAuditOutbox"]
