"""Durable, server-owned progression for safe Task Workbench actions."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Update

from deskpilot.application.task_workbench_service import (
    TaskWorkbenchError,
    TaskWorkbenchNotFoundError,
    TaskWorkbenchService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import (
    TaskExecutionRunRecord,
    TaskRecord,
    WorkbenchRuntimeItemRecord,
)

logger = logging.getLogger(__name__)


class WorkbenchRuntimeFenceRejectedError(RuntimeError):
    """A newer worker claim or cancellation superseded this worker."""


class WorkbenchRuntimeNoProgressError(RuntimeError):
    """The reducer remained actionable without changing its signed projection."""


@dataclass(frozen=True, slots=True)
class WorkbenchRuntimeClaim:
    work_item_id: str
    task_id: str
    claim_owner_id: str
    claim_fencing_token: int
    attempt_count: int
    consecutive_failure_count: int


@dataclass(frozen=True, slots=True)
class WorkbenchRuntimeBatchResult:
    claimed: int = 0
    advanced: int = 0
    applied: int = 0
    retried: int = 0
    dead_lettered: int = 0
    fenced: int = 0


class WorkbenchRuntimeStore:
    """Transactional queue store; task state remains authoritative."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def enqueue(self, task_id: str, projection_digest: str) -> str:
        work_item_id = f"wki_{sha256_digest({'task_id': task_id, 'action': 'advance'})}"
        for attempt in range(2):
            try:
                async with self._database.session() as session:
                    async with session.begin():
                        task = await session.get(TaskRecord, task_id)
                        if task is None:
                            raise TaskWorkbenchNotFoundError("Task does not exist")
                        now = await database_utc_now(session)
                        record = await session.get(WorkbenchRuntimeItemRecord, work_item_id)
                        if record is None:
                            session.add(
                                WorkbenchRuntimeItemRecord(
                                    work_item_id=work_item_id,
                                    task_id=task_id,
                                    action="advance",
                                    status="pending",
                                    revision=1,
                                    attempt_count=0,
                                    consecutive_failure_count=0,
                                    available_at=now,
                                    observed_projection_digest=projection_digest,
                                    claim_fencing_token=0,
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                        elif record.status == "dead_letter":
                            return work_item_id
                        elif record.status not in {"pending", "processing"}:
                            record.status = "pending"
                            record.revision += 1
                            record.consecutive_failure_count = 0
                            record.available_at = now
                            record.observed_projection_digest = projection_digest
                            record.claim_owner_id = None
                            record.claim_acquired_at = None
                            record.claim_heartbeat_at = None
                            record.claim_expires_at = None
                            record.last_error_code = None
                            record.last_error_detail = None
                            record.applied_at = None
                            record.cancelled_at = None
                            record.updated_at = now
                        await session.flush()
                return work_item_id
            except IntegrityError:
                if attempt:
                    raise
        raise RuntimeError("Workbench runtime enqueue retry was exhausted")

    async def cancel(self, task_id: str) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                now = await database_utc_now(session)
                result = await session.execute(
                    update(WorkbenchRuntimeItemRecord)
                    .where(
                        WorkbenchRuntimeItemRecord.task_id == task_id,
                        WorkbenchRuntimeItemRecord.status.in_(("pending", "processing")),
                    )
                    .values(
                        status="cancelled",
                        revision=WorkbenchRuntimeItemRecord.revision + 1,
                        claim_owner_id=None,
                        claim_fencing_token=(WorkbenchRuntimeItemRecord.claim_fencing_token + 1),
                        claim_acquired_at=None,
                        claim_heartbeat_at=None,
                        claim_expires_at=None,
                        cancelled_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                return int(getattr(result, "rowcount", 0)) == 1

    async def claim(
        self,
        owner_id: str,
        *,
        ttl_seconds: float,
        limit: int,
    ) -> tuple[WorkbenchRuntimeClaim, ...]:
        claims: list[WorkbenchRuntimeClaim] = []
        async with self._database.session() as session:
            async with session.begin():
                now = await database_utc_now(session)
                ready = or_(
                    (
                        (WorkbenchRuntimeItemRecord.status == "pending")
                        & (WorkbenchRuntimeItemRecord.available_at <= now)
                    ),
                    (
                        (WorkbenchRuntimeItemRecord.status == "processing")
                        & (WorkbenchRuntimeItemRecord.claim_expires_at.is_not(None))
                        & (WorkbenchRuntimeItemRecord.claim_expires_at <= now)
                    ),
                )
                statement = (
                    select(WorkbenchRuntimeItemRecord)
                    .where(ready)
                    .order_by(
                        WorkbenchRuntimeItemRecord.available_at,
                        WorkbenchRuntimeItemRecord.created_at,
                        WorkbenchRuntimeItemRecord.work_item_id,
                    )
                    .limit(limit)
                )
                if self._database.engine.dialect.name == "postgresql":
                    statement = statement.with_for_update(
                        skip_locked=True,
                        of=WorkbenchRuntimeItemRecord,
                    )
                candidates = tuple((await session.scalars(statement)).all())
                for candidate in candidates:
                    revision = candidate.revision
                    previous_fence = candidate.claim_fencing_token
                    next_fence = previous_fence + 1
                    result = await session.execute(
                        update(WorkbenchRuntimeItemRecord)
                        .where(
                            WorkbenchRuntimeItemRecord.work_item_id == candidate.work_item_id,
                            WorkbenchRuntimeItemRecord.revision == revision,
                            WorkbenchRuntimeItemRecord.claim_fencing_token == previous_fence,
                            ready,
                        )
                        .values(
                            status="processing",
                            revision=revision + 1,
                            attempt_count=candidate.attempt_count + 1,
                            claim_owner_id=owner_id,
                            claim_fencing_token=next_fence,
                            claim_acquired_at=now,
                            claim_heartbeat_at=now,
                            claim_expires_at=now + timedelta(seconds=ttl_seconds),
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(getattr(result, "rowcount", 0)) != 1:
                        continue
                    claims.append(
                        WorkbenchRuntimeClaim(
                            work_item_id=candidate.work_item_id,
                            task_id=candidate.task_id,
                            claim_owner_id=owner_id,
                            claim_fencing_token=next_fence,
                            attempt_count=candidate.attempt_count + 1,
                            consecutive_failure_count=(candidate.consecutive_failure_count),
                        )
                    )
        return tuple(claims)

    async def renew(self, claim: WorkbenchRuntimeClaim, *, ttl_seconds: float) -> None:
        async with self._database.session() as session:
            async with session.begin():
                now = await database_utc_now(session)
                result = await session.execute(
                    self._claimed_update(claim, now).values(
                        claim_heartbeat_at=now,
                        claim_expires_at=now + timedelta(seconds=ttl_seconds),
                        updated_at=now,
                    )
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise WorkbenchRuntimeFenceRejectedError(claim.work_item_id)

    async def complete(
        self,
        claim: WorkbenchRuntimeClaim,
        *,
        projection_digest: str,
        requeue: bool,
    ) -> Literal["pending", "applied"]:
        next_status: Literal["pending", "applied"] = "pending" if requeue else "applied"
        async with self._database.session() as session:
            async with session.begin():
                now = await database_utc_now(session)
                result = await session.execute(
                    self._claimed_update(claim, now).values(
                        status=next_status,
                        revision=WorkbenchRuntimeItemRecord.revision + 1,
                        consecutive_failure_count=0,
                        available_at=now,
                        observed_projection_digest=projection_digest,
                        claim_owner_id=None,
                        claim_acquired_at=None,
                        claim_heartbeat_at=None,
                        claim_expires_at=None,
                        last_error_code=None,
                        last_error_detail=None,
                        applied_at=None if requeue else now,
                        updated_at=now,
                    )
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise WorkbenchRuntimeFenceRejectedError(claim.work_item_id)
        return next_status

    async def retry(
        self,
        claim: WorkbenchRuntimeClaim,
        *,
        error_code: str,
        error_detail: str,
        max_failures: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
    ) -> Literal["pending", "dead_letter"]:
        next_failure = claim.consecutive_failure_count + 1
        dead_lettered = next_failure >= max_failures
        next_status: Literal["pending", "dead_letter"] = (
            "dead_letter" if dead_lettered else "pending"
        )
        delay = min(
            retry_base_seconds * (2 ** min(next_failure - 1, 16)),
            retry_max_seconds,
        )
        async with self._database.session() as session:
            async with session.begin():
                now = await database_utc_now(session)
                result = await session.execute(
                    self._claimed_update(claim, now).values(
                        status=next_status,
                        revision=WorkbenchRuntimeItemRecord.revision + 1,
                        consecutive_failure_count=next_failure,
                        available_at=now + timedelta(seconds=delay),
                        claim_owner_id=None,
                        claim_acquired_at=None,
                        claim_heartbeat_at=None,
                        claim_expires_at=None,
                        last_error_code=error_code[:100],
                        last_error_detail=error_detail[:1_000],
                        updated_at=now,
                    )
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise WorkbenchRuntimeFenceRejectedError(claim.work_item_id)
        return next_status

    @staticmethod
    def _claimed_update(
        claim: WorkbenchRuntimeClaim,
        now: datetime,
    ) -> Update:
        return (
            update(WorkbenchRuntimeItemRecord)
            .where(
                WorkbenchRuntimeItemRecord.work_item_id == claim.work_item_id,
                WorkbenchRuntimeItemRecord.status == "processing",
                WorkbenchRuntimeItemRecord.claim_owner_id == claim.claim_owner_id,
                WorkbenchRuntimeItemRecord.claim_fencing_token == claim.claim_fencing_token,
                WorkbenchRuntimeItemRecord.claim_expires_at > now,
            )
            .execution_options(synchronize_session=False)
        )


class WorkbenchRuntimeCoordinator:
    """Own safe Workbench progress independently from any browser session."""

    def __init__(
        self,
        database: Database,
        workbench: TaskWorkbenchService,
        *,
        instance_id: str,
        poll_interval_seconds: float,
        claim_ttl_seconds: float,
        concurrency: int,
        max_failures: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        planner_recovery_scan_interval_seconds: float | None = None,
        planner_recovery_scan_limit: int = 100,
    ) -> None:
        if planner_recovery_scan_interval_seconds is not None and (
            planner_recovery_scan_interval_seconds <= 0
            or planner_recovery_scan_interval_seconds > 3_600
        ):
            raise ValueError("Turn Planner recovery scan interval is invalid")
        if not 1 <= planner_recovery_scan_limit <= 1_000:
            raise ValueError("Turn Planner recovery scan limit is invalid")
        self._store = WorkbenchRuntimeStore(database)
        self._database = database
        self._workbench = workbench
        self._instance_id = f"{instance_id[:104]}:workbench"
        self._poll_interval_seconds = poll_interval_seconds
        self._claim_ttl_seconds = claim_ttl_seconds
        self._concurrency = concurrency
        self._max_failures = max_failures
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._planner_recovery_scan_interval_seconds = (
            planner_recovery_scan_interval_seconds
            if planner_recovery_scan_interval_seconds is not None
            else max(1.0, min(60.0, poll_interval_seconds * 100))
        )
        self._planner_recovery_scan_limit = planner_recovery_scan_limit
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("Workbench runtime coordinator already started")
        self._stopping = False
        self._runner = asyncio.create_task(self._run(), name="workbench-runtime")
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    async def schedule(self, task_id: str, projection_digest: str) -> None:
        await self._store.enqueue(task_id, projection_digest)
        self.notify()

    async def cancel(self, task_id: str) -> None:
        await self._store.cancel(task_id)
        self.notify()

    async def shutdown(self) -> None:
        if self._runner is None:
            return
        self._stopping = True
        self.notify()
        await self._runner
        self._runner = None

    async def advance_pending(self) -> WorkbenchRuntimeBatchResult:
        claims = await self._store.claim(
            self._instance_id,
            ttl_seconds=self._claim_ttl_seconds,
            limit=self._concurrency,
        )
        if not claims:
            return WorkbenchRuntimeBatchResult()
        results = await asyncio.gather(*(self._process(claim) for claim in claims))
        return WorkbenchRuntimeBatchResult(
            claimed=len(claims),
            advanced=sum(result.advanced for result in results),
            applied=sum(result.applied for result in results),
            retried=sum(result.retried for result in results),
            dead_lettered=sum(result.dead_lettered for result in results),
            fenced=sum(result.fenced for result in results),
        )

    async def recover_runnable_tasks(self, *, limit: int = 1_000) -> int:
        """Seed WorkItems for active pre-migration tasks; existing rows are idempotent."""

        async with self._database.session() as session:
            task_ids = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionRunRecord.task_id)
                        .where(
                            TaskExecutionRunRecord.status.in_(("active", "awaiting_verification"))
                        )
                        .distinct()
                        .order_by(TaskExecutionRunRecord.task_id)
                        .limit(limit)
                    )
                ).all()
            )
        planner_task_ids = await self._workbench.turn_planner_recoverable_task_ids(limit=limit)
        planner_task_id_set = set(planner_task_ids)
        recoverable_task_ids = tuple(dict.fromkeys((*task_ids, *planner_task_ids)))
        recovered = 0
        for task_id in recoverable_task_ids:
            try:
                workbench = await self._workbench.get(task_id)
            except TaskWorkbenchError:
                logger.exception(
                    "Workbench startup recovery rejected task %s",
                    task_id,
                )
                continue
            directly_recovered = False
            if (
                self._workbench.automatic_action(workbench) is None
                and task_id in planner_task_id_set
            ):
                try:
                    workbench = await self._workbench.interpret_turn(task_id)
                    directly_recovered = True
                except TaskWorkbenchError:
                    logger.exception(
                        "Turn Planner startup recovery rejected task %s",
                        task_id,
                    )
                    continue
            if self._workbench.automatic_action(workbench) is None:
                recovered += 1 if directly_recovered else 0
                continue
            await self._store.enqueue(task_id, workbench.projection_digest)
            recovered += 1
        if len(task_ids) == limit or len(planner_task_ids) == limit:
            logger.warning(
                "Workbench startup recovery reached its bounded %s-task scan limit",
                limit,
            )
        return recovered

    async def recover_turn_planner_tasks(self, *, limit: int | None = None) -> int:
        """Recover a bounded Planner batch after leases become eligible."""

        scan_limit = self._planner_recovery_scan_limit if limit is None else limit
        if not 1 <= scan_limit <= 1_000:
            raise ValueError("Turn Planner recovery scan limit is invalid")
        task_ids = await self._workbench.turn_planner_recoverable_task_ids(
            limit=scan_limit
        )
        recovered = 0
        for task_id in task_ids:
            if self._stopping:
                break
            try:
                before = await self._workbench.get(task_id)
                if self._workbench.automatic_action(before) is not None:
                    await self._store.enqueue(task_id, before.projection_digest)
                    recovered += 1
                    continue
                if (
                    before.turn_planning is not None
                    and before.turn_planning.run.status != "dispatching"
                ):
                    # A terminal bound Route without a safe automatic action is
                    # not a Planner crash window. In particular, do not repeat
                    # failed workspace preparation or its conversation message.
                    continue
                workbench = await self._workbench.interpret_turn(task_id)
            except TaskWorkbenchError:
                logger.exception(
                    "Periodic Turn Planner recovery rejected task %s",
                    task_id,
                )
                continue
            if self._workbench.automatic_action(workbench) is not None:
                await self._store.enqueue(task_id, workbench.projection_digest)
            recovered += 1
        if len(task_ids) == scan_limit:
            logger.warning(
                "Periodic Turn Planner recovery reached its bounded %s-task scan limit",
                scan_limit,
            )
        return recovered

    async def _process(self, claim: WorkbenchRuntimeClaim) -> WorkbenchRuntimeBatchResult:
        heartbeat_stop = asyncio.Event()
        heartbeat_fenced = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(claim, heartbeat_stop, heartbeat_fenced),
            name=f"workbench-heartbeat-{claim.work_item_id}",
        )
        advanced = False
        try:
            before = await self._workbench.get(claim.task_id)
            if self._workbench.automatic_action(before) is None:
                after = before
            else:
                after = await self._workbench.advance(claim.task_id)
                advanced = True
                if (
                    after.projection_digest == before.projection_digest
                    and self._workbench.automatic_action(after) is not None
                ):
                    raise WorkbenchRuntimeNoProgressError(
                        "Workbench remained actionable without projection progress"
                    )
        except TaskWorkbenchNotFoundError:
            await self._store.cancel(claim.task_id)
            return WorkbenchRuntimeBatchResult(fenced=1)
        except (TaskWorkbenchError, WorkbenchRuntimeNoProgressError) as error:
            return await self._retry(claim, error)
        except Exception as error:
            logger.exception(
                "Unexpected Workbench runtime failure for task %s",
                claim.task_id,
            )
            return await self._retry(claim, error)
        finally:
            heartbeat_stop.set()
            await heartbeat

        if heartbeat_fenced.is_set():
            return WorkbenchRuntimeBatchResult(fenced=1)
        try:
            state = await self._store.complete(
                claim,
                projection_digest=after.projection_digest,
                requeue=self._workbench.automatic_action(after) is not None,
            )
        except WorkbenchRuntimeFenceRejectedError:
            return WorkbenchRuntimeBatchResult(fenced=1)
        return WorkbenchRuntimeBatchResult(
            advanced=1 if advanced else 0,
            applied=1 if state == "applied" else 0,
        )

    async def _retry(
        self,
        claim: WorkbenchRuntimeClaim,
        error: Exception,
    ) -> WorkbenchRuntimeBatchResult:
        code = getattr(error, "code", None) or type(error).__name__.upper()
        try:
            state = await self._store.retry(
                claim,
                error_code=str(code),
                error_detail=f"{type(error).__name__}: {error}",
                max_failures=self._max_failures,
                retry_base_seconds=self._retry_base_seconds,
                retry_max_seconds=self._retry_max_seconds,
            )
        except WorkbenchRuntimeFenceRejectedError:
            return WorkbenchRuntimeBatchResult(fenced=1)
        if state == "dead_letter":
            logger.error(
                "Workbench runtime item %s entered dead letter after %s attempts",
                claim.work_item_id,
                claim.attempt_count,
            )
            return WorkbenchRuntimeBatchResult(dead_lettered=1)
        return WorkbenchRuntimeBatchResult(retried=1)

    async def _heartbeat(
        self,
        claim: WorkbenchRuntimeClaim,
        stop: asyncio.Event,
        fenced: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._claim_ttl_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self._store.renew(claim, ttl_seconds=self._claim_ttl_seconds)
            except WorkbenchRuntimeFenceRejectedError:
                fenced.set()
                return
            except Exception:
                logger.exception(
                    "Workbench runtime heartbeat failed for item %s",
                    claim.work_item_id,
                )
                fenced.set()
                return

    async def _run(self) -> None:
        try:
            await self.recover_runnable_tasks()
        except Exception:
            logger.exception("Unexpected Workbench startup recovery failure")
        loop = asyncio.get_running_loop()
        next_planner_recovery_scan_at = (
            loop.time() + self._planner_recovery_scan_interval_seconds
        )
        while True:
            self._wake.clear()
            if loop.time() >= next_planner_recovery_scan_at:
                try:
                    await self.recover_turn_planner_tasks()
                except Exception:
                    logger.exception("Unexpected periodic Turn Planner recovery failure")
                next_planner_recovery_scan_at = (
                    loop.time() + self._planner_recovery_scan_interval_seconds
                )
            try:
                result = await self.advance_pending()
            except Exception:
                logger.exception("Unexpected Workbench runtime polling failure")
                result = WorkbenchRuntimeBatchResult()
            if self._stopping:
                return
            if result.claimed >= self._concurrency:
                continue
            wait_seconds = min(
                self._poll_interval_seconds,
                max(0.0, next_planner_recovery_scan_at - loop.time()),
            )
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=wait_seconds,
                )
            except TimeoutError:
                pass
