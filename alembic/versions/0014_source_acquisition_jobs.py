"""Persist explicit source candidates and bounded external acquisition jobs.

Search results are project-scoped candidates, not facts.  A user selection
creates one queued acquisition job; only the worker may claim and settle it.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"
WORKER_ROLE = "study_worker"
TABLES = ["source_candidates", "acquisition_jobs"]
PROJECT_SCOPED = {"source_candidates", "acquisition_jobs"}
NO_DELETE = {"source_candidates", "acquisition_jobs"}
APP_ROLE_GRANTS_OVERRIDES = {"acquisition_jobs": "SELECT, INSERT"}
WORKER_ROLE_GRANTS = {
    "source_candidates": "SELECT, INSERT, UPDATE",
    "acquisition_jobs": "SELECT, UPDATE",
}


def _assert_roles_exist() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; create it before migrating';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
        RAISE EXCEPTION 'role {WORKER_ROLE} does not exist; create it before migrating';
    END IF;
END
$$
"""
    )


def _create_tables() -> list[str]:
    return [
        """
CREATE TABLE source_candidates (
    candidate_id    text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL,
    url             text NOT NULL,
    title           text NOT NULL,
    snippet         text NOT NULL DEFAULT '',
    source_domain   text NOT NULL,
    status          text NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'selected', 'rejected', 'expired')),
    discovered_at   timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    UNIQUE (tenant_id, project_id, candidate_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    CHECK (expires_at > discovered_at)
)
""",
        """
CREATE TABLE acquisition_jobs (
    acquisition_id  text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants (tenant_id),
    project_id      text NOT NULL,
    source_id       text NOT NULL,
    candidate_id    text NOT NULL,
    requested_by    text NOT NULL,
    url             text NOT NULL,
    title           text NOT NULL,
    media_type      text NOT NULL
        CHECK (media_type IN ('text/plain', 'text/markdown', 'text/html')),
    language        text NOT NULL,
    idempotency_key text NOT NULL,
    status          text NOT NULL
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'unknown')),
    attempt_count   integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner     text,
    lease_until     timestamptz,
    claim_token     uuid,
    error_code      text NOT NULL DEFAULT '',
    error_detail    text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_id, idempotency_key),
    UNIQUE (tenant_id, project_id, acquisition_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id),
    FOREIGN KEY (tenant_id, project_id, candidate_id)
        REFERENCES source_candidates (tenant_id, project_id, candidate_id),
    CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL AND claim_token IS NOT NULL)),
    CHECK ((status IN ('failed', 'unknown')) = (error_code <> ''))
)
""",
    ]


def _rls(table: str) -> list[str]:
    predicate = (
        "tenant_id = current_setting('app.tenant_id', true)\n"
        "       AND project_id = current_setting('app.project_id', true)"
    )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _worker_policy() -> str:
    predicate = "NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL"
    return (
        "CREATE POLICY acquisition_jobs_worker ON acquisition_jobs\n"
        f"    TO {WORKER_ROLE}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    _assert_roles_exist()
    for statement in _create_tables():
        op.execute(statement)
    for table in TABLES:
        for statement in _rls(table):
            op.execute(statement)
    op.execute(_worker_policy())
    op.execute(
        "CREATE INDEX source_candidates_scope_idx"
        " ON source_candidates (tenant_id, project_id, discovered_at DESC)"
    )
    op.execute(
        "CREATE INDEX acquisition_jobs_claim_idx"
        " ON acquisition_jobs (created_at, acquisition_id)"
        " WHERE status IN ('queued', 'running')"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON source_candidates TO study_app")
    op.execute("GRANT SELECT, INSERT ON acquisition_jobs TO study_app")
    op.execute("GRANT USAGE ON SCHEMA public TO study_worker")
    for table, privileges in WORKER_ROLE_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {WORKER_ROLE}")
    op.execute("REVOKE UPDATE ON acquisition_jobs FROM study_app")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS acquisition_jobs_claim_idx")
    op.execute("DROP INDEX IF EXISTS source_candidates_scope_idx")
    op.execute("REVOKE ALL ON acquisition_jobs FROM study_worker")
    op.execute("REVOKE ALL ON source_candidates FROM study_worker")
    op.execute("DROP POLICY IF EXISTS acquisition_jobs_worker ON acquisition_jobs")
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
    op.execute("DROP TABLE IF EXISTS acquisition_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS source_candidates CASCADE")
