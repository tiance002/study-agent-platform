"""占位重导出：PostgreSQL 幂等适配器实现在 api.http_idempotency（与守卫同文件，
因为 claim 的翻译逻辑与错误语义强耦合，拆两处只会让"内存/PG 行为一致"
变成两份代码的口头约定）。

本文件保留导入路径的稳定：`from app.db.idempotency_store import ...`。
"""

from app.api.http_idempotency import PostgresHttpIdempotencyStore

__all__ = ["PostgresHttpIdempotencyStore"]
