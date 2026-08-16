"""Database-coordinated fair admission for effect DAG Runner work."""

import asyncio
import hashlib
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.effect_dag_admission import (
    EffectDagAdmissionCancelledError,
    EffectDagAdmissionPermitPort,
    EffectDagAdmissionRequest,
    EffectDagAdmissionSnapshot,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.effect_graph import EffectDagAdmissionProof
from deskpilot.infrastructure.admission_shard_queries import (
    build_postgresql_admission_candidate_statement,
    build_postgresql_admission_shard_lock_statement,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import (
    ToolEffectDagAdmissionRecord,
    ToolEffectDagAdmissionStateRecord,
)

_T = TypeVar("_T")
_SCHEDULER_SCOPE = "global"
_SCHEDULING_SHARD_COUNT = 16
_POSTGRESQL_CANDIDATE_LIMIT = 2_048
_POSTGRESQL_GRANT_SEQUENCE = "tool_effect_dag_admission_grant_seq"


class EffectDagClusterAdmissionStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    RELEASED = "released"
    CANCELLED = "cancelled"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class EffectDagAdmissionFenceRejectedError(RuntimeError):
    """A stale admission permit attempted to renew or release capacity."""


class EffectDagAdmissionPermitLostError(RuntimeError):
    """The database-backed capacity permit expired or lost its fence."""


class EffectDagAdmissionConfigurationMismatchError(RuntimeError):
    """Live cluster tickets are bound to a different capacity configuration."""


class _SchedulerRevisionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EffectDagClusterAdmissionEntry:
    admission_id: str
    batch_id: str
    graph_id: str
    request: EffectDagAdmissionRequest
    owner_id: str
    status: EffectDagClusterAdmissionStatus
    revision: int
    fencing_token: int
    grant_sequence: int | None
    expires_at: datetime


class EffectDagClusterAdmissionStore:
    """Transactional tickets, capacity accounting, and fair grant sequencing."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._scheduler_lock = asyncio.Lock()

    async def ensure_configuration(
        self,
        *,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int],
    ) -> str:
        configuration_digest, tool_limits_digest = self._configuration_digests(
            global_limit=global_limit,
            per_graph_limit=per_graph_limit,
            default_tool_limit=default_tool_limit,
            tool_limits=tool_limits,
        )

        async with self._database.session() as session:
            current_digest = await session.scalar(
                select(ToolEffectDagAdmissionStateRecord.configuration_digest).where(
                    ToolEffectDagAdmissionStateRecord.scope_id == _SCHEDULER_SCOPE
                )
            )
        if current_digest == configuration_digest:
            return configuration_digest

        async def operation() -> str:
            async with self._database.session() as session:
                async with session.begin():
                    database_now = await database_utc_now(session)
                    state_revision, _, current_digest = await self._lock_scheduler(
                        session,
                        database_now,
                    )
                    if current_digest == configuration_digest:
                        return configuration_digest
                    live_count = await session.scalar(
                        select(func.count())
                        .select_from(ToolEffectDagAdmissionRecord)
                        .where(
                            ToolEffectDagAdmissionRecord.status.in_(
                                (
                                    EffectDagClusterAdmissionStatus.PENDING.value,
                                    EffectDagClusterAdmissionStatus.GRANTED.value,
                                )
                            ),
                            ToolEffectDagAdmissionRecord.expires_at > database_now,
                        )
                    )
                    if current_digest is not None and int(live_count or 0) > 0:
                        raise EffectDagAdmissionConfigurationMismatchError(configuration_digest)
                    await self._write_configuration(
                        session,
                        state_revision=state_revision,
                        configuration_digest=configuration_digest,
                        global_limit=global_limit,
                        per_graph_limit=per_graph_limit,
                        default_tool_limit=default_tool_limit,
                        tool_limits_digest=tool_limits_digest,
                        database_now=database_now,
                    )
                    return configuration_digest

        return await self._retry_serialized(operation)

    async def register_batch(
        self,
        graph_id: str,
        requests: tuple[EffectDagAdmissionRequest, ...],
        *,
        owner_id: str,
        lease_ttl_seconds: int,
    ) -> str:
        batch_id = f"edb_{uuid4().hex}"
        scheduling_shard = self._scheduling_shard(graph_id)
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                expires_at = database_now + timedelta(seconds=lease_ttl_seconds)
                session.add_all(
                    ToolEffectDagAdmissionRecord(
                        admission_id=f"eda_{uuid4().hex}",
                        batch_id=batch_id,
                        graph_id=graph_id,
                        node_id=request.node_id,
                        tool_name=request.tool_name,
                        owner_id=owner_id,
                        status=EffectDagClusterAdmissionStatus.PENDING.value,
                        scheduling_shard=scheduling_shard,
                        lease_ttl_seconds=lease_ttl_seconds,
                        revision=1,
                        fencing_token=0,
                        created_at=database_now,
                        updated_at=database_now,
                        expires_at=expires_at,
                    )
                    for request in requests
                )
                await session.flush()
        return batch_id

    async def renew_pending_batch(
        self,
        batch_id: str,
        *,
        owner_id: str,
        lease_ttl_seconds: int,
    ) -> int:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                result = await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.batch_id == batch_id,
                        ToolEffectDagAdmissionRecord.owner_id == owner_id,
                        ToolEffectDagAdmissionRecord.status
                        == EffectDagClusterAdmissionStatus.PENDING.value,
                        ToolEffectDagAdmissionRecord.expires_at > database_now,
                    )
                    .values(
                        revision=ToolEffectDagAdmissionRecord.revision + 1,
                        expires_at=database_now + timedelta(seconds=lease_ttl_seconds),
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                return int(getattr(result, "rowcount", 0))

    async def read_batch(self, batch_id: str) -> tuple[EffectDagClusterAdmissionEntry, ...]:
        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(ToolEffectDagAdmissionRecord)
                        .where(ToolEffectDagAdmissionRecord.batch_id == batch_id)
                        .order_by(
                            ToolEffectDagAdmissionRecord.created_at,
                            ToolEffectDagAdmissionRecord.admission_id,
                        )
                    )
                ).all()
            )
        return tuple(self._to_entry(record) for record in records)

    async def schedule(
        self,
        *,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int],
    ) -> int:
        await self.ensure_configuration(
            global_limit=global_limit,
            per_graph_limit=per_graph_limit,
            default_tool_limit=default_tool_limit,
            tool_limits=tool_limits,
        )

        async def operation() -> int:
            if self._database.engine.dialect.name == "postgresql":
                return await self._schedule_postgresql_once(
                    global_limit=global_limit,
                    per_graph_limit=per_graph_limit,
                    default_tool_limit=default_tool_limit,
                    tool_limits=tool_limits,
                )
            return await self._schedule_once(
                global_limit=global_limit,
                per_graph_limit=per_graph_limit,
                default_tool_limit=default_tool_limit,
                tool_limits=tool_limits,
            )

        return await self._retry_serialized(operation)

    async def _schedule_postgresql_once(
        self,
        *,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int],
    ) -> int:
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                database_now = await database_utc_now(session)
                configuration_digest, _ = self._configuration_digests(
                    global_limit=global_limit,
                    per_graph_limit=per_graph_limit,
                    default_tool_limit=default_tool_limit,
                    tool_limits=tool_limits,
                )
                current_digest = await session.scalar(
                    select(ToolEffectDagAdmissionStateRecord.configuration_digest).where(
                        ToolEffectDagAdmissionStateRecord.scope_id == _SCHEDULER_SCOPE
                    )
                )
                if current_digest != configuration_digest:
                    raise EffectDagAdmissionConfigurationMismatchError(configuration_digest)
                shard = await session.scalar(
                    build_postgresql_admission_shard_lock_statement(
                        database_time=database_now,
                    )
                )
                if shard is None:
                    return 0
                await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.status
                        == EffectDagClusterAdmissionStatus.GRANTED.value,
                        ToolEffectDagAdmissionRecord.expires_at <= database_now,
                    )
                    .values(
                        status=EffectDagClusterAdmissionStatus.EXPIRED.value,
                        revision=ToolEffectDagAdmissionRecord.revision + 1,
                        released_at=database_now,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.scheduling_shard == shard.shard_id,
                        ToolEffectDagAdmissionRecord.status
                        == EffectDagClusterAdmissionStatus.PENDING.value,
                        ToolEffectDagAdmissionRecord.expires_at <= database_now,
                    )
                    .values(
                        status=EffectDagClusterAdmissionStatus.EXPIRED.value,
                        revision=ToolEffectDagAdmissionRecord.revision + 1,
                        released_at=database_now,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                active = tuple(
                    (
                        await session.scalars(
                            select(ToolEffectDagAdmissionRecord).where(
                                ToolEffectDagAdmissionRecord.status
                                == EffectDagClusterAdmissionStatus.GRANTED.value,
                                ToolEffectDagAdmissionRecord.expires_at > database_now,
                            )
                        )
                    ).all()
                )
                pending = tuple(
                    (
                        await session.scalars(
                            build_postgresql_admission_candidate_statement(
                                shard_id=shard.shard_id,
                                database_time=database_now,
                                candidate_limit=_POSTGRESQL_CANDIDATE_LIMIT,
                            )
                        )
                    ).all()
                )
                if not pending or len(active) >= global_limit:
                    return 0
                turn_sequence = int(
                    await session.scalar(text(f"SELECT nextval('{_POSTGRESQL_GRANT_SEQUENCE}')"))
                )
                shard.revision += 1
                shard.last_grant_sequence = turn_sequence
                shard.updated_at = database_now

                candidate_graph_ids = tuple({record.graph_id for record in pending})
                historical = tuple(
                    (
                        await session.execute(
                            select(
                                ToolEffectDagAdmissionRecord.graph_id,
                                func.max(ToolEffectDagAdmissionRecord.grant_sequence),
                            )
                            .where(
                                ToolEffectDagAdmissionRecord.graph_id.in_(candidate_graph_ids),
                                ToolEffectDagAdmissionRecord.grant_sequence.is_not(None),
                            )
                            .group_by(ToolEffectDagAdmissionRecord.graph_id)
                        )
                    ).all()
                )
                last_grant = {
                    graph_id: int(sequence)
                    for graph_id, sequence in historical
                    if sequence is not None
                }
                active_by_graph: defaultdict[str, int] = defaultdict(int)
                active_by_tool: defaultdict[str, int] = defaultdict(int)
                for record in active:
                    active_by_graph[record.graph_id] += 1
                    active_by_tool[record.tool_name] += 1

                graph_batches: dict[
                    str,
                    dict[str, list[ToolEffectDagAdmissionRecord]],
                ] = {}
                for record in pending:
                    batches = graph_batches.setdefault(record.graph_id, {})
                    batches.setdefault(record.batch_id, []).append(record)
                candidates: list[
                    tuple[
                        tuple[int, datetime, str],
                        ToolEffectDagAdmissionRecord,
                    ]
                ] = []
                for graph_id, batches in graph_batches.items():
                    if active_by_graph[graph_id] >= per_graph_limit:
                        continue
                    head = next(iter(batches.values()))
                    grantable = next(
                        (
                            record
                            for record in head
                            if active_by_tool[record.tool_name]
                            < tool_limits.get(
                                record.tool_name,
                                default_tool_limit,
                            )
                        ),
                        None,
                    )
                    if grantable is not None:
                        candidates.append(
                            (
                                (
                                    last_grant.get(graph_id, 0),
                                    grantable.created_at,
                                    graph_id,
                                ),
                                grantable,
                            )
                        )
                if not candidates:
                    await session.flush()
                    return 0
                _, granted = min(candidates, key=lambda item: item[0])
                granted.status = EffectDagClusterAdmissionStatus.GRANTED.value
                granted.revision += 1
                granted.fencing_token += 1
                granted.grant_sequence = turn_sequence
                granted.granted_at = database_now
                granted.heartbeat_at = database_now
                granted.expires_at = database_now + timedelta(seconds=granted.lease_ttl_seconds)
                granted.updated_at = database_now
                for record in pending:
                    if (
                        record.batch_id == granted.batch_id
                        and record.admission_id != granted.admission_id
                    ):
                        record.status = EffectDagClusterAdmissionStatus.WITHDRAWN.value
                        record.revision += 1
                        record.released_at = database_now
                        record.updated_at = database_now
                await session.flush()
                return 1

    async def _schedule_once(
        self,
        *,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int],
    ) -> int:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                state_revision, next_grant_sequence, current_digest = await self._lock_scheduler(
                    session,
                    database_now,
                )
                configuration_digest, tool_limits_digest = self._configuration_digests(
                    global_limit=global_limit,
                    per_graph_limit=per_graph_limit,
                    default_tool_limit=default_tool_limit,
                    tool_limits=tool_limits,
                )
                if current_digest is None:
                    await self._write_configuration(
                        session,
                        state_revision=state_revision,
                        configuration_digest=configuration_digest,
                        global_limit=global_limit,
                        per_graph_limit=per_graph_limit,
                        default_tool_limit=default_tool_limit,
                        tool_limits_digest=tool_limits_digest,
                        database_now=database_now,
                    )
                elif current_digest != configuration_digest:
                    raise EffectDagAdmissionConfigurationMismatchError(configuration_digest)
                await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.status.in_(
                            (
                                EffectDagClusterAdmissionStatus.PENDING.value,
                                EffectDagClusterAdmissionStatus.GRANTED.value,
                            )
                        ),
                        ToolEffectDagAdmissionRecord.expires_at <= database_now,
                    )
                    .values(
                        status=EffectDagClusterAdmissionStatus.EXPIRED.value,
                        revision=ToolEffectDagAdmissionRecord.revision + 1,
                        released_at=database_now,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                active = tuple(
                    (
                        await session.scalars(
                            select(ToolEffectDagAdmissionRecord).where(
                                ToolEffectDagAdmissionRecord.status
                                == EffectDagClusterAdmissionStatus.GRANTED.value,
                                ToolEffectDagAdmissionRecord.expires_at > database_now,
                            )
                        )
                    ).all()
                )
                pending = tuple(
                    (
                        await session.scalars(
                            select(ToolEffectDagAdmissionRecord)
                            .where(
                                ToolEffectDagAdmissionRecord.status
                                == EffectDagClusterAdmissionStatus.PENDING.value,
                                ToolEffectDagAdmissionRecord.expires_at > database_now,
                            )
                            .order_by(
                                ToolEffectDagAdmissionRecord.created_at,
                                ToolEffectDagAdmissionRecord.admission_id,
                            )
                        )
                    ).all()
                )
                historical = tuple(
                    (
                        await session.execute(
                            select(
                                ToolEffectDagAdmissionRecord.graph_id,
                                func.max(ToolEffectDagAdmissionRecord.grant_sequence),
                            )
                            .where(ToolEffectDagAdmissionRecord.grant_sequence.is_not(None))
                            .group_by(ToolEffectDagAdmissionRecord.graph_id)
                        )
                    ).all()
                )
                last_grant = {
                    graph_id: int(sequence)
                    for graph_id, sequence in historical
                    if sequence is not None
                }
                active_by_graph: defaultdict[str, int] = defaultdict(int)
                active_by_tool: defaultdict[str, int] = defaultdict(int)
                for record in active:
                    active_by_graph[record.graph_id] += 1
                    active_by_tool[record.tool_name] += 1
                active_total = len(active)

                graph_batches: dict[
                    str,
                    dict[str, list[ToolEffectDagAdmissionRecord]],
                ] = {}
                for record in pending:
                    batches = graph_batches.setdefault(record.graph_id, {})
                    batches.setdefault(record.batch_id, []).append(record)

                granted_batches: set[str] = set()
                granted_count = 0
                while active_total < global_limit:
                    candidates: list[
                        tuple[
                            tuple[int, datetime, str],
                            ToolEffectDagAdmissionRecord,
                        ]
                    ] = []
                    for graph_id, batches in graph_batches.items():
                        if not batches or active_by_graph[graph_id] >= per_graph_limit:
                            continue
                        head = next(iter(batches.values()))
                        grantable = next(
                            (
                                record
                                for record in head
                                if record.status == EffectDagClusterAdmissionStatus.PENDING.value
                                and active_by_tool[record.tool_name]
                                < tool_limits.get(record.tool_name, default_tool_limit)
                            ),
                            None,
                        )
                        if grantable is None:
                            continue
                        candidates.append(
                            (
                                (
                                    last_grant.get(graph_id, 0),
                                    grantable.created_at,
                                    graph_id,
                                ),
                                grantable,
                            )
                        )
                    if not candidates:
                        break
                    _, granted = min(candidates, key=lambda item: item[0])
                    granted.status = EffectDagClusterAdmissionStatus.GRANTED.value
                    granted.revision += 1
                    granted.fencing_token += 1
                    granted.grant_sequence = next_grant_sequence
                    granted.granted_at = database_now
                    granted.heartbeat_at = database_now
                    granted.expires_at = database_now + timedelta(seconds=granted.lease_ttl_seconds)
                    granted.updated_at = database_now
                    granted_batches.add(granted.batch_id)
                    last_grant[granted.graph_id] = next_grant_sequence
                    next_grant_sequence += 1
                    active_total += 1
                    active_by_graph[granted.graph_id] += 1
                    active_by_tool[granted.tool_name] += 1
                    granted_count += 1

                for record in pending:
                    if (
                        record.batch_id in granted_batches
                        and record.status == EffectDagClusterAdmissionStatus.PENDING.value
                    ):
                        record.status = EffectDagClusterAdmissionStatus.WITHDRAWN.value
                        record.revision += 1
                        record.released_at = database_now
                        record.updated_at = database_now
                state_result = await session.execute(
                    update(ToolEffectDagAdmissionStateRecord)
                    .where(
                        ToolEffectDagAdmissionStateRecord.scope_id == _SCHEDULER_SCOPE,
                        ToolEffectDagAdmissionStateRecord.revision == state_revision + 1,
                    )
                    .values(
                        next_grant_sequence=next_grant_sequence,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(state_result, "rowcount", 0)) != 1:
                    raise _SchedulerRevisionConflict(_SCHEDULER_SCOPE)
                await session.flush()
                return granted_count

    async def cancel_pending_graph(self, graph_id: str) -> int:
        async def operation() -> int:
            async with self._database.session() as session:
                async with session.begin():
                    database_now = await database_utc_now(session)
                    await self._lock_scheduler(session, database_now)
                    result = await session.execute(
                        update(ToolEffectDagAdmissionRecord)
                        .where(
                            ToolEffectDagAdmissionRecord.graph_id == graph_id,
                            ToolEffectDagAdmissionRecord.status
                            == EffectDagClusterAdmissionStatus.PENDING.value,
                        )
                        .values(
                            status=EffectDagClusterAdmissionStatus.CANCELLED.value,
                            revision=ToolEffectDagAdmissionRecord.revision + 1,
                            released_at=database_now,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    return int(getattr(result, "rowcount", 0))

        return await self._retry_serialized(operation)

    async def withdraw_batch(self, batch_id: str, *, owner_id: str) -> int:
        async def operation() -> int:
            async with self._database.session() as session:
                async with session.begin():
                    database_now = await database_utc_now(session)
                    await self._lock_scheduler(session, database_now)
                    pending = await session.execute(
                        update(ToolEffectDagAdmissionRecord)
                        .where(
                            ToolEffectDagAdmissionRecord.batch_id == batch_id,
                            ToolEffectDagAdmissionRecord.owner_id == owner_id,
                            ToolEffectDagAdmissionRecord.status
                            == EffectDagClusterAdmissionStatus.PENDING.value,
                        )
                        .values(
                            status=EffectDagClusterAdmissionStatus.WITHDRAWN.value,
                            revision=ToolEffectDagAdmissionRecord.revision + 1,
                            released_at=database_now,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    granted = await session.execute(
                        update(ToolEffectDagAdmissionRecord)
                        .where(
                            ToolEffectDagAdmissionRecord.batch_id == batch_id,
                            ToolEffectDagAdmissionRecord.owner_id == owner_id,
                            ToolEffectDagAdmissionRecord.status
                            == EffectDagClusterAdmissionStatus.GRANTED.value,
                        )
                        .values(
                            status=EffectDagClusterAdmissionStatus.RELEASED.value,
                            revision=ToolEffectDagAdmissionRecord.revision + 1,
                            released_at=database_now,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    return int(getattr(pending, "rowcount", 0)) + int(
                        getattr(granted, "rowcount", 0)
                    )

        return await self._retry_serialized(operation)

    async def renew_permit(
        self,
        admission_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        lease_ttl_seconds: int,
    ) -> datetime:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                expires_at = database_now + timedelta(seconds=lease_ttl_seconds)
                result = await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.admission_id == admission_id,
                        ToolEffectDagAdmissionRecord.owner_id == owner_id,
                        ToolEffectDagAdmissionRecord.status
                        == EffectDagClusterAdmissionStatus.GRANTED.value,
                        ToolEffectDagAdmissionRecord.fencing_token == fencing_token,
                        ToolEffectDagAdmissionRecord.expires_at > database_now,
                    )
                    .values(
                        revision=ToolEffectDagAdmissionRecord.revision + 1,
                        heartbeat_at=database_now,
                        expires_at=expires_at,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise EffectDagAdmissionFenceRejectedError(admission_id)
                return expires_at

    async def release_permit(
        self,
        admission_id: str,
        *,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                result = await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.admission_id == admission_id,
                        ToolEffectDagAdmissionRecord.owner_id == owner_id,
                        ToolEffectDagAdmissionRecord.status
                        == EffectDagClusterAdmissionStatus.GRANTED.value,
                        ToolEffectDagAdmissionRecord.fencing_token == fencing_token,
                        ToolEffectDagAdmissionRecord.expires_at > database_now,
                    )
                    .values(
                        status=EffectDagClusterAdmissionStatus.RELEASED.value,
                        revision=ToolEffectDagAdmissionRecord.revision + 1,
                        released_at=database_now,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                return int(getattr(result, "rowcount", 0)) == 1

    async def snapshot(self) -> EffectDagAdmissionSnapshot:
        async with self._database.session() as session:
            database_now = await database_utc_now(session)
            active = tuple(
                (
                    await session.scalars(
                        select(ToolEffectDagAdmissionRecord).where(
                            ToolEffectDagAdmissionRecord.status
                            == EffectDagClusterAdmissionStatus.GRANTED.value,
                            ToolEffectDagAdmissionRecord.expires_at > database_now,
                        )
                    )
                ).all()
            )
            pending = tuple(
                (
                    await session.scalars(
                        select(ToolEffectDagAdmissionRecord)
                        .where(
                            ToolEffectDagAdmissionRecord.status
                            == EffectDagClusterAdmissionStatus.PENDING.value,
                            ToolEffectDagAdmissionRecord.expires_at > database_now,
                        )
                        .order_by(
                            ToolEffectDagAdmissionRecord.created_at,
                            ToolEffectDagAdmissionRecord.admission_id,
                        )
                    )
                ).all()
            )
        active_by_graph: defaultdict[str, int] = defaultdict(int)
        active_by_tool: defaultdict[str, int] = defaultdict(int)
        for record in active:
            active_by_graph[record.graph_id] += 1
            active_by_tool[record.tool_name] += 1
        waiting_batches = len({record.batch_id for record in pending})
        waiting_graphs = tuple(dict.fromkeys(record.graph_id for record in pending))
        return EffectDagAdmissionSnapshot(
            active_total=len(active),
            active_by_graph=dict(active_by_graph),
            active_by_tool=dict(active_by_tool),
            waiting_batches=waiting_batches,
            waiting_graphs=waiting_graphs,
        )

    async def _lock_scheduler(
        self,
        session: AsyncSession,
        database_now: datetime,
    ) -> tuple[int, int, str | None]:
        state = await session.get(ToolEffectDagAdmissionStateRecord, _SCHEDULER_SCOPE)
        if state is None:
            raise RuntimeError("Effect DAG admission scheduler state is missing")
        revision = state.revision
        next_grant_sequence = state.next_grant_sequence
        result = await session.execute(
            update(ToolEffectDagAdmissionStateRecord)
            .where(
                ToolEffectDagAdmissionStateRecord.scope_id == _SCHEDULER_SCOPE,
                ToolEffectDagAdmissionStateRecord.revision == revision,
            )
            .values(revision=revision + 1, updated_at=database_now)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0)) != 1:
            raise _SchedulerRevisionConflict(_SCHEDULER_SCOPE)
        return revision, next_grant_sequence, state.configuration_digest

    @staticmethod
    async def _write_configuration(
        session: AsyncSession,
        *,
        state_revision: int,
        configuration_digest: str,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits_digest: str,
        database_now: datetime,
    ) -> None:
        result = await session.execute(
            update(ToolEffectDagAdmissionStateRecord)
            .where(
                ToolEffectDagAdmissionStateRecord.scope_id == _SCHEDULER_SCOPE,
                ToolEffectDagAdmissionStateRecord.revision == state_revision + 1,
            )
            .values(
                configuration_digest=configuration_digest,
                global_limit=global_limit,
                per_graph_limit=per_graph_limit,
                default_tool_limit=default_tool_limit,
                tool_limits_digest=tool_limits_digest,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0)) != 1:
            raise _SchedulerRevisionConflict(_SCHEDULER_SCOPE)

    @staticmethod
    def _configuration_digests(
        *,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int],
    ) -> tuple[str, str]:
        normalized_tool_limits = dict(sorted(tool_limits.items()))
        tool_limits_digest = sha256_digest(normalized_tool_limits)
        configuration_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-dag-admission-config.v1",
                "global_limit": global_limit,
                "per_graph_limit": per_graph_limit,
                "default_tool_limit": default_tool_limit,
                "tool_limits": normalized_tool_limits,
            }
        )
        return configuration_digest, tool_limits_digest

    @staticmethod
    def _scheduling_shard(graph_id: str) -> int:
        digest = hashlib.sha256(graph_id.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % _SCHEDULING_SHARD_COUNT

    @staticmethod
    def _is_postgresql_retryable(error: DBAPIError) -> bool:
        original = error.orig
        sqlstate = getattr(original, "sqlstate", None) or getattr(
            original,
            "pgcode",
            None,
        )
        return sqlstate in {"40001", "40P01"}

    async def _retry_serialized(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        last_error: BaseException | None = None
        async with self._scheduler_lock:
            for attempt in range(100):
                try:
                    return await operation()
                except (_SchedulerRevisionConflict, OperationalError) as error:
                    last_error = error
                    delay = min(0.05, 0.001 * (attempt + 1))
                    await asyncio.sleep(delay + random.uniform(0, delay))
                except DBAPIError as error:
                    if not self._is_postgresql_retryable(error):
                        raise
                    last_error = error
                    delay = min(0.05, 0.001 * (attempt + 1))
                    await asyncio.sleep(delay + random.uniform(0, delay))
        if last_error is not None:
            raise last_error
        raise RuntimeError("Effect DAG admission scheduler retry was exhausted")

    @classmethod
    def _to_entry(
        cls,
        record: ToolEffectDagAdmissionRecord,
    ) -> EffectDagClusterAdmissionEntry:
        return EffectDagClusterAdmissionEntry(
            admission_id=record.admission_id,
            batch_id=record.batch_id,
            graph_id=record.graph_id,
            request=EffectDagAdmissionRequest(
                node_id=record.node_id,
                tool_name=record.tool_name,
            ),
            owner_id=record.owner_id,
            status=EffectDagClusterAdmissionStatus(record.status),
            revision=record.revision,
            fencing_token=record.fencing_token,
            grant_sequence=record.grant_sequence,
            expires_at=cls._as_utc(record.expires_at),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class EffectDagClusterAdmissionPermit:
    """Heartbeat-backed permit whose loss cancels guarded Runner work."""

    def __init__(
        self,
        controller: "EffectDagClusterAdmissionController",
        entry: EffectDagClusterAdmissionEntry,
    ) -> None:
        self.graph_id = entry.graph_id
        self.request = entry.request
        self.admission_id = entry.admission_id
        self.fencing_token = entry.fencing_token
        self.proof = EffectDagAdmissionProof(
            admission_id=entry.admission_id,
            owner_id=entry.owner_id,
            fencing_token=entry.fencing_token,
        )
        self._controller = controller
        self._released = False
        self._stop = asyncio.Event()
        self._lost = asyncio.Event()
        self._loss_error: BaseException | None = None
        self._heartbeat = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"dag-admission-heartbeat:{self.admission_id}",
        )

    async def run(self, work: Awaitable[_T]) -> _T:
        work_task = asyncio.ensure_future(work)
        lost_task = asyncio.create_task(
            self._lost.wait(),
            name=f"dag-admission-loss:{self.admission_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {work_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work_task in done:
                return await work_task
            work_task.cancel()
            await asyncio.gather(work_task, return_exceptions=True)
            raise EffectDagAdmissionPermitLostError(self.admission_id) from self._loss_error
        finally:
            lost_task.cancel()
            await asyncio.gather(lost_task, return_exceptions=True)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._stop.set()
        await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self._controller._release_permit(self)

    async def _heartbeat_loop(self) -> None:
        interval = max(0.1, self._controller.lease_ttl_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    await self._controller._renew_permit(self)
                except BaseException as error:
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    self._loss_error = error
                    self._lost.set()
                    return


class EffectDagClusterAdmissionController:
    """Poll durable tickets and expose the dispatcher admission interface."""

    def __init__(
        self,
        store: EffectDagClusterAdmissionStore,
        *,
        owner_id: str,
        global_limit: int,
        per_graph_limit: int,
        default_tool_limit: int,
        tool_limits: Mapping[str, int] | None = None,
        lease_ttl_seconds: int = 15,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not 1 <= len(owner_id) <= 80:
            raise ValueError("Effect DAG admission owner ID is invalid")
        if not 1 <= global_limit <= 1_024:
            raise ValueError("Effect DAG global concurrency is invalid")
        if not 1 <= per_graph_limit <= global_limit:
            raise ValueError("Effect DAG per-graph concurrency is invalid")
        if not 1 <= default_tool_limit <= global_limit:
            raise ValueError("Effect DAG per-tool concurrency is invalid")
        if not 1 <= lease_ttl_seconds <= 3_600:
            raise ValueError("Effect DAG admission lease TTL is invalid")
        if not 0 < poll_interval_seconds <= 60:
            raise ValueError("Effect DAG admission poll interval is invalid")
        configured_tool_limits = dict(tool_limits or {})
        if any(
            not tool_name or not 1 <= limit <= global_limit
            for tool_name, limit in configured_tool_limits.items()
        ):
            raise ValueError("Effect DAG tool concurrency override is invalid")
        self._store = store
        self._owner_id = owner_id
        self._global_limit = global_limit
        self._per_graph_limit = per_graph_limit
        self._default_tool_limit = default_tool_limit
        self._tool_limits = configured_tool_limits
        self._lease_ttl = lease_ttl_seconds
        self._poll_interval = poll_interval_seconds
        self._wake = asyncio.Event()
        self._pending_batches: set[str] = set()
        self._permits: set[EffectDagClusterAdmissionPermit] = set()
        self._stopping = False

    @property
    def per_graph_limit(self) -> int:
        return self._per_graph_limit

    @property
    def lease_ttl_seconds(self) -> int:
        return self._lease_ttl

    async def acquire_batch(
        self,
        graph_id: str,
        requests: tuple[EffectDagAdmissionRequest, ...],
    ) -> tuple[EffectDagAdmissionPermitPort, ...]:
        self._validate_requests(graph_id, requests)
        if self._stopping:
            raise RuntimeError("Effect DAG cluster admission is shutting down")
        await self._ensure_configuration()
        batch_id = await self._store.register_batch(
            graph_id,
            requests,
            owner_id=self._owner_id,
            lease_ttl_seconds=self._lease_ttl,
        )
        self._pending_batches.add(batch_id)
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + self._lease_ttl / 3
        try:
            while True:
                if self._stopping:
                    raise asyncio.CancelledError
                if loop.time() >= next_renewal:
                    await self._store.renew_pending_batch(
                        batch_id,
                        owner_id=self._owner_id,
                        lease_ttl_seconds=self._lease_ttl,
                    )
                    next_renewal = loop.time() + self._lease_ttl / 3
                await self._schedule()
                entries = await self._store.read_batch(batch_id)
                granted = tuple(
                    entry
                    for entry in entries
                    if entry.status is EffectDagClusterAdmissionStatus.GRANTED
                )
                if granted:
                    self._pending_batches.discard(batch_id)
                    permits = tuple(
                        EffectDagClusterAdmissionPermit(self, entry) for entry in granted
                    )
                    self._permits.update(permits)
                    return permits
                statuses = {entry.status for entry in entries}
                if EffectDagClusterAdmissionStatus.CANCELLED in statuses:
                    raise EffectDagAdmissionCancelledError(graph_id)
                if EffectDagClusterAdmissionStatus.PENDING not in statuses:
                    raise EffectDagAdmissionPermitLostError(batch_id)
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=min(self._poll_interval, max(0.1, self._lease_ttl / 3)),
                    )
                    self._wake.clear()
                except TimeoutError:
                    pass
        except BaseException:
            self._pending_batches.discard(batch_id)
            await self._store.withdraw_batch(batch_id, owner_id=self._owner_id)
            await self._schedule()
            raise

    async def cancel_waiters(self, graph_id: str) -> None:
        await self._store.cancel_pending_graph(graph_id)
        await self._schedule()
        self._wake.set()

    async def snapshot(self) -> EffectDagAdmissionSnapshot:
        await self._schedule()
        return await self._store.snapshot()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._wake.set()
        for batch_id in tuple(self._pending_batches):
            await self._store.withdraw_batch(batch_id, owner_id=self._owner_id)
            self._pending_batches.discard(batch_id)
        await asyncio.gather(
            *(permit.release() for permit in tuple(self._permits)),
            return_exceptions=True,
        )
        await self._schedule()

    async def _renew_permit(self, permit: EffectDagClusterAdmissionPermit) -> None:
        await self._store.renew_permit(
            permit.admission_id,
            owner_id=self._owner_id,
            fencing_token=permit.fencing_token,
            lease_ttl_seconds=self._lease_ttl,
        )

    async def _release_permit(self, permit: EffectDagClusterAdmissionPermit) -> None:
        await self._store.release_permit(
            permit.admission_id,
            owner_id=self._owner_id,
            fencing_token=permit.fencing_token,
        )
        self._permits.discard(permit)
        await self._schedule()
        self._wake.set()

    async def _schedule(self) -> int:
        await self._ensure_configuration()
        return await self._store.schedule(
            global_limit=self._global_limit,
            per_graph_limit=self._per_graph_limit,
            default_tool_limit=self._default_tool_limit,
            tool_limits=self._tool_limits,
        )

    async def _ensure_configuration(self) -> str:
        return await self._store.ensure_configuration(
            global_limit=self._global_limit,
            per_graph_limit=self._per_graph_limit,
            default_tool_limit=self._default_tool_limit,
            tool_limits=self._tool_limits,
        )

    @staticmethod
    def _validate_requests(
        graph_id: str,
        requests: tuple[EffectDagAdmissionRequest, ...],
    ) -> None:
        if not graph_id:
            raise ValueError("Effect DAG admission graph ID is required")
        if not requests or len({request.node_id for request in requests}) != len(requests):
            raise ValueError("Effect DAG admission requires distinct candidates")
        if any(not request.node_id or not request.tool_name for request in requests):
            raise ValueError("Effect DAG admission candidate is invalid")


__all__ = [
    "EffectDagAdmissionConfigurationMismatchError",
    "EffectDagAdmissionFenceRejectedError",
    "EffectDagAdmissionPermitLostError",
    "EffectDagClusterAdmissionController",
    "EffectDagClusterAdmissionEntry",
    "EffectDagClusterAdmissionPermit",
    "EffectDagClusterAdmissionStatus",
    "EffectDagClusterAdmissionStore",
]
