"""Add versioned evaluation runs and trace chains.

Revision ID: 0029_evaluation_traces
Revises: 0028_controlled_mcp
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_evaluation_traces"
down_revision: str | None = "0028_controlled_mcp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("run_id", sa.String(40), primary_key=True),
        sa.Column("suite_id", sa.String(80), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("suite_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("replay_of_run_id", sa.String(40), nullable=True),
        sa.Column("replay_match", sa.Boolean(), nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("safety_case_count", sa.Integer(), nullable=False),
        sa.Column("safety_passed_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("result_manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("case_count >= 1", name="ck_evaluation_run_case_count"),
    )
    op.create_index("ix_evaluation_runs_started", "evaluation_runs", ["started_at"])
    op.create_table(
        "evaluation_trace_events",
        sa.Column(
            "run_id",
            sa.String(40),
            sa.ForeignKey("evaluation_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.String(80), nullable=False),
        sa.Column("scenario", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("output_digest", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("previous_event_digest", sa.String(64), nullable=True),
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("run_id", "case_id", name="uq_evaluation_trace_case"),
        sa.CheckConstraint("sequence >= 1 AND duration_ms >= 0", name="ck_evaluation_trace_values"),
    )


def downgrade() -> None:
    op.drop_table("evaluation_trace_events")
    op.drop_index("ix_evaluation_runs_started", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
