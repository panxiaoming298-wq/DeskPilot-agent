"""allow planner-only single Offers in the generic task loop

Revision ID: 0055_planner_only_single_task_loop
Revises: 0054_task_loop_cycle_events
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0055_planner_only_single_task_loop"
down_revision: str | None = "0054_task_loop_cycle_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_planner_drafts") as batch:
        batch.drop_constraint("ck_model_planner_draft_steps", type_="check")
        batch.create_check_constraint(
            "ck_model_planner_draft_steps",
            "step_count BETWEEN 1 AND 8",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "planner-only single-Offer Task Loop proofs",
                "SELECT 1 FROM model_planner_drafts WHERE step_count = 1 LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("model_planner_drafts") as batch:
        batch.drop_constraint("ck_model_planner_draft_steps", type_="check")
        batch.create_check_constraint(
            "ck_model_planner_draft_steps",
            "step_count BETWEEN 2 AND 8",
        )
