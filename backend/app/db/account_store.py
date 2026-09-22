"""PostgreSQL account adapter for migration 0012 SECURITY DEFINER functions."""

from __future__ import annotations

from datetime import datetime

from psycopg import errors as pg_errors

from app.core.errors import ErrorCode, deny
from app.db.session import connect
from app.identity.models import Principal, UserSession
from app.identity.ports import AccountLookup, AccountRegistrationResult


class PostgresAccountRepository:
    def __init__(self, *, sessions, dsn: str | None = None, clock=None) -> None:
        self._sessions = sessions
        self._dsn = dsn
        self._clock = clock

    def lookup(self, username_normalized: str) -> AccountLookup | None:
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT * FROM public.lookup_account_for_login(%s)",
                (username_normalized,),
            ).fetchone()
        if row is None:
            return None
        return AccountLookup(*row)

    def register(
        self,
        *,
        username_original: str,
        username_normalized: str,
        password_hash: str,
        hash_version: int,
        session_expires_at: datetime,
    ) -> AccountRegistrationResult:
        try:
            with connect(self._dsn) as conn:
                row = conn.execute(
                    "SELECT * FROM public.register_account(%s, %s, %s, %s, %s)",
                    (
                        username_original,
                        username_normalized,
                        password_hash,
                        hash_version,
                        session_expires_at,
                    ),
                ).fetchone()
                conn.commit()
        except pg_errors.UniqueViolation as exc:
            raise deny(ErrorCode.USERNAME_TAKEN, "用户名已注册") from exc
        if row is None:
            raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "注册事务未返回账号")
        credential_id, session_id, tenant_id, principal_id, expires_at, generation, project_id, project_name = row
        session = self._sessions.get_live(
            Principal(principal_id=principal_id, tenant_id=tenant_id),
            session_id,
        )
        if session is None:
            raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "注册会话不可见")
        lookup = self.lookup(username_normalized)
        if lookup is None:
            raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "注册凭据不可见")
        return AccountRegistrationResult(
            lookup=lookup,
            session=session,
            default_project_id=project_id,
            default_project_name=project_name,
        )

    def complete_login(
        self,
        *,
        lookup: AccountLookup,
        session_expires_at: datetime,
        new_password_hash: str | None = None,
        new_hash_version: int | None = None,
    ) -> UserSession | None:
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT * FROM public.complete_account_login(%s, %s, %s, %s, %s)",
                (
                    lookup.credential_id,
                    lookup.security_generation,
                    session_expires_at,
                    new_password_hash,
                    new_hash_version,
                ),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        session_id, tenant_id, principal_id, expires_at, generation = row
        session = self._sessions.get_live(
            Principal(principal_id=principal_id, tenant_id=tenant_id),
            session_id,
        )
        if session is None:
            raise deny(ErrorCode.INTERNAL_CONSISTENCY_ERROR, "登录会话不可见")
        return session
