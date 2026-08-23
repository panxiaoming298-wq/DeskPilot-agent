"""Add persisted trusted Conversation Turn route decisions.

Revision ID: 0037_turn_routes
Revises: 0036_artifact_exports
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0037_turn_routes"
down_revision: str | None = "0036_artifact_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turn_routes",
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "conversation_id",
            sa.String(40),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_message_id",
            sa.String(40),
            sa.ForeignKey("conversation_messages.message_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("route_id", sa.String(64), nullable=True),
        sa.Column("route_version", sa.String(16), nullable=True),
        sa.Column("route_manifest_digest", sa.String(64), nullable=True),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("parameter_digest", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_manifest", sa.JSON(), nullable=True),
        sa.Column("result_digest", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('routed', 'needs_clarification', 'unsupported')",
            name="ck_turn_route_decision",
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'running', 'needs_user_action', 'succeeded', "
            "'failed', 'not_applicable')",
            name="ck_turn_route_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_turn_route_revision"),
    )
    op.create_index(
        "ix_turn_routes_conversation",
        "turn_routes",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(("persisted Conversation Turn route", "SELECT 1 FROM turn_routes LIMIT 1"),),
    )
    op.drop_index("ix_turn_routes_conversation", table_name="turn_routes")
    op.drop_table("turn_routes")
