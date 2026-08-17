"""Add persistent Agent invocation and read-only research runtime.

Revision ID: 0031_agent_research_runtime
Revises: 0030_task_contract_plans
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_agent_research_runtime"
down_revision: str | None = "0030_task_contract_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_execution_runs",
        sa.Column("run_id", sa.String(68), primary_key=True),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["task_id", "plan_generation"],
            ["task_plan_generations.task_id", "task_plan_generations.generation"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "plan_generation >= 1 AND revision >= 1", name="ck_execution_run"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'awaiting_verification', 'paused', 'cancelled', "
            "'superseded', 'failed')",
            name="ck_execution_run_status",
        ),
        sa.UniqueConstraint("task_id", "plan_generation", name="uq_execution_run_plan"),
    )
    op.create_index("ix_execution_runs_task", "task_execution_runs", ["task_id", "created_at"])
    op.create_table(
        "task_execution_nodes",
        sa.Column("node_id", sa.String(68), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("local_key", sa.String(64), nullable=False),
        sa.Column("node_kind", sa.String(32), nullable=False),
        sa.Column("node_spec_digest", sa.String(64), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("bound_agent", sa.JSON(), nullable=True),
        sa.Column("capability", sa.JSON(), nullable=True),
        sa.Column("acceptance_refs", sa.JSON(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("runtime_enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("claim_owner_id", sa.String(128), nullable=True),
        sa.Column("claim_fencing_token", sa.Integer(), nullable=False),
        sa.Column("claim_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1 AND attempt_count >= 0", name="ck_execution_node"),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'claimed', 'running', "
            "'awaiting_verification', 'cancelled', 'failed')",
            name="ck_execution_node_status",
        ),
        sa.UniqueConstraint("run_id", "local_key", name="uq_execution_node_key"),
    )
    op.create_index(
        "ix_execution_nodes_ready", "task_execution_nodes", ["run_id", "status", "local_key"]
    )
    op.create_index(
        "ix_execution_nodes_lease", "task_execution_nodes", ["status", "claim_expires_at"]
    )
    op.create_table(
        "task_execution_edges",
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "from_node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "to_node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("requirement", sa.String(32), nullable=False),
        sa.CheckConstraint("requirement IN ('verified')", name="ck_execution_edge_requirement"),
    )
    op.create_table(
        "agent_handoffs",
        sa.Column("handoff_id", sa.String(68), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("handoff_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_handoffs_run_id", "agent_handoffs", ["run_id"])
    op.create_table(
        "agent_invocations",
        sa.Column("invocation_id", sa.String(68), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "handoff_id",
            sa.String(68),
            sa.ForeignKey("agent_handoffs.handoff_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("agent_contract_digest", sa.String(64), nullable=False),
        sa.Column("prompt_package_digest", sa.String(64), nullable=False),
        sa.Column("execution_status", sa.String(32), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("result_id", sa.String(68), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_agent_invocation_attempt"),
        sa.CheckConstraint(
            "execution_status IN ('created', 'running', 'result_submitted', "
            "'failed_retryable', 'failed_terminal', 'cancelled', 'expired')",
            name="ck_agent_invocation_execution_status",
        ),
        sa.CheckConstraint(
            "verification_status IN ('not_requested', 'pending')",
            name="ck_agent_invocation_verification_status",
        ),
        sa.UniqueConstraint("node_id", "attempt", name="uq_agent_invocation_attempt"),
    )
    op.create_index("ix_agent_invocations_run", "agent_invocations", ["run_id", "created_at"])
    op.create_table(
        "agent_model_turns",
        sa.Column("turn_id", sa.String(68), primary_key=True),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("stable_error_code", sa.String(100), nullable=True),
        sa.Column("claim_owner_id", sa.String(128), nullable=False),
        sa.Column("claim_fencing_token", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("turn_no >= 1", name="ck_agent_model_turn_no"),
        sa.CheckConstraint(
            "status IN ('prepared', 'dispatching', 'succeeded', 'failed', "
            "'outcome_unknown')",
            name="ck_agent_model_turn_status",
        ),
        sa.UniqueConstraint("invocation_id", "turn_no", name="uq_agent_model_turn"),
    )
    op.create_index("ix_agent_model_turns_invocation_id", "agent_model_turns", ["invocation_id"])
    op.create_table(
        "agent_results",
        sa.Column("result_id", sa.String(68), primary_key=True),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "research_sessions",
        sa.Column("research_session_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('created', 'running', 'awaiting_verification', 'failed')",
            name="ck_research_session_status",
        ),
    )
    op.create_index("ix_research_sessions_task_id", "research_sessions", ["task_id"])
    op.create_table(
        "research_search_calls",
        sa.Column("search_call_id", sa.String(68), primary_key=True),
        sa.Column(
            "research_session_id",
            sa.String(68),
            sa.ForeignKey("research_sessions.research_session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("query_digest", sa.String(64), nullable=False),
        sa.Column("hits", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("research_session_id", "attempt", name="uq_research_search_attempt"),
    )
    op.create_index(
        "ix_research_search_calls_research_session_id",
        "research_search_calls",
        ["research_session_id"],
    )
    op.create_table(
        "research_page_snapshots",
        sa.Column("page_snapshot_id", sa.String(68), primary_key=True),
        sa.Column(
            "research_session_id",
            sa.String(68),
            sa.ForeignKey("research_sessions.research_session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("search_hit_id", sa.String(68), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_page_snapshots_research_session_id",
        "research_page_snapshots",
        ["research_session_id"],
    )
    op.create_table(
        "research_claims",
        sa.Column("claim_id", sa.String(68), primary_key=True),
        sa.Column(
            "research_session_id",
            sa.String(68),
            sa.ForeignKey("research_sessions.research_session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("claim_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_claims_research_session_id", "research_claims", ["research_session_id"]
    )
    op.create_table(
        "research_citations",
        sa.Column("citation_id", sa.String(68), primary_key=True),
        sa.Column(
            "research_session_id",
            sa.String(68),
            sa.ForeignKey("research_sessions.research_session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "claim_id",
            sa.String(68),
            sa.ForeignKey("research_claims.claim_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "page_snapshot_id",
            sa.String(68),
            sa.ForeignKey("research_page_snapshots.page_snapshot_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("citation_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_citations_research_session_id",
        "research_citations",
        ["research_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_citations_research_session_id", table_name="research_citations")
    op.drop_table("research_citations")
    op.drop_index("ix_research_claims_research_session_id", table_name="research_claims")
    op.drop_table("research_claims")
    op.drop_index(
        "ix_research_page_snapshots_research_session_id",
        table_name="research_page_snapshots",
    )
    op.drop_table("research_page_snapshots")
    op.drop_index(
        "ix_research_search_calls_research_session_id",
        table_name="research_search_calls",
    )
    op.drop_table("research_search_calls")
    op.drop_index("ix_research_sessions_task_id", table_name="research_sessions")
    op.drop_table("research_sessions")
    op.drop_table("agent_results")
    op.drop_index("ix_agent_model_turns_invocation_id", table_name="agent_model_turns")
    op.drop_table("agent_model_turns")
    op.drop_index("ix_agent_invocations_run", table_name="agent_invocations")
    op.drop_table("agent_invocations")
    op.drop_index("ix_agent_handoffs_run_id", table_name="agent_handoffs")
    op.drop_table("agent_handoffs")
    op.drop_table("task_execution_edges")
    op.drop_index("ix_execution_nodes_lease", table_name="task_execution_nodes")
    op.drop_index("ix_execution_nodes_ready", table_name="task_execution_nodes")
    op.drop_table("task_execution_nodes")
    op.drop_index("ix_execution_runs_task", table_name="task_execution_runs")
    op.drop_table("task_execution_runs")
