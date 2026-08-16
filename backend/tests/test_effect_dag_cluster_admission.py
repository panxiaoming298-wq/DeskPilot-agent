import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from deskpilot.application.effect_dag_admission import EffectDagAdmissionRequest
from deskpilot.application.effect_dag_cluster_admission import (
    EffectDagAdmissionConfigurationMismatchError,
    EffectDagAdmissionFenceRejectedError,
    EffectDagAdmissionPermitLostError,
    EffectDagClusterAdmissionController,
    EffectDagClusterAdmissionStatus,
    EffectDagClusterAdmissionStore,
)
from deskpilot.application.effect_dag_dispatcher import (
    EffectDagDispatcher,
    EffectNodeExecutionResult,
)
from deskpilot.application.task_service import (
    EffectDagAdmissionProofRejectedError,
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
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import ToolEffectDagAdmissionRecord, utc_now
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


async def _create_graph(
    service: TaskService,
    goal: str,
    node_count: int = 1,
) -> tuple[str, str, tuple[str, ...]]:
    task = await service.create_task(TaskCreate(goal=goal))
    graph = await service.create_effect_dag(
        task.task_id,
        tuple(_definition(f"root_{index}") for index in range(node_count)),
    )
    return task.task_id, graph.graph_id, tuple(node.node_id for node in graph.nodes)


def _controller(
    store: EffectDagClusterAdmissionStore,
    owner_id: str,
    *,
    global_limit: int = 1,
    per_graph_limit: int = 1,
    tool_limit: int = 1,
    lease_ttl_seconds: int = 15,
) -> EffectDagClusterAdmissionController:
    return EffectDagClusterAdmissionController(
        store,
        owner_id=owner_id,
        global_limit=global_limit,
        per_graph_limit=per_graph_limit,
        default_tool_limit=tool_limit,
        lease_ttl_seconds=lease_ttl_seconds,
        poll_interval_seconds=0.01,
    )


async def _wait_for_waiters(
    controller: EffectDagClusterAdmissionController,
    expected: int,
) -> None:
    async with asyncio.timeout(3):
        while (await controller.snapshot()).waiting_batches != expected:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_two_database_controllers_share_global_capacity_before_node_claim(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cluster-capacity.db"
    first_database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    second_database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await first_database.migrate()
    service = TaskService(first_database, "/api/v1")
    _, first_graph_id, first_nodes = await _create_graph(service, "capacity holder")
    second_task_id, second_graph_id, second_nodes = await _create_graph(
        service,
        "capacity waiter",
    )
    first = _controller(EffectDagClusterAdmissionStore(first_database), "api_a:dag")
    second = _controller(EffectDagClusterAdmissionStore(second_database), "api_b:dag")
    holder = await first.acquire_batch(
        first_graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=first_nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
    )
    waiter = asyncio.create_task(
        second.acquire_batch(
            second_graph_id,
            (
                EffectDagAdmissionRequest(
                    node_id=second_nodes[0],
                    tool_name=DISK_USAGE_CONTRACT.name,
                ),
            ),
        )
    )
    try:
        await _wait_for_waiters(second, 1)
        waiting_graph = await service.get_effect_graph(second_task_id)
        assert waiting_graph.nodes[0].status is EffectNodeStatus.PENDING
        assert waiting_graph.nodes[0].claim_owner_id is None
        assert (await first.snapshot()).active_total == 1

        await holder[0].release()
        admitted = await asyncio.wait_for(waiter, timeout=3)
        assert len(admitted) == 1
        snapshot = await first.snapshot()
        assert snapshot.active_total == 1
        assert snapshot.active_by_graph == {second_graph_id: 1}
        await admitted[0].release()
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await asyncio.gather(*(permit.release() for permit in holder))
        await first.shutdown()
        await second.shutdown()
        await first_database.dispose()
        await second_database.dispose()


@pytest.mark.asyncio
async def test_cluster_tool_limit_blocks_same_tool_but_not_independent_tool(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cluster-tool-limit.db"
    first_database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    second_database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await first_database.migrate()
    service = TaskService(first_database, "/api/v1")
    _, first_graph_id, first_nodes = await _create_graph(service, "tool holder")
    _, blocked_graph_id, blocked_nodes = await _create_graph(service, "same tool waiter")
    _, free_graph_id, free_nodes = await _create_graph(service, "other tool waiter")
    first = _controller(
        EffectDagClusterAdmissionStore(first_database),
        "api_a",
        global_limit=2,
        per_graph_limit=2,
        tool_limit=1,
    )
    second = _controller(
        EffectDagClusterAdmissionStore(second_database),
        "api_b",
        global_limit=2,
        per_graph_limit=2,
        tool_limit=1,
    )
    holder = await first.acquire_batch(
        first_graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=first_nodes[0],
                tool_name="tool_a",
            ),
        ),
    )
    blocked = asyncio.create_task(
        second.acquire_batch(
            blocked_graph_id,
            (EffectDagAdmissionRequest(node_id=blocked_nodes[0], tool_name="tool_a"),),
        )
    )
    independent = asyncio.create_task(
        second.acquire_batch(
            free_graph_id,
            (EffectDagAdmissionRequest(node_id=free_nodes[0], tool_name="tool_b"),),
        )
    )
    try:
        free_permits = await asyncio.wait_for(independent, timeout=3)
        assert not blocked.done()
        snapshot = await first.snapshot()
        assert snapshot.active_total == 2
        assert snapshot.active_by_tool == {"tool_a": 1, "tool_b": 1}

        await holder[0].release()
        blocked_permits = await asyncio.wait_for(blocked, timeout=3)
        assert {permit.request.tool_name for permit in blocked_permits} == {"tool_a"}
        await asyncio.gather(
            *(permit.release() for permit in free_permits + blocked_permits)
        )
    finally:
        for waiter in (blocked, independent):
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(blocked, independent, return_exceptions=True)
        await asyncio.gather(*(permit.release() for permit in holder))
        await first.shutdown()
        await second.shutdown()
        await first_database.dispose()
        await second_database.dispose()


@pytest.mark.asyncio
async def test_live_cluster_configuration_mismatch_fails_closed_then_switches_when_idle(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'cluster-config.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    _, first_graph_id, first_nodes = await _create_graph(service, "first config")
    _, second_graph_id, second_nodes = await _create_graph(service, "second config")
    store = EffectDagClusterAdmissionStore(database)
    first = _controller(store, "api_a", global_limit=1)
    second = _controller(
        store,
        "api_b",
        global_limit=2,
        per_graph_limit=2,
        tool_limit=2,
    )
    permits = await first.acquire_batch(
        first_graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=first_nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
    )
    try:
        with pytest.raises(EffectDagAdmissionConfigurationMismatchError):
            await second.acquire_batch(
                second_graph_id,
                (
                    EffectDagAdmissionRequest(
                        node_id=second_nodes[0],
                        tool_name=DISK_USAGE_CONTRACT.name,
                    ),
                ),
            )
        assert (await first.snapshot()).active_total == 1

        await permits[0].release()
        switched = await second.acquire_batch(
            second_graph_id,
            (
                EffectDagAdmissionRequest(
                    node_id=second_nodes[0],
                    tool_name=DISK_USAGE_CONTRACT.name,
                ),
            ),
        )
        assert len(switched) == 1
        await switched[0].release()
    finally:
        await asyncio.gather(*(permit.release() for permit in permits))
        await first.shutdown()
        await second.shutdown()
        await database.dispose()


class _ClusterRecordingExecutor:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult:
        assert node.node_id == claim.node_id
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(task_id)
        try:
            await asyncio.sleep(0.02)
            return EffectNodeExecutionResult(status=EffectNodeStatus.SUCCEEDED)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_cross_instance_dispatchers_use_durable_round_robin_grant_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cluster-fairness.db"
    first_database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    second_database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await first_database.migrate()
    first_service = TaskService(first_database, "/api/v1")
    second_service = TaskService(second_database, "/api/v1")
    first_task_id, _, _ = await _create_graph(first_service, "first graph", node_count=3)
    second_task_id, _, _ = await _create_graph(second_service, "second graph", node_count=3)
    first = _controller(EffectDagClusterAdmissionStore(first_database), "api_a:dag")
    second = _controller(EffectDagClusterAdmissionStore(second_database), "api_b:dag")
    executor = _ClusterRecordingExecutor()
    first_dispatcher = EffectDagDispatcher(
        first_service,
        executor,
        instance_id="api_a:dag",
        max_concurrency=3,
        admission_controller=first,
    )
    second_dispatcher = EffectDagDispatcher(
        second_service,
        executor,
        instance_id="api_b:dag",
        max_concurrency=3,
        admission_controller=second,
    )
    try:
        results = await asyncio.gather(
            first_dispatcher.run_until_idle(first_task_id),
            second_dispatcher.run_until_idle(second_task_id),
        )
        assert all(result.graph_status is EffectGraphStatus.SUCCEEDED for result in results)
        assert executor.max_active == 1
        assert len(executor.order) == 6
        assert all(
            current != following
            for current, following in zip(executor.order, executor.order[1:], strict=False)
        )
        assert set(executor.order) == {first_task_id, second_task_id}
    finally:
        await first.shutdown()
        await second.shutdown()
        await first_database.dispose()
        await second_database.dispose()


@pytest.mark.asyncio
async def test_cluster_backpressure_cancel_wakes_waiter_without_node_claim(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'cluster-cancel-waiter.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    _, holder_graph_id, holder_nodes = await _create_graph(service, "capacity holder")
    task_id, waiting_graph_id, _ = await _create_graph(service, "cancelled waiter")
    store = EffectDagClusterAdmissionStore(database)
    holder_controller = _controller(store, "holder")
    waiting_controller = _controller(store, "waiting_owner:dag")
    holder = await holder_controller.acquire_batch(
        holder_graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=holder_nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
    )
    executor = _ClusterRecordingExecutor()
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id="waiting_owner:dag",
        admission_controller=waiting_controller,
    )
    run = asyncio.create_task(dispatcher.run_until_idle(task_id))
    try:
        await _wait_for_waiters(waiting_controller, 1)
        waiting = await service.get_effect_graph(task_id)
        assert waiting.graph_id == waiting_graph_id
        assert waiting.nodes[0].status is EffectNodeStatus.PENDING
        assert waiting.nodes[0].claim_owner_id is None

        await dispatcher.request_cancel(task_id, reason="cancel durable waiter")
        result = await asyncio.wait_for(run, timeout=3)
        graph = await service.get_effect_graph(task_id)
        assert result.graph_status is EffectGraphStatus.CANCELLED
        assert graph.nodes[0].status is EffectNodeStatus.CANCELLED
        assert graph.nodes[0].claim_owner_id is None
        assert executor.order == []
    finally:
        await asyncio.gather(*(permit.release() for permit in holder))
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await holder_controller.shutdown()
        await waiting_controller.shutdown()
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_permit_is_reclaimed_and_stale_fence_cannot_release_it(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'permit-expiry.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    _, first_graph_id, first_nodes = await _create_graph(service, "expired permit")
    _, second_graph_id, second_nodes = await _create_graph(service, "replacement permit")
    store = EffectDagClusterAdmissionStore(database)
    first_batch = await store.register_batch(
        first_graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=first_nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
        owner_id="stopped_owner",
        lease_ttl_seconds=15,
    )
    await store.schedule(
        global_limit=1,
        per_graph_limit=1,
        default_tool_limit=1,
        tool_limits={},
    )
    stale = (await store.read_batch(first_batch))[0]
    assert stale.status is EffectDagClusterAdmissionStatus.GRANTED
    async with database.session() as session:
        async with session.begin():
            await session.execute(
                update(ToolEffectDagAdmissionRecord)
                .where(ToolEffectDagAdmissionRecord.admission_id == stale.admission_id)
                .values(expires_at=utc_now() - timedelta(seconds=1))
            )

    second_batch = await store.register_batch(
        second_graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=second_nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
        owner_id="replacement_owner",
        lease_ttl_seconds=15,
    )
    await store.schedule(
        global_limit=1,
        per_graph_limit=1,
        default_tool_limit=1,
        tool_limits={},
    )
    current = (await store.read_batch(second_batch))[0]
    expired = (await store.read_batch(first_batch))[0]
    try:
        assert expired.status is EffectDagClusterAdmissionStatus.EXPIRED
        assert current.status is EffectDagClusterAdmissionStatus.GRANTED
        assert current.grant_sequence is not None
        assert stale.grant_sequence is not None
        assert current.grant_sequence > stale.grant_sequence
        assert not await store.release_permit(
            stale.admission_id,
            owner_id=stale.owner_id,
            fencing_token=stale.fencing_token,
        )
        with pytest.raises(EffectDagAdmissionFenceRejectedError):
            await store.renew_permit(
                stale.admission_id,
                owner_id=stale.owner_id,
                fencing_token=stale.fencing_token,
                lease_ttl_seconds=15,
            )
    finally:
        await store.release_permit(
            current.admission_id,
            owner_id=current.owner_id,
            fencing_token=current.fencing_token,
        )
        await database.dispose()


@pytest.mark.asyncio
async def test_node_claim_transaction_rejects_expired_admission_proof(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'claim-proof.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task_id, graph_id, nodes = await _create_graph(service, "admission-bound claim")
    lease = await service.acquire_effect_graph_lease(
        task_id,
        owner_id="claim_owner",
        ttl_seconds=15,
    )
    checkpoint = await service.checkpoint_effect_dag_ready_set(
        task_id,
        lease_owner_id="claim_owner",
        fencing_token=lease.fencing_token,
    )
    store = EffectDagClusterAdmissionStore(database)
    controller = _controller(store, "claim_owner")
    permits = await controller.acquire_batch(
        graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
    )
    proof = permits[0].proof
    assert proof is not None
    try:
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.admission_id
                        == permits[0].admission_id
                    )
                    .values(expires_at=utc_now() - timedelta(seconds=1))
                )
        await store.schedule(
            global_limit=1,
            per_graph_limit=1,
            default_tool_limit=1,
            tool_limits={},
        )

        with pytest.raises(EffectDagAdmissionProofRejectedError):
            await service.claim_effect_dag_nodes(
                task_id,
                (nodes[0],),
                ready_proof_digest=checkpoint.proof_digest,
                claim_owner_id="claim_owner",
                claim_ttl_seconds=15,
                lease_owner_id="claim_owner",
                fencing_token=lease.fencing_token,
                admission_proofs={nodes[0]: proof},
            )
        graph = await service.get_effect_graph(task_id)
        assert graph.nodes[0].status is EffectNodeStatus.PENDING
        assert graph.nodes[0].claim_owner_id is None
    finally:
        await asyncio.gather(*(permit.release() for permit in permits))
        await controller.shutdown()
        await service.release_effect_graph_lease(
            task_id,
            owner_id="claim_owner",
            fencing_token=lease.fencing_token,
        )
        await database.dispose()


@pytest.mark.asyncio
async def test_lost_permit_cancels_guarded_work_before_capacity_is_reused(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'permit-guard.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    _, graph_id, nodes = await _create_graph(service, "guarded permit")
    store = EffectDagClusterAdmissionStore(database)
    controller = _controller(
        store,
        "guard_owner",
        lease_ttl_seconds=1,
    )
    permits = await controller.acquire_batch(
        graph_id,
        (
            EffectDagAdmissionRequest(
                node_id=nodes[0],
                tool_name=DISK_USAGE_CONTRACT.name,
            ),
        ),
    )
    cancelled = asyncio.Event()

    async def guarded_work() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    guarded = asyncio.create_task(permits[0].run(guarded_work()))
    try:
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(
                        ToolEffectDagAdmissionRecord.admission_id
                        == permits[0].admission_id
                    )
                    .values(expires_at=utc_now() - timedelta(seconds=1))
                )
        await store.schedule(
            global_limit=1,
            per_graph_limit=1,
            default_tool_limit=1,
            tool_limits={},
        )
        with pytest.raises(EffectDagAdmissionPermitLostError):
            await asyncio.wait_for(guarded, timeout=2)
        assert cancelled.is_set()
    finally:
        if not guarded.done():
            guarded.cancel()
            await asyncio.gather(guarded, return_exceptions=True)
        await asyncio.gather(*(permit.release() for permit in permits))
        await controller.shutdown()
        await database.dispose()
