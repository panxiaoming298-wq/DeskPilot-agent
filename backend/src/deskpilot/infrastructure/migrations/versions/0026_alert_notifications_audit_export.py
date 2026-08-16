"""Add durable alert lifecycle notifications and audit export state.

Revision ID: 0026_alert_notifications
Revises: 0025_graph_control_claims
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_alert_notifications"
down_revision: str | None = "0025_graph_control_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("effect_runtime_operations_state") as batch_op:
        batch_op.drop_constraint(
            "ck_effect_runtime_operations_state_versions",
            type_="check",
        )
        batch_op.add_column(
            sa.Column(
                "next_alert_sequence",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("last_alert_event_digest", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_effect_runtime_operations_state_versions",
            "revision >= 1 AND next_sequence >= 1 AND next_alert_sequence >= 1",
        )

    op.create_table(
        "effect_runtime_alert_states",
        sa.Column("alert_code", sa.String(length=120), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1 AND count >= 0",
            name="ck_effect_runtime_alert_states_values",
        ),
        sa.PrimaryKeyConstraint("alert_code"),
    )
    op.create_index(
        "ix_effect_runtime_alert_states_active",
        "effect_runtime_alert_states",
        ["active", "updated_at", "alert_code"],
    )
    op.create_table(
        "effect_runtime_alert_notifications",
        sa.Column("notification_id", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("alert_code", sa.String(length=120), nullable=False),
        sa.Column("transition", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("alert_revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("audit_event_id", sa.String(length=40), nullable=False),
        sa.Column("audit_sequence", sa.Integer(), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1 AND count >= 0 AND alert_revision >= 1 AND audit_sequence >= 1",
            name="ck_effect_runtime_alert_notifications_values",
        ),
        sa.ForeignKeyConstraint(
            ["audit_event_id"],
            ["effect_runtime_operations_audit.event_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("notification_id"),
        sa.UniqueConstraint(
            "event_digest",
            name="uq_effect_runtime_alert_notifications_event_digest",
        ),
        sa.UniqueConstraint(
            "sequence",
            name="uq_effect_runtime_alert_notifications_sequence",
        ),
    )
    op.create_index(
        "ix_effect_runtime_alert_notifications_code_sequence",
        "effect_runtime_alert_notifications",
        ["alert_code", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_effect_runtime_alert_notifications_code_sequence",
        table_name="effect_runtime_alert_notifications",
    )
    op.drop_table("effect_runtime_alert_notifications")
    op.drop_index(
        "ix_effect_runtime_alert_states_active",
        table_name="effect_runtime_alert_states",
    )
    op.drop_table("effect_runtime_alert_states")
    with op.batch_alter_table("effect_runtime_operations_state") as batch_op:
        batch_op.drop_constraint(
            "ck_effect_runtime_operations_state_versions",
            type_="check",
        )
        batch_op.drop_column("last_alert_event_digest")
        batch_op.drop_column("next_alert_sequence")
        batch_op.create_check_constraint(
            "ck_effect_runtime_operations_state_versions",
            "revision >= 1 AND next_sequence >= 1",
        )
