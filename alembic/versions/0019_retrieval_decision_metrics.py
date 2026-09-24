"""Persist the retrieval-mode decision and aggregate it in the metrics snapshot.

Follows 0017 (teaching_runs.routing_decision): a jsonb column with a closed-set
CHECK, plus a downgrade guard. The metrics snapshot function from 0018 is
replaced wholesale with a self-contained body that adds the retrieval_decisions
aggregate; downgrade restores the exact 0018 body so the function never
references a dropped column. Both bodies live in this file on purpose:
migrations must stay self-contained and replayable (see findings 2026-09-19).
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
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


_RETRIEVAL_AGGREGATE = r"""retrieval_rows AS (
    SELECT jsonb_agg(
        jsonb_build_object(
            'mode', retrieval_decision->>'mode',
            'reason_code', retrieval_decision->>'reason_code',
            'count', decision_count
        ) ORDER BY retrieval_decision->>'mode', retrieval_decision->>'reason_code'
    ) AS rows
    FROM (
        SELECT retrieval_decision, count(*)::bigint AS decision_count
        FROM public.teaching_runs
        WHERE retrieval_decision IS NOT NULL
        GROUP BY retrieval_decision
    ) AS grouped_retrievals
),
retrieval_json AS (
    SELECT COALESCE(rows, '[]'::jsonb) AS rows FROM retrieval_rows
)"""


def _metrics_function_sql(*, with_retrieval: bool) -> str:
    """0018 的快照函数体（`with_retrieval=False`）加检索模式聚合后的新版。

    两版函数体只差一个 CTE 与一个输出键 —— 但仍然整段生成而不是
    运行时拼 0018 的代码：历史迁移必须自包含，`alembic downgrade`
    在 0018 已经被改写或删除时也要能重放出正确的 SQL。
    """
    retrieval_cte = (
        ",\n" + _RETRIEVAL_AGGREGATE + ",\n" if with_retrieval else ",\n"
    )
    retrieval_output = (
        "    'retrieval_decisions', retrieval_json.rows,\n" if with_retrieval else ""
    )
    retrieval_join = (
        " CROSS JOIN retrieval_json" if with_retrieval else ""
    )
    return (
        r"""
CREATE OR REPLACE FUNCTION public.study_metrics_snapshot()
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
)"""
        + retrieval_cte
        + r"""
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
"""
        + retrieval_output
        + r"""    'provider_attempts', attempt_json.rows,
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
     CROSS JOIN route_json CROSS JOIN attempt_json"""
        + retrieval_join
        + """
     CROSS JOIN token_json CROSS JOIN usage_json
$function$
"""
    )


def upgrade() -> None:
    _assert_roles_exist()
    op.execute("ALTER TABLE teaching_runs ADD COLUMN retrieval_decision jsonb")
    op.execute(
        """
ALTER TABLE teaching_runs
    ADD CONSTRAINT teaching_runs_retrieval_decision_check
    CHECK (
            retrieval_decision IS NULL
            OR (
                jsonb_typeof(retrieval_decision) = 'object'
                AND retrieval_decision ?& ARRAY[
                    'policy_version', 'mode', 'reason_code', 'ranking_version'
                ]
                AND retrieval_decision - ARRAY[
                    'policy_version', 'mode', 'reason_code', 'ranking_version'
                ] = '{}'::jsonb
                AND jsonb_typeof(retrieval_decision->'policy_version') = 'string'
                AND jsonb_typeof(retrieval_decision->'mode') = 'string'
                AND jsonb_typeof(retrieval_decision->'reason_code') = 'string'
                AND jsonb_typeof(retrieval_decision->'ranking_version') = 'string'
                AND retrieval_decision->>'policy_version' = 'retrieval-route/v1'
            AND retrieval_decision->>'mode' IN ('keyword', 'hybrid', 'degraded')
            AND (
                (
                    retrieval_decision->>'mode' <> 'degraded'
                    AND retrieval_decision->>'reason_code' = ''
                )
                OR
                (
                    retrieval_decision->>'mode' = 'degraded'
                    AND retrieval_decision->>'reason_code' IN (
                        'embedding_provider_failed',
                        'vector_index_unavailable',
                        'embedding_model_version_mismatch'
                    )
                )
            )
            AND retrieval_decision->>'ranking_version' <> ''
        )
    )
"""
    )
    op.execute(_metrics_function_sql(with_retrieval=True))


def downgrade() -> None:
    existing = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM teaching_runs WHERE retrieval_decision IS NOT NULL)"
    ).scalar_one()
    if existing:
        raise RuntimeError("cannot downgrade after retrieval decisions were recorded")
    op.execute("ALTER TABLE teaching_runs DROP CONSTRAINT teaching_runs_retrieval_decision_check")
    op.execute("ALTER TABLE teaching_runs DROP COLUMN retrieval_decision")
    # 恢复 0018 版函数体：函数引用了刚删除的列，不恢复会让 /metrics 在
    # 降级后直接 500 —— 那不是"回到旧版"，是"旧版加一个坏掉的端点"。
    op.execute(_metrics_function_sql(with_retrieval=False))
