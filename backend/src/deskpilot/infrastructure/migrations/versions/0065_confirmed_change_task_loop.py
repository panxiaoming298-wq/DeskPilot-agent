"""activate confirmed change proposals in the existing TaskLoop state machine

Revision ID: 0065_confirmed_change_task_loop
Revises: 0064_workspace_coding_change_proposals
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0065_confirmed_change_task_loop"
down_revision: str | None = "0064_workspace_coding_change_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_loop_executions") as batch:
        batch.drop_constraint("ck_task_loop_execution_source", type_="check")
        batch.add_column(sa.Column("write_plan_binding_id", sa.String(length=68), nullable=True))
        batch.add_column(
            sa.Column("write_plan_binding_digest", sa.String(length=64), nullable=True)
        )
        batch.create_foreign_key(
            "fk_task_loop_execution_write_plan_binding",
            "workspace_coding_write_plan_bindings",
            ["write_plan_binding_id"],
            ["binding_id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_task_loop_execution_write_plan_binding",
            ["write_plan_binding_id"],
        )
        batch.create_check_constraint(
            "ck_task_loop_execution_source",
            "(source_kind = 'model_planner' AND loop_id IS NOT NULL AND "
            "draft_id IS NOT NULL AND source_binding_id IS NULL AND "
            "source_binding_digest IS NULL AND write_plan_binding_id IS NULL AND "
            "write_plan_binding_digest IS NULL) OR "
            "(source_kind = 'confirmed_file_set' AND loop_id IS NULL AND "
            "draft_id IS NULL AND source_binding_id IS NOT NULL AND "
            "source_binding_digest IS NOT NULL AND write_plan_binding_id IS NULL AND "
            "write_plan_binding_digest IS NULL) OR "
            "(source_kind = 'confirmed_change_proposal' AND loop_id IS NULL AND "
            "draft_id IS NULL AND source_binding_id IS NULL AND "
            "source_binding_digest IS NULL AND write_plan_binding_id IS NOT NULL AND "
            "write_plan_binding_digest IS NOT NULL)",
        )

    with op.batch_alter_table("model_planner_node_bindings") as batch:
        batch.drop_constraint("ck_model_planner_node_source", type_="check")
        batch.add_column(sa.Column("write_plan_binding_id", sa.String(length=68), nullable=True))
        batch.add_column(
            sa.Column("write_plan_binding_digest", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("workspace_coding_write_node_proof_manifest", sa.JSON(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "workspace_coding_write_node_proof_digest",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            "fk_model_planner_node_write_plan_binding",
            "workspace_coding_write_plan_bindings",
            ["write_plan_binding_id"],
            ["binding_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_model_planner_node_source",
            "(source_kind = 'model_planner' AND draft_id IS NOT NULL AND "
            "step_binding_id IS NOT NULL AND step_binding_digest IS NOT NULL AND "
            "step_ordinal IS NOT NULL AND offer_id IS NOT NULL AND offer_key IS NOT NULL AND "
            "offer_digest IS NOT NULL AND recipe_manifest IS NOT NULL AND "
            "recipe_digest IS NOT NULL AND policy_snapshot_digest IS NOT NULL AND "
            "source_binding_id IS NULL AND source_binding_digest IS NULL AND "
            "workspace_reader_node_proof_manifest IS NULL AND "
            "workspace_reader_node_proof_digest IS NULL AND write_plan_binding_id IS NULL AND "
            "write_plan_binding_digest IS NULL AND "
            "workspace_coding_write_node_proof_manifest IS NULL AND "
            "workspace_coding_write_node_proof_digest IS NULL) OR "
            "(source_kind = 'confirmed_file_set' AND draft_id IS NULL AND "
            "step_binding_id IS NULL AND step_binding_digest IS NULL AND "
            "step_ordinal IS NULL AND offer_id IS NULL AND offer_key IS NULL AND "
            "offer_digest IS NULL AND recipe_manifest IS NULL AND recipe_digest IS NULL AND "
            "policy_snapshot_digest IS NULL AND source_binding_id IS NOT NULL AND "
            "source_binding_digest IS NOT NULL AND "
            "workspace_reader_node_proof_manifest IS NOT NULL AND "
            "workspace_reader_node_proof_digest IS NOT NULL AND write_plan_binding_id IS NULL "
            "AND write_plan_binding_digest IS NULL AND "
            "workspace_coding_write_node_proof_manifest IS NULL AND "
            "workspace_coding_write_node_proof_digest IS NULL) OR "
            "(source_kind = 'confirmed_change_proposal' AND draft_id IS NULL AND "
            "step_binding_id IS NULL AND step_binding_digest IS NULL AND "
            "step_ordinal IS NULL AND offer_id IS NULL AND offer_key IS NULL AND "
            "offer_digest IS NULL AND recipe_manifest IS NOT NULL AND recipe_digest IS NOT NULL "
            "AND policy_snapshot_digest IS NULL AND source_binding_id IS NULL AND "
            "source_binding_digest IS NULL AND workspace_reader_node_proof_manifest IS NULL "
            "AND workspace_reader_node_proof_digest IS NULL AND write_plan_binding_id IS NOT NULL "
            "AND write_plan_binding_digest IS NOT NULL AND "
            "workspace_coding_write_node_proof_manifest IS NOT NULL AND "
            "workspace_coding_write_node_proof_digest IS NOT NULL)",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "confirmed change Proposal TaskLoop execution",
                "SELECT 1 FROM task_loop_executions "
                "WHERE source_kind = 'confirmed_change_proposal' LIMIT 1",
            ),
            (
                "confirmed change Proposal node binding",
                "SELECT 1 FROM model_planner_node_bindings "
                "WHERE source_kind = 'confirmed_change_proposal' LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("model_planner_node_bindings") as batch:
        batch.drop_constraint("ck_model_planner_node_source", type_="check")
        batch.drop_constraint(
            "fk_model_planner_node_write_plan_binding",
            type_="foreignkey",
        )
        batch.drop_column("workspace_coding_write_node_proof_digest")
        batch.drop_column("workspace_coding_write_node_proof_manifest")
        batch.drop_column("write_plan_binding_digest")
        batch.drop_column("write_plan_binding_id")
        batch.create_check_constraint(
            "ck_model_planner_node_source",
            "(source_kind = 'model_planner' AND draft_id IS NOT NULL AND "
            "step_binding_id IS NOT NULL AND step_binding_digest IS NOT NULL AND "
            "step_ordinal IS NOT NULL AND offer_id IS NOT NULL AND offer_key IS NOT NULL AND "
            "offer_digest IS NOT NULL AND recipe_manifest IS NOT NULL AND "
            "recipe_digest IS NOT NULL AND policy_snapshot_digest IS NOT NULL AND "
            "source_binding_id IS NULL AND source_binding_digest IS NULL AND "
            "workspace_reader_node_proof_manifest IS NULL AND "
            "workspace_reader_node_proof_digest IS NULL) OR "
            "(source_kind = 'confirmed_file_set' AND draft_id IS NULL AND "
            "step_binding_id IS NULL AND step_binding_digest IS NULL AND "
            "step_ordinal IS NULL AND offer_id IS NULL AND offer_key IS NULL AND "
            "offer_digest IS NULL AND recipe_manifest IS NULL AND recipe_digest IS NULL AND "
            "policy_snapshot_digest IS NULL AND source_binding_id IS NOT NULL AND "
            "source_binding_digest IS NOT NULL AND "
            "workspace_reader_node_proof_manifest IS NOT NULL AND "
            "workspace_reader_node_proof_digest IS NOT NULL)",
        )

    with op.batch_alter_table("task_loop_executions") as batch:
        batch.drop_constraint("ck_task_loop_execution_source", type_="check")
        batch.drop_constraint(
            "uq_task_loop_execution_write_plan_binding",
            type_="unique",
        )
        batch.drop_constraint(
            "fk_task_loop_execution_write_plan_binding",
            type_="foreignkey",
        )
        batch.drop_column("write_plan_binding_digest")
        batch.drop_column("write_plan_binding_id")
        batch.create_check_constraint(
            "ck_task_loop_execution_source",
            "(source_kind = 'model_planner' AND loop_id IS NOT NULL AND "
            "draft_id IS NOT NULL AND source_binding_id IS NULL AND "
            "source_binding_digest IS NULL) OR "
            "(source_kind = 'confirmed_file_set' AND loop_id IS NULL AND "
            "draft_id IS NULL AND source_binding_id IS NOT NULL AND "
            "source_binding_digest IS NOT NULL)",
        )
