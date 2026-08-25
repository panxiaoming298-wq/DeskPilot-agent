"""separate planner-only Task Loop authority from an active execution Plan

Revision ID: 0057_task_loop_deferred_binding
Revises: 0056_workspace_command_plan_bindings
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0057_task_loop_deferred_binding"
down_revision: str | None = "0056_workspace_command_plan_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_constraints(*, include_task_loop_deferred: bool) -> None:
    statuses = (
        "'bound', 'multi_step_deferred', 'task_loop_deferred', 'not_applicable'"
        if include_task_loop_deferred
        else "'bound', 'multi_step_deferred', 'not_applicable'"
    )
    target = (
        "(status = 'bound' AND offer_id IS NOT NULL AND offer_digest IS NOT NULL AND "
        "plan_id IS NOT NULL AND plan_generation IS NOT NULL AND "
        "plan_manifest_digest IS NOT NULL AND contract_id IS NOT NULL AND "
        "contract_version IS NOT NULL AND contract_digest IS NOT NULL) OR "
    )
    if include_task_loop_deferred:
        target += (
            "(status = 'task_loop_deferred' AND offer_id IS NOT NULL AND "
            "offer_digest IS NOT NULL AND plan_id IS NULL AND plan_generation IS NULL "
            "AND plan_manifest_digest IS NULL AND contract_id IS NULL AND "
            "contract_version IS NULL AND contract_digest IS NULL) OR "
        )
    target += (
        "(status IN ('multi_step_deferred', 'not_applicable') AND offer_id IS NULL "
        "AND offer_digest IS NULL AND plan_id IS NULL AND plan_generation IS NULL AND "
        "plan_manifest_digest IS NULL AND contract_id IS NULL AND "
        "contract_version IS NULL AND contract_digest IS NULL)"
    )
    with op.batch_alter_table("turn_plan_bindings") as batch:
        batch.drop_constraint("ck_turn_plan_binding_status", type_="check")
        batch.drop_constraint("ck_turn_plan_binding_target", type_="check")
        batch.create_check_constraint(
            "ck_turn_plan_binding_status",
            f"status IN ({statuses})",
        )
        batch.create_check_constraint("ck_turn_plan_binding_target", target)


def upgrade() -> None:
    _replace_constraints(include_task_loop_deferred=True)


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "planner-only Task Loop deferred bindings",
                "SELECT 1 FROM turn_plan_bindings "
                "WHERE status = 'task_loop_deferred' LIMIT 1",
            ),
        ),
    )
    _replace_constraints(include_task_loop_deferred=False)
