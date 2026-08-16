import asyncio
from pathlib import Path

import pytest

from deskpilot.application.effect_dag_admission import (
    EffectDagAdmissionController,
    EffectDagAdmissionRequest,
)
from deskpilot.application.effect_dag_dispatcher import (
    EffectDagDispatcher,
    EffectNodeExecutionResult,
)
from deskpilot.application.task_service import TaskService
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
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _requests(
    prefix: str,
    *tool_names: str,
) -> tuple[EffectDagAdmissionRequest, ...]:
    return tuple(
        EffectDagAdmissionRequest(node_id=f"{prefix}_{index}", tool_name=tool_name)
        for index, tool_name in enumerate(tool_names)
    )


def _definition(node_key: str) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


async def _wait_for_waiters(
    controller: EffectDagAdmissionController,
    expected: int,
) -> None:
    async with asyncio.timeout(2):
        while (  # noqa: ASYNC110 - polling an observable scheduler snapshot
            await controller.snapshot()
        ).waiting_batches != expected:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_admission_round_robins_capacity_across_waiting_graphs() -> None:
    controller = EffectDagAdmissionController(
        global_limit=2,
        per_graph_limit=2,
        default_tool_limit=2,
    )
    blockers = await controller.acquire_batch(
        "blocker",
        _requests("blocker", "tool", "tool"),
    )
    first_waiter = asyncio.create_task(
        controller.acquire_batch("graph_a", _requests("a", "tool", "tool"))
    )
    second_waiter = asyncio.create_task(
        controller.acquire_batch("graph_b", _requests("b", "tool", "tool"))
    )
    await _wait_for_waiters(controller, 2)

    await blockers[0].release()
    await blockers[1].release()
    first, second = await asyncio.gather(first_waiter, second_waiter)
    try:
        assert len(first) == 1
        assert len(second) == 1
        snapshot = await controller.snapshot()
        assert snapshot.active_total == 2
        assert snapshot.active_by_graph == {"graph_a": 1, "graph_b": 1}
    finally:
        await asyncio.gather(*(permit.release() for permit in first + second))


@pytest.mark.asyncio
async def test_admission_enforces_independent_per_tool_limits() -> None:
    controller = EffectDagAdmissionController(
        global_limit=3,
        per_graph_limit=3,
        default_tool_limit=1,
    )
    permits = await controller.acquire_batch(
        "graph",
        _requests("node", "tool_a", "tool_a", "tool_b"),
    )
    try:
        assert {permit.request.tool_name for permit in permits} == {
            "tool_a",
            "tool_b",
        }
        snapshot = await controller.snapshot()
        assert snapshot.active_total == 2
        assert snapshot.active_by_tool == {"tool_a": 1, "tool_b": 1}
    finally:
        await asyncio.gather(*(permit.release() for permit in permits))


@pytest.mark.asyncio
async def test_cancelled_admission_waiter_is_withdrawn_without_capacity_leak() -> None:
    controller = EffectDagAdmissionController(
        global_limit=1,
        per_graph_limit=1,
        default_tool_limit=1,
    )
    blocker = await controller.acquire_batch(
        "blocker",
        _requests("blocker", "tool"),
    )
    waiter = asyncio.create_task(
        controller.acquire_batch("cancelled", _requests("cancelled", "tool"))
    )
    await _wait_for_waiters(controller, 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    snapshot = await controller.snapshot()
    assert snapshot.waiting_batches == 0
    assert snapshot.active_by_graph == {"blocker": 1}
    await blocker[0].release()


class _SharedRecordingExecutor:
    def __init__(self, *, delay: float = 0.01) -> None:
        self.delay = delay
        self.task_order: list[str] = []
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
        self.task_order.append(task_id)
        try:
            await asyncio.sleep(self.delay)
            return EffectNodeExecutionResult(status=EffectNodeStatus.SUCCEEDED)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_two_dispatchers_share_global_capacity_without_graph_starvation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'fair-dispatch.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    tasks = [
        await service.create_task(TaskCreate(goal=f"fair graph {index}")) for index in range(2)
    ]
    for task in tasks:
        await service.create_effect_dag(
            task.task_id,
            tuple(_definition(f"root_{index}") for index in range(3)),
        )
    controller = EffectDagAdmissionController(
        global_limit=1,
        per_graph_limit=1,
        default_tool_limit=1,
    )
    executor = _SharedRecordingExecutor()
    dispatchers = [
        EffectDagDispatcher(
            service,
            executor,
            instance_id=f"fair_dispatcher_{index}",
            max_concurrency=3,
            admission_controller=controller,
        )
        for index in range(2)
    ]
    try:
        results = await asyncio.gather(
            *(
                dispatcher.run_until_idle(task.task_id)
                for dispatcher, task in zip(dispatchers, tasks, strict=True)
            )
        )

        assert all(result.graph_status is EffectGraphStatus.SUCCEEDED for result in results)
        assert executor.max_active == 1
        task_ids = {task.task_id for task in tasks}
        assert set(executor.task_order[:2]) == task_ids
        assert len(executor.task_order) == 6
        assert all(
            current != following
            for current, following in zip(
                executor.task_order,
                executor.task_order[1:],
                strict=False,
            )
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cancel_wakes_admission_wait_without_creating_a_node_claim(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cancel-backpressure.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="cancel backpressured graph"))
    created = await service.create_effect_dag(
        task.task_id,
        (_definition("root"),),
    )
    controller = EffectDagAdmissionController(
        global_limit=1,
        per_graph_limit=1,
        default_tool_limit=1,
    )
    blocker = await controller.acquire_batch(
        "capacity_holder",
        _requests("holder", DISK_USAGE_CONTRACT.name),
    )
    executor = _SharedRecordingExecutor(delay=0)
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id="cancel_backpressure",
        admission_controller=controller,
    )
    run = asyncio.create_task(dispatcher.run_until_idle(task.task_id))
    try:
        await _wait_for_waiters(controller, 1)
        waiting_graph = await service.get_effect_graph(task.task_id)
        assert waiting_graph.nodes[0].status is EffectNodeStatus.PENDING
        assert waiting_graph.nodes[0].claim_owner_id is None

        await dispatcher.request_cancel(task.task_id, reason="cancel while queued")
        result = await asyncio.wait_for(run, timeout=2)
        graph = await service.get_effect_graph(task.task_id)

        assert created.graph_id == graph.graph_id
        assert result.graph_status is EffectGraphStatus.CANCELLED
        assert graph.nodes[0].status is EffectNodeStatus.CANCELLED
        assert graph.nodes[0].claim_owner_id is None
        assert executor.task_order == []
    finally:
        await asyncio.gather(*(permit.release() for permit in blocker))
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await database.dispose()
