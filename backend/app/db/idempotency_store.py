"""PostgreSQL HTTP command idempotency adapter."""

from __future__ import annotations

import json

from app.core.ids import new_id
from app.db.session import connect
from app.identity.idempotency import (
    IDEMPOTENCY_LEASE_SECONDS,
    ClaimOutcome,
    _bounded_body,
    _claim_id,
)


class PostgresHttpIdempotencyStore:
    """PostgreSQL 实现。占用的原子性由 UNIQUE (tenant_id, principal_id,
    command_scope, client_key) 保证 —— INSERT 成功即占用成功，
    冲突即别人持有，不需要任何进程内锁。

    租约到期只把 `pending` 原子推进到 `indeterminate`。业务可能已经提交，
    未经对账直接接管会重复副作用。
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn
        self._connect = connect

    @staticmethod
    def _set_rls_context(conn, tenant_id: str, principal_id: str) -> None:
        """在每个方法开头设置 RLS 上下文（铁律 33：有主体列就必须约束主体维度）。

        用会话级 `is_local=false`：连接随 with 块关闭，不会跨请求残留；
        `is_local=true` 要求显式事务块，而本类用 commit/rollback 手工管理，
        两套事务控制混用只会引入新错误。
        """
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
        conn.execute("SELECT set_config('app.principal_id', %s, false)", (principal_id,))

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        owner_token = new_id("idm")
        with self._connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            inserted = conn.execute(
                "INSERT INTO http_idempotency"
                " (claim_id, tenant_id, principal_id, command_scope, client_key,"
                "  request_hash, project_id, state, owner_token)"
                " VALUES (%s, %s, %s, %s, %s, %s, NULL, 'pending', %s)"
                " ON CONFLICT (tenant_id, principal_id, command_scope, client_key)"
                " DO NOTHING"
                " RETURNING claim_id",
                (
                    _claim_id(
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        command_scope=command_scope,
                        client_key=client_key,
                    ),
                    tenant_id, principal_id, command_scope, client_key, fingerprint,
                    owner_token,
                ),
            ).fetchone()
            if inserted is not None:
                conn.commit()
                return ClaimOutcome(kind="claimed", owner_token=owner_token)

            row = conn.execute(
                "SELECT request_hash, state, status_code, response_body"
                " FROM http_idempotency"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s",
                (tenant_id, principal_id, command_scope, client_key),
            ).fetchone()
            assert row is not None, "唯一键冲突却查不到行 —— 并发窗口内行被删，需复查"
            request_hash, state_name, status_code, response_body = row
            if request_hash != fingerprint:
                conn.rollback()
                return ClaimOutcome(kind="violation")
            if state_name == "completed":
                conn.commit()
                return ClaimOutcome(
                    kind="replay",
                    cached_status_code=status_code,
                    cached_body=response_body,
                )
            if state_name == "indeterminate":
                conn.commit()
                return ClaimOutcome(kind="reconciliation_required")
            if state_name == "released":
                # 释放后的重试 = 重新执行：CAS 到 pending（仍以唯一键防并发抢占）。
                updated = conn.execute(
                    "UPDATE http_idempotency SET state = 'pending', owner_token = %s,"
                    " claimed_at = now()"
                    " WHERE tenant_id = %s AND principal_id = %s"
                    "   AND command_scope = %s AND client_key = %s"
                    "   AND state = 'released'"
                    " RETURNING claim_id",
                    (owner_token, tenant_id, principal_id, command_scope, client_key),
                ).fetchone()
                conn.commit()
                if updated is not None:
                    return ClaimOutcome(kind="claimed", owner_token=owner_token)
                return ClaimOutcome(kind="in_progress")

            # pending 超时只能说明持有者失联，不能证明业务未提交。
            # 原子进入 indeterminate，禁止后来者盲目重执行。
            indeterminate = conn.execute(
                "UPDATE http_idempotency SET state = 'indeterminate', owner_token = NULL"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s"
                "   AND state = 'pending'"
                "   AND claimed_at < now() - make_interval(secs => %s)"
                " RETURNING claim_id",
                (
                    tenant_id, principal_id, command_scope, client_key,
                    float(IDEMPOTENCY_LEASE_SECONDS),
                ),
            ).fetchone()
            conn.commit()
            if indeterminate is not None:
                return ClaimOutcome(kind="reconciliation_required")
            return ClaimOutcome(kind="in_progress")

    def complete(
        self, *, tenant_id: str, principal_id: str, command_scope: str,
        client_key: str, status_code: int, response_body: dict,
        owner_token: str | None = None,
    ) -> None:
        with self._connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            # owner_token 校验：占用不属于当前持有者或已经进入不确定态时，
            # 迟到的 complete 不得改写结果。
            conn.execute(
                "UPDATE http_idempotency"
                " SET state = 'completed', status_code = %s, response_body = %s,"
                "     completed_at = now()"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s AND state = 'pending'"
                "   AND owner_token IS NOT DISTINCT FROM %s",
                (
                    status_code,
                    json.dumps(_bounded_body(response_body), ensure_ascii=False, sort_keys=True),
                    tenant_id, principal_id, command_scope, client_key, owner_token,
                ),
            )
            conn.commit()

    def release(
        self, *, tenant_id: str, principal_id: str, command_scope: str,
        client_key: str, owner_token: str | None = None,
    ) -> None:
        with self._connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            conn.execute(
                "UPDATE http_idempotency SET state = 'released'"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s AND state = 'pending'"
                "   AND owner_token IS NOT DISTINCT FROM %s",
                (tenant_id, principal_id, command_scope, client_key, owner_token),
            )
            conn.commit()


__all__ = ["PostgresHttpIdempotencyStore"]
