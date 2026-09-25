"""Persist per-claim fetch observations and expose closed aggregate metrics."""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _assert_roles_exist() -> None:
    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'study_app') THEN
        RAISE EXCEPTION 'role study_app does not exist; create it before migrating';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'study_worker') THEN
        RAISE EXCEPTION 'role study_worker does not exist; create it before migrating';
    END IF;
END
$$
"""
    )


def _metrics_function_sql() -> str:
    return r"""
CREATE FUNCTION public.study_metrics_snapshot()
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH
job_totals AS (
    SELECT
        COALESCE(sum(attempt_count), 0)::bigint AS claims_total,
        count(*) FILTER (WHERE status = 'succeeded')::bigint AS succeeded,
        count(*) FILTER (WHERE status = 'failed')::bigint AS failed,
        count(*) FILTER (WHERE status = 'unknown')::bigint AS unknown,
        count(*) FILTER (WHERE status = 'queued')::bigint AS queued,
        count(*) FILTER (WHERE status = 'running')::bigint AS running,
        count(*)::bigint AS all_jobs
    FROM public.acquisition_jobs
),
fetch_duration AS (
    SELECT COALESCE(jsonb_agg(row_value ORDER BY row_value->>'outcome'), '[]'::jsonb) AS rows
    FROM (
      SELECT jsonb_build_object(
            'outcome', outcome,
            'bucket_counts', jsonb_build_object(
                '0.1', count(*) FILTER (WHERE duration_seconds <= 0.1),
                '0.25', count(*) FILTER (WHERE duration_seconds <= 0.25),
                '0.5', count(*) FILTER (WHERE duration_seconds <= 0.5),
                '1', count(*) FILTER (WHERE duration_seconds <= 1),
                '2.5', count(*) FILTER (WHERE duration_seconds <= 2.5),
                '5', count(*) FILTER (WHERE duration_seconds <= 5),
                '10', count(*) FILTER (WHERE duration_seconds <= 10),
                '30', count(*) FILTER (WHERE duration_seconds <= 30),
                '+Inf', count(*)
            ),
            'count', count(*),
            'sum', COALESCE(sum(duration_seconds), 0)
      ) AS row_value
      FROM public.acquisition_fetch_observations
      WHERE duration_seconds IS NOT NULL
      GROUP BY outcome
    ) AS grouped_fetch_durations
),
fetch_bytes AS (
    SELECT COALESCE(jsonb_agg(row_value ORDER BY row_value->>'outcome'), '[]'::jsonb) AS rows
    FROM (
      SELECT jsonb_build_object(
            'outcome', outcome,
            'bucket_counts', jsonb_build_object(
                '1024', count(*) FILTER (WHERE response_body_bytes <= 1024),
                '4096', count(*) FILTER (WHERE response_body_bytes <= 4096),
                '16384', count(*) FILTER (WHERE response_body_bytes <= 16384),
                '65536', count(*) FILTER (WHERE response_body_bytes <= 65536),
                '262144', count(*) FILTER (WHERE response_body_bytes <= 262144),
                '1048576', count(*) FILTER (WHERE response_body_bytes <= 1048576),
                '+Inf', count(*)
            ),
            'count', count(*),
            'sum', COALESCE(sum(response_body_bytes), 0)
      ) AS row_value
      FROM public.acquisition_fetch_observations
      WHERE response_body_bytes IS NOT NULL
      GROUP BY outcome
    ) AS grouped_fetch_bytes
),
route_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object(
            'query_rewrite_status', routing_decision->>'query_rewrite_status',
            'reason_code', routing_decision->>'reason_code',
            'count', decision_count
        ) ORDER BY routing_decision->>'query_rewrite_status', routing_decision->>'reason_code'
    ) AS rows
    FROM (
        SELECT routing_decision, count(*)::bigint AS decision_count
        FROM public.teaching_runs
        WHERE routing_decision IS NOT NULL
        GROUP BY routing_decision
    ) AS grouped_routes
),
provider_base AS (
    SELECT
        p.provider_family AS provider,
        p.status AS attempt_status,
        p.input_tokens,
        p.output_tokens,
        CASE
            WHEN p.result_payload->>'provider_status' IN
                ('completed', 'refused', 'dispatch_failed', 'timeout', 'malformed', 'truncated')
                THEN p.result_payload->>'provider_status'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) = 'PROVIDER_DISPATCH_FAILED'
                THEN 'dispatch_failed'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                ('PROVIDER_REFUSED', 'PROVIDER_REFUSED_USAGE_UNKNOWN') THEN 'refused'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                ('PROVIDER_MALFORMED', 'PROVIDER_MALFORMED_USAGE_UNKNOWN') THEN 'malformed'
            WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                ('PROVIDER_TRUNCATED', 'PROVIDER_TRUNCATED_USAGE_UNKNOWN') THEN 'truncated'
            WHEN r.error_code = 'PROVIDER_TIMEOUT' THEN 'timeout'
            ELSE 'unknown'
        END AS outcome,
        CASE
            WHEN p.input_tokens IS NOT NULL AND p.output_tokens IS NOT NULL THEN 'reported'
            WHEN p.input_tokens IS NULL AND p.output_tokens IS NULL
                 AND p.status IN ('completed', 'failed') THEN 'missing'
            ELSE 'unknown'
        END AS usage_state
    FROM public.provider_attempts AS p
    JOIN public.teaching_runs AS r
      ON r.tenant_id = p.tenant_id
     AND r.project_id = p.project_id
     AND r.run_id = p.run_id
),
provider_attempt_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object('provider', provider, 'outcome', outcome, 'count', attempt_count)
        ORDER BY provider, outcome
    ) AS rows
    FROM (
        SELECT provider, outcome, count(*)::bigint AS attempt_count
        FROM provider_base
        GROUP BY provider, outcome
    ) AS grouped_attempts
),
provider_token_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object('provider', provider, 'direction', direction, 'count', token_count)
        ORDER BY provider, direction
    ) AS rows
    FROM (
        SELECT provider, 'input'::text AS direction, sum(input_tokens)::bigint AS token_count
        FROM provider_base
        WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL
        GROUP BY provider
        UNION ALL
        SELECT provider, 'output'::text AS direction, sum(output_tokens)::bigint AS token_count
        FROM provider_base
        WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL
        GROUP BY provider
    ) AS grouped_tokens
),
provider_usage_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object('provider', provider, 'state', usage_state, 'count', usage_count)
        ORDER BY provider, usage_state
    ) AS rows
    FROM (
        SELECT provider, usage_state, count(*)::bigint AS usage_count
        FROM provider_base
        GROUP BY provider, usage_state
    ) AS grouped_usage
),
route_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM route_rows
),
attempt_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM provider_attempt_rows
),
token_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM provider_token_rows
),
usage_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM provider_usage_rows
)
SELECT jsonb_build_object(
    'acquisition_claims_total', job_totals.claims_total,
    'acquisition_jobs_total', jsonb_build_object(
        'succeeded', job_totals.succeeded,
        'failed', job_totals.failed,
        'unknown', job_totals.unknown
    ),
    'acquisition_jobs', jsonb_build_object(
        'queued', job_totals.queued,
        'running', job_totals.running,
        'succeeded', job_totals.succeeded,
        'failed', job_totals.failed,
        'unknown', job_totals.unknown
    ),
    'fetch_duration', fetch_duration.rows,
    'response_body_bytes', fetch_bytes.rows,
    'route_decisions', route_json.rows,
    'provider_attempts', attempt_json.rows,
    'provider_tokens', token_json.rows,
    'provider_usage', usage_json.rows,
    'reconciliation_pending', jsonb_build_object(
        'acquisition', (
            SELECT count(*)::bigint FROM public.acquisition_jobs WHERE status = 'unknown'
        ),
        'teaching', (
            SELECT count(*)::bigint FROM public.teaching_runs WHERE status = 'reconciliation_required'
        )
    )
)
FROM job_totals CROSS JOIN fetch_duration CROSS JOIN fetch_bytes
     CROSS JOIN route_json CROSS JOIN attempt_json CROSS JOIN token_json CROSS JOIN usage_json
