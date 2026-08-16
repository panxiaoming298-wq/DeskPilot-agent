"""Add explicit graph terminals for compensation failure and unknown outcomes.

Revision ID: 0017_parallel_compensation
Revises: 0016_dag_dispatch_delivery
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_parallel_compensation"
down_revision: str | None = "0016_dag_dispatch_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_effect_graphs") as batch:
        batch.drop_constraint("ck_tool_effect_graphs_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_effect_graphs_status",
            "status IN ('active', 'compensating', 'succeeded', 'compensated', "
            "'failed', 'cancelled', 'blocked_unknown', 'blocked_non_compensable', "
            "'blocked_compensation_failed', 'blocked_compensation_unknown')",
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_effect_graphs") as batch:
        batch.drop_constraint("ck_tool_effect_graphs_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_effect_graphs_status",
            "status IN ('active', 'compensating', 'succeeded', 'compensated', "
            "'failed', 'cancelled', 'blocked_unknown', 'blocked_non_compensable')",
        )
