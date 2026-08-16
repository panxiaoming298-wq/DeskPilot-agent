"""Durable cross-instance routing for owner/fence-bound effect graph controls."""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from deskpilot.application.task_service import (
    EffectGraphFenceRejectedError,
    EffectGraphLeaseUnavailableError,
    EffectGraphNotFoundError,
    InvalidTaskTransitionError,
    TaskNotFoundError,
    TaskService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.effect_graph_control import (
    EffectGraphControlClaimRead,
    EffectGraphControlCommand,
    EffectGraphControlRead,
    EffectGraphControlStatus,
    effect_graph_control_id,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolEffectGraphControlRecord,
    ToolEffectGraphRecord,
)
from deskpilot.infrastructure.postgresql_claims import (
    build_postgresql_graph_control_claim_statement,
)

ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
logger = logging.getLogger(__name__)


class EffectGraphControlOwnerUnavailableError(RuntimeError):
    """The target process no longer owns a matching in-memory graph runtime."""


class EffectGraphControlDeliveryTimeoutError(TimeoutError):
    code = "EFFECT_GRAPH_CONTROL_PENDING"

    def __init__(self, control_id: str) -> None:
        super().__init__(f"Effect graph control remains pending: {control_id}")
        self.control_id = control_id


class EffectGraphControlFenceRejectedError(RuntimeError):
    """The mailbox delivery claim was superseded by another delivery fence."""


class EffectGraphControlHandler(Protocol):
    async def __call__(self, control: EffectGraphControlClaimRead) -> None: ...


@dataclass(frozen=True, slots=True)
class EffectGraphControlBatchResult:
    claimed: int = 0
    applied: int = 0
    retried: int = 0
    superseded: int = 0
    takeovers: int = 0


class EffectGraphControlStore:
    """Transactional mailbox storage independent from process-local brokers."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def request_cancel(
        self,
        task_id: str,
        *,
        reason: str | None,
        requested_by: str,
    ) -> EffectGraphControlRead:
        if not 1 <= len(requested_by) <= 80:
            raise ValueError("Effect graph control requester ID is invalid")
        if reason is not None and not 1 <= len(reason) <= 500:
            raise ValueError("Effect graph control reason is invalid")
        for attempt in range(2):
            try:
                return await self._insert_or_read_cancel(
                    task_id,
                    reason=reason,
                    requested_by=requested_by,
                )
            except IntegrityError:
                if attempt:
                    raise
        raise RuntimeError("Effect graph control insert retry was exhausted")

    async def _insert_or_read_cancel(
        self,
        task_id: str,
        *,
        reason: str | None,
        requested_by: str,
    ) -> EffectGraphControlRead:
        async with self._database.session() as session:
            async with session.begin():
                task = await session.get(TaskRecord, task_id)
                if task is None:
                    raise TaskNotFoundError(task_id)
                graph = await session.scalar(
                    select(ToolEffectGraphRecord).where(ToolEffectGraphRecord.task_id == task_id)
                )
                if graph is None:
                    raise EffectGraphNotFoundError(task_id)
                command = EffectGraphControlCommand.CANCEL
                control_id = effect_graph_control_id(graph.graph_id, command)
                existing = await session.get(
                    ToolEffectGraphControlRecord,
                    control_id,
                )
                database_now = await database_utc_now(session)
                if existing is not None:
                    return self._to_read(existing)
                target_owner_id, target_fencing_token = self._live_target(
                    graph,
                    database_now,
                )
                request_digest = sha256_digest(
                    {
                        "schema_version": "deskpilot.effect-graph-control.v1",
                        "graph_id": graph.graph_id,
                        "command": command.value,
                        "reason": reason,
                    }
                )
                record = ToolEffectGraphControlRecord(
                    control_id=control_id,
                    task_id=task_id,
                    graph_id=graph.graph_id,
                    command=command.value,
                    reason=reason,
                    request_digest=request_digest,
                    requested_by=requested_by,
                    target_owner_id=target_owner_id,
                    target_fencing_token=target_fencing_token,
                    status=EffectGraphControlStatus.PENDING.value,
                    revision=1,
                    attempt_count=0,
                    available_at=database_now,
                    claim_fencing_token=0,
                    created_at=database_now,
                    updated_at=database_now,
                )
                session.add(record)
                await session.flush()
                return self._to_read(record)

    async def get(self, control_id: str) -> EffectGraphControlRead:
        async with self._database.session() as session:
            record = await session.get(ToolEffectGraphControlRecord, control_id)
            if record is None:
                raise LookupError(control_id)
            return self._to_read(record)

    async def route_pending(
        self,
        *,
        limit: int = 100,
    ) -> tuple[EffectGraphControlRead, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Effect graph control route limit is invalid")
        routed: list[EffectGraphControlRead] = []
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                route_statement = (
                    select(ToolEffectGraphControlRecord)
                    .where(
                        ToolEffectGraphControlRecord.status
                        != EffectGraphControlStatus.APPLIED.value,
                        (
                            ToolEffectGraphControlRecord.status
                            != EffectGraphControlStatus.PROCESSING.value
                        )
                        | ToolEffectGraphControlRecord.claim_expires_at.is_(None)
                        | (ToolEffectGraphControlRecord.claim_expires_at <= database_now),
                    )
                    .order_by(
                        ToolEffectGraphControlRecord.created_at,
                        ToolEffectGraphControlRecord.control_id,
                    )
                    .limit(limit)
                )
                if self._database.engine.dialect.name == "postgresql":
                    route_statement = route_statement.with_for_update(
                        skip_locked=True,
                        of=ToolEffectGraphControlRecord,
                    )
                records = tuple((await session.scalars(route_statement)).all())
                for record in records:
                    graph = await session.get(
                        ToolEffectGraphRecord,
                        record.graph_id,
                    )
                    if graph is None:
                        continue
                    target_owner_id, target_fencing_token = self._live_target(
                        graph,
                        database_now,
                    )
                    record.target_owner_id = target_owner_id
                    record.target_fencing_token = target_fencing_token
                    record.status = EffectGraphControlStatus.PENDING.value
                    record.claim_owner_id = None
                    record.claim_acquired_at = None
                    record.claim_expires_at = None
                    record.revision += 1
                    record.updated_at = database_now
                    routed.append(self._to_read(record))
                await session.flush()
        return tuple(routed)

    async def claim_for_owner(
        self,
        owner_id: str,
        *,
        ttl_seconds: float,
        limit: int = 20,
    ) -> tuple[EffectGraphControlClaimRead, ...]:
        if not 1 <= len(owner_id) <= 80:
            raise ValueError("Effect graph control owner ID is invalid")
        if not 1 <= ttl_seconds <= 3_600:
            raise ValueError("Effect graph control claim TTL is invalid")
        if not 1 <= limit <= 1_000:
            raise ValueError("Effect graph control claim limit is invalid")
        if self._database.engine.dialect.name == "postgresql":
            return await self._claim_for_owner_postgresql(
                owner_id,
                ttl_seconds=ttl_seconds,
                limit=limit,
            )
        claims: list[EffectGraphControlClaimRead] = []
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                expires_at = database_now + timedelta(seconds=ttl_seconds)
                candidates = tuple(
                    (
                        await session.scalars(
                            select(ToolEffectGraphControlRecord)
                            .where(
                                ToolEffectGraphControlRecord.status
                                == EffectGraphControlStatus.PENDING.value,
                                ToolEffectGraphControlRecord.target_owner_id == owner_id,
                                ToolEffectGraphControlRecord.available_at <= database_now,
                            )
                            .order_by(
                                ToolEffectGraphControlRecord.created_at,
                                ToolEffectGraphControlRecord.control_id,
                            )
                            .limit(limit)
                        )
                    ).all()
                )
                for candidate in candidates:
                    graph = await session.get(
                        ToolEffectGraphRecord,
                        candidate.graph_id,
                    )
                    if not self._matches_live_target(
                        graph,
                        owner_id=owner_id,
                        fencing_token=candidate.target_fencing_token,
                        database_now=database_now,
                    ):
                        continue
                    revision = candidate.revision
                    next_claim_fence = candidate.claim_fencing_token + 1
                    result = await session.execute(
                        update(ToolEffectGraphControlRecord)
                        .where(
                            ToolEffectGraphControlRecord.control_id == candidate.control_id,
                            ToolEffectGraphControlRecord.revision == revision,
                            ToolEffectGraphControlRecord.status
                            == EffectGraphControlStatus.PENDING.value,
                            ToolEffectGraphControlRecord.target_owner_id == owner_id,
                            ToolEffectGraphControlRecord.target_fencing_token
                            == candidate.target_fencing_token,
                        )
                        .values(
                            status=EffectGraphControlStatus.PROCESSING.value,
                            revision=revision + 1,
                            attempt_count=candidate.attempt_count + 1,
                            claim_owner_id=owner_id,
                            claim_acquired_at=database_now,
                            claim_expires_at=expires_at,
                            claim_fencing_token=next_claim_fence,
                            last_error_code=None,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(getattr(result, "rowcount", 0)) != 1:
                        continue
                    await session.refresh(candidate)
                    claims.append(self._to_claim(candidate))
        return tuple(claims)

    async def _claim_for_owner_postgresql(
        self,
        owner_id: str,
        *,
        ttl_seconds: float,
        limit: int,
    ) -> tuple[EffectGraphControlClaimRead, ...]:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                records = tuple(
                    (
                        await session.scalars(
                            build_postgresql_graph_control_claim_statement(
                                owner_id=owner_id,
                                database_now=database_now,
                                expires_at=database_now + timedelta(seconds=ttl_seconds),
                                batch_size=limit,
                            )
                        )
                    ).all()
                )
                records = tuple(
                    sorted(
                        records,
                        key=lambda record: (
                            record.available_at,
                            record.created_at,
                            record.control_id,
                        ),
                    )
                )
                return tuple(self._to_claim(record) for record in records)

    async def renew_claim(
        self,
        control: EffectGraphControlClaimRead,
        *,
        ttl_seconds: float,
    ) -> None:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                result = await session.execute(
                    update(ToolEffectGraphControlRecord)
                    .where(
                        ToolEffectGraphControlRecord.control_id == control.control_id,
                        ToolEffectGraphControlRecord.status
                        == EffectGraphControlStatus.PROCESSING.value,
                        ToolEffectGraphControlRecord.claim_owner_id == control.claim_owner_id,
                        ToolEffectGraphControlRecord.claim_fencing_token
                        == control.claim_fencing_token,
                        ToolEffectGraphControlRecord.claim_expires_at > database_now,
                    )
                    .values(
                        claim_expires_at=database_now + timedelta(seconds=ttl_seconds),
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise EffectGraphControlFenceRejectedError(control.control_id)

    async def mark_applied(
        self,
        control: EffectGraphControlClaimRead,
    ) -> EffectGraphControlRead:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                result = await session.execute(
                    update(ToolEffectGraphControlRecord)
                    .where(
                        ToolEffectGraphControlRecord.control_id == control.control_id,
                        ToolEffectGraphControlRecord.status
                        == EffectGraphControlStatus.PROCESSING.value,
                        ToolEffectGraphControlRecord.claim_owner_id == control.claim_owner_id,
                        ToolEffectGraphControlRecord.claim_fencing_token
                        == control.claim_fencing_token,
                        ToolEffectGraphControlRecord.claim_expires_at > database_now,
                    )
                    .values(
                        status=EffectGraphControlStatus.APPLIED.value,
                        revision=ToolEffectGraphControlRecord.revision + 1,
                        applied_graph_fencing_token=(control.target_fencing_token),
                        claim_expires_at=None,
                        applied_at=database_now,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise EffectGraphControlFenceRejectedError(control.control_id)
                record = await session.get(
                    ToolEffectGraphControlRecord,
                    control.control_id,
                )
                if record is None:
                    raise LookupError(control.control_id)
                await session.refresh(record)
                return self._to_read(record)

    async def retry(
        self,
        control: EffectGraphControlClaimRead,
        *,
        error_code: str,
        superseded: bool,
    ) -> None:
        if ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("Effect graph control error code is invalid")
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                delay = min(5.0, 0.05 * (2 ** min(control.attempt_count, 7)))
                result = await session.execute(
                    update(ToolEffectGraphControlRecord)
                    .where(
                        ToolEffectGraphControlRecord.control_id == control.control_id,
                        ToolEffectGraphControlRecord.status
                        == EffectGraphControlStatus.PROCESSING.value,
                        ToolEffectGraphControlRecord.claim_owner_id == control.claim_owner_id,
                        ToolEffectGraphControlRecord.claim_fencing_token
                        == control.claim_fencing_token,
                        ToolEffectGraphControlRecord.claim_expires_at > database_now,
                    )
                    .values(
                        status=(
                            EffectGraphControlStatus.SUPERSEDED.value
                            if superseded
                            else EffectGraphControlStatus.PENDING.value
                        ),
                        revision=ToolEffectGraphControlRecord.revision + 1,
                        target_owner_id=(None if superseded else control.target_owner_id),
                        target_fencing_token=(None if superseded else control.target_fencing_token),
                        last_error_code=error_code,
                        available_at=database_now + timedelta(seconds=delay),
                        claim_owner_id=None,
                        claim_acquired_at=None,
                        claim_expires_at=None,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise EffectGraphControlFenceRejectedError(control.control_id)

    @staticmethod
    def _live_target(
        graph: ToolEffectGraphRecord,
        database_now: datetime,
    ) -> tuple[str | None, int | None]:
        expiry = (
            EffectGraphControlStore._as_utc(graph.lease_expires_at)
            if graph.lease_expires_at is not None
            else None
        )
        if (
            graph.lease_owner_id is None
            or expiry is None
            or expiry <= database_now
            or graph.fencing_token < 1
        ):
            return None, None
        return graph.lease_owner_id, graph.fencing_token

    @classmethod
    def _matches_live_target(
        cls,
        graph: ToolEffectGraphRecord | None,
        *,
        owner_id: str,
        fencing_token: int | None,
        database_now: datetime,
    ) -> bool:
        if graph is None or fencing_token is None:
            return False
        target = cls._live_target(graph, database_now)
        return target == (owner_id, fencing_token)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _to_read(
        cls,
        record: ToolEffectGraphControlRecord,
    ) -> EffectGraphControlRead:
        return EffectGraphControlRead(
            control_id=record.control_id,
            task_id=record.task_id,
            graph_id=record.graph_id,
            command=EffectGraphControlCommand(record.command),
            reason=record.reason,
            request_digest=record.request_digest,
            requested_by=record.requested_by,
            target_owner_id=record.target_owner_id,
            target_fencing_token=record.target_fencing_token,
            status=EffectGraphControlStatus(record.status),
            revision=record.revision,
            attempt_count=record.attempt_count,
            last_error_code=record.last_error_code,
            available_at=cls._as_utc(record.available_at),
            applied_graph_fencing_token=record.applied_graph_fencing_token,
            created_at=cls._as_utc(record.created_at),
            updated_at=cls._as_utc(record.updated_at),
            applied_at=(cls._as_utc(record.applied_at) if record.applied_at is not None else None),
        )

    @classmethod
    def _to_claim(
        cls,
        record: ToolEffectGraphControlRecord,
    ) -> EffectGraphControlClaimRead:
        read = cls._to_read(record)
        if (
            record.claim_owner_id is None
            or record.claim_acquired_at is None
            or record.claim_expires_at is None
        ):
            raise EffectGraphControlFenceRejectedError(record.control_id)
        return EffectGraphControlClaimRead(
            **read.model_dump(),
            claim_owner_id=record.claim_owner_id,
            claim_fencing_token=record.claim_fencing_token,
            claim_acquired_at=cls._as_utc(record.claim_acquired_at),
            claim_expires_at=cls._as_utc(record.claim_expires_at),
        )


class EffectGraphControlRouter:
    """Poll, claim, apply, and acknowledge controls for one API DAG owner."""

    def __init__(
        self,
        store: EffectGraphControlStore,
        task_service: TaskService,
        *,
        owner_id: str,
        handler: EffectGraphControlHandler,
        applied_callback: Callable[[str], None] | None = None,
        poll_interval_seconds: float = 0.05,
        claim_ttl_seconds: float = 15.0,
        request_timeout_seconds: float = 30.0,
        graph_lease_ttl_seconds: float = 15.0,
    ) -> None:
        if not 1 <= len(owner_id) <= 80:
            raise ValueError("Effect graph control router owner ID is invalid")
        if not 0 < poll_interval_seconds <= 60:
            raise ValueError("Effect graph control poll interval is invalid")
        if not 1 <= claim_ttl_seconds <= 3_600:
            raise ValueError("Effect graph control claim TTL is invalid")
        if not 0 < request_timeout_seconds <= 3_600:
            raise ValueError("Effect graph control request timeout is invalid")
        if not 1 <= graph_lease_ttl_seconds <= 3_600:
            raise ValueError("Effect graph control graph lease TTL is invalid")
        self._store = store
        self._task_service = task_service
        self._owner_id = owner_id
        self._handler = handler
        self._applied_callback = applied_callback
        self._poll_interval = poll_interval_seconds
        self._claim_ttl = claim_ttl_seconds
        self._request_timeout = request_timeout_seconds
        self._graph_lease_ttl = graph_lease_ttl_seconds
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("Effect graph control router already started")
        self._stopping = False
        self._runner = asyncio.create_task(
            self._run(),
            name=f"effect-graph-control:{self._owner_id}",
        )
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    async def shutdown(self) -> None:
        if self._runner is None:
            return
        self._stopping = True
        self.notify()
        await self._runner
        self._runner = None

    async def request_cancel(
        self,
        task_id: str,
        *,
        reason: str | None,
    ) -> bool:
        try:
            control = await self._store.request_cancel(
                task_id,
                reason=reason,
                requested_by=self._owner_id,
            )
        except EffectGraphNotFoundError:
            return False
        self.notify()
        deadline = asyncio.get_running_loop().time() + self._request_timeout
        while True:
            current = await self._store.get(control.control_id)
            if current.status is EffectGraphControlStatus.APPLIED:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise EffectGraphControlDeliveryTimeoutError(control.control_id)
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=min(self._poll_interval, remaining),
                )
                self._wake.clear()
            except TimeoutError:
                pass

    async def process_once(self) -> EffectGraphControlBatchResult:
        routed = await self._store.route_pending()
        takeovers: dict[str, int] = {}
        for control in routed:
            if control.target_owner_id is not None:
                continue
            try:
                lease = await self._task_service.acquire_effect_graph_lease(
                    control.task_id,
                    owner_id=self._owner_id,
                    ttl_seconds=self._graph_lease_ttl,
                )
            except (
                EffectGraphLeaseUnavailableError,
                EffectGraphNotFoundError,
                TaskNotFoundError,
            ):
                continue
            takeovers[control.control_id] = lease.fencing_token
        if takeovers:
            await self._store.route_pending()
        claims = await self._store.claim_for_owner(
            self._owner_id,
            ttl_seconds=self._claim_ttl,
        )
        applied = 0
        retried = 0
        superseded = 0
        for control in claims:
            direct_takeover = takeovers.get(control.control_id)
            outcome = await self._apply_claim(
                control,
                direct_takeover_fence=direct_takeover,
            )
            if outcome == "applied":
                applied += 1
            elif outcome == "superseded":
                superseded += 1
            else:
                retried += 1
        return EffectGraphControlBatchResult(
            claimed=len(claims),
            applied=applied,
            retried=retried,
            superseded=superseded,
            takeovers=len(takeovers),
        )

    async def _apply_claim(
        self,
        control: EffectGraphControlClaimRead,
        *,
        direct_takeover_fence: int | None,
    ) -> str:
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_claim(control, stop_heartbeat),
            name=f"effect-graph-control-heartbeat:{control.control_id}",
        )
        used_direct_path = False
        try:
            try:
                await self._handler(control)
            except EffectGraphControlOwnerUnavailableError:
                used_direct_path = True
                await self._task_service.request_effect_dag_cancel(
                    control.task_id,
                    lease_owner_id=self._owner_id,
                    fencing_token=control.target_fencing_token or 0,
                )
                await self._task_service.reduce_effect_dag(
                    control.task_id,
                    lease_owner_id=self._owner_id,
                    fencing_token=control.target_fencing_token or 0,
                )
            if heartbeat.done():
                heartbeat.result()
            try:
                await self._task_service.cancel_task(
                    control.task_id,
                    reason=control.reason,
                )
            except InvalidTaskTransitionError:
                task = await self._task_service.get_task(control.task_id)
                if not task.status.is_terminal:
                    raise
            await self._store.mark_applied(control)
            if self._applied_callback is not None:
                self._applied_callback(control.task_id)
            self.notify()
            return "applied"
        except (EffectGraphFenceRejectedError, EffectGraphControlFenceRejectedError):
            await self._store.retry(
                control,
                error_code="GRAPH_CONTROL_TARGET_FENCE_CHANGED",
                superseded=True,
            )
            self.notify()
            return "superseded"
        except Exception:
            await self._store.retry(
                control,
                error_code="GRAPH_CONTROL_HANDLER_RETRY",
                superseded=False,
            )
            self.notify()
            return "retried"
        finally:
            stop_heartbeat.set()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if used_direct_path or direct_takeover_fence is not None:
                await self._task_service.release_effect_graph_lease(
                    control.task_id,
                    owner_id=self._owner_id,
                    fencing_token=control.target_fencing_token or 0,
                )

    async def _renew_claim(
        self,
        control: EffectGraphControlClaimRead,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._claim_ttl / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                await self._store.renew_claim(
                    control,
                    ttl_seconds=self._claim_ttl,
                )

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.process_once()
            except Exception:
                logger.exception(
                    "Unexpected effect graph control routing failure; polling will continue"
                )
            if self._stopping:
                return
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                pass


__all__ = [
    "EffectGraphControlBatchResult",
    "EffectGraphControlDeliveryTimeoutError",
    "EffectGraphControlFenceRejectedError",
    "EffectGraphControlOwnerUnavailableError",
    "EffectGraphControlRouter",
    "EffectGraphControlStore",
]
