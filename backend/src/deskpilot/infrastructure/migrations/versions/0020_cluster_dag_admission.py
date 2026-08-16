"""Add fenced cluster-wide DAG admission tickets and scheduler state.

Revision ID: 0020_cluster_dag_admission
Revises: 0019_graph_control_mailbox
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_cluster_dag_admission"
down_revision: str | None = "0019_graph_control_mailbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_effect_dag_admission_state",
        sa.Column("scope_id", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "next_grant_sequence",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("configuration_digest", sa.String(length=64), nullable=True),
        sa.Column("global_limit", sa.Integer(), nullable=True),
        sa.Column("per_graph_limit", sa.Integer(), nullable=True),
        sa.Column("default_tool_limit", sa.Integer(), nullable=True),
        sa.Column("tool_limits_digest", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1 AND next_grant_sequence >= 1",
            name="ck_tool_effect_dag_admission_state_versions",
        ),
        sa.CheckConstraint(
            "(configuration_digest IS NULL AND global_limit IS NULL "
            "AND per_graph_limit IS NULL AND default_tool_limit IS NULL "
            "AND tool_limits_digest IS NULL) OR "
            "(configuration_digest IS NOT NULL AND global_limit >= 1 "
            "AND per_graph_limit >= 1 AND per_graph_limit <= global_limit "
            "AND default_tool_limit >= 1 AND default_tool_limit <= global_limit "
            "AND tool_limits_digest IS NOT NULL)",
            name="ck_tool_effect_dag_admission_state_configuration",
        ),
        sa.PrimaryKeyConstraint("scope_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO tool_effect_dag_admission_state "
            "(scope_id, revision, next_grant_sequence, configuration_digest, "
            "global_limit, per_graph_limit, default_tool_limit, "
            "tool_limits_digest, updated_at) "
            "VALUES ('global', 1, 1, NULL, NULL, NULL, NULL, NULL, "
            "CURRENT_TIMESTAMP)"
        )
    )
    op.create_table(
        "tool_effect_dag_admissions",
        sa.Column("admission_id", sa.String(length=40), nullable=False),
        sa.Column("batch_id", sa.String(length=40), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("owner_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("lease_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("fencing_token", sa.Integer(), server_default="0", nullable=False),
        sa.Column("grant_sequence", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'granted', 'released', 'cancelled', 'withdrawn', 'expired')",
            name="ck_tool_effect_dag_admissions_status",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND fencing_token >= 0 AND lease_ttl_seconds >= 1",
            name="ck_tool_effect_dag_admissions_versions",
        ),
        sa.CheckConstraint(
            "(status = 'granted' AND grant_sequence IS NOT NULL "
            "AND fencing_token >= 1 AND granted_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL) OR status != 'granted'",
            name="ck_tool_effect_dag_admissions_grant",
        ),
        sa.ForeignKeyConstraint(
            ["graph_id"],
            ["tool_effect_graphs.graph_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("admission_id"),
        sa.UniqueConstraint(
            "batch_id",
            "node_id",
            name="uq_tool_effect_dag_admissions_batch_node",
        ),
    )
    op.create_index(
        "ix_tool_effect_dag_admissions_route",
        "tool_effect_dag_admissions",
        ["status", "expires_at", "created_at"],
    )
    op.create_index(
        "ix_tool_effect_dag_admissions_active",
        "tool_effect_dag_admissions",
        ["status", "graph_id", "tool_name", "expires_at"],
    )
    op.create_index(
        "ix_tool_effect_dag_admissions_owner",
        "tool_effect_dag_admissions",
        ["owner_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_effect_dag_admissions_owner",
        table_name="tool_effect_dag_admissions",
    )
    op.drop_index(
        "ix_tool_effect_dag_admissions_active",
        table_name="tool_effect_dag_admissions",
    )
    op.drop_index(
        "ix_tool_effect_dag_admissions_route",
        table_name="tool_effect_dag_admissions",
    )
    op.drop_table("tool_effect_dag_admissions")
    op.drop_table("tool_effect_dag_admission_state")
