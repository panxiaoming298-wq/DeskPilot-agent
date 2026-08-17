"""Add verified research, artifact workspace, browser evidence, and delivery.

Revision ID: 0032_verified_artifact_delivery
Revises: 0031_agent_research_runtime
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_verified_artifact_delivery"
down_revision: str | None = "0031_agent_research_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_status_checks(*, upgraded: bool) -> None:
    values = {
        "task_execution_runs": (
            "ck_execution_run_status",
            "status IN ('active', 'awaiting_verification', 'paused', 'cancelled', "
            + ("'superseded', 'failed', 'succeeded')" if upgraded else "'superseded', 'failed')"),
        ),
        "task_execution_nodes": (
            "ck_execution_node_status",
            "status IN ('pending', 'ready', 'claimed', 'running', "
            + (
                "'awaiting_verification', 'verified', 'cancelled', 'failed')"
                if upgraded
                else "'awaiting_verification', 'cancelled', 'failed')"
            ),
        ),
        "agent_invocations": (
            "ck_agent_invocation_verification_status",
            (
                "verification_status IN ('not_requested', 'pending', 'verified', 'rejected')"
                if upgraded
                else "verification_status IN ('not_requested', 'pending')"
            ),
        ),
        "research_sessions": (
            "ck_research_session_status",
            "status IN ('created', 'running', 'awaiting_verification', "
            + ("'verified', 'rejected', 'failed')" if upgraded else "'failed')"),
        ),
    }
    for table_name, (constraint_name, expression) in values.items():
        with op.batch_alter_table(table_name) as batch:
            batch.drop_constraint(constraint_name, type_="check")
            batch.create_check_constraint(constraint_name, expression)


def upgrade() -> None:
    _replace_status_checks(upgraded=True)
    op.create_table(
        "verification_runs",
        sa.Column("verification_run_id", sa.String(68), primary_key=True),
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
        sa.Column(
            "result_id",
            sa.String(68),
            sa.ForeignKey("agent_results.result_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.String(100), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("evidence_snapshot_id", sa.String(68), nullable=True),
        sa.Column("input_manifest_digest", sa.String(64), nullable=False),
        sa.Column("grader_request_digest", sa.String(64), nullable=False),
        sa.Column("grader_output_digest", sa.String(64), nullable=True),
        sa.Column("grader_provider_id", sa.String(64), nullable=False),
        sa.Column("grader_model", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="ck_verification_run_attempt"),
        sa.CheckConstraint("status IN ('completed', 'failed')", name="ck_verification_run_status"),
        sa.CheckConstraint(
            "outcome IN ('verified', 'rejected', 'verification_error')",
            name="ck_verification_run_outcome",
        ),
        sa.UniqueConstraint(
            "result_id", "policy_digest", "attempt", name="uq_verification_attempt"
        ),
    )
    op.create_index("ix_verification_runs_run_id", "verification_runs", ["run_id"])
    op.create_table(
        "verification_evidence_snapshots",
        sa.Column("evidence_snapshot_id", sa.String(68), primary_key=True),
        sa.Column(
            "verification_run_id",
            sa.String(68),
            sa.ForeignKey("verification_runs.verification_run_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "claim_verdicts",
        sa.Column(
            "verification_run_id",
            sa.String(68),
            sa.ForeignKey("verification_runs.verification_run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "claim_id",
            sa.String(68),
            sa.ForeignKey("research_claims.claim_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("citation_ids", sa.JSON(), nullable=False),
        sa.Column("verdict_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "task_artifact_workspaces",
        sa.Column("workspace_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("allowed_extensions", sa.JSON(), nullable=False),
        sa.Column("max_total_bytes", sa.Integer(), nullable=False),
        sa.Column("max_files", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_task_artifact_workspace_revision"),
        sa.CheckConstraint("status IN ('active', 'delivered')", name="ck_task_workspace_status"),
        sa.UniqueConstraint("task_id", "run_id", name="uq_task_workspace_run"),
    )
    op.create_index("ix_task_artifact_workspaces_task_id", "task_artifact_workspaces", ["task_id"])
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(68), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(68),
            sa.ForeignKey("task_artifact_workspaces.workspace_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column("active_revision_id", sa.String(68), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "relative_path", name="uq_artifact_path"),
    )
    op.create_index("ix_artifacts_workspace_id", "artifacts", ["workspace_id"])
    op.create_table(
        "artifact_revisions",
        sa.Column("revision_id", sa.String(68), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(68),
            sa.ForeignKey("artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("blob_name", sa.String(128), nullable=False),
        sa.Column("patch_receipt_id", sa.String(68), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision_no >= 1 AND byte_count >= 1", name="ck_artifact_revision"),
        sa.UniqueConstraint("artifact_id", "revision_no", name="uq_artifact_revision_no"),
    )
    op.create_index("ix_artifact_revisions_artifact_id", "artifact_revisions", ["artifact_id"])
    op.create_table(
        "artifact_patch_receipts",
        sa.Column("patch_receipt_id", sa.String(68), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(68),
            sa.ForeignKey("task_artifact_workspaces.workspace_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(68),
            sa.ForeignKey("artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column("base_revision_id", sa.String(68), nullable=True),
        sa.Column("new_revision_id", sa.String(68), nullable=False, unique=True),
        sa.Column("base_digest", sa.String(64), nullable=True),
        sa.Column("new_digest", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("receipt_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_artifact_patch_receipts_workspace_id", "artifact_patch_receipts", ["workspace_id"]
    )
    op.create_table(
        "browser_render_runs",
        sa.Column("browser_run_id", sa.String(68), primary_key=True),
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
        sa.Column(
            "revision_id",
            sa.String(68),
            sa.ForeignKey("artifact_revisions.revision_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("engine", sa.String(200), nullable=False),
        sa.Column("profile_id", sa.String(100), nullable=False),
        sa.Column("viewport_width", sa.Integer(), nullable=False),
        sa.Column("viewport_height", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('passed', 'failed')", name="ck_browser_render_status"),
        sa.UniqueConstraint("run_id", "revision_id", name="uq_browser_render_revision"),
    )
    op.create_index("ix_browser_render_runs_run_id", "browser_render_runs", ["run_id"])
    op.create_table(
        "delivery_manifests",
        sa.Column("delivery_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_delivery_manifests_task_id", "delivery_manifests", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_delivery_manifests_task_id", table_name="delivery_manifests")
    op.drop_table("delivery_manifests")
    op.drop_index("ix_browser_render_runs_run_id", table_name="browser_render_runs")
    op.drop_table("browser_render_runs")
    op.drop_index("ix_artifact_patch_receipts_workspace_id", table_name="artifact_patch_receipts")
    op.drop_table("artifact_patch_receipts")
    op.drop_index("ix_artifact_revisions_artifact_id", table_name="artifact_revisions")
    op.drop_table("artifact_revisions")
    op.drop_index("ix_artifacts_workspace_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_task_artifact_workspaces_task_id", table_name="task_artifact_workspaces")
    op.drop_table("task_artifact_workspaces")
    op.drop_table("claim_verdicts")
    op.drop_table("verification_evidence_snapshots")
    op.drop_index("ix_verification_runs_run_id", table_name="verification_runs")
    op.drop_table("verification_runs")
    _replace_status_checks(upgraded=False)
