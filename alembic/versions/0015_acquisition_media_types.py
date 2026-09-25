"""Restrict web acquisition to media types with a supported parser."""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " DROP CONSTRAINT acquisition_jobs_media_type_check"
    )
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " ADD CONSTRAINT acquisition_jobs_media_type_check"
        " CHECK (media_type IN ('text/plain', 'text/markdown'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " DROP CONSTRAINT acquisition_jobs_media_type_check"
    )
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " ADD CONSTRAINT acquisition_jobs_media_type_check"
        " CHECK (media_type IN ('text/plain', 'text/markdown', 'text/html'))"
    )
