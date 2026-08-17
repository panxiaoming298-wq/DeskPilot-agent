"""Add conversation, task working memory, and Context Manifest proofs.

Revision ID: 0033_context_working_memory
Revises: 0032_verified_artifact_delivery
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_context_working_memory"
down_revision: str | None = "0032_verified_artifact_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(40), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("message_id", sa.String(40), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(40),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_ref", sa.String(500), nullable=True),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_conversation_message_role"),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'sensitive')",
            name="ck_conversation_message_classification",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deleted')", name="ck_conversation_message_status"
        ),
        sa.CheckConstraint(
            "(content IS NULL) <> (content_ref IS NULL)",
            name="ck_conversation_message_content",
        ),
    )
    op.create_index(
        "ix_conversation_messages_scope",
        "conversation_messages",
        ["conversation_id", "task_id", "created_at"],
    )
    op.create_table(
        "working_memory_items",
        sa.Column("memory_item_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(40), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(500), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("verification_status", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('current_goal', 'active_constraint', 'confirmed_decision', "
            "'open_question', 'selected_artifact', 'temporary_fact')",
            name="ck_working_memory_kind",
        ),
        sa.CheckConstraint(
            "source_type IN ('user_explicit', 'task_contract', 'verified_claim')",
            name="ck_working_memory_source_type",
        ),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'sensitive')",
            name="ck_working_memory_classification",
        ),
        sa.CheckConstraint(
            "verification_status IN ('not_required', 'verified')",
            name="ck_working_memory_verification",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'deleted')", name="ck_working_memory_status"
        ),
    )
    op.create_index("ix_working_memory_items_task_id", "working_memory_items", ["task_id"])
    op.create_index(
        "ix_working_memory_active",
        "working_memory_items",
        ["task_id", "status", "created_at"],
    )
    op.create_table(
        "context_requests",
        sa.Column("context_request_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_turn_id",
            sa.String(68),
            sa.ForeignKey("agent_model_turns.turn_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("allowed_sources", sa.JSON(), nullable=False),
        sa.Column("selectors", sa.JSON(), nullable=False),
        sa.Column("maximum_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("privacy_mode", sa.String(32), nullable=False),
        sa.Column("target_provider_location", sa.String(16), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_context_requests_invocation_id", "context_requests", ["invocation_id"])
    op.create_table(
        "context_manifests",
        sa.Column("manifest_id", sa.String(68), primary_key=True),
        sa.Column(
            "context_request_id",
            sa.String(68),
            sa.ForeignKey("context_requests.context_request_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_turn_id",
            sa.String(68),
            sa.ForeignKey("agent_model_turns.turn_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_context_manifests_task_id", "context_manifests", ["task_id"])
    op.create_index(
        "ix_context_manifests_invocation_id", "context_manifests", ["invocation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_context_manifests_invocation_id", table_name="context_manifests")
    op.drop_index("ix_context_manifests_task_id", table_name="context_manifests")
    op.drop_table("context_manifests")
    op.drop_index("ix_context_requests_invocation_id", table_name="context_requests")
    op.drop_table("context_requests")
    op.drop_index("ix_working_memory_active", table_name="working_memory_items")
    op.drop_index("ix_working_memory_items_task_id", table_name="working_memory_items")
    op.drop_table("working_memory_items")
    op.drop_index("ix_conversation_messages_scope", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")

