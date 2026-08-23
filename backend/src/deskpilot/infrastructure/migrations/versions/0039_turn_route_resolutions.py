"""Bind clarification follow-ups to their source Turn Route.

Revision ID: 0039_turn_route_resolutions
Revises: 0038_pdf_render_evidence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0039_turn_route_resolutions"
down_revision: str | None = "0038_pdf_render_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("turn_routes") as batch:
        batch.add_column(sa.Column("resolved_from_task_id", sa.String(40), nullable=True))
        batch.add_column(sa.Column("resolution_rule", sa.String(64), nullable=True))
        batch.add_column(sa.Column("resolution_digest", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_turn_routes_resolved_from",
            "turn_routes",
            ["resolved_from_task_id"],
            ["task_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_turn_route_resolution",
            "(resolved_from_task_id IS NULL AND resolution_rule IS NULL AND "
            "resolution_digest IS NULL) OR "
            "(resolved_from_task_id IS NOT NULL AND resolution_rule IS NOT NULL AND "
            "resolution_digest IS NOT NULL AND resolved_from_task_id <> task_id)",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted Turn route resolution provenance",
                "SELECT 1 FROM turn_routes "
                "WHERE resolved_from_task_id IS NOT NULL "
                "OR resolution_rule IS NOT NULL "
                "OR resolution_digest IS NOT NULL LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("turn_routes") as batch:
        batch.drop_constraint("ck_turn_route_resolution", type_="check")
        batch.drop_constraint("fk_turn_routes_resolved_from", type_="foreignkey")
        batch.drop_column("resolution_digest")
        batch.drop_column("resolution_rule")
        batch.drop_column("resolved_from_task_id")
