"""Add PostgreSQL-native admission scheduling shards.

Revision ID: 0024_admission_shards
Revises: 0023_ready_membership
Create Date: 2026-08-15
"""

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_admission_shards"
down_revision: str | None = "0023_ready_membership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARD_COUNT = 16
_SEQUENCE_NAME = "tool_effect_dag_admission_grant_seq"


def _scheduling_shard(graph_id: str) -> int:
    digest = hashlib.sha256(graph_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % _SHARD_COUNT


def upgrade() -> None:
    op.create_table(
        "tool_effect_dag_admission_shards",
        sa.Column("shard_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_grant_sequence", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "shard_id >= 0 AND shard_id < 16 AND revision >= 1 "
            "AND (last_grant_sequence IS NULL OR last_grant_sequence >= 1)",
            name="ck_tool_effect_dag_admission_shards_values",
        ),
        sa.PrimaryKeyConstraint("shard_id"),
    )
    op.create_index(
        "ix_tool_effect_dag_admission_shards_fairness",
        "tool_effect_dag_admission_shards",
        ["last_grant_sequence", "shard_id"],
    )
    for shard_id in range(_SHARD_COUNT):
        op.execute(
            sa.text(
                "INSERT INTO tool_effect_dag_admission_shards "
                "(shard_id, revision, last_grant_sequence, updated_at) "
                "VALUES (:shard_id, 1, NULL, CURRENT_TIMESTAMP)"
            ).bindparams(shard_id=shard_id)
        )

    with op.batch_alter_table("tool_effect_dag_admissions") as batch:
        batch.add_column(
            sa.Column(
                "scheduling_shard",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.drop_constraint(
            "ck_tool_effect_dag_admissions_versions",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_tool_effect_dag_admissions_versions",
            "revision >= 1 AND fencing_token >= 0 AND lease_ttl_seconds >= 1 "
            "AND scheduling_shard >= 0 AND scheduling_shard < 16",
        )
    connection = op.get_bind()
    existing_admissions = connection.execute(
        sa.text("SELECT admission_id, graph_id FROM tool_effect_dag_admissions")
    ).mappings()
    for admission in existing_admissions:
        connection.execute(
            sa.text(
                "UPDATE tool_effect_dag_admissions "
                "SET scheduling_shard = :scheduling_shard "
                "WHERE admission_id = :admission_id"
            ),
            {
                "scheduling_shard": _scheduling_shard(str(admission["graph_id"])),
                "admission_id": admission["admission_id"],
            },
        )
    op.create_index(
        "ix_tool_effect_dag_admissions_shard_route",
        "tool_effect_dag_admissions",
        [
            "scheduling_shard",
            "status",
            "expires_at",
            "created_at",
            "batch_id",
            "admission_id",
        ],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f"CREATE SEQUENCE {_SEQUENCE_NAME} START WITH 1"))
        op.execute(
            sa.text(
                f"SELECT setval('{_SEQUENCE_NAME}', "
                "COALESCE((SELECT MAX(grant_sequence) + 1 "
                "FROM tool_effect_dag_admissions), 1), false)"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {_SEQUENCE_NAME}"))
    op.drop_index(
        "ix_tool_effect_dag_admissions_shard_route",
        table_name="tool_effect_dag_admissions",
    )
    with op.batch_alter_table("tool_effect_dag_admissions") as batch:
        batch.drop_constraint(
            "ck_tool_effect_dag_admissions_versions",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_tool_effect_dag_admissions_versions",
            "revision >= 1 AND fencing_token >= 0 AND lease_ttl_seconds >= 1",
        )
        batch.drop_column("scheduling_shard")
    op.drop_index(
        "ix_tool_effect_dag_admission_shards_fairness",
        table_name="tool_effect_dag_admission_shards",
    )
    op.drop_table("tool_effect_dag_admission_shards")
