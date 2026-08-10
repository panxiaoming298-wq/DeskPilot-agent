"""Add protected task runtime checkpoints.

Revision ID: 0012_task_runtime_checkpoints
Revises: 0011_file_move_compensation
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_task_runtime_checkpoints"
down_revision: str | None = "0011_file_move_compensation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runtime_checkpoints",
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("next_stage", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("protection_scheme", sa.String(length=64), nullable=False),
        sa.Column("protected_payload", sa.LargeBinary(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "next_stage >= 0 AND next_stage <= 8",
            name="ck_task_runtime_checkpoints_next_stage",
        ),
        sa.CheckConstraint(
            "event_seq >= 1 AND revision >= 1",
            name="ck_task_runtime_checkpoints_positive_versions",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_runtime_checkpoints_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        "ix_task_runtime_checkpoints_stage",
        "task_runtime_checkpoints",
        ["next_stage", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_runtime_checkpoints_stage",
        table_name="task_runtime_checkpoints",
    )
    op.drop_table("task_runtime_checkpoints")
