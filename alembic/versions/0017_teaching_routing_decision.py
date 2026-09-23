"""Persist the bounded local-query/cloud-answer routing decision."""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE teaching_runs ADD COLUMN routing_decision jsonb")
    op.execute(
        """
ALTER TABLE teaching_runs
    ADD CONSTRAINT teaching_runs_routing_decision_check
    CHECK (
        routing_decision IS NULL
        OR (
            jsonb_typeof(routing_decision) = 'object'
            AND routing_decision ?& ARRAY[
                'policy_version', 'answer_route',
                'query_rewrite_status', 'reason_code'
            ]
            AND routing_decision - ARRAY[
                'policy_version', 'answer_route',
                'query_rewrite_status', 'reason_code'
            ] = '{}'::jsonb
            AND routing_decision->>'policy_version' = 'teaching-route/v1'
            AND routing_decision->>'answer_route' = 'cloud'
            AND (
                (routing_decision->>'query_rewrite_status' = 'disabled'
                 AND routing_decision->>'reason_code' = 'local_not_configured')
                OR
                (routing_decision->>'query_rewrite_status' = 'applied'
                 AND routing_decision->>'reason_code' = 'local_rewrite_accepted')
                OR
                (routing_decision->>'query_rewrite_status' = 'fallback'
                 AND routing_decision->>'reason_code' = 'local_unavailable_or_invalid')
            )
        )
    )
"""
    )


def downgrade() -> None:
    existing = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM teaching_runs WHERE routing_decision IS NOT NULL)"
    ).scalar_one()
    if existing:
        raise RuntimeError("cannot downgrade after teaching routing decisions were recorded")
    op.execute("ALTER TABLE teaching_runs DROP CONSTRAINT teaching_runs_routing_decision_check")
    op.execute("ALTER TABLE teaching_runs DROP COLUMN routing_decision")
