"""persist verified Reader change proposals and confirmed write Plans

Revision ID: 0064_workspace_coding_change_proposals
Revises: 0063_confirmed_reader_task_loop
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import refuse_downgrade_if_rows

revision: str = "0064_workspace_coding_change_proposals"
down_revision: str | None = "0063_confirmed_reader_task_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_coding_change_run_bindings",
        sa.Column("binding_id", sa.String(length=68), primary_key=True),
        sa.Column(
            "file_set_binding_id",
            sa.String(length=68),
            sa.ForeignKey(
                "workspace_coding_file_set_plan_bindings.binding_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("file_set_binding_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "reader_execution_id",
            sa.String(length=68),
            sa.ForeignKey("task_loop_executions.execution_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reader_execution_digest", sa.String(length=64), nullable=False),
        sa.Column("reader_terminal_event_digest", sa.String(length=64), nullable=False),
        sa.Column("reader_result_set_digest", sa.String(length=64), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("reader_task_id", sa.String(length=40), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "proposer_node_id",
            sa.String(length=68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("proposer_node_spec_digest", sa.String(length=64), nullable=False),
        sa.Column("proposer_agent_id", sa.String(length=128), nullable=False),
        sa.Column("proposer_agent_version", sa.String(length=32), nullable=False),
        sa.Column("proposer_agent_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("proposer_prompt_package_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version = 2 AND plan_generation = 2 AND result_count BETWEEN 2 AND 8",
            name="ck_workspace_coding_change_run_scope",
        ),
        sa.UniqueConstraint(
            "file_set_binding_id", name="uq_workspace_coding_change_run_file_set"
        ),
        sa.UniqueConstraint(
            "reader_execution_id", name="uq_workspace_coding_change_run_execution"
        ),
        sa.UniqueConstraint("reader_task_id", name="uq_workspace_coding_change_run_task"),
        sa.UniqueConstraint("run_id", name="uq_workspace_coding_change_run_run"),
        sa.UniqueConstraint("proposer_node_id", name="uq_workspace_coding_change_run_node"),
        sa.UniqueConstraint("binding_digest", name="uq_workspace_coding_change_run_digest"),
        sa.ForeignKeyConstraint(
            ["reader_task_id", "contract_version"],
            ["task_contract_versions.task_id", "task_contract_versions.version"],
            name="fk_workspace_coding_change_run_contract",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reader_task_id", "plan_generation"],
            ["task_plan_generations.task_id", "task_plan_generations.generation"],
            name="fk_workspace_coding_change_run_plan",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_workspace_coding_change_run_created",
        "workspace_coding_change_run_bindings",
        ["created_at"],
    )

    op.create_table(
        "workspace_coding_change_proposals",
        sa.Column("proposal_id", sa.String(length=68), primary_key=True),
        sa.Column(
            "run_binding_id",
            sa.String(length=68),
            sa.ForeignKey("workspace_coding_change_run_bindings.binding_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_binding_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "reader_task_id",
            sa.String(length=40),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reader_execution_id", sa.String(length=68), nullable=False),
        sa.Column("reader_result_set_digest", sa.String(length=64), nullable=False),
        sa.Column("proposer_agent_id", sa.String(length=128), nullable=False),
        sa.Column("proposer_agent_version", sa.String(length=32), nullable=False),
        sa.Column("proposer_agent_contract_digest", sa.String(length=64), nullable=False),
        sa.Column("proposer_prompt_package_digest", sa.String(length=64), nullable=False),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "change_count BETWEEN 2 AND 8",
            name="ck_workspace_coding_change_proposal_count",
        ),
        sa.UniqueConstraint("run_binding_id", name="uq_workspace_coding_change_proposal_run"),
        sa.UniqueConstraint(
            "proposal_digest", name="uq_workspace_coding_change_proposal_digest"
        ),
    )
    op.create_index(
        "ix_workspace_coding_change_proposal_created",
        "workspace_coding_change_proposals",
        ["created_at"],
    )

    op.create_table(
        "workspace_coding_change_turn_proofs",
        sa.Column("proof_id", sa.String(length=68), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(length=68),
            sa.ForeignKey("workspace_coding_change_proposals.proposal_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "run_binding_id",
            sa.String(length=68),
            sa.ForeignKey("workspace_coding_change_run_bindings.binding_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_binding_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "invocation_id",
            sa.String(length=68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.String(length=68),
            sa.ForeignKey("agent_model_turns.turn_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "agent_decision_id",
            sa.String(length=68),
            sa.ForeignKey("agent_decisions.decision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("agent_decision_digest", sa.String(length=64), nullable=False),
        sa.Column("model_request_digest", sa.String(length=64), nullable=False),
        sa.Column("model_response_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("proposal_id", name="uq_workspace_coding_change_turn_proposal"),
        sa.UniqueConstraint("invocation_id", name="uq_workspace_coding_change_turn_invocation"),
        sa.UniqueConstraint("turn_id", name="uq_workspace_coding_change_turn_turn"),
        sa.UniqueConstraint(
            "agent_decision_id", name="uq_workspace_coding_change_turn_decision"
        ),
        sa.UniqueConstraint("proof_digest", name="uq_workspace_coding_change_turn_digest"),
    )
    op.create_index(
        "ix_workspace_coding_change_turn_created",
        "workspace_coding_change_turn_proofs",
        ["created_at"],
    )

    op.create_table(
        "workspace_coding_write_plan_bindings",
        sa.Column("binding_id", sa.String(length=68), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(length=68),
            sa.ForeignKey("workspace_coding_change_proposals.proposal_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("successor_task_id", sa.String(length=40), nullable=False),
        sa.Column(
            "confirmation_message_id",
            sa.String(length=40),
            sa.ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("confirmation_message_digest", sa.String(length=64), nullable=False),
        sa.Column("route_id", sa.String(length=64), nullable=False),
        sa.Column("route_version", sa.String(length=8), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("parameter_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("parameters_digest", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version = 1 AND plan_generation = 1 AND change_count BETWEEN 2 AND 8",
            name="ck_workspace_coding_write_plan_scope",
        ),
        sa.UniqueConstraint("proposal_id", name="uq_workspace_coding_write_plan_proposal"),
        sa.UniqueConstraint("successor_task_id", name="uq_workspace_coding_write_plan_task"),
        sa.UniqueConstraint(
            "confirmation_message_id", name="uq_workspace_coding_write_plan_message"
        ),
        sa.UniqueConstraint("binding_digest", name="uq_workspace_coding_write_plan_digest"),
        sa.ForeignKeyConstraint(
            ["successor_task_id", "contract_version"],
            ["task_contract_versions.task_id", "task_contract_versions.version"],
            name="fk_workspace_coding_write_plan_contract",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_task_id", "plan_generation"],
            ["task_plan_generations.task_id", "task_plan_generations.generation"],
            name="fk_workspace_coding_write_plan_plan",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_workspace_coding_write_plan_created",
        "workspace_coding_write_plan_bindings",
        ["created_at"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace coding change proof",
                "SELECT 1 FROM workspace_coding_change_run_bindings LIMIT 1",
            ),
            (
                "workspace coding write Plan binding",
                "SELECT 1 FROM workspace_coding_write_plan_bindings LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workspace_coding_write_plan_created",
        table_name="workspace_coding_write_plan_bindings",
    )
    op.drop_table("workspace_coding_write_plan_bindings")
    op.drop_index(
        "ix_workspace_coding_change_turn_created",
        table_name="workspace_coding_change_turn_proofs",
    )
    op.drop_table("workspace_coding_change_turn_proofs")
    op.drop_index(
        "ix_workspace_coding_change_proposal_created",
        table_name="workspace_coding_change_proposals",
    )
    op.drop_table("workspace_coding_change_proposals")
    op.drop_index(
        "ix_workspace_coding_change_run_created",
        table_name="workspace_coding_change_run_bindings",
    )
    op.drop_table("workspace_coding_change_run_bindings")
