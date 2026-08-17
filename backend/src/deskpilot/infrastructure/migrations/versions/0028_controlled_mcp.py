"""Add controlled MCP server state and append-only audit.

Revision ID: 0028_controlled_mcp
Revises: 0027_local_knowledge
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_controlled_mcp"
down_revision: str | None = "0027_local_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_server_states",
        sa.Column("server_id", sa.String(80), primary_key=True),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_mcp_server_state_revision"),
    )
    op.create_table(
        "mcp_audit_state",
        sa.Column("state_id", sa.String(32), primary_key=True),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.Column("last_event_digest", sa.String(64), nullable=True),
        sa.CheckConstraint("next_sequence >= 1", name="ck_mcp_audit_state_sequence"),
    )
    op.bulk_insert(
        sa.table(
            "mcp_audit_state",
            sa.column("state_id", sa.String(32)),
            sa.column("next_sequence", sa.Integer()),
            sa.column("last_event_digest", sa.String(64)),
        ),
        [{"state_id": "mcp", "next_sequence": 1, "last_event_digest": None}],
    )
    op.create_table(
        "mcp_audit_events",
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(40), nullable=False),
        sa.Column("server_id", sa.String(80), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("previous_event_digest", sa.String(64), nullable=True),
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_mcp_audit_event_id"),
        sa.UniqueConstraint("event_digest", name="uq_mcp_audit_event_digest"),
        sa.CheckConstraint("sequence >= 1", name="ck_mcp_audit_sequence"),
    )
    op.create_index(
        "ix_mcp_audit_server_sequence",
        "mcp_audit_events",
        ["server_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_audit_server_sequence", table_name="mcp_audit_events")
    op.drop_table("mcp_audit_events")
    op.drop_table("mcp_audit_state")
    op.drop_table("mcp_server_states")
