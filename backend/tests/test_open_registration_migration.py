"""0012 open-registration migration: schema, constraints, and privilege boundary.

These tests intentionally stay below the HTTP/repository layers.  The migration is
the security boundary for credentials, session provenance, and the three
unauthenticated SECURITY DEFINER entry points.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pg_support
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "0012_open_registration_auth.py"


def _reachable() -> bool:
    return pg_support.reachable()


PG_ONLY = pytest.mark.skipif(
    not _reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)


def _run_alembic(dsn: str, *arguments: str, check: bool = True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env={**os.environ, "STUDY_PLATFORM_MIGRATION_DSN": dsn},
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError((result.stdout or "") + (result.stderr or ""))
    return result


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0012", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0012_declares_frozen_schema_and_security_contract():
    """The revision exposes every frozen database boundary in one auditable file."""

    module = _migration_module()
    source = MIGRATION.read_text(encoding="utf-8")

    assert module.revision == "0012"
    assert module.down_revision == "0011"
    for required in (
        "account_credentials",
        'username_normalized text COLLATE "C"',
        "password_hash",
        "hash_version",
        "security_generation",
        "disabled_at",
        "auth_method",
        "credential_id",
        "register_account",
        "lookup_account_for_login",
        "complete_account_login",
        "SECURITY DEFINER",
        "SET search_path = pg_catalog",
        "REVOKE ALL ON FUNCTION",
        "GRANT EXECUTE ON FUNCTION",
        "default_project_id",
        "default_project_name",
    ):
        assert required in source

    # Registration must never accept caller-supplied identity or ownership IDs.
    register_sql = module._register_account_function()
    signature = register_sql.split("RETURNS TABLE", 1)[0]
    assert "p_tenant_id" not in signature
    assert "p_principal_id" not in signature
    assert "p_session_id" not in signature
    assert "p_project_id" not in signature


def test_0012_statements_abort_unknown_session_provenance_before_backfill():
    module = _migration_module()
    statements = "\n".join(module._session_upgrade_statements())

    provenance_guard = statements.index("unverified pre-0012 user_sessions")
    backfill = statements.index("SET auth_method = 'invitation'")
    not_null = statements.index("ALTER COLUMN auth_method SET NOT NULL")
    assert provenance_guard < backfill < not_null
    assert "consumed_by = invitee_principal_id" in statements
    assert "invitation_count" in statements
    assert "s.session_count > COALESCE(i.invitation_count, 0)" in statements


def test_0012_session_constraints_bind_password_sessions_to_same_principal():
    module = _migration_module()
    statements = "\n".join(module._session_upgrade_statements())

    assert "user_sessions_auth_shape_check" in statements
    assert "auth_method = 'invitation' AND credential_id IS NULL" in statements
    assert "auth_method = 'password' AND credential_id IS NOT NULL" in statements
    assert "FOREIGN KEY (tenant_id, principal_id, credential_id)" in statements
    assert (
        "REFERENCES account_credentials (tenant_id, principal_id, credential_id)"
        in statements
    )


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_0012_credentials_are_only_reachable_through_app_definer_functions():
    signatures = (
        "register_account(text,text,text,integer,timestamp with time zone)",
        "lookup_account_for_login(text)",
        "complete_account_login(text,bigint,timestamp with time zone,text,integer)",
    )
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        columns = conn.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'account_credentials'
            """
        ).fetchall()
        assert {row[0] for row in columns} >= {
            "credential_id",
            "tenant_id",
            "principal_id",
            "username",
            "username_normalized",
            "password_hash",
            "hash_version",
            "security_generation",
            "disabled_at",
        }
        index_definition, index_collation = conn.execute(
            """
            SELECT pg_get_indexdef(i.indexrelid), coll.collname
              FROM pg_index AS i
              JOIN pg_class AS idx ON idx.oid = i.indexrelid
              JOIN pg_namespace AS ns ON ns.oid = idx.relnamespace
              JOIN pg_collation AS coll ON coll.oid = i.indcollation[0]
             WHERE ns.nspname = 'public'
               AND idx.relname = 'account_credentials_username_normalized_uq'
            """
        ).fetchone()
        assert "UNIQUE INDEX" in index_definition
        # PostgreSQL omits a redundant COLLATE clause from pg_get_indexdef when
        # the indexed column itself is COLLATE "C".  indcollation is the actual
        # execution metadata and therefore the contract to assert.
        assert index_collation == "C"

        for role in ("study_app", "study_worker"):
            assert conn.execute(
                "SELECT has_table_privilege(%s, 'public.account_credentials', 'SELECT')",
                (role,),
            ).fetchone()[0] is False
        for signature in signatures:
            app_exec, worker_exec, search_path, public_exec = conn.execute(
                """
                SELECT
                    has_function_privilege('study_app', p.oid, 'EXECUTE'),
                    has_function_privilege('study_worker', p.oid, 'EXECUTE'),
                    p.proconfig,
                    EXISTS (
                        SELECT 1 FROM aclexplode(p.proacl) acl
                         WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                    )
                  FROM pg_proc p
                 WHERE p.oid = %s::regprocedure
                """,
                ("public." + signature,),
            ).fetchone()
            assert app_exec is True
            assert worker_exec is False
            assert search_path == ["search_path=pg_catalog"]
            assert public_exec is False

    for dsn in (pg_support.app_dsn(), pg_support.worker_dsn()):
        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT username FROM public.account_credentials")


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_platform_budget_functions_are_worker_only_and_period_is_database_utc():
    signatures = (
        "reserve_platform_paid(text,date,text,text,bigint)",
        "mark_platform_paid_in_flight(text)",
        "release_platform_paid_held(text)",
        "release_platform_paid_failed(text)",
        "settle_platform_paid(text,bigint)",
    )
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        for signature in signatures:
            app_exec, worker_exec, public_exec = conn.execute(
                """
                SELECT
                    has_function_privilege('study_app', p.oid, 'EXECUTE'),
                    has_function_privilege('study_worker', p.oid, 'EXECUTE'),
                    EXISTS (
                        SELECT 1 FROM aclexplode(p.proacl) acl
                         WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                    )
                  FROM pg_proc p
                 WHERE p.oid = %s::regprocedure
                """,
                ("public." + signature,),
            ).fetchone()
            assert app_exec is False
            assert worker_exec is True
            assert public_exec is False

    with psycopg.connect(pg_support.worker_dsn()) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "SELECT * FROM public.reserve_platform_paid(%s, %s, %s, %s, %s)",
                ("paid_wrong_period", "2000-01-01", "provider", "v1", 1),
            ).fetchone()

    period = datetime.now(timezone.utc).date().replace(day=1)
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        conn.execute(
            "UPDATE platform_budget_config"
            " SET monthly_cap_micro = 100, paid_dispatch_enabled = true"
            " WHERE config_id = true"
        )
        conn.commit()
    with psycopg.connect(pg_support.worker_dsn()) as conn:
        first = conn.execute(
            "SELECT * FROM public.reserve_platform_paid(%s, %s, %s, %s, %s)",
            ("paid_idempotent", period, "provider", "v1", 60),
        ).fetchone()
        retried = conn.execute(
            "SELECT * FROM public.reserve_platform_paid(%s, %s, %s, %s, %s)",
            ("paid_idempotent", period, "provider", "v1", 60),
        ).fetchone()
        assert retried == first
        first_flight = conn.execute(
            "SELECT * FROM public.mark_platform_paid_in_flight(%s)",
            ("paid_idempotent",),
        ).fetchone()
        retried_flight = conn.execute(
            "SELECT * FROM public.mark_platform_paid_in_flight(%s)",
            ("paid_idempotent",),
        ).fetchone()
        assert retried_flight == first_flight
        released = conn.execute(
            "SELECT * FROM public.release_platform_paid_failed(%s)",
            ("paid_idempotent",),
        ).fetchone()
        assert released[5] == "released"
        assert conn.execute(
            "SELECT * FROM public.release_platform_paid_failed(%s)",
            ("paid_idempotent",),
        ).fetchone() == released
        assert conn.execute(
            "SELECT * FROM public.mark_platform_paid_in_flight(%s)",
            ("paid_idempotent",),
        ).fetchone() is None

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        conn.execute(
            "UPDATE platform_budget_config"
            " SET monthly_cap_micro = NULL, paid_dispatch_enabled = true"
            " WHERE config_id = true"
        )
        conn.commit()
    with psycopg.connect(pg_support.worker_dsn()) as conn:
        unlimited = conn.execute(
            "SELECT * FROM public.reserve_platform_paid(%s, %s, %s, %s, %s)",
            ("paid_unlimited", period, "provider", "v1", 10**12),
        ).fetchone()
        assert unlimited is not None
        assert unlimited[4] == 10**12


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_register_account_generates_identity_default_project_and_session_atomically():
    suffix = uuid.uuid4().hex
    username = "注册" + suffix[:6]
    normalized = "注册" + suffix[:6]
    password_hash = "$argon2id$v=19$m=19456,t=2,p=1$test$hash"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)

    with psycopg.connect(pg_support.app_dsn()) as conn:
        registered = conn.execute(
            "SELECT * FROM public.register_account(%s, %s, %s, %s, %s)",
            (username, normalized, password_hash, 1, expires_at),
        ).fetchone()
        assert registered is not None
        (
            credential_id,
            session_id,
            tenant_id,
            principal_id,
            returned_expiry,
            generation,
            project_id,
            project_name,
        ) = registered
        assert returned_expiry == expires_at
        assert generation == 1
        assert project_name == "我的学习项目"

        lookup = conn.execute(
            "SELECT * FROM public.lookup_account_for_login(%s)", (normalized,)
        ).fetchone()
        assert lookup == (
            credential_id,
            tenant_id,
            principal_id,
            username,
            password_hash,
            1,
            1,
            None,
        )

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        principal = conn.execute(
            "SELECT tenant_id, display_name FROM principals WHERE principal_id = %s",
            (principal_id,),
        ).fetchone()
        assert principal == (tenant_id, username)
        project = conn.execute(
            "SELECT tenant_id, name FROM projects WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        assert project == (tenant_id, project_name)
        grant = conn.execute(
            """
            SELECT tenant_id FROM project_grants
             WHERE principal_id = %s AND project_id = %s
            """,
            (principal_id, project_id),
        ).fetchone()
        assert grant == (tenant_id,)
        session = conn.execute(
            """
            SELECT auth_method, credential_id, security_generation
              FROM user_sessions WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone()
        assert session == ("password", credential_id, 1)

    with psycopg.connect(pg_support.app_dsn()) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
            conn.execute(
                "SELECT * FROM public.register_account(%s, %s, %s, %s, %s)",
                (username + "x", normalized, password_hash, 1, expires_at),
            )
        assert excinfo.value.diag.constraint_name == (
            "account_credentials_username_normalized_uq"
        )

    # The uniqueness failure happens after tenant/principal/project/grant writes.
    # The definer function's exception block must roll those earlier writes back.
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM tenants WHERE name = %s", (username + "x",)
        ).fetchone()[0] == 0


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_concurrent_normalized_username_registration_creates_one_complete_account():
    suffix = uuid.uuid4().hex[:6]
    normalized = "并发" + suffix
    raw_usernames = ("并发甲" + suffix, "并发乙" + suffix)
    password_hash = "$argon2id$v=19$m=19456,t=2,p=1$test$hash"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    barrier = threading.Barrier(2)

    def register(raw_username: str):
        with psycopg.connect(pg_support.app_dsn()) as conn:
            barrier.wait(timeout=5)
            try:
                row = conn.execute(
                    "SELECT * FROM public.register_account(%s, %s, %s, 1, %s)",
                    (raw_username, normalized, password_hash, expires_at),
                ).fetchone()
                return "created", row
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                return "duplicate", exc.diag.constraint_name

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, raw_usernames))

    assert sorted(result[0] for result in results) == ["created", "duplicate"]
    duplicate = next(result for result in results if result[0] == "duplicate")
    assert duplicate[1] == "account_credentials_username_normalized_uq"
    created = next(result[1] for result in results if result[0] == "created")
    tenant_id = created[2]

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM account_credentials WHERE username_normalized = %s",
            (normalized,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM tenants WHERE name = ANY(%s)",
            (list(raw_usernames),),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM principals WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM projects WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM project_grants WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT count(*) FROM user_sessions
             WHERE tenant_id = %s AND auth_method = 'password'
            """,
            (tenant_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT count(*) FROM auth_audit_outbox
             WHERE tenant_id = %s AND event_type = 'account_registered'
            """,
            (tenant_id,),
        ).fetchone()[0] == 1


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_password_session_constraints_and_login_completion_enforce_binding():
    suffix = uuid.uuid4().hex[:6]
    password_hash = "$argon2id$v=19$m=19456,t=2,p=1$test$hash"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    accounts = []
    with psycopg.connect(pg_support.app_dsn()) as conn:
        for marker in ("甲", "乙"):
            accounts.append(
                conn.execute(
                    "SELECT * FROM public.register_account(%s, %s, %s, 1, %s)",
                    (marker + suffix, marker + suffix, password_hash, expires_at),
                ).fetchone()
            )

    first, second = accounts
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        bad_id = "sess_bad_" + uuid.uuid4().hex
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO user_sessions (
                    session_id, tenant_id, principal_id, issued_at, expires_at,
                    auth_method, credential_id, security_generation
                ) VALUES (%s, %s, %s, now(), %s, 'invitation', %s, 1)
                """,
                (bad_id, first[2], first[3], expires_at, first[0]),
            )
        conn.rollback()

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO user_sessions (
                    session_id, tenant_id, principal_id, issued_at, expires_at,
                    auth_method, credential_id, security_generation
                ) VALUES (%s, %s, %s, now(), %s, 'password', NULL, 1)
                """,
                (bad_id, first[2], first[3], expires_at),
            )
        conn.rollback()

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                INSERT INTO user_sessions (
                    session_id, tenant_id, principal_id, issued_at, expires_at,
                    auth_method, credential_id, security_generation
                ) VALUES (%s, %s, %s, now(), %s, 'password', %s, 1)
                """,
                (bad_id, second[2], second[3], expires_at, first[0]),
            )
        conn.rollback()

    with psycopg.connect(pg_support.app_dsn()) as conn:
        stale = conn.execute(
            "SELECT * FROM public.complete_account_login(%s, %s, %s, NULL, NULL)",
            (first[0], 999, expires_at),
        ).fetchone()
        assert stale is None
        completed = conn.execute(
            "SELECT * FROM public.complete_account_login(%s, %s, %s, NULL, NULL)",
            (first[0], 1, expires_at),
        ).fetchone()
        assert completed is not None
        assert completed[1:3] == (first[2], first[3])

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        conn.execute(
            "UPDATE account_credentials SET disabled_at = now() WHERE credential_id = %s",
            (first[0],),
        )
    with psycopg.connect(pg_support.app_dsn()) as conn:
        assert conn.execute(
            "SELECT * FROM public.complete_account_login(%s, 1, %s, NULL, NULL)",
            (first[0], expires_at),
        ).fetchone() is None


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_existing_invitation_exchange_creates_explicit_invitation_session():
    suffix = uuid.uuid4().hex
    tenant_id = "t_inv_" + suffix
    principal_id = "u_inv_" + suffix
    token_hash = "hash_" + suffix
    session_id = "sess_inv_" + suffix
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, 'invitation')",
            (tenant_id,),
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
            (principal_id, tenant_id),
        )
        conn.execute(
            """
            INSERT INTO invitations (
                invitation_id, tenant_id, token_hash, issued_by,
                invitee_principal_id, issued_at, expires_at
            ) VALUES (%s, %s, %s, %s, %s, now(), %s)
            """,
            (
                "inv_" + suffix,
                tenant_id,
                token_hash,
                principal_id,
                principal_id,
                expires_at,
            ),
        )

    with psycopg.connect(pg_support.app_dsn()) as conn:
        exchanged = conn.execute(
            "SELECT * FROM public.exchange_invitation(%s, %s, %s)",
            (token_hash, session_id, expires_at),
        ).fetchone()
        assert exchanged == (session_id, tenant_id, principal_id, expires_at)

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        assert conn.execute(
            """
            SELECT auth_method, credential_id, security_generation
              FROM user_sessions WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == ("invitation", None, 1)


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_0012_migration_cycle_preserves_verified_invitation_provenance():
    database = pg_support.create_test_database()
    try:
        _run_alembic(database.migration_dsn, "downgrade", "0011")
        issued_at = datetime.now(timezone.utc)
        tenant_id = "t_cycle_" + uuid.uuid4().hex
        principal_id = "u_cycle_" + uuid.uuid4().hex
        session_id = "sess_cycle_" + uuid.uuid4().hex
        with psycopg.connect(database.migration_dsn) as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, 'cycle')",
                (tenant_id,),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
                (principal_id, tenant_id),
            )
            conn.execute(
                """
                INSERT INTO invitations (
                    invitation_id, tenant_id, token_hash, issued_by, invitee_principal_id,
                    issued_at, expires_at, consumed_at, consumed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "inv_" + uuid.uuid4().hex,
                    tenant_id,
                    "hash_" + uuid.uuid4().hex,
                    principal_id,
                    principal_id,
                    issued_at - timedelta(hours=1),
                    issued_at + timedelta(hours=1),
                    issued_at,
                    principal_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO user_sessions (
                    session_id, tenant_id, principal_id, issued_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    tenant_id,
                    principal_id,
                    issued_at,
                    issued_at + timedelta(hours=1),
                ),
            )

        _run_alembic(database.migration_dsn, "upgrade", "0012")
        with psycopg.connect(database.migration_dsn) as conn:
            assert conn.execute(
                """
                SELECT auth_method, credential_id, security_generation
                  FROM user_sessions WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone() == ("invitation", None, 1)
        _run_alembic(database.migration_dsn, "downgrade", "0011")
        _run_alembic(database.migration_dsn, "upgrade", "0012")
    finally:
        pg_support.drop_test_database(database)


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_0012_migration_aborts_on_unknown_existing_session_source():
    database = pg_support.create_test_database()
    try:
        _run_alembic(database.migration_dsn, "downgrade", "0011")
        tenant_id = "t_unknown_" + uuid.uuid4().hex
        principal_id = "u_unknown_" + uuid.uuid4().hex
        issued_at = datetime.now(timezone.utc)
        with psycopg.connect(database.migration_dsn) as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, 'unknown')",
                (tenant_id,),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
                (principal_id, tenant_id),
            )
            conn.execute(
                """
                INSERT INTO user_sessions (
                    session_id, tenant_id, principal_id, issued_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    "sess_unknown_" + uuid.uuid4().hex,
                    tenant_id,
                    principal_id,
                    issued_at,
                    issued_at + timedelta(hours=1),
                ),
            )

        failed = _run_alembic(
            database.migration_dsn, "upgrade", "0012", check=False
        )
        assert failed.returncode != 0
        assert "unverified pre-0012 user_sessions" in (
            (failed.stdout or "") + (failed.stderr or "")
        )
        with psycopg.connect(database.migration_dsn) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0011"
            assert conn.execute(
                """
                SELECT count(*) FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'user_sessions'
                   AND column_name = 'auth_method'
                """
            ).fetchone()[0] == 0
    finally:
        pg_support.drop_test_database(database)


@PG_ONLY
@pytest.mark.postgres
@pytest.mark.invariant
def test_0012_migration_aborts_when_one_invitation_could_mask_two_sessions():
    """Historical provenance must be one-to-one, not merely an EXISTS match."""

    database = pg_support.create_test_database()
    try:
        _run_alembic(database.migration_dsn, "downgrade", "0011")
        tenant_id = "t_ambiguous_" + uuid.uuid4().hex
        principal_id = "u_ambiguous_" + uuid.uuid4().hex
        issued_at = datetime.now(timezone.utc)
        with psycopg.connect(database.migration_dsn) as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, 'ambiguous')",
                (tenant_id,),
            )
            conn.execute(
                "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
                (principal_id, tenant_id),
            )
            conn.execute(
                """
                INSERT INTO invitations (
                    invitation_id, tenant_id, token_hash, issued_by,
                    invitee_principal_id, issued_at, expires_at,
                    consumed_at, consumed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "inv_" + uuid.uuid4().hex,
                    tenant_id,
                    "hash_" + uuid.uuid4().hex,
                    principal_id,
                    principal_id,
                    issued_at - timedelta(hours=1),
                    issued_at + timedelta(hours=1),
                    issued_at,
                    principal_id,
                ),
            )
            for marker in ("a", "b"):
                conn.execute(
                    """
                    INSERT INTO user_sessions (
                        session_id, tenant_id, principal_id, issued_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        f"sess_{marker}_" + uuid.uuid4().hex,
                        tenant_id,
                        principal_id,
                        issued_at,
                        issued_at + timedelta(hours=1),
                    ),
                )

        failed = _run_alembic(
            database.migration_dsn, "upgrade", "0012", check=False
        )
        assert failed.returncode != 0
        assert "unverified pre-0012 user_sessions" in (
            (failed.stdout or "") + (failed.stderr or "")
        )
    finally:
        pg_support.drop_test_database(database)
