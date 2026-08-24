"""add atomic model-planner task-loop execution evidence

Revision ID: 0053_task_loop_execution
Revises: 0052_model_planner_task_loop
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0053_task_loop_execution"
down_revision: str | None = "0052_model_planner_task_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_loop_executions",
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("loop_id", sa.String(length=68), nullable=False),
        sa.Column("draft_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("latest_event_id", sa.String(length=68), nullable=False),
        sa.Column("latest_event_digest", sa.String(length=64), nullable=False),
        sa.Column("node_binding_count", sa.Integer(), nullable=False),
        sa.Column("binding_set_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("execution_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "plan_generation = 1 AND revision >= 1 AND event_count >= 1 "
            "AND node_binding_count >= 1",
            name="ck_task_loop_execution_versions",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'awaiting_user', 'repairing', "
            "'failed', 'succeeded', 'cancelled')",
            name="ck_task_loop_execution_status",
        ),
        sa.ForeignKeyConstraint(
            ["loop_id"],
            ["task_loops.loop_id"],
            name="fk_task_loop_executions_loop_id_task_loops",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["model_planner_drafts.draft_id"],
            name="fk_task_loop_executions_draft_id_model_planner_drafts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_loop_executions_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_execution_runs.run_id"],
            name="fk_task_loop_executions_run_id_task_execution_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.UniqueConstraint("loop_id", name="uq_task_loop_execution_loop"),
        sa.UniqueConstraint("draft_id", name="uq_task_loop_execution_draft"),
        sa.UniqueConstraint("run_id", name="uq_task_loop_execution_run"),
        sa.UniqueConstraint(
            "execution_digest", name="uq_task_loop_execution_digest"
        ),
    )
    op.create_index(
        "ix_task_loop_executions_recovery",
        "task_loop_executions",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_loop_executions_task",
        "task_loop_executions",
        ["task_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "task_loop_execution_events",
        sa.Column("event_id", sa.String(length=68), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("binding_set_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_task_loop_execution_event_sequence"
        ),
        sa.CheckConstraint(
            "kind IN ('activated', 'paused', 'resumed', 'awaiting_user', "
            "'repair_started', 'failed', 'succeeded', 'cancelled')",
            name="ck_task_loop_execution_event_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'activated' AND sequence = 1 AND previous_event_digest IS NULL) "
            "OR (kind != 'activated' AND sequence > 1 AND "
            "previous_event_digest IS NOT NULL)",
            name="ck_task_loop_execution_event_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name=(
                "fk_task_loop_execution_events_execution_id_"
                "task_loop_executions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_loop_execution_events_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "previous_event_digest"],
            [
                "task_loop_execution_events.execution_id",
                "task_loop_execution_events.event_digest",
            ],
            name="fk_task_loop_execution_event_previous",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_task_loop_execution_event_sequence",
        ),
        sa.UniqueConstraint(
            "event_digest", name="uq_task_loop_execution_event_digest"
        ),
        sa.UniqueConstraint(
            "execution_id",
            "event_digest",
            name="uq_task_loop_execution_event_chain_target",
        ),
    )
    op.create_index(
        "ix_task_loop_execution_events_chain",
        "task_loop_execution_events",
        ["execution_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "model_planner_node_bindings",
        sa.Column("node_binding_id", sa.String(length=68), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("draft_id", sa.String(length=68), nullable=False),
        sa.Column("step_binding_id", sa.String(length=68), nullable=False),
        sa.Column("step_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("step_ordinal", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.String(length=68), nullable=False),
        sa.Column("offer_key", sa.String(length=68), nullable=False),
        sa.Column("offer_digest", sa.String(length=64), nullable=False),
        sa.Column("recipe_manifest", sa.JSON(), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("source_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("source_plan_id", sa.String(length=68), nullable=False),
        sa.Column("source_plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("source_node_id", sa.String(length=68), nullable=False),
        sa.Column("source_node_spec_digest", sa.String(length=64), nullable=False),
        sa.Column("composite_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("composite_plan_id", sa.String(length=68), nullable=False),
        sa.Column(
            "composite_plan_manifest_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("composite_node_id", sa.String(length=68), nullable=False),
        sa.Column(
            "composite_node_spec_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("mapping_manifest", sa.JSON(), nullable=False),
        sa.Column("mapping_digest", sa.String(length=64), nullable=False),
        sa.Column("parameter_bindings_manifest", sa.JSON(), nullable=False),
        sa.Column("parameter_bindings_digest", sa.String(length=64), nullable=False),
        sa.Column("bound_input_manifest", sa.JSON(), nullable=False),
        sa.Column("bound_input_digest", sa.String(length=64), nullable=False),
        sa.Column("effective_authority_manifest", sa.JSON(), nullable=False),
        sa.Column("effective_authority_digest", sa.String(length=64), nullable=False),
        sa.Column("runtime_eligibility_manifest", sa.JSON(), nullable=False),
        sa.Column("runtime_eligibility_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "step_ordinal BETWEEN 1 AND 8", name="ck_model_planner_node_step"
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name="fk_mpn_binding_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_model_planner_node_bindings_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["conversation_messages.message_id"],
            name="fk_mpn_binding_user_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["model_planner_drafts.draft_id"],
            name=(
                "fk_model_planner_node_bindings_draft_id_"
                "model_planner_drafts"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["step_binding_id"],
            ["model_planner_step_bindings.step_binding_id"],
            name="fk_mpn_binding_step_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["turn_planning_offers.offer_id"],
            name=(
                "fk_model_planner_node_bindings_offer_id_"
                "turn_planning_offers"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["composite_node_id"],
            ["task_execution_nodes.node_id"],
            name="fk_mpn_binding_composite_node",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("node_binding_id"),
        sa.UniqueConstraint(
            "execution_id",
            "composite_node_id",
            name="uq_model_planner_node_composite",
        ),
        sa.UniqueConstraint(
            "execution_id",
            "step_binding_id",
            "source_node_id",
            name="uq_model_planner_node_source",
        ),
        sa.UniqueConstraint(
            "binding_digest", name="uq_model_planner_node_binding_digest"
        ),
    )
    op.create_index(
        "ix_model_planner_node_bindings_step",
        "model_planner_node_bindings",
        ["execution_id", "step_ordinal"],
        unique=False,
    )

    op.create_table(
        "task_loop_node_attempts",
        sa.Column("attempt_id", sa.String(length=68), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("node_binding_id", sa.String(length=68), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("claim_owner_id", sa.String(length=128), nullable=True),
        sa.Column("claim_fencing_token", sa.Integer(), nullable=False),
        sa.Column("claim_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("context_manifest", sa.JSON(), nullable=False),
        sa.Column("context_digest", sa.String(length=64), nullable=False),
        sa.Column("candidate_manifest", sa.JSON(), nullable=True),
        sa.Column("candidate_digest", sa.String(length=64), nullable=True),
        sa.Column("candidate_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_manifest", sa.JSON(), nullable=True),
        sa.Column("verification_digest", sa.String(length=64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_manifest", sa.JSON(), nullable=True),
        sa.Column("receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("attempt_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt >= 1 AND revision >= 1 AND claim_fencing_token >= 0",
            name="ck_task_loop_node_attempt_versions",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'claimed', 'running', "
            "'awaiting_verification', 'verified', 'failed', "
            "'outcome_unknown', 'cancelled')",
            name="ck_task_loop_node_attempt_status",
        ),
        sa.CheckConstraint(
            "((candidate_manifest IS NULL AND candidate_digest IS NULL AND "
            "candidate_recorded_at IS NULL) OR "
            "(candidate_manifest IS NOT NULL AND candidate_digest IS NOT NULL AND "
            "candidate_recorded_at IS NOT NULL)) AND "
            "((verification_manifest IS NULL AND verification_digest IS NULL AND "
            "verified_at IS NULL) OR "
            "(verification_manifest IS NOT NULL AND verification_digest IS NOT NULL AND "
            "verified_at IS NOT NULL AND candidate_manifest IS NOT NULL))",
            name="ck_task_loop_node_attempt_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name=(
                "fk_task_loop_node_attempts_execution_id_"
                "task_loop_executions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_binding_id"],
            ["model_planner_node_bindings.node_binding_id"],
            name="fk_tl_attempt_node_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_execution_runs.run_id"],
            name=(
                "fk_task_loop_node_attempts_run_id_task_execution_runs"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["task_execution_nodes.node_id"],
            name="fk_task_loop_node_attempts_node_id_task_execution_nodes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "execution_id", "node_id", "attempt", name="uq_task_loop_node_attempt"
        ),
        sa.UniqueConstraint(
            "attempt_digest", name="uq_task_loop_node_attempt_digest"
        ),
    )
    op.create_index(
        "ix_task_loop_node_attempts_claim",
        "task_loop_node_attempts",
        ["status", "claim_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_loop_node_attempts_node",
        "task_loop_node_attempts",
        ["execution_id", "node_id", "attempt"],
        unique=False,
    )

    op.create_table(
        "task_loop_verified_results",
        sa.Column("result_ref_id", sa.String(length=68), nullable=False),
        sa.Column("attempt_id", sa.String(length=68), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("node_binding_id", sa.String(length=68), nullable=False),
        sa.Column("node_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("producer_kind", sa.String(length=24), nullable=False),
        sa.Column("capability_manifest", sa.JSON(), nullable=False),
        sa.Column("capability_digest", sa.String(length=64), nullable=False),
        sa.Column("agent_binding_manifest", sa.JSON(), nullable=True),
        sa.Column("agent_binding_digest", sa.String(length=64), nullable=True),
        sa.Column("executor_manifest_digest", sa.String(length=64), nullable=True),
        sa.Column("agent_result_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("input_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("context_digest", sa.String(length=64), nullable=False),
        sa.Column("candidate_digest", sa.String(length=64), nullable=True),
        sa.Column("result_kind", sa.String(length=64), nullable=False),
        sa.Column("output_manifest", sa.JSON(), nullable=False),
        sa.Column("output_schema_digest", sa.String(length=64), nullable=False),
        sa.Column("output_digest", sa.String(length=64), nullable=False),
        sa.Column("verification_manifest", sa.JSON(), nullable=False),
        sa.Column("verification_digest", sa.String(length=64), nullable=False),
        sa.Column("result_ref_manifest", sa.JSON(), nullable=False),
        sa.Column("result_ref_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "producer_kind IN ('capability_executor', 'agent_bridge')",
            name="ck_task_loop_verified_result_producer",
        ),
        sa.CheckConstraint(
            "(producer_kind = 'capability_executor' AND "
            "agent_binding_manifest IS NULL AND agent_binding_digest IS NULL AND "
            "agent_result_proof_digest IS NULL AND "
            "executor_manifest_digest IS NOT NULL AND candidate_digest IS NOT NULL) "
            "OR (producer_kind = 'agent_bridge' AND "
            "agent_binding_manifest IS NOT NULL AND agent_binding_digest IS NOT NULL AND "
            "agent_result_proof_digest IS NOT NULL AND "
            "executor_manifest_digest IS NULL AND candidate_digest IS NULL)",
            name="ck_task_loop_verified_result_producer_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["task_loop_node_attempts.attempt_id"],
            name="fk_tl_result_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name=(
                "fk_task_loop_verified_results_execution_id_"
                "task_loop_executions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_binding_id"],
            ["model_planner_node_bindings.node_binding_id"],
            name="fk_tl_result_node_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_execution_runs.run_id"],
            name=(
                "fk_task_loop_verified_results_run_id_task_execution_runs"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["task_execution_nodes.node_id"],
            name="fk_task_loop_verified_results_node_id_task_execution_nodes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("result_ref_id"),
        sa.UniqueConstraint(
            "attempt_id", name="uq_task_loop_verified_result_attempt"
        ),
        sa.UniqueConstraint(
            "result_ref_digest", name="uq_task_loop_verified_result_digest"
        ),
    )
    op.create_index(
        "ix_task_loop_verified_results_node",
        "task_loop_verified_results",
        ["execution_id", "node_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "task-loop verified results",
                "SELECT 1 FROM task_loop_verified_results LIMIT 1",
            ),
            (
                "task-loop node attempts",
                "SELECT 1 FROM task_loop_node_attempts LIMIT 1",
            ),
            (
                "model Planner node bindings",
                "SELECT 1 FROM model_planner_node_bindings LIMIT 1",
            ),
            (
                "task-loop execution events",
                "SELECT 1 FROM task_loop_execution_events LIMIT 1",
            ),
            (
                "task-loop executions",
                "SELECT 1 FROM task_loop_executions LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_task_loop_verified_results_node",
        table_name="task_loop_verified_results",
    )
    op.drop_table("task_loop_verified_results")
    op.drop_index(
        "ix_task_loop_node_attempts_node", table_name="task_loop_node_attempts"
    )
    op.drop_index(
        "ix_task_loop_node_attempts_claim", table_name="task_loop_node_attempts"
    )
    op.drop_table("task_loop_node_attempts")
    op.drop_index(
        "ix_model_planner_node_bindings_step",
        table_name="model_planner_node_bindings",
    )
    op.drop_table("model_planner_node_bindings")
    op.drop_index(
        "ix_task_loop_execution_events_chain",
        table_name="task_loop_execution_events",
    )
    op.drop_table("task_loop_execution_events")
    op.drop_index(
        "ix_task_loop_executions_task", table_name="task_loop_executions"
    )
    op.drop_index(
        "ix_task_loop_executions_recovery", table_name="task_loop_executions"
    )
    op.drop_table("task_loop_executions")
