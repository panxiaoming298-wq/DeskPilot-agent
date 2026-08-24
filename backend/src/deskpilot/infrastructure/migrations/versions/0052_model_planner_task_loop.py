"""add model Planner Draft and task-loop proof chain

Revision ID: 0052_model_planner_task_loop
Revises: 0051_turn_planning_offers
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0052_model_planner_task_loop"
down_revision: str | None = "0051_turn_planning_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_loops",
        sa.Column("loop_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("source_run_id", sa.String(length=68), nullable=False),
        sa.Column("source_run_digest", sa.String(length=64), nullable=False),
        sa.Column("source_adjudication_id", sa.String(length=68), nullable=False),
        sa.Column("source_adjudication_digest", sa.String(length=64), nullable=False),
        sa.Column("source_turn_plan_binding_id", sa.String(length=68), nullable=False),
        sa.Column(
            "source_turn_plan_binding_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("latest_event_id", sa.String(length=68), nullable=False),
        sa.Column("latest_event_digest", sa.String(length=64), nullable=False),
        sa.Column("progress_digest", sa.String(length=64), nullable=False),
        sa.Column("active_draft_id", sa.String(length=68), nullable=True),
        sa.Column("active_draft_record_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_manifest", sa.JSON(), nullable=True),
        sa.Column("failure_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("loop_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('observed', 'planned', 'failed') AND "
            "phase IN ('observe', 'plan')",
            name="ck_task_loop_state",
        ),
        sa.CheckConstraint(
            "(status = 'observed' AND phase = 'observe' AND revision = 1 AND "
            "event_count = 1 AND active_draft_id IS NULL AND "
            "active_draft_record_digest IS NULL AND failure_manifest IS NULL AND "
            "failure_digest IS NULL) OR "
            "(status = 'planned' AND phase = 'plan' AND revision = 2 AND "
            "event_count = 2 AND active_draft_id IS NOT NULL AND "
            "active_draft_record_digest IS NOT NULL AND failure_manifest IS NULL AND "
            "failure_digest IS NULL) OR "
            "(status = 'failed' AND phase = 'plan' AND revision = 2 AND "
            "event_count = 2 AND active_draft_id IS NULL AND "
            "active_draft_record_digest IS NULL AND failure_manifest IS NOT NULL AND "
            "failure_digest IS NOT NULL)",
            name="ck_task_loop_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_loop_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["conversation_messages.message_id"],
            name="fk_task_loop_message",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_run_id",
                "task_id",
                "user_message_id",
                "user_message_digest",
                "source_run_digest",
            ],
            [
                "turn_planner_runs.run_id",
                "turn_planner_runs.task_id",
                "turn_planner_runs.user_message_id",
                "turn_planner_runs.user_message_digest",
                "turn_planner_runs.run_digest",
            ],
            name="fk_task_loop_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_adjudication_id",
                "task_id",
                "user_message_id",
                "user_message_digest",
                "source_adjudication_digest",
            ],
            [
                "turn_planner_adjudications.adjudication_id",
                "turn_planner_adjudications.task_id",
                "turn_planner_adjudications.user_message_id",
                "turn_planner_adjudications.user_message_digest",
                "turn_planner_adjudications.adjudication_digest",
            ],
            name="fk_task_loop_adjudication_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_turn_plan_binding_id",
                "source_adjudication_id",
                "task_id",
                "user_message_id",
                "source_turn_plan_binding_digest",
            ],
            [
                "turn_plan_bindings.binding_id",
                "turn_plan_bindings.adjudication_id",
                "turn_plan_bindings.task_id",
                "turn_plan_bindings.user_message_id",
                "turn_plan_bindings.binding_digest",
            ],
            name="fk_task_loop_turn_binding_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("loop_id"),
        sa.UniqueConstraint(
            "source_turn_plan_binding_id", name="uq_task_loop_source_binding"
        ),
        sa.UniqueConstraint("loop_digest", name="uq_task_loop_digest"),
        sa.UniqueConstraint(
            "loop_id",
            "task_id",
            "user_message_id",
            "user_message_digest",
            name="uq_task_loop_scope",
        ),
    )
    op.create_index(
        "ix_task_loops_recovery", "task_loops", ["status", "updated_at"], unique=False
    )
    op.create_index(
        "ix_task_loops_message", "task_loops", ["task_id", "user_message_id"], unique=False
    )

    op.create_table(
        "task_loop_events",
        sa.Column("event_id", sa.String(length=68), nullable=False),
        sa.Column("loop_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=64), nullable=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("draft_id", sa.String(length=68), nullable=True),
        sa.Column("draft_record_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_manifest", sa.JSON(), nullable=True),
        sa.Column("failure_digest", sa.String(length=64), nullable=True),
        sa.Column("progress_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence BETWEEN 1 AND 2", name="ck_task_loop_event_sequence"
        ),
        sa.CheckConstraint(
            "phase IN ('observe', 'plan') AND "
            "kind IN ('observed', 'plan_bound', 'plan_failed')",
            name="ck_task_loop_event_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'observed' AND phase = 'observe' AND sequence = 1 AND "
            "previous_event_digest IS NULL AND draft_id IS NULL AND "
            "draft_record_digest IS NULL AND failure_manifest IS NULL AND "
            "failure_digest IS NULL) OR "
            "(kind = 'plan_bound' AND phase = 'plan' AND sequence = 2 AND "
            "previous_event_digest IS NOT NULL AND draft_id IS NOT NULL AND "
            "draft_record_digest IS NOT NULL AND failure_manifest IS NULL AND "
            "failure_digest IS NULL) OR "
            "(kind = 'plan_failed' AND phase = 'plan' AND sequence = 2 AND "
            "previous_event_digest IS NOT NULL AND draft_id IS NULL AND "
            "draft_record_digest IS NULL AND failure_manifest IS NOT NULL AND "
            "failure_digest IS NOT NULL)",
            name="ck_task_loop_event_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["loop_id", "task_id", "user_message_id", "user_message_digest"],
            [
                "task_loops.loop_id",
                "task_loops.task_id",
                "task_loops.user_message_id",
                "task_loops.user_message_digest",
            ],
            name="fk_task_loop_event_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["loop_id", "previous_event_digest"],
            ["task_loop_events.loop_id", "task_loop_events.event_digest"],
            name="fk_task_loop_event_previous",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("loop_id", "sequence", name="uq_task_loop_event_sequence"),
        sa.UniqueConstraint("event_digest", name="uq_task_loop_event_digest"),
        sa.UniqueConstraint(
            "loop_id", "event_digest", name="uq_task_loop_event_chain_target"
        ),
    )
    op.create_index(
        "ix_task_loop_events_loop",
        "task_loop_events",
        ["loop_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "model_planner_drafts",
        sa.Column("draft_id", sa.String(length=68), nullable=False),
        sa.Column("loop_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("source_run_id", sa.String(length=68), nullable=False),
        sa.Column("source_run_digest", sa.String(length=64), nullable=False),
        sa.Column("source_adjudication_id", sa.String(length=68), nullable=False),
        sa.Column("source_adjudication_digest", sa.String(length=64), nullable=False),
        sa.Column("source_turn_plan_binding_id", sa.String(length=68), nullable=False),
        sa.Column(
            "source_turn_plan_binding_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("composer_version", sa.String(length=64), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("ordered_steps_manifest", sa.JSON(), nullable=False),
        sa.Column("step_set_digest", sa.String(length=64), nullable=False),
        sa.Column("task_contract_manifest", sa.JSON(), nullable=False),
        sa.Column("task_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("draft_plan_manifest", sa.JSON(), nullable=False),
        sa.Column("draft_plan_digest", sa.String(length=64), nullable=False),
        sa.Column("expected_plan_manifest", sa.JSON(), nullable=False),
        sa.Column("expected_plan_id", sa.String(length=68), nullable=False),
        sa.Column("expected_plan_generation", sa.Integer(), nullable=False),
        sa.Column("expected_plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "expected_plan_binding_snapshot_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("draft_record_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "step_count BETWEEN 2 AND 8", name="ck_model_planner_draft_steps"
        ),
        sa.CheckConstraint(
            "expected_plan_generation = 1",
            name="ck_model_planner_draft_expected_plan",
        ),
        sa.ForeignKeyConstraint(
            ["loop_id", "task_id", "user_message_id", "user_message_digest"],
            [
                "task_loops.loop_id",
                "task_loops.task_id",
                "task_loops.user_message_id",
                "task_loops.user_message_digest",
            ],
            name="fk_model_planner_draft_loop_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_turn_plan_binding_id",
                "source_adjudication_id",
                "task_id",
                "user_message_id",
                "source_turn_plan_binding_digest",
            ],
            [
                "turn_plan_bindings.binding_id",
                "turn_plan_bindings.adjudication_id",
                "turn_plan_bindings.task_id",
                "turn_plan_bindings.user_message_id",
                "turn_plan_bindings.binding_digest",
            ],
            name="fk_model_planner_draft_source_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("draft_id"),
        sa.UniqueConstraint("loop_id", name="uq_model_planner_draft_loop"),
        sa.UniqueConstraint(
            "source_turn_plan_binding_id", name="uq_model_planner_draft_source_binding"
        ),
        sa.UniqueConstraint(
            "draft_record_digest", name="uq_model_planner_draft_digest"
        ),
        sa.UniqueConstraint(
            "draft_id",
            "loop_id",
            "task_id",
            "user_message_id",
            name="uq_model_planner_draft_scope",
        ),
    )
    op.create_index(
        "ix_model_planner_drafts_message",
        "model_planner_drafts",
        ["task_id", "user_message_id"],
        unique=False,
    )

    op.create_table(
        "model_planner_step_bindings",
        sa.Column("step_binding_id", sa.String(length=68), nullable=False),
        sa.Column("draft_id", sa.String(length=68), nullable=False),
        sa.Column("loop_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_id", sa.String(length=40), nullable=False),
        sa.Column("user_message_digest", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.String(length=68), nullable=False),
        sa.Column("offer_key", sa.String(length=68), nullable=False),
        sa.Column("offer_digest", sa.String(length=64), nullable=False),
        sa.Column("recipe_id", sa.String(length=64), nullable=False),
        sa.Column("recipe_version", sa.String(length=16), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("source_plan_id", sa.String(length=68), nullable=False),
        sa.Column("source_plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "source_plan_binding_snapshot_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("budget_manifest", sa.JSON(), nullable=False),
        sa.Column("budget_digest", sa.String(length=64), nullable=False),
        sa.Column("parameter_bindings_manifest", sa.JSON(), nullable=False),
        sa.Column("parameter_bindings_digest", sa.String(length=64), nullable=False),
        sa.Column("node_mappings_manifest", sa.JSON(), nullable=False),
        sa.Column("node_mappings_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("step_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal BETWEEN 1 AND 8", name="ck_model_planner_step_ordinal"
        ),
        sa.ForeignKeyConstraint(
            ["draft_id", "loop_id", "task_id", "user_message_id"],
            [
                "model_planner_drafts.draft_id",
                "model_planner_drafts.loop_id",
                "model_planner_drafts.task_id",
                "model_planner_drafts.user_message_id",
            ],
            name="fk_model_planner_step_draft_scope",
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
            name="fk_model_planner_step_offer_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("step_binding_id"),
        sa.UniqueConstraint(
            "draft_id", "ordinal", name="uq_model_planner_step_ordinal"
        ),
        sa.UniqueConstraint("draft_id", "offer_id", name="uq_model_planner_step_offer"),
        sa.UniqueConstraint(
            "step_binding_digest", name="uq_model_planner_step_binding_digest"
        ),
    )
    op.create_index(
        "ix_model_planner_steps_draft",
        "model_planner_step_bindings",
        ["draft_id", "ordinal"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "model Planner step bindings",
                "SELECT 1 FROM model_planner_step_bindings LIMIT 1",
            ),
            ("model Planner Drafts", "SELECT 1 FROM model_planner_drafts LIMIT 1"),
            ("task-loop events", "SELECT 1 FROM task_loop_events LIMIT 1"),
            ("task loops", "SELECT 1 FROM task_loops LIMIT 1"),
        ),
    )
    op.drop_index(
        "ix_model_planner_steps_draft", table_name="model_planner_step_bindings"
    )
    op.drop_table("model_planner_step_bindings")
    op.drop_index("ix_model_planner_drafts_message", table_name="model_planner_drafts")
    op.drop_table("model_planner_drafts")
    op.drop_index("ix_task_loop_events_loop", table_name="task_loop_events")
    op.drop_table("task_loop_events")
    op.drop_index("ix_task_loops_message", table_name="task_loops")
    op.drop_index("ix_task_loops_recovery", table_name="task_loops")
    op.drop_table("task_loops")
