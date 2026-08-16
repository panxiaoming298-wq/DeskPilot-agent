"""PostgreSQL-native bounded admission shard scheduling queries."""

from datetime import datetime

from sqlalchemy import Select, exists, select

from deskpilot.infrastructure.models import (
    ToolEffectDagAdmissionRecord,
    ToolEffectDagAdmissionShardRecord,
)


def build_postgresql_admission_shard_lock_statement(
    *,
    database_time: datetime,
) -> Select[tuple[ToolEffectDagAdmissionShardRecord]]:
    """Lock the least-recently-served shard that still has live pending work."""
    pending = exists(
        select(1).where(
            ToolEffectDagAdmissionRecord.scheduling_shard
            == ToolEffectDagAdmissionShardRecord.shard_id,
            ToolEffectDagAdmissionRecord.status == "pending",
            ToolEffectDagAdmissionRecord.expires_at > database_time,
        )
    )
    return (
        select(ToolEffectDagAdmissionShardRecord)
        .where(pending)
        .order_by(
            ToolEffectDagAdmissionShardRecord.last_grant_sequence.asc().nulls_first(),
            ToolEffectDagAdmissionShardRecord.shard_id,
        )
        .limit(1)
        .with_for_update(skip_locked=True, of=ToolEffectDagAdmissionShardRecord)
    )


def build_postgresql_admission_candidate_statement(
    *,
    shard_id: int,
    database_time: datetime,
    candidate_limit: int,
) -> Select[tuple[ToolEffectDagAdmissionRecord]]:
    """Read a bounded, stable candidate prefix owned by one locked shard."""
    return (
        select(ToolEffectDagAdmissionRecord)
        .where(
            ToolEffectDagAdmissionRecord.scheduling_shard == shard_id,
            ToolEffectDagAdmissionRecord.status == "pending",
            ToolEffectDagAdmissionRecord.expires_at > database_time,
        )
        .order_by(
            ToolEffectDagAdmissionRecord.expires_at,
            ToolEffectDagAdmissionRecord.created_at,
            ToolEffectDagAdmissionRecord.batch_id,
            ToolEffectDagAdmissionRecord.admission_id,
        )
        .limit(candidate_limit)
    )
