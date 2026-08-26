"""bind workspace Explorer proposals to persistent Agent Model Turns

Revision ID: 0062_workspace_coding_explorer_turns
Revises: 0061_workspace_coding_explorations
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0062_workspace_coding_explorer_turns"
down_revision: str | None = "0061_workspace_coding_explorations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input', "
            "'propose_handoff', 'propose_task_graph', 'propose_file_set')",
        )
    op.create_table(
        "workspace_coding_explorer_run_bindings",
        sa.Column("binding_id", sa.String(length=68), nullable=False),
        sa.Column("snapshot_id", sa.String(length=68), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("source_task_id", sa.String(length=40), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("explorer_node_id", sa.String(length=68), nullable=False),
        sa.Column("explorer_node_spec_digest", sa.String(length=64), nullable=False),
        sa.Column("explorer_agent_id", sa.String(length=128), nullable=False),
        sa.Column("explorer_agent_version", sa.String(length=32), nullable=False),
        sa.Column(
            "explorer_agent_contract_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "explorer_prompt_package_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version = 1 AND plan_generation = 1",
            name="ck_workspace_coding_explorer_run_generation",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["workspace_coding_exploration_snapshots.snapshot_id"],
            name="fk_workspace_coding_explorer_run_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id", "contract_version"],
            ["task_contract_versions.task_id", "task_contract_versions.version"],
            name="fk_workspace_coding_explorer_run_contract",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id", "plan_generation"],
            ["task_plan_generations.task_id", "task_plan_generations.generation"],
            name="fk_workspace_coding_explorer_run_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_execution_runs.run_id"],
            name="fk_workspace_coding_explorer_run_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["explorer_node_id"],
            ["task_execution_nodes.node_id"],
            name="fk_workspace_coding_explorer_run_node",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "binding_id",
            name="pk_workspace_coding_explorer_run_bindings",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            name="uq_workspace_coding_explorer_run_snapshot",
        ),
        sa.UniqueConstraint(
            "source_task_id",
            name="uq_workspace_coding_explorer_run_task",
        ),
        sa.UniqueConstraint("run_id", name="uq_workspace_coding_explorer_run_run"),
        sa.UniqueConstraint(
            "explorer_node_id",
            name="uq_workspace_coding_explorer_run_node",
        ),
        sa.UniqueConstraint(
            "binding_digest",
            name="uq_workspace_coding_explorer_run_digest",
        ),
    )
    op.create_index(
        "ix_workspace_coding_explorer_run_created",
        "workspace_coding_explorer_run_bindings",
        ["source_task_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "workspace_coding_explorer_turn_proofs",
        sa.Column("proof_id", sa.String(length=68), nullable=False),
        sa.Column("proposal_id", sa.String(length=68), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("run_binding_id", sa.String(length=68), nullable=False),
        sa.Column("run_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("invocation_id", sa.String(length=68), nullable=False),
        sa.Column("turn_id", sa.String(length=68), nullable=False),
        sa.Column("agent_decision_id", sa.String(length=68), nullable=False),
        sa.Column("agent_decision_digest", sa.String(length=64), nullable=False),
        sa.Column("model_request_digest", sa.String(length=64), nullable=False),
        sa.Column("model_response_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["workspace_coding_exploration_proposals.proposal_id"],
            name="fk_workspace_coding_explorer_turn_proposal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_binding_id"],
            ["workspace_coding_explorer_run_bindings.binding_id"],
            name="fk_workspace_coding_explorer_turn_run_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["agent_invocations.invocation_id"],
            name="fk_workspace_coding_explorer_turn_invocation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["agent_model_turns.turn_id"],
            name="fk_workspace_coding_explorer_turn_model_turn",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_decision_id"],
            ["agent_decisions.decision_id"],
            name="fk_workspace_coding_explorer_turn_decision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "proof_id",
            name="pk_workspace_coding_explorer_turn_proofs",
        ),
        sa.UniqueConstraint(
            "proposal_id",
            name="uq_workspace_coding_explorer_turn_proposal",
        ),
        sa.UniqueConstraint(
            "run_binding_id",
            name="uq_workspace_coding_explorer_turn_run_binding",
        ),
        sa.UniqueConstraint(
            "invocation_id",
            name="uq_workspace_coding_explorer_turn_invocation",
        ),
        sa.UniqueConstraint(
            "turn_id",
            name="uq_workspace_coding_explorer_turn_turn",
        ),
        sa.UniqueConstraint(
            "agent_decision_id",
            name="uq_workspace_coding_explorer_turn_decision",
        ),
        sa.UniqueConstraint(
            "proof_digest",
            name="uq_workspace_coding_explorer_turn_digest",
        ),
    )
    op.create_index(
        "ix_workspace_coding_explorer_turn_created",
        "workspace_coding_explorer_turn_proofs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace coding Explorer Model Turn proof",
                "SELECT 1 FROM workspace_coding_explorer_turn_proofs LIMIT 1",
            ),
            (
                "workspace coding Explorer Run binding",
                "SELECT 1 FROM workspace_coding_explorer_run_bindings LIMIT 1",
            ),
            (
                "agent decision kind 'propose_file_set'",
                "SELECT 1 FROM agent_decisions WHERE kind = 'propose_file_set' LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workspace_coding_explorer_turn_created",
        table_name="workspace_coding_explorer_turn_proofs",
    )
    op.drop_table("workspace_coding_explorer_turn_proofs")
    op.drop_index(
        "ix_workspace_coding_explorer_run_created",
        table_name="workspace_coding_explorer_run_bindings",
    )
    op.drop_table("workspace_coding_explorer_run_bindings")
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input', "
            "'propose_handoff', 'propose_task_graph')",
        )
