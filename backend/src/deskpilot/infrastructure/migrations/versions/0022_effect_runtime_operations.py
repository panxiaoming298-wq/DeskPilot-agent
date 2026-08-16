"""Add protected effect-runtime operations audit state and chain.

Revision ID: 0022_effect_runtime_ops
Revises: 0021_incremental_ready
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_effect_runtime_ops"
down_revision: str | None = "0021_incremental_ready"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_effect_dag_ready_states",
        sa.Column("rebuild_count", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "tool_effect_dag_ready_states",
        sa.Column("last_rebuild_duration_ms", sa.Integer(), nullable=True),
    )
    op.create_table(
        "effect_runtime_operations_state",
        sa.Column("scope_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_event_digest", sa.String(length=64), nullable=True),
        sa.Column("last_retention_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1 AND next_sequence >= 1",
            name="ck_effect_runtime_operations_state_versions",
        ),
        sa.PrimaryKeyConstraint("scope_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO effect_runtime_operations_state "
            "(scope_id, revision, next_sequence, last_event_digest, "
            "last_retention_at, updated_at) VALUES "
            "('effect_runtime', 1, 1, NULL, NULL, CURRENT_TIMESTAMP)"
        )
    )
    op.create_table(
        "effect_runtime_operations_audit",
        sa.Column("event_id", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_effect_runtime_operations_audit_sequence",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "action",
            "idempotency_key_digest",
            name="uq_effect_runtime_operations_audit_idempotency",
        ),
        sa.UniqueConstraint(
            "event_digest",
            name="uq_effect_runtime_operations_audit_event_digest",
        ),
        sa.UniqueConstraint(
            "sequence",
            name="uq_effect_runtime_operations_audit_sequence",
        ),
    )
    op.create_index(
        "ix_effect_runtime_operations_audit_occurred",
        "effect_runtime_operations_audit",
        ["occurred_at", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_effect_runtime_operations_audit_occurred",
        table_name="effect_runtime_operations_audit",
    )
    op.drop_table("effect_runtime_operations_audit")
    op.drop_table("effect_runtime_operations_state")
    op.drop_column("tool_effect_dag_ready_states", "last_rebuild_duration_ms")
    op.drop_column("tool_effect_dag_ready_states", "rebuild_count")
