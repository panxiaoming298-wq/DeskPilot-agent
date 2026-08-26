"""allow bounded 2-8 file workspace coding deliveries

Revision ID: 0060_workspace_coding_bounded_files
Revises: 0059_workspace_coding_amendments
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0060_workspace_coding_bounded_files"
down_revision: str | None = "0059_workspace_coding_amendments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_coding_deliveries") as batch_op:
        batch_op.drop_constraint(
            "ck_workspace_coding_delivery_counts",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_workspace_coding_delivery_counts",
            "changed_file_count BETWEEN 2 AND 8 "
            "AND test_run_count BETWEEN 1 AND 2 "
            "AND failure_count BETWEEN 0 AND 1",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "bounded workspace coding deliveries",
                "SELECT 1 FROM workspace_coding_deliveries "
                "WHERE changed_file_count <> 2 LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("workspace_coding_deliveries") as batch_op:
        batch_op.drop_constraint(
            "ck_workspace_coding_delivery_counts",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_workspace_coding_delivery_counts",
            "changed_file_count = 2 AND test_run_count BETWEEN 1 AND 2 "
            "AND failure_count BETWEEN 0 AND 1",
        )
