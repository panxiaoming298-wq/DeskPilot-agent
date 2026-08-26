"""persist controlled workspace exploration and confirmed file-set plans

Revision ID: 0061_workspace_coding_explorations
Revises: 0060_workspace_coding_bounded_files
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0061_workspace_coding_explorations"
down_revision: str | None = "0060_workspace_coding_bounded_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_coding_exploration_snapshots",
        sa.Column("snapshot_id", sa.String(length=68), nullable=False),
        sa.Column("source_task_id", sa.String(length=40), nullable=False),
        sa.Column("source_user_message_id", sa.String(length=40), nullable=False),
        sa.Column("source_user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("project_path", sa.Text(), nullable=False),
        sa.Column("ecosystem", sa.String(length=16), nullable=False),
        sa.Column("test_path", sa.Text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("catalog_digest", sa.String(length=64), nullable=False),
        sa.Column("scanned_file_count", sa.Integer(), nullable=False),
        sa.Column("scanned_byte_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ecosystem IN ('python', 'node') AND file_count BETWEEN 2 AND 256 "
            "AND scanned_file_count BETWEEN 2 AND 2000 AND scanned_byte_count >= 0",
            name="ck_workspace_coding_exploration_snapshot_scope",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id"],
            ["tasks.task_id"],
            name="fk_workspace_coding_exploration_snapshot_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_user_message_id"],
            ["conversation_messages.message_id"],
            name="fk_workspace_coding_exploration_snapshot_message",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id",
            name="pk_workspace_coding_exploration_snapshots",
        ),
        sa.UniqueConstraint(
            "source_task_id",
            name="uq_workspace_coding_exploration_snapshot_task",
        ),
        sa.UniqueConstraint(
            "snapshot_digest",
            name="uq_workspace_coding_exploration_snapshot_digest",
        ),
    )
    op.create_index(
        "ix_workspace_coding_exploration_snapshots_project",
        "workspace_coding_exploration_snapshots",
        ["project_path", "created_at"],
        unique=False,
    )
    op.create_table(
        "workspace_coding_exploration_proposals",
        sa.Column("proposal_id", sa.String(length=68), nullable=False),
        sa.Column("snapshot_id", sa.String(length=68), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("explorer_agent_id", sa.String(length=128), nullable=False),
        sa.Column("explorer_agent_version", sa.String(length=32), nullable=False),
        sa.Column(
            "explorer_agent_contract_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "explorer_prompt_package_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "candidate_count BETWEEN 2 AND 8",
            name="ck_workspace_coding_exploration_proposal_count",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["workspace_coding_exploration_snapshots.snapshot_id"],
            name="fk_workspace_coding_exploration_proposal_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "proposal_id",
            name="pk_workspace_coding_exploration_proposals",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            name="uq_workspace_coding_exploration_proposal_snapshot",
        ),
        sa.UniqueConstraint(
            "proposal_digest",
            name="uq_workspace_coding_exploration_proposal_digest",
        ),
    )
    op.create_table(
        "workspace_coding_file_set_plan_bindings",
        sa.Column("binding_id", sa.String(length=68), nullable=False),
        sa.Column("proposal_id", sa.String(length=68), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("successor_task_id", sa.String(length=40), nullable=False),
        sa.Column("confirmation_message_id", sa.String(length=40), nullable=False),
        sa.Column("confirmation_message_digest", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("mappings_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version = 1 AND plan_generation = 1 "
            "AND file_count BETWEEN 2 AND 8",
            name="ck_workspace_coding_file_set_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["workspace_coding_exploration_proposals.proposal_id"],
            name="fk_workspace_coding_file_set_plan_proposal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_task_id", "contract_version"],
            ["task_contract_versions.task_id", "task_contract_versions.version"],
            name="fk_workspace_coding_file_set_contract",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_task_id", "plan_generation"],
            ["task_plan_generations.task_id", "task_plan_generations.generation"],
            name="fk_workspace_coding_file_set_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_message_id"],
            ["conversation_messages.message_id"],
            name="fk_workspace_coding_file_set_plan_message",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "binding_id",
            name="pk_workspace_coding_file_set_plan_bindings",
        ),
        sa.UniqueConstraint(
            "proposal_id",
            name="uq_workspace_coding_file_set_plan_proposal",
        ),
        sa.UniqueConstraint(
            "successor_task_id",
            name="uq_workspace_coding_file_set_plan_task",
        ),
        sa.UniqueConstraint(
            "confirmation_message_id",
            name="uq_workspace_coding_file_set_plan_message",
        ),
        sa.UniqueConstraint(
            "binding_digest",
            name="uq_workspace_coding_file_set_plan_digest",
        ),
    )
    op.create_index(
        "ix_workspace_coding_file_set_plan_created",
        "workspace_coding_file_set_plan_bindings",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace coding file-set Plan bindings",
                "SELECT 1 FROM workspace_coding_file_set_plan_bindings LIMIT 1",
            ),
            (
                "workspace coding exploration proposals",
                "SELECT 1 FROM workspace_coding_exploration_proposals LIMIT 1",
            ),
            (
                "workspace coding exploration snapshots",
                "SELECT 1 FROM workspace_coding_exploration_snapshots LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workspace_coding_file_set_plan_created",
        table_name="workspace_coding_file_set_plan_bindings",
    )
    op.drop_table("workspace_coding_file_set_plan_bindings")
    op.drop_table("workspace_coding_exploration_proposals")
    op.drop_index(
        "ix_workspace_coding_exploration_snapshots_project",
        table_name="workspace_coding_exploration_snapshots",
    )
    op.drop_table("workspace_coding_exploration_snapshots")