$function$
"""


def upgrade() -> None:
    _assert_roles_exist()
    op.execute(
        "ALTER TABLE public.provider_attempts"
        " ADD COLUMN provider_family text NOT NULL DEFAULT 'unknown'"
        " CHECK (provider_family IN ('openai', 'unknown'))"
    )
    op.execute(
        """
CREATE TABLE public.acquisition_fetch_observations (
    tenant_id          text NOT NULL,
    project_id         text NOT NULL,
    acquisition_id     text NOT NULL,
    attempt_number     integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    outcome            text NOT NULL DEFAULT 'unknown'
        CHECK (outcome IN ('succeeded', 'failed', 'unknown')),
    duration_seconds   double precision,
    response_body_bytes bigint,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_id, acquisition_id, attempt_number),
    FOREIGN KEY (tenant_id, project_id, acquisition_id)
        REFERENCES public.acquisition_jobs (tenant_id, project_id, acquisition_id),
    CHECK (duration_seconds IS NULL OR duration_seconds BETWEEN 0 AND 86400),
    CHECK (response_body_bytes IS NULL OR response_body_bytes BETWEEN 0 AND 1048576),
    CHECK (response_body_bytes IS NULL OR duration_seconds IS NOT NULL),
    CHECK (
        duration_seconds IS NOT NULL
        OR (outcome = 'unknown' AND response_body_bytes IS NULL)
    )
)
"""
    )
    op.execute("ALTER TABLE public.acquisition_fetch_observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.acquisition_fetch_observations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
CREATE POLICY acquisition_fetch_observations_scope ON public.acquisition_fetch_observations
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
"""
    )
    op.execute(
        "CREATE INDEX acquisition_fetch_observations_aggregate_idx"
        " ON public.acquisition_fetch_observations (outcome)"
        " INCLUDE (duration_seconds, response_body_bytes)"
    )
    op.execute(
        "REVOKE ALL ON public.acquisition_fetch_observations FROM PUBLIC, study_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON public.acquisition_fetch_observations TO study_worker"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO study_app")
    op.execute(_metrics_function_sql())
    op.execute("REVOKE ALL ON FUNCTION public.study_metrics_snapshot() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.study_metrics_snapshot() TO study_app")


def downgrade() -> None:
    facts_exist = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM public.acquisition_fetch_observations)"
    ).scalar_one()
    provider_labels_exist = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM public.provider_attempts WHERE provider_family <> 'unknown')"
    ).scalar_one()
    if facts_exist or provider_labels_exist:
        raise RuntimeError("cannot downgrade after acquisition/provider metrics facts were recorded")
    op.execute("REVOKE EXECUTE ON FUNCTION public.study_metrics_snapshot() FROM study_app")
    op.execute("DROP FUNCTION public.study_metrics_snapshot()")
    op.execute("REVOKE ALL ON public.acquisition_fetch_observations FROM study_worker")
    op.execute("DROP INDEX IF EXISTS public.acquisition_fetch_observations_aggregate_idx")
    op.execute("DROP POLICY IF EXISTS acquisition_fetch_observations_scope ON public.acquisition_fetch_observations")
    op.execute("DROP TABLE public.acquisition_fetch_observations")
    op.execute("ALTER TABLE public.provider_attempts DROP CONSTRAINT provider_attempts_provider_family_check")
    op.execute("ALTER TABLE public.provider_attempts DROP COLUMN provider_family")
