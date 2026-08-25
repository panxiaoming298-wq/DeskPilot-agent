"""persist same-conversation workspace coding amendment lineage

Revision ID: 0059_workspace_coding_amendments
Revises: 0058_workspace_coding_deliveries
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0059_workspace_coding_amendments"
down_revision: str | None = "0058_workspace_coding_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_coding_amendment_bindings",
        sa.Column("amendment_id", sa.String(length=68), nullable=False),
        sa.Column("conversation_id", sa.String(length=40), nullable=False),
        sa.Column("source_task_id", sa.String(length=40), nullable=False),
        sa.Column("source_execution_id", sa.String(length=68), nullable=False),
        sa.Column("source_contract_version", sa.Integer(), nullable=False),
        sa.Column("source_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("source_plan_generation", sa.Integer(), nullable=False),
        sa.Column("source_plan_digest", sa.String(length=64), nullable=False),
        sa.Column("source_execution_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "source_execution_event_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("successor_task_id", sa.String(length=40), nullable=False),
        sa.Column("successor_user_message_id", sa.String(length=40), nullable=False),
        sa.Column(
            "successor_user_message_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("amendment_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_contract_version >= 1 AND source_plan_generation >= 1 "
            "AND source_task_id <> successor_task_id",
            name="ck_workspace_coding_amendment_scope",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_workspace_coding_amendment_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id", "source_contract_version"],
            ["task_contract_versions.task_id", "task_contract_versions.version"],
            name="fk_workspace_coding_amendment_contract",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id", "source_plan_generation"],
            ["task_plan_generations.task_id", "task_plan_generations.generation"],
            name="fk_workspace_coding_amendment_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id"],
            ["task_loop_executions.execution_id"],
            name="fk_workspace_coding_amendment_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id", "source_execution_event_digest"],
            [
                "task_loop_execution_events.execution_id",
                "task_loop_execution_events.event_digest",
            ],
            name="fk_workspace_coding_amendment_terminal_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_task_id"],
            ["tasks.task_id"],
            name="fk_workspace_coding_amendment_successor_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_user_message_id"],
            ["conversation_messages.message_id"],
            name="fk_workspace_coding_amendment_successor_message",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "amendment_id",
            name="pk_workspace_coding_amendment_bindings",
        ),
        sa.UniqueConstraint(
            "source_execution_id",
            name="uq_workspace_coding_amendment_source_execution",
        ),
        sa.UniqueConstraint(
            "successor_task_id",
            name="uq_workspace_coding_amendment_successor_task",
        ),
        sa.UniqueConstraint(
            "successor_user_message_id",
            name="uq_workspace_coding_amendment_successor_message",
        ),
        sa.UniqueConstraint(
            "amendment_digest",
            name="uq_workspace_coding_amendment_digest",
        ),
    )
    op.create_index(
        "ix_workspace_coding_amendments_conversation",
        "workspace_coding_amendment_bindings",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace coding amendment bindings",
                "SELECT 1 FROM workspace_coding_amendment_bindings LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workspace_coding_amendments_conversation",
        table_name="workspace_coding_amendment_bindings",
    )
    op.drop_table("workspace_coding_amendment_bindings")
