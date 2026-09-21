"""Bootstrap a tenant/principal and print one single-use invitation token."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
from app.core.ids import new_id
from app.db.identity_store import PostgresInvitationRepository
from app.identity.ports import SystemContext


def _psycopg_dsn(dsn: str) -> str:
    """Normalize the SQLAlchemy PostgreSQL scheme for direct psycopg use."""
    if dsn.startswith("postgresql+psycopg://"):
        return "postgresql://" + dsn[len("postgresql+psycopg://") :]
    return dsn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="t_first_release")
    parser.add_argument("--tenant-name", default="首版用户")
    parser.add_argument("--principal", default="u_first_release")
    parser.add_argument("--display-name", default="首版用户")
    parser.add_argument("--expires-hours", type=int, default=24)
    args = parser.parse_args()
    if args.expires_hours <= 0 or args.expires_hours > 168:
        parser.error("--expires-hours 必须在 1 到 168 之间")

    migration_dsn = os.environ.get("STUDY_PLATFORM_MIGRATION_DSN", "")
    app_dsn = os.environ.get("STUDY_PLATFORM_DSN", "")
    if not migration_dsn or not app_dsn:
        raise SystemExit("需要 STUDY_PLATFORM_MIGRATION_DSN 和 STUDY_PLATFORM_DSN")

    with psycopg.connect(_psycopg_dsn(migration_dsn)) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET name = EXCLUDED.name",
            (args.tenant, args.tenant_name),
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (principal_id) DO UPDATE SET display_name = EXCLUDED.display_name",
            (args.principal, args.tenant, args.display_name),
        )

    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    repository = PostgresInvitationRepository(dsn=app_dsn)
    repository.issue(
        SystemContext(args.tenant, "production invitation bootstrap"),
        invitation_id=new_id("inv"),
        token_hash="sha256:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        issued_by=args.principal,
        invitee_principal_id=args.principal,
        issued_at=now,
        expires_at=now + timedelta(hours=args.expires_hours),
    )
    print(raw_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
