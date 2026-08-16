import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from deskpilot.application.effect_dag_dispatcher import (
    EffectDagDispatcher,
    EffectNodeExecutionResult,
)
from deskpilot.application.effect_graph_control_router import (
    EffectGraphControlFenceRejectedError,
    EffectGraphControlOwnerUnavailableError,
    EffectGraphControlRouter,
    EffectGraphControlStore,
)
from deskpilot.application.task_service import (
    EffectGraphFenceRejectedError,
    TaskService,
)
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagNodeDefinition,
    EffectGraphStatus,
    EffectNodeClaimRead,
    EffectNodeRead,
    EffectNodeStatus,
)
from deskpilot.domain.effect_graph_control import (
    EffectGraphControlClaimRead,
    EffectGraphControlStatus,
)
from deskpilot.domain.schemas import TaskCreate, TaskStatus
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ToolEffectGraphControlRecord,
    utc_now,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _definition(node_key: str) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


class _RoutedCancellingExecutor:
    def __init__(self, service: TaskService, expected_starts: int) -> None:
        self._service = service
        self._expected_starts = expected_starts
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.started_claims: list[tuple[str, int]] = []
        self.cancelled_claims: list[tuple[str, int, str]] = []
        self.intent_preceded_cancel: list[bool] = []

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult:
        assert task_id
        self.started_claims.append((node.node_id, claim.fencing_token))
        if len(self.started_claims) == self._expected_starts:
            self.started.set()
        await self.release.wait()
        return EffectNodeExecutionResult(status=EffectNodeStatus.CANCELLED)

    async def cancel(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        *,
        reason: str,
    ) -> None:
        graph = await self._service.get_effect_graph(task_id)
        self.intent_preceded_cancel.append(graph.cancel_requested_at is not None)
        self.cancelled_claims.append((node.node_id, claim.fencing_token, reason))
        if len(self.cancelled_claims) == self._expected_starts:
            self.release.set()


async def _unavailable_handler(_: EffectGraphControlClaimRead) -> None:
    raise EffectGraphControlOwnerUnavailableError("runtime unavailable")


