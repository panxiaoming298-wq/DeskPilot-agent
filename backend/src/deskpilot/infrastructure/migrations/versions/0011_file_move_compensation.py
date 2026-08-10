"""Add explicit receipt-bound compensation lineage.

Revision ID: 0011_file_move_compensation
Revises: 0010_reconciliation_receipt_evidence
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_file_move_compensation"
down_revision: str | None = "0010_reconciliation_receipt_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_reconciliations") as batch_op:
        batch_op.add_column(
            sa.Column("compensation_task_id", sa.String(length=40), nullable=True)
        )
        batch_op.add_column(
            sa.Column("compensation_receipt_id", sa.String(length=68), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "compensation_created_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_tool_reconciliations_compensation_task_id_tasks",
            "tasks",
            ["compensation_task_id"],
            ["task_id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_tool_reconciliations_compensation_receipt_id_receipts",
            "tool_commit_receipts",
            ["compensation_receipt_id"],
            ["receipt_id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_tool_reconciliations_compensation_task_id",
            ["compensation_task_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_reconciliations") as batch_op:
        batch_op.drop_constraint(
            "uq_tool_reconciliations_compensation_task_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_tool_reconciliations_compensation_receipt_id_receipts",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_tool_reconciliations_compensation_task_id_tasks",
            type_="foreignkey",
        )
        batch_op.drop_column("compensation_created_at")
        batch_op.drop_column("compensation_receipt_id")
        batch_op.drop_column("compensation_task_id")
