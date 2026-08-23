"""Add immutable PDF render evidence to Artifact revisions.

Revision ID: 0038_pdf_render_evidence
Revises: 0037_turn_routes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0038_pdf_render_evidence"
down_revision: str | None = "0037_turn_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifact_revisions") as batch:
        batch.add_column(sa.Column("render_evidence", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("render_evidence_digest", sa.String(64), nullable=True))


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted PDF artifact render evidence",
                "SELECT 1 FROM artifact_revisions "
                "WHERE render_evidence IS NOT NULL "
                "OR render_evidence_digest IS NOT NULL LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("artifact_revisions") as batch:
        batch.drop_column("render_evidence_digest")
        batch.drop_column("render_evidence")
