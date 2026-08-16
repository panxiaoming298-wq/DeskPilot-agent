"""Expand the ordered route index for PostgreSQL-native graph-control claims.

Revision ID: 0025_graph_control_claims
Revises: 0024_admission_shards
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025_graph_control_claims"
down_revision: str | None = "0024_admission_shards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_tool_effect_graph_controls_owner_claim",
        table_name="tool_effect_graph_controls",
        if_exists=True,
    )
    op.drop_index(
        "ix_tool_effect_graph_controls_route",
        table_name="tool_effect_graph_controls",
    )
    op.create_index(
        "ix_tool_effect_graph_controls_route",
        "tool_effect_graph_controls",
        [
            "status",
            "target_owner_id",
            "available_at",
            "created_at",
            "control_id",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_effect_graphs_owner_lease",
        table_name="tool_effect_graphs",
        if_exists=True,
    )
    op.drop_index(
        "ix_tool_effect_graph_controls_owner_claim",
        table_name="tool_effect_graph_controls",
        if_exists=True,
    )
    op.drop_index(
        "ix_tool_effect_graph_controls_route",
        table_name="tool_effect_graph_controls",
    )
    op.create_index(
        "ix_tool_effect_graph_controls_route",
        "tool_effect_graph_controls",
        ["status", "target_owner_id", "available_at"],
    )
