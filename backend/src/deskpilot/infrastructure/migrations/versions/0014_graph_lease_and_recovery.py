"""Add graph leases, fencing, and graph-level reconciliation recovery.

Revision ID: 0014_graph_lease_recovery
Revises: 0013_tool_effect_graphs
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_graph_lease_recovery"
down_revision: str | None = "0013_tool_effect_graphs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_effect_graphs",
        sa.Column("lease_owner_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "tool_effect_graphs",
        sa.Column("lease_acquired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_effect_graphs",
        sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_effect_graphs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_effect_graphs",
        sa.Column(
            "fencing_token",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_tool_effect_graphs_lease_expires_at",
        "tool_effect_graphs",
        ["lease_expires_at"],
    )

    with op.batch_alter_table("tool_reconciliations") as batch:
        batch.add_column(
            sa.Column(
                "graph_recovery_status",
                sa.String(length=24),
                nullable=False,
                server_default="not_applicable",
            )
        )
        batch.add_column(sa.Column("graph_recovery_action", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("graph_recovery_event_id", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("graph_recovered_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_tool_reconciliations_graph_recovery_event",
            "task_events",
            ["graph_recovery_event_id"],
            ["event_id"],
            ondelete="SET NULL",
        )
    op.execute(
        sa.text(
            """
            UPDATE tool_reconciliations
            SET graph_recovery_status = 'pending'
            WHERE EXISTS (
                SELECT 1
                FROM tool_effect_attempts AS attempt
                JOIN tool_effect_nodes AS node ON node.node_id = attempt.node_id
                JOIN tool_effect_graphs AS graph ON graph.graph_id = node.graph_id
                WHERE attempt.call_id = tool_reconciliations.call_id
                  AND graph.status = 'blocked_unknown'
            )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("tool_reconciliations") as batch:
        batch.drop_constraint(
            "fk_tool_reconciliations_graph_recovery_event",
            type_="foreignkey",
        )
        batch.drop_column("graph_recovered_at")
        batch.drop_column("graph_recovery_event_id")
        batch.drop_column("graph_recovery_action")
        batch.drop_column("graph_recovery_status")
    op.drop_index(
        "ix_tool_effect_graphs_lease_expires_at",
        table_name="tool_effect_graphs",
    )
    op.drop_column("tool_effect_graphs", "fencing_token")
    op.drop_column("tool_effect_graphs", "lease_expires_at")
    op.drop_column("tool_effect_graphs", "lease_heartbeat_at")
    op.drop_column("tool_effect_graphs", "lease_acquired_at")
    op.drop_column("tool_effect_graphs", "lease_owner_id")
