import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from deskpilot.application.effect_dag_admission import EffectDagAdmissionController
from deskpilot.application.effect_dag_dispatcher import (
    EffectBranchDecisionSelection,
    EffectDagDispatcher,
    EffectNodeExecutionResult,
)
from deskpilot.application.task_service import (
    InvalidEffectTransitionError,
    TaskService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagBranchCondition,
    EffectDagNodeDefinition,
    EffectGraphStatus,
    EffectNodeClaimRead,
    EffectNodeRead,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import ToolEffectReadySetCheckpointRecord
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _definition(
    node_key: str,
    *,
    depends_on: tuple[str, ...] = (),
    compensable: bool = False,
    when: EffectDagBranchCondition | None = None,
) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=(
            CompensationStrategy.RECEIPT_BOUND_REVERSE if compensable else CompensationStrategy.NONE
        ),
        depends_on=depends_on,
        conditional_depends_on=(() if when is None else (when,)),
    )


class RecordingExecutor:
    def __init__(self, *, delay: float = 0, failed_key: str | None = None) -> None:
        self.delay = delay
        self.failed_key = failed_key
        self.active = 0
        self.max_active = 0
        self.executed: list[str] = []

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult:
        assert task_id
        assert claim.node_id == node.node_id
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            self.executed.append(node.node_key)
            return EffectNodeExecutionResult(
                status=(
                    EffectNodeStatus.FAILED
                    if node.node_key == self.failed_key
                    else EffectNodeStatus.SUCCEEDED
                )
            )
        finally:
            self.active -= 1


class CancellingExecutor:
    def __init__(
        self,
        expected_starts: int,
        *,
        outcome_status: EffectNodeStatus = EffectNodeStatus.CANCELLED,
    ) -> None:
        self.expected_starts = expected_starts
        self.outcome_status = outcome_status
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.started_claims: list[tuple[str, int]] = []
        self.cancelled_claims: list[tuple[str, int, str]] = []

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult:
        assert task_id
        self.started_claims.append((node.node_id, claim.fencing_token))
        if len(self.started_claims) == self.expected_starts:
            self.started.set()
        await self.release.wait()
        return EffectNodeExecutionResult(
            status=self.outcome_status,
            error_code=(
                "TOOL_CANCELLED"
                if self.outcome_status is EffectNodeStatus.CANCELLED
                else "TOOL_CANCEL_OUTCOME_UNKNOWN"
                if self.outcome_status is EffectNodeStatus.UNKNOWN
                else None
            ),
        )

    async def cancel(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        *,
        reason: str,
    ) -> None:
        assert task_id
        assert node.node_id == claim.node_id
        self.cancelled_claims.append((claim.node_id, claim.fencing_token, reason))
        if len(self.cancelled_claims) == self.expected_starts:
            self.release.set()


class FastBranchResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def resolve(
        self,
        task_id: str,
        source: EffectNodeRead,
        decision_key: str,
        declared_outcomes: tuple[str, ...],
    ) -> EffectBranchDecisionSelection:
        assert task_id
        assert source.node_key == "evaluate"
        self.calls.append((decision_key, declared_outcomes))
        return EffectBranchDecisionSelection(
            outcome="fast",
            evidence_digest=sha256_digest({"trusted_route": "fast"}),
        )


