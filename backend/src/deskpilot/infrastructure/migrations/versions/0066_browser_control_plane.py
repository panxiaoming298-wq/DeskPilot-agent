"""persist the local-only Edge Browser control-plane projection

Revision ID: 0066_browser_control_plane
Revises: 0065_confirmed_change_task_loop
Create Date: 2026-08-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_browser_control_plane"
down_revision: str | None = "0065_confirmed_change_task_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_control_plane_state",
        sa.Column("configuration_id", sa.String(length=64), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("browser_product", sa.String(length=64), nullable=False),
        sa.Column("profile_name", sa.String(length=64), nullable=False),
        sa.Column("profile_mode", sa.String(length=64), nullable=False),
        sa.Column(
            "profile_created",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "operator_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("active_allowlist_revision", sa.Integer(), nullable=False),
        sa.Column("active_allowlist_digest", sa.String(length=64), nullable=False),
        sa.Column("control_plane_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "configuration_id = 'edge-deskpilot-v1'",
            name="ck_browser_control_plane_configuration",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND active_allowlist_revision >= 1",
            name="ck_browser_control_plane_revisions",
        ),
        sa.CheckConstraint(
            "browser_product = 'microsoft_edge' AND profile_name = 'DeskPilot' "
            "AND profile_mode = 'application_managed_dedicated'",
            name="ck_browser_control_plane_profile",
        ),
        sa.CheckConstraint(
            "profile_created = false AND operator_enabled = false",
            name="ck_browser_control_plane_disabled",
        ),
        sa.PrimaryKeyConstraint("configuration_id"),
        sa.UniqueConstraint("control_plane_digest"),
    )
    op.create_table(
        "browser_origin_allowlist_snapshots",
        sa.Column("configuration_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("origins", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "configuration_id = 'edge-deskpilot-v1' AND revision >= 1",
            name="ck_browser_origin_allowlist_identity",
        ),
        sa.CheckConstraint(
            "updated_by = 'local_user'",
            name="ck_browser_origin_allowlist_actor",
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id"],
            ["browser_control_plane_state.configuration_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("configuration_id", "revision"),
        sa.UniqueConstraint("snapshot_digest"),
    )
    op.create_index(
        "ix_browser_origin_allowlist_updated_at",
        "browser_origin_allowlist_snapshots",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_browser_origin_allowlist_updated_at",
        table_name="browser_origin_allowlist_snapshots",
    )
    op.drop_table("browser_origin_allowlist_snapshots")
    op.drop_table("browser_control_plane_state")