@pytest.mark.asyncio
async def test_remote_router_delivers_cancel_once_to_exact_live_owner_fence(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'remote-control.db').as_posix()}")
    await database.migrate()
    owner_service = TaskService(database, "/api/v1")
    requester_service = TaskService(database, "/api/v1")
    task = await owner_service.create_task(TaskCreate(goal="remote graph cancel"))
    await owner_service.create_effect_dag(
        task.task_id,
        (_definition("left"), _definition("right")),
    )
    owner_id = "api_owner:dag"
    executor = _RoutedCancellingExecutor(owner_service, expected_starts=2)
    dispatcher = EffectDagDispatcher(
        owner_service,
        executor,
        instance_id=owner_id,
        max_concurrency=2,
    )
    run = asyncio.create_task(dispatcher.run_until_idle(task.task_id))
    owner_router: EffectGraphControlRouter | None = None
    requester_router: EffectGraphControlRouter | None = None
    try:
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        active_graph = await owner_service.get_effect_graph(task.task_id)
        handled = []

        async def owner_handler(control: EffectGraphControlClaimRead) -> None:
            handled.append(control.control_id)
            await dispatcher.request_cancel(
                task.task_id,
                reason=control.reason or "remote cancel",
                expected_graph_fencing_token=control.target_fencing_token,
            )
            await run

        owner_router = EffectGraphControlRouter(
            EffectGraphControlStore(database),
            owner_service,
            owner_id=owner_id,
            handler=owner_handler,
            poll_interval_seconds=0.01,
            request_timeout_seconds=3,
        )
        requester_router = EffectGraphControlRouter(
            EffectGraphControlStore(database),
            requester_service,
            owner_id="api_requester:dag",
            handler=_unavailable_handler,
            poll_interval_seconds=0.01,
            request_timeout_seconds=3,
        )
        owner_router.start()
        requester_router.start()

        delivered = await asyncio.gather(
            requester_router.request_cancel(
                task.task_id,
                reason="cancel from another API",
            ),
            requester_router.request_cancel(
                task.task_id,
                reason="cancel from another API",
            ),
        )
        graph = await owner_service.get_effect_graph(task.task_id)
        cancelled_task = await owner_service.get_task(task.task_id)
        control_id = handled[0]
        control = await EffectGraphControlStore(database).get(control_id)

        assert delivered == [True, True]
        assert len(handled) == 1
        assert control.status is EffectGraphControlStatus.APPLIED
        assert control.target_owner_id == owner_id
        assert control.target_fencing_token == active_graph.fencing_token
        assert control.applied_graph_fencing_token == active_graph.fencing_token
        assert control.attempt_count == 1
        assert cancelled_task.status is TaskStatus.CANCELLED
        assert graph.status is EffectGraphStatus.CANCELLED
        assert all(executor.intent_preceded_cancel)
        assert {reason for _, _, reason in executor.cancelled_claims} == {"cancel from another API"}
        assert {(node_id, fence) for node_id, fence, _ in executor.cancelled_claims} == set(
            executor.started_claims
        )
    finally:
        if requester_router is not None:
            await requester_router.shutdown()
        if owner_router is not None:
            await owner_router.shutdown()
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_target_fence_is_rejected_then_retargeted_to_new_owner(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'retarget-control.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    store = EffectGraphControlStore(database)
    task = await service.create_task(TaskCreate(goal="retarget graph control"))
    await service.create_effect_dag(task.task_id, (_definition("root"),))
    first_lease = await service.acquire_effect_graph_lease(
        task.task_id,
        owner_id="owner_a",
        ttl_seconds=30,
    )
    try:
        control = await store.request_cancel(
            task.task_id,
            reason="follow the live owner",
            requested_by="requester",
        )
        first_claim = (await store.claim_for_owner("owner_a", ttl_seconds=30))[0]
        assert first_claim.target_fencing_token == first_lease.fencing_token

        await service.release_effect_graph_lease(
            task.task_id,
            owner_id="owner_a",
            fencing_token=first_lease.fencing_token,
        )
        second_lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="owner_b",
            ttl_seconds=30,
        )
        with pytest.raises(EffectGraphFenceRejectedError):
            await service.request_effect_dag_cancel(
                task.task_id,
                lease_owner_id="owner_a",
                fencing_token=first_lease.fencing_token,
            )
        await store.retry(
            first_claim,
            error_code="GRAPH_CONTROL_TARGET_FENCE_CHANGED",
            superseded=True,
        )
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await store.mark_applied(first_claim)

        await store.route_pending()
        await asyncio.sleep(0.12)
        second_claim = (await store.claim_for_owner("owner_b", ttl_seconds=30))[0]
        await service.request_effect_dag_cancel(
            task.task_id,
            lease_owner_id="owner_b",
            fencing_token=second_lease.fencing_token,
        )
        await service.reduce_effect_dag(
            task.task_id,
            lease_owner_id="owner_b",
            fencing_token=second_lease.fencing_token,
        )
        await service.cancel_task(task.task_id, reason=second_claim.reason)
        applied = await store.mark_applied(second_claim)

        assert applied.control_id == control.control_id
        assert applied.status is EffectGraphControlStatus.APPLIED
        assert applied.target_owner_id == "owner_b"
        assert applied.target_fencing_token == second_lease.fencing_token
        assert applied.applied_graph_fencing_token == second_lease.fencing_token
        assert applied.attempt_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_control_delivery_fences_stale_ack(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'delivery-fence.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    store = EffectGraphControlStore(database)
    task = await service.create_task(TaskCreate(goal="control delivery fence"))
    await service.create_effect_dag(task.task_id, (_definition("root"),))
    lease = await service.acquire_effect_graph_lease(
        task.task_id,
        owner_id="delivery_owner",
        ttl_seconds=30,
    )
    try:
        await store.request_cancel(
            task.task_id,
            reason=None,
            requested_by="requester",
        )
        stale = (await store.claim_for_owner("delivery_owner", ttl_seconds=30))[0]
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectGraphControlRecord)
                    .where(ToolEffectGraphControlRecord.control_id == stale.control_id)
                    .values(claim_expires_at=utc_now() - timedelta(seconds=1))
                )
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await store.mark_applied(stale)
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await store.retry(
                stale,
                error_code="GRAPH_CONTROL_HANDLER_RETRY",
                superseded=False,
            )
        await store.route_pending()
        current = (await store.claim_for_owner("delivery_owner", ttl_seconds=30))[0]

        assert current.claim_fencing_token == stale.claim_fencing_token + 1
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await store.mark_applied(stale)

        await service.request_effect_dag_cancel(
            task.task_id,
            lease_owner_id="delivery_owner",
            fencing_token=lease.fencing_token,
        )
        await service.reduce_effect_dag(
            task.task_id,
            lease_owner_id="delivery_owner",
            fencing_token=lease.fencing_token,
        )
        applied = await store.mark_applied(current)
        assert applied.status is EffectGraphControlStatus.APPLIED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_persisted_unrouted_cancel_is_applied_after_router_restart_takeover(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'takeover-control.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="durable control takeover"))
    await service.create_effect_dag(task.task_id, (_definition("root"),))
    first_store = EffectGraphControlStore(database)
    control = await first_store.request_cancel(
        task.task_id,
        reason="survive requester restart",
        requested_by="stopped_requester",
    )
    assert control.target_owner_id is None

    restarted_router = EffectGraphControlRouter(
        EffectGraphControlStore(database),
        service,
        owner_id="restarted_owner",
        handler=_unavailable_handler,
        graph_lease_ttl_seconds=30,
    )
    try:
        result = await restarted_router.process_once()
        applied = await EffectGraphControlStore(database).get(control.control_id)
        graph = await service.get_effect_graph(task.task_id)
        cancelled_task = await service.get_task(task.task_id)

        assert (result.claimed, result.applied, result.takeovers) == (1, 1, 1)
        assert applied.status is EffectGraphControlStatus.APPLIED
        assert applied.target_owner_id == "restarted_owner"
        assert graph.cancel_requested_at is not None
        assert graph.status is EffectGraphStatus.CANCELLED
        assert graph.lease_owner_id is None
        assert cancelled_task.status is TaskStatus.CANCELLED
    finally:
        await database.dispose()