@pytest.mark.asyncio
async def test_large_ready_set_uses_bounded_pages_under_dispatch_backpressure(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'large-ready-set.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="bounded large graph"))
    node_count = 128
    await service.create_effect_dag(
        task.task_id,
        tuple(_definition(f"root_{index:03d}") for index in range(node_count)),
    )
    controller = EffectDagAdmissionController(
        global_limit=8,
        per_graph_limit=8,
        default_tool_limit=8,
    )
    executor = RecordingExecutor()
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id="large_ready_set",
        max_concurrency=8,
        admission_controller=controller,
        ready_page_size=17,
    )
    try:
        result = await dispatcher.run_until_idle(task.task_id)
        graph = await service.get_effect_graph(task.task_id)
        async with database.session() as session:
            checkpoints = tuple(
                (
                    await session.scalars(
                        select(ToolEffectReadySetCheckpointRecord).where(
                            ToolEffectReadySetCheckpointRecord.graph_id == graph.graph_id
                        )
                    )
                ).all()
            )

        assert result.graph_status is EffectGraphStatus.SUCCEEDED
        assert (result.claimed, result.completed) == (node_count, node_count)
        assert result.rounds == node_count // 8
        assert executor.max_active == 8
        assert checkpoints
        assert max(len(checkpoint.ready_node_ids) for checkpoint in checkpoints) <= 17
        assert all(
            checkpoint.predecessor_proof["schema_version"] == "deskpilot.effect-ready-set.v6"
            for checkpoint in checkpoints
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatcher_runs_parallel_roots_renews_claims_and_completes_join(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dispatcher.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="parallel dispatcher"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("left"),
            _definition("right"),
            _definition("join", depends_on=("left", "right")),
        ),
    )
    executor = RecordingExecutor(delay=1.1)
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id="dispatcher_test",
        max_concurrency=2,
        graph_lease_ttl_seconds=1,
        node_claim_ttl_seconds=1,
    )
    try:
        result = await dispatcher.run_until_idle(task.task_id)
        graph = await service.get_effect_graph(task.task_id)

        assert result.graph_status is EffectGraphStatus.SUCCEEDED
        assert (result.rounds, result.claimed, result.completed, result.fenced) == (
            2,
            3,
            3,
            0,
        )
        assert executor.max_active == 2
        assert set(executor.executed[:2]) == {"left", "right"}
        assert executor.executed[2] == "join"
        assert all(node.status is EffectNodeStatus.SUCCEEDED for node in graph.nodes)
        assert all(node.claim_heartbeat_at is None for node in graph.nodes)
        events = await service.list_events(task.task_id)
        assert any(event.type == "effect.node.succeeded" for event in events)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatcher_persists_cancel_then_targets_current_node_fences(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dispatcher-cancel.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="cancel in-flight DAG calls"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("left"),
            _definition("right"),
            _definition("join", depends_on=("left", "right")),
        ),
    )
    executor = CancellingExecutor(expected_starts=2)
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id="dispatcher_cancel",
        max_concurrency=2,
    )
    run = asyncio.create_task(dispatcher.run_until_idle(task.task_id))
    try:
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        await dispatcher.request_cancel(
            task.task_id,
            reason="user cancelled the graph",
        )
        result = await asyncio.wait_for(run, timeout=2)
        graph = await service.get_effect_graph(task.task_id)

        assert result.graph_status is EffectGraphStatus.CANCELLED
        assert graph.cancel_requested_at is not None
        assert set(executor.started_claims) == {
            (node_id, fence) for node_id, fence, _ in executor.cancelled_claims
        }
        assert {reason for _, _, reason in executor.cancelled_claims} == {
            "user cancelled the graph"
        }
        assert [node.status for node in graph.nodes] == [
            EffectNodeStatus.CANCELLED,
            EffectNodeStatus.CANCELLED,
            EffectNodeStatus.SKIPPED,
        ]
        assert all(node.claim_owner_id is None for node in graph.nodes[:2])
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_outcome", "graph_status", "node_statuses"),
    (
        (
            EffectNodeStatus.UNKNOWN,
            EffectGraphStatus.BLOCKED_UNKNOWN,
            (EffectNodeStatus.UNKNOWN, EffectNodeStatus.SKIPPED),
        ),
        (
            EffectNodeStatus.SUCCEEDED,
            EffectGraphStatus.CANCELLED,
            (EffectNodeStatus.SUCCEEDED, EffectNodeStatus.CANCELLED),
        ),
    ),
)
async def test_cancel_preserves_unknown_or_committed_runner_truth(
    tmp_path: Path,
    runner_outcome: EffectNodeStatus,
    graph_status: EffectGraphStatus,
    node_statuses: tuple[EffectNodeStatus, ...],
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / f'cancel-{runner_outcome.value}.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="preserve cancel boundary truth"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("root"),
            _definition("child", depends_on=("root",)),
        ),
    )
    executor = CancellingExecutor(
        expected_starts=1,
        outcome_status=runner_outcome,
    )
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id=f"cancel_{runner_outcome.value}",
    )
    run = asyncio.create_task(dispatcher.run_until_idle(task.task_id))
    try:
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        await dispatcher.request_cancel(task.task_id, reason="cancel boundary test")
        result = await asyncio.wait_for(run, timeout=2)
        graph = await service.get_effect_graph(task.task_id)

        assert result.graph_status is graph_status
        assert tuple(node.status for node in graph.nodes) == node_statuses
        assert graph.cancel_requested_at is not None
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatcher_persists_trusted_branch_selection_before_dispatch(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dispatcher-branch.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="dispatcher branch"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("evaluate"),
            _definition(
                "fast",
                when=EffectDagBranchCondition(
                    predecessor_key="evaluate",
                    decision_key="route",
                    expected_outcome="fast",
                ),
            ),
            _definition(
                "safe",
                when=EffectDagBranchCondition(
                    predecessor_key="evaluate",
                    decision_key="route",
                    expected_outcome="safe",
                ),
            ),
        ),
    )
    resolver = FastBranchResolver()
    dispatcher = EffectDagDispatcher(
        service,
        RecordingExecutor(),
        instance_id="dispatcher_branch",
        branch_decision_resolver=resolver,
    )
    try:
        result = await dispatcher.run_until_idle(task.task_id)
        graph = await service.get_effect_graph(task.task_id)

        assert result.graph_status is EffectGraphStatus.SUCCEEDED
        assert (result.rounds, result.claimed, result.completed) == (2, 2, 2)
        assert resolver.calls == [("route", ("fast", "safe"))]
        assert graph.branch_decisions[0].outcome == "fast"
        assert {node.node_key: node.status for node in graph.nodes} == {
            "evaluate": EffectNodeStatus.SUCCEEDED,
            "fast": EffectNodeStatus.SUCCEEDED,
            "safe": EffectNodeStatus.SKIPPED,
        }
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_failure_skips_descendants_and_builds_parallel_compensation_waves(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'compensation-plan.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="parallel compensation"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("left", compensable=True),
            _definition("right", compensable=True),
            _definition("join", depends_on=("left", "right"), compensable=True),
            _definition("fail", compensable=True),
            _definition("skipped", depends_on=("fail",), compensable=True),
        ),
    )
    executor = RecordingExecutor(failed_key="fail")
    dispatcher = EffectDagDispatcher(
        service,
        executor,
        instance_id="dispatcher_compensation",
        max_concurrency=4,
    )
    try:
        result = await dispatcher.run_until_idle(task.task_id)
        graph = await service.get_effect_graph(task.task_id)
        status_by_key = {node.node_key: node.status for node in graph.nodes}
        node_id_by_key = {node.node_key: node.node_id for node in graph.nodes}

        assert result.graph_status is EffectGraphStatus.COMPENSATING
        assert status_by_key["fail"] is EffectNodeStatus.FAILED
        assert status_by_key["skipped"] is EffectNodeStatus.SKIPPED
        assert result.compensation_plan is not None
        assert [wave.node_ids for wave in result.compensation_plan.waves] == [
            (node_id_by_key["join"],),
            (node_id_by_key["left"], node_id_by_key["right"]),
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cancel_reducer_cancels_roots_and_skips_descendants(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cancel-reducer.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="cancel dag"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("root"),
            _definition("child", depends_on=("root",)),
        ),
    )
    try:
        lease = await service.acquire_effect_graph_lease(
            task.task_id, owner_id="cancel_owner", ttl_seconds=30
        )
        await service.request_effect_dag_cancel(
            task.task_id,
            lease_owner_id="cancel_owner",
            fencing_token=lease.fencing_token,
        )
        graph = await service.reduce_effect_dag(
            task.task_id,
            lease_owner_id="cancel_owner",
            fencing_token=lease.fencing_token,
        )

        assert graph.status is EffectGraphStatus.CANCELLED
        assert [node.status for node in graph.nodes] == [
            EffectNodeStatus.CANCELLED,
            EffectNodeStatus.SKIPPED,
        ]
        checkpoint = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="cancel_owner",
            fencing_token=lease.fencing_token,
        )
        assert checkpoint.ready_nodes == ()
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_node_status", "terminal_graph_status"),
    (
        (
            EffectNodeStatus.COMPENSATION_FAILED,
            EffectGraphStatus.BLOCKED_COMPENSATION_FAILED,
        ),
        (
            EffectNodeStatus.COMPENSATION_UNKNOWN,
            EffectGraphStatus.BLOCKED_COMPENSATION_UNKNOWN,
        ),
    ),
)
async def test_compensation_claim_enforces_wave_barrier_and_reduces_blocked_status(
    tmp_path: Path,
    terminal_node_status: EffectNodeStatus,
    terminal_graph_status: EffectGraphStatus,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / f'{terminal_node_status.value}.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="compensation wave barrier"))
    await service.create_effect_dag(
        task.task_id,
        (
            _definition("left", compensable=True),
            _definition("right", compensable=True),
            _definition("join", depends_on=("left", "right"), compensable=True),
            _definition("fail"),
        ),
    )
    dispatcher = EffectDagDispatcher(
        service,
        RecordingExecutor(failed_key="fail"),
        instance_id="barrier_forward",
        max_concurrency=4,
    )
    try:
        result = await dispatcher.run_until_idle(task.task_id)
        assert result.compensation_plan is not None
        plan = result.compensation_plan
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="barrier_compensation",
            ttl_seconds=30,
        )

        with pytest.raises(
            InvalidEffectTransitionError,
            match="wave barrier",
        ):
            await service.claim_effect_dag_compensation_nodes(
                task.task_id,
                plan.waves[1].node_ids,
                plan_id=plan.plan_id,
                wave_ordinal=1,
                claim_owner_id="barrier_compensation",
                claim_ttl_seconds=30,
                lease_owner_id="barrier_compensation",
                fencing_token=lease.fencing_token,
            )

        first_wave = await service.claim_effect_dag_compensation_nodes(
            task.task_id,
            plan.waves[0].node_ids,
            plan_id=plan.plan_id,
            wave_ordinal=0,
            claim_owner_id="barrier_compensation",
            claim_ttl_seconds=30,
            lease_owner_id="barrier_compensation",
            fencing_token=lease.fencing_token,
        )
        await service.transition_claimed_effect_node(
            task.task_id,
            first_wave[0].node_id,
            expected_statuses=frozenset({EffectNodeStatus.COMPENSATING}),
            target_status=EffectNodeStatus.COMPENSATED,
            transition_kind="test_compensated",
            event_type="effect.compensation.compensated",
            claim_owner_id=first_wave[0].owner_id,
            node_fencing_token=first_wave[0].fencing_token,
            lease_owner_id="barrier_compensation",
            fencing_token=lease.fencing_token,
            graph_status=EffectGraphStatus.COMPENSATING,
        )

        second_wave = await service.claim_effect_dag_compensation_nodes(
            task.task_id,
            plan.waves[1].node_ids,
            plan_id=plan.plan_id,
            wave_ordinal=1,
            claim_owner_id="barrier_compensation",
            claim_ttl_seconds=30,
            lease_owner_id="barrier_compensation",
            fencing_token=lease.fencing_token,
        )
        for index, claim in enumerate(second_wave):
            await service.transition_claimed_effect_node(
                task.task_id,
                claim.node_id,
                expected_statuses=frozenset({EffectNodeStatus.COMPENSATING}),
                target_status=(
                    terminal_node_status if index == 0 else EffectNodeStatus.COMPENSATED
                ),
                transition_kind="test_compensation_terminal",
                event_type="effect.compensation.test_terminal",
                claim_owner_id=claim.owner_id,
                node_fencing_token=claim.fencing_token,
                lease_owner_id="barrier_compensation",
                fencing_token=lease.fencing_token,
                graph_status=EffectGraphStatus.COMPENSATING,
            )
        graph = await service.reduce_effect_dag_compensation(
            task.task_id,
            plan_id=plan.plan_id,
            lease_owner_id="barrier_compensation",
            fencing_token=lease.fencing_token,
        )

        assert graph.status is terminal_graph_status
    finally:
        await database.dispose()
