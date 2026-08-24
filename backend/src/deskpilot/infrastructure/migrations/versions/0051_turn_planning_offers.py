"""add immutable turn planning offers and proofs

Revision ID: 0051_turn_planning_offers
Revises: 0050_agent_graph_test_conditions
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0051_turn_planning_offers"
down_revision: str | None = "0050_agent_graph_test_conditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turn_planning_offers",
        sa.Column("offer_id", sa.String(length=68), nullable=False),
        sa.Column("offer_key", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("contract_id", sa.String(length=40), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("execution_agents_manifest", sa.JSON(), nullable=False),
        sa.Column("execution_agents_digest", sa.String(length=64), nullable=False),
        sa.Column("expected_plan_manifest", sa.JSON(), nullable=False),
        sa.Column("expected_plan_id", sa.String(length=68), nullable=False),
        sa.Column("expected_plan_generation", sa.Integer(), nullable=False),
        sa.Column("expected_plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "expected_plan_binding_snapshot_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("capabilities_manifest", sa.JSON(), nullable=False),
        sa.Column("capabilities_digest", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_model", sa.String(length=200), nullable=False),
        sa.Column("provider_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("recipe_id", sa.String(length=64), nullable=False),
        sa.Column("recipe_version", sa.String(length=16), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("budget_manifest", sa.JSON(), nullable=False),
        sa.Column("budget_digest", sa.String(length=64), nullable=False),
        sa.Column("parameter_schema_manifest", sa.JSON(), nullable=False),
        sa.Column("parameter_schema_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("offer_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version >= 1",
            name="ck_turn_planning_offer_contract",
        ),
        sa.CheckConstraint(
            "expected_plan_generation = 1",
            name="ck_turn_planning_offer_expected_plan",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["conversation_messages.message_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("offer_id"),
        sa.UniqueConstraint("offer_key", name="uq_turn_planning_offer_key"),
        sa.UniqueConstraint("offer_digest", name="uq_turn_planning_offer_digest"),
        sa.UniqueConstraint(
            "offer_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "offer_digest",
            name="uq_turn_planning_offer_scope",
        ),
    )
    op.create_index(
        "ix_turn_planning_offers_message",
        "turn_planning_offers",
        ["task_id", "user_message_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "turn_planner_runs",
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("planner_agent_id", sa.String(length=100), nullable=False),
        sa.Column("planner_agent_version", sa.String(length=16), nullable=False),
        sa.Column("planner_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("planner_prompt_package_digest", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_model", sa.String(length=200), nullable=False),
        sa.Column("provider_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("offer_set_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("fallback_candidate_digest", sa.String(length=64), nullable=False),
        sa.Column("reservation_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("claim_owner_id", sa.String(length=100), nullable=True),
        sa.Column("claim_fencing_token", sa.Integer(), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("run_digest", sa.String(length=64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared', 'dispatching', 'succeeded', 'failed', "
            "'outcome_unknown', 'cancelled')",
            name="ck_turn_planner_run_status",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND claim_fencing_token >= 0",
            name="ck_turn_planner_run_revision",
        ),
        sa.CheckConstraint(
            "(status = 'prepared' AND claim_owner_id IS NULL AND "
            "claim_fencing_token = 0 AND claim_expires_at IS NULL AND "
            "request_dispatched_at IS NULL AND completed_at IS NULL AND "
            "response_digest IS NULL AND failure_code IS NULL AND failure_digest IS NULL) OR "
            "(status = 'dispatching' AND claim_owner_id IS NOT NULL AND "
            "claim_fencing_token >= 1 AND claim_expires_at IS NOT NULL AND "
            "request_dispatched_at IS NOT NULL AND completed_at IS NULL AND "
            "response_digest IS NULL AND failure_code IS NULL AND failure_digest IS NULL) OR "
            "(status = 'succeeded' AND claim_owner_id IS NULL AND claim_expires_at IS NULL "
            "AND request_dispatched_at IS NOT NULL AND completed_at IS NOT NULL AND "
            "response_digest IS NOT NULL AND failure_code IS NULL AND failure_digest IS NULL) OR "
            "(status = 'failed' AND claim_owner_id IS NULL AND claim_expires_at IS NULL "
            "AND request_dispatched_at IS NOT NULL AND completed_at IS NOT NULL AND "
            "response_digest IS NULL AND failure_code IS NOT NULL AND "
            "failure_code NOT IN ('PLANNER_OUTCOME_UNKNOWN', 'PLANNER_CANCELLED') "
            "AND failure_digest IS NOT NULL) OR "
            "(status = 'outcome_unknown' AND claim_owner_id IS NULL AND "
            "claim_expires_at IS NULL AND request_dispatched_at IS NOT NULL AND "
            "completed_at IS NOT NULL AND response_digest IS NULL AND "
            "failure_code = 'PLANNER_OUTCOME_UNKNOWN' AND failure_digest IS NOT NULL) OR "
            "(status = 'cancelled' AND claim_owner_id IS NULL AND claim_expires_at IS NULL "
            "AND completed_at IS NOT NULL AND response_digest IS NULL AND "
            "failure_code = 'PLANNER_CANCELLED' AND failure_digest IS NOT NULL)",
            name="ck_turn_planner_run_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["conversation_messages.message_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "task_id",
            "user_message_id",
            name="uq_turn_planner_run_message",
        ),
        sa.UniqueConstraint("run_digest", name="uq_turn_planner_run_digest"),
        sa.UniqueConstraint(
            "run_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "run_digest",
            name="uq_turn_planner_run_scope",
        ),
        sa.UniqueConstraint(
            "run_id",
            "task_id",
            "user_message_id",
            "reservation_digest",
            name="uq_turn_planner_run_reservation",
        ),
    )
    op.create_index(
        "ix_turn_planner_runs_message",
        "turn_planner_runs",
        ["task_id", "user_message_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_turn_planner_runs_claim",
        "turn_planner_runs",
        ["status", "claim_expires_at"],
        unique=False,
    )

    op.create_table(
        "turn_planner_adjudications",
        sa.Column("adjudication_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("run_digest", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("selected_offer_count", sa.Integer(), nullable=False),
        sa.Column("parameter_bindings_manifest", sa.JSON(), nullable=True),
        sa.Column("parameter_bindings_digest", sa.String(length=64), nullable=True),
        sa.Column("proposal_digest", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("adjudication_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('single_step', 'multi_step_deferred', "
            "'deterministic_fallback', 'needs_user_input', 'unsupported')",
            name="ck_turn_planner_adjudication_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'single_step' AND selected_offer_count = 1 AND "
            "proposal_digest IS NOT NULL AND parameter_bindings_manifest IS NOT NULL "
            "AND parameter_bindings_digest IS NOT NULL) OR "
            "(outcome = 'multi_step_deferred' AND selected_offer_count BETWEEN 2 AND 8 "
            "AND proposal_digest IS NOT NULL AND parameter_bindings_manifest IS NOT NULL "
            "AND parameter_bindings_digest IS NOT NULL) OR "
            "(outcome = 'needs_user_input' AND selected_offer_count BETWEEN 0 AND 1 "
            "AND proposal_digest IS NOT NULL AND parameter_bindings_manifest IS NOT NULL "
            "AND parameter_bindings_digest IS NOT NULL) OR "
            "(outcome = 'unsupported' AND selected_offer_count = 0 AND "
            "proposal_digest IS NOT NULL AND parameter_bindings_manifest IS NOT NULL "
            "AND parameter_bindings_digest IS NOT NULL) OR "
            "(outcome = 'deterministic_fallback' AND selected_offer_count = 0 "
            "AND proposal_digest IS NULL AND parameter_bindings_manifest IS NULL "
            "AND parameter_bindings_digest IS NULL)",
            name="ck_turn_planner_adjudication_selection",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["conversation_messages.message_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "run_id",
                "task_id",
                "user_message_id",
                "user_message_digest",
                "run_digest",
            ],
            [
                "turn_planner_runs.run_id",
                "turn_planner_runs.task_id",
                "turn_planner_runs.user_message_id",
                "turn_planner_runs.user_message_digest",
                "turn_planner_runs.run_digest",
            ],
            name="fk_turn_planner_adjudication_run_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("adjudication_id"),
        sa.UniqueConstraint("run_id", name="uq_turn_planner_adjudication_run"),
        sa.UniqueConstraint(
            "adjudication_digest",
            name="uq_turn_planner_adjudication_digest",
        ),
        sa.UniqueConstraint(
            "adjudication_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "adjudication_digest",
            name="uq_turn_planner_adjudication_scope",
        ),
    )
    op.create_index(
        "ix_turn_planner_adjudications_message",
        "turn_planner_adjudications",
        ["task_id", "user_message_id", "created_at"],
        unique=False,
    )

    op.create_index(
        "uq_task_plan_generation_binding",
        "task_plan_generations",
        [
            "task_id",
            "generation",
            "plan_id",
            "plan_manifest_digest",
            "contract_version",
            "contract_digest",
        ],
        unique=True,
    )

    op.create_table(
        "turn_plan_bindings",
        sa.Column("binding_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("adjudication_id", sa.String(length=68), nullable=False),
        sa.Column("adjudication_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("offer_id", sa.String(length=68), nullable=True),
        sa.Column("offer_digest", sa.String(length=64), nullable=True),
        sa.Column("plan_id", sa.String(length=68), nullable=True),
        sa.Column("plan_generation", sa.Integer(), nullable=True),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=True),
        sa.Column("contract_id", sa.String(length=40), nullable=True),
        sa.Column("contract_version", sa.Integer(), nullable=True),
        sa.Column("contract_digest", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('bound', 'multi_step_deferred', 'not_applicable')",
            name="ck_turn_plan_binding_status",
        ),
        sa.CheckConstraint(
            "(status = 'bound' AND offer_id IS NOT NULL AND offer_digest IS NOT NULL AND "
            "plan_id IS NOT NULL AND "
            "plan_generation IS NOT NULL AND plan_manifest_digest IS NOT NULL AND "
            "contract_id IS NOT NULL AND contract_version IS NOT NULL AND "
            "contract_digest IS NOT NULL) OR "
            "(status IN ('multi_step_deferred', 'not_applicable') AND offer_id IS NULL "
            "AND offer_digest IS NULL "
            "AND plan_id IS NULL AND plan_generation IS NULL AND "
            "plan_manifest_digest IS NULL AND contract_id IS NULL AND "
            "contract_version IS NULL AND contract_digest IS NULL)",
            name="ck_turn_plan_binding_target",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["conversation_messages.message_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "adjudication_id",
                "task_id",
                "user_message_id",
                "user_message_digest",
                "adjudication_digest",
            ],
            [
                "turn_planner_adjudications.adjudication_id",
                "turn_planner_adjudications.task_id",
                "turn_planner_adjudications.user_message_id",
                "turn_planner_adjudications.user_message_digest",
                "turn_planner_adjudications.adjudication_digest",
            ],
            name="fk_turn_plan_binding_adjudication_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "offer_id",
                "task_id",
                "user_message_id",
                "user_message_digest",
                "offer_digest",
            ],
            [
                "turn_planning_offers.offer_id",
                "turn_planning_offers.task_id",
                "turn_planning_offers.user_message_id",
                "turn_planning_offers.user_message_digest",
                "turn_planning_offers.offer_digest",
            ],
            name="fk_turn_plan_binding_offer_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "task_id",
                "plan_generation",
                "plan_id",
                "plan_manifest_digest",
                "contract_version",
                "contract_digest",
            ],
            [
                "task_plan_generations.task_id",
                "task_plan_generations.generation",
                "task_plan_generations.plan_id",
                "task_plan_generations.plan_manifest_digest",
                "task_plan_generations.contract_version",
                "task_plan_generations.contract_digest",
            ],
            name="fk_turn_plan_binding_plan_generation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint(
            "adjudication_id",
            name="uq_turn_plan_binding_adjudication",
        ),
        sa.UniqueConstraint("binding_digest", name="uq_turn_plan_binding_digest"),
        sa.UniqueConstraint(
            "binding_id",
            "adjudication_id",
            "task_id",
            "user_message_id",
            "binding_digest",
            name="uq_turn_plan_binding_route_scope",
        ),
    )
    op.create_index(
        "ix_turn_plan_bindings_message",
        "turn_plan_bindings",
        ["task_id", "user_message_id", "created_at"],
        unique=False,
    )

    with op.batch_alter_table("turn_routes") as batch_op:
        batch_op.add_column(
            sa.Column("turn_planner_run_id", sa.String(length=68), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "turn_planning_reservation_digest",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "turn_planning_adjudication_id",
                sa.String(length=68),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("turn_plan_binding_id", sa.String(length=68), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "turn_plan_binding_digest",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "turn_planning_provenance_digest",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_turn_route_planner_reservation",
            "(turn_planner_run_id IS NULL AND "
            "turn_planning_reservation_digest IS NULL) OR "
            "(turn_planner_run_id IS NOT NULL AND "
            "turn_planning_reservation_digest IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_turn_route_planning_provenance",
            "(turn_planning_adjudication_id IS NULL AND turn_plan_binding_id IS NULL "
            "AND turn_plan_binding_digest IS NULL AND "
            "turn_planning_provenance_digest IS NULL) OR "
            "(turn_planning_adjudication_id IS NOT NULL AND turn_plan_binding_id IS NOT NULL "
            "AND turn_plan_binding_digest IS NOT NULL AND "
            "turn_planning_provenance_digest IS NOT NULL)",
        )
        batch_op.create_foreign_key(
            "fk_turn_route_planner_reservation",
            "turn_planner_runs",
            [
                "turn_planner_run_id",
                "task_id",
                "user_message_id",
                "turn_planning_reservation_digest",
            ],
            [
                "run_id",
                "task_id",
                "user_message_id",
                "reservation_digest",
            ],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_turn_route_planning_provenance",
            "turn_plan_bindings",
            [
                "turn_plan_binding_id",
                "turn_planning_adjudication_id",
                "task_id",
                "user_message_id",
                "turn_plan_binding_digest",
            ],
            [
                "binding_id",
                "adjudication_id",
                "task_id",
                "user_message_id",
                "binding_digest",
            ],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_turn_routes_planner_run",
        "turn_routes",
        ["turn_planner_run_id"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "turn route planning provenance",
                "SELECT 1 FROM turn_routes WHERE "
                "turn_planner_run_id IS NOT NULL OR "
                "turn_planning_reservation_digest IS NOT NULL OR "
                "turn_planning_adjudication_id IS NOT NULL OR "
                "turn_plan_binding_id IS NOT NULL OR "
                "turn_plan_binding_digest IS NOT NULL OR "
                "turn_planning_provenance_digest IS NOT NULL LIMIT 1",
            ),
            ("turn plan bindings", "SELECT 1 FROM turn_plan_bindings LIMIT 1"),
            (
                "turn planner adjudications",
                "SELECT 1 FROM turn_planner_adjudications LIMIT 1",
            ),
            ("turn planner runs", "SELECT 1 FROM turn_planner_runs LIMIT 1"),
            ("turn planning offers", "SELECT 1 FROM turn_planning_offers LIMIT 1"),
        ),
    )

    op.drop_index("ix_turn_routes_planner_run", table_name="turn_routes")
    with op.batch_alter_table("turn_routes") as batch_op:
        batch_op.drop_constraint(
            "fk_turn_route_planner_reservation",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_turn_route_planning_provenance",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_turn_route_planner_reservation",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_turn_route_planning_provenance",
            type_="check",
        )
        batch_op.drop_column("turn_planning_provenance_digest")
        batch_op.drop_column("turn_plan_binding_digest")
        batch_op.drop_column("turn_plan_binding_id")
        batch_op.drop_column("turn_planning_adjudication_id")
        batch_op.drop_column("turn_planning_reservation_digest")
        batch_op.drop_column("turn_planner_run_id")

    op.drop_index("ix_turn_plan_bindings_message", table_name="turn_plan_bindings")
    op.drop_table("turn_plan_bindings")
    op.drop_index(
        "uq_task_plan_generation_binding",
        table_name="task_plan_generations",
    )
    op.drop_index(
        "ix_turn_planner_adjudications_message",
        table_name="turn_planner_adjudications",
    )
    op.drop_table("turn_planner_adjudications")
    op.drop_index("ix_turn_planner_runs_claim", table_name="turn_planner_runs")
    op.drop_index("ix_turn_planner_runs_message", table_name="turn_planner_runs")
    op.drop_table("turn_planner_runs")
    op.drop_index("ix_turn_planning_offers_message", table_name="turn_planning_offers")
    op.drop_table("turn_planning_offers")
