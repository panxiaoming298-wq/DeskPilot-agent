"""Add durable bounded Agent Model Loop decisions and observations.

Revision ID: 0040_durable_agent_model_loop
Revises: 0039_turn_route_resolutions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0040_durable_agent_model_loop"
down_revision: str | None = "0039_turn_route_resolutions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_dispatch_attempts",
        sa.Column("dispatch_attempt_id", sa.String(68), primary_key=True),
        sa.Column(
            "turn_id",
            sa.String(68),
            sa.ForeignKey("agent_model_turns.turn_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("stable_error_code", sa.String(100), nullable=True),
        sa.Column("claim_owner_id", sa.String(128), nullable=False),
        sa.Column("claim_fencing_token", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_no >= 1", name="ck_model_dispatch_attempt_no"),
        sa.CheckConstraint(
            "status IN ('prepared', 'dispatching', 'succeeded', 'failed', 'outcome_unknown')",
            name="ck_model_dispatch_attempt_status",
        ),
        sa.UniqueConstraint("turn_id", "attempt_no", name="uq_model_dispatch_attempt"),
    )
    op.create_index("ix_model_dispatch_attempts_turn_id", "model_dispatch_attempts", ["turn_id"])
    op.create_table(
        "agent_decisions",
        sa.Column("decision_id", sa.String(68), primary_key=True),
        sa.Column(
            "turn_id",
            sa.String(68),
            sa.ForeignKey("agent_model_turns.turn_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("binding_id", sa.String(68), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('request_route', 'submit_result')", name="ck_agent_decision_kind"
        ),
    )
    op.create_index("ix_agent_decisions_invocation_id", "agent_decisions", ["invocation_id"])
    op.create_table(
        "agent_observations",
        sa.Column("observation_id", sa.String(68), primary_key=True),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            sa.String(68),
            sa.ForeignKey("agent_decisions.decision_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("binding_id", sa.String(68), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_ref", sa.String(100), nullable=False),
        sa.Column("projection", sa.JSON(), nullable=False),
        sa.Column("observation_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('route') AND status IN ('succeeded', 'failed')",
            name="ck_agent_observation_state",
        ),
    )
    op.create_index("ix_agent_observations_invocation_id", "agent_observations", ["invocation_id"])


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted model dispatch attempt proof",
                "SELECT 1 FROM model_dispatch_attempts LIMIT 1",
            ),
            (
                "persisted Agent decision proof",
                "SELECT 1 FROM agent_decisions LIMIT 1",
            ),
            (
                "persisted Agent observation proof",
                "SELECT 1 FROM agent_observations LIMIT 1",
            ),
        ),
    )
    op.drop_index("ix_agent_observations_invocation_id", table_name="agent_observations")
    op.drop_table("agent_observations")
    op.drop_index("ix_agent_decisions_invocation_id", table_name="agent_decisions")
    op.drop_table("agent_decisions")
    op.drop_index("ix_model_dispatch_attempts_turn_id", table_name="model_dispatch_attempts")
    op.drop_table("model_dispatch_attempts")
