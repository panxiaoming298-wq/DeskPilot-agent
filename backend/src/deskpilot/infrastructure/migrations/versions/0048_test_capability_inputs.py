"""Allow fixed Python and Node test results in dynamic Agent graphs.

Revision ID: 0048_agent_test_capability_inputs
Revises: 0047_agent_replans
"""

from collections.abc import Sequence

from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0048_agent_test_capability_inputs"
down_revision: str | None = "0047_agent_replans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_agent_results") as batch:
        batch.drop_constraint("ck_workspace_agent_result_kind", type_="check")
        batch.create_check_constraint(
            "ck_workspace_agent_result_kind",
            "result_kind IN ('file', 'directory', 'python_test', 'node_test')",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace Python or Node test result proof",
                "SELECT 1 FROM workspace_agent_results "
                "WHERE result_kind IN ('python_test', 'node_test') LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("workspace_agent_results") as batch:
        batch.drop_constraint("ck_workspace_agent_result_kind", type_="check")
        batch.create_check_constraint(
            "ck_workspace_agent_result_kind",
            "result_kind IN ('file', 'directory')",
        )
