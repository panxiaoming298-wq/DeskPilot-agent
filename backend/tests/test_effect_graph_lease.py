from datetime import UTC, datetime
from pathlib import Path

import pytest

from deskpilot.application.task_service import (
    EffectGraphFenceRejectedError,
    EffectGraphLeaseUnavailableError,
    TaskService,
    ToolCallNotFoundError,
    ToolCallStatus,
)
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectAttemptKind,
    EffectAttemptStatus,
    EffectExecutionMode,
    EffectGraphStatus,
    EffectNodeDefinition,
    EffectNodeStatus,
    effect_attempt_id,
    effect_call_id,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.domain.task_checkpoints import TaskCheckpointPayload
from deskpilot.infrastructure.database import Database
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


@pytest.mark.asyncio
async def test_graph_claim_uses_database_time_not_process_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'database-clock.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="database clock"))
        await service.create_effect_graph(
            task.task_id,
            (
                EffectNodeDefinition(
                    node_key="disk",
                    step_id="disk",
                    tool_name=DISK_USAGE_CONTRACT.name,
                    tool_version=DISK_USAGE_CONTRACT.version,
                    contract_digest=DISK_USAGE_CONTRACT.digest,
                    compensation_strategy=CompensationStrategy.NONE,
                ),
            ),
        )
        monkeypatch.setattr(
            "deskpilot.application.task_service.utc_now",
            lambda: datetime(2000, 1, 1, tzinfo=UTC),
        )

        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="database_clock_owner",
            ttl_seconds=30,
        )

        assert lease.acquired_at.year > 2000
        assert lease.expires_at > lease.acquired_at
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_graph_lease_takeover_increments_fence_and_rejects_stale_owner(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'graph-lease.db').as_posix()}"
    )
    await database.migrate()
    first = TaskService(database, "/api/v1")
    second = TaskService(database, "/api/v1")
    try:
        task = await first.create_task(TaskCreate(goal="lease test"))
        graph = await first.create_effect_graph(
            task.task_id,
            (
                EffectNodeDefinition(
                    node_key="disk",
                    step_id="disk",
                    tool_name=DISK_USAGE_CONTRACT.name,
                    tool_version=DISK_USAGE_CONTRACT.version,
                    contract_digest=DISK_USAGE_CONTRACT.digest,
                    compensation_strategy=CompensationStrategy.NONE,
                ),
            ),
        )
        first_lease = await first.acquire_effect_graph_lease(
            task.task_id,
            owner_id="api_first",
            ttl_seconds=30,
        )

        with pytest.raises(EffectGraphLeaseUnavailableError):
            await second.acquire_effect_graph_lease(
                task.task_id,
                owner_id="api_second",
                ttl_seconds=30,
            )

        assert await first.release_effect_graph_lease(
            task.task_id,
            owner_id="api_first",
            fencing_token=first_lease.fencing_token,
        )
        second_lease = await second.acquire_effect_graph_lease(
            task.task_id,
            owner_id="api_second",
            ttl_seconds=30,
        )
        assert second_lease.fencing_token == first_lease.fencing_token + 1

        with pytest.raises(EffectGraphFenceRejectedError):
            await first.transition_effect_node(
                task.task_id,
                graph.nodes[0].node_id,
                expected_statuses=frozenset({EffectNodeStatus.PENDING}),
                target_status=EffectNodeStatus.ACTIVE,
                transition_kind="stale_owner",
                event_type="effect.node.started",
                graph_status=EffectGraphStatus.ACTIVE,
                lease_owner_id="api_first",
                fencing_token=first_lease.fencing_token,
            )

        await second.transition_effect_node(
            task.task_id,
            graph.nodes[0].node_id,
            expected_statuses=frozenset({EffectNodeStatus.PENDING}),
            target_status=EffectNodeStatus.ACTIVE,
            transition_kind="current_owner",
            event_type="effect.node.started",
            graph_status=EffectGraphStatus.ACTIVE,
            lease_owner_id="api_second",
            fencing_token=second_lease.fencing_token,
        )
        updated = await second.get_effect_graph(task.task_id)
        assert updated.nodes[0].status is EffectNodeStatus.ACTIVE
        assert updated.fencing_token == second_lease.fencing_token
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_effect_request_rolls_back_ledger_transition_and_checkpoint_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'effect-command.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="atomic command"))
        graph = await service.create_effect_graph(
            task.task_id,
            (
                EffectNodeDefinition(
                    node_key="disk",
                    step_id="disk",
                    tool_name=DISK_USAGE_CONTRACT.name,
                    tool_version=DISK_USAGE_CONTRACT.version,
                    contract_digest=DISK_USAGE_CONTRACT.digest,
                    compensation_strategy=CompensationStrategy.NONE,
                ),
            ),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="api_atomic",
            ttl_seconds=30,
        )
        node_id = graph.nodes[0].node_id
        attempt_id = effect_attempt_id(node_id, EffectAttemptKind.FORWARD)
        call_id = effect_call_id(node_id, EffectAttemptKind.FORWARD)

        async def fail_checkpoint(*_: object, **__: object) -> None:
            raise RuntimeError("checkpoint write failed")

        monkeypatch.setattr(service, "_write_task_checkpoint", fail_checkpoint)
        with pytest.raises(RuntimeError, match="checkpoint write failed"):
            await service.request_effect_tool_call(
                task.task_id,
                node_id,
                call_id=call_id,
                attempt_id=attempt_id,
                attempt_kind=EffectAttemptKind.FORWARD,
                step_id="disk",
                tool_name=DISK_USAGE_CONTRACT.name,
                tool_version=DISK_USAGE_CONTRACT.version,
                contract_digest=DISK_USAGE_CONTRACT.digest,
                arguments={"path": "."},
                idempotency=DISK_USAGE_CONTRACT.execution.idempotency,
                idempotency_key=None,
                tool_attempt=1,
                risk=DISK_USAGE_CONTRACT.risk_level.value,
                checkpoint=TaskCheckpointPayload(
                    task_id=task.task_id,
                    next_stage=0,
                    tool_call_id=call_id,
                ),
                lease_owner_id="api_atomic",
                fencing_token=lease.fencing_token,
            )

        with pytest.raises(ToolCallNotFoundError):
            await service.get_tool_call_status(task.task_id, call_id)
        after = await service.get_effect_graph(task.task_id)
        assert after.nodes[0].status is EffectNodeStatus.PENDING
        assert after.nodes[0].attempts == ()
        assert not any(
            event.type in {"tool.requested", "effect.attempt.requested"}
            for event in await service.list_events(task.task_id)
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_startup_recovery_does_not_cross_another_instances_live_lease(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'startup-lease.db').as_posix()}"
    )
    await database.migrate()
    owner = TaskService(database, "/api/v1")
    contender = TaskService(database, "/api/v1")
    try:
        task = await owner.create_task(TaskCreate(goal="owned startup graph"))
        graph = await owner.create_effect_graph(
            task.task_id,
            (
                EffectNodeDefinition(
                    node_key="disk",
                    step_id="disk",
                    tool_name=DISK_USAGE_CONTRACT.name,
                    tool_version=DISK_USAGE_CONTRACT.version,
                    contract_digest=DISK_USAGE_CONTRACT.digest,
                    compensation_strategy=CompensationStrategy.NONE,
                ),
            ),
        )
        lease = await owner.acquire_effect_graph_lease(
            task.task_id,
            owner_id="api_owner",
            ttl_seconds=30,
        )
        node_id = graph.nodes[0].node_id
        attempt_id = effect_attempt_id(node_id, EffectAttemptKind.FORWARD)
        call_id = effect_call_id(node_id, EffectAttemptKind.FORWARD)
        await owner.request_effect_tool_call(
            task.task_id,
            node_id,
            call_id=call_id,
            attempt_id=attempt_id,
            attempt_kind=EffectAttemptKind.FORWARD,
            step_id="disk",
            tool_name=DISK_USAGE_CONTRACT.name,
            tool_version=DISK_USAGE_CONTRACT.version,
            contract_digest=DISK_USAGE_CONTRACT.digest,
            arguments={"path": "."},
            idempotency=DISK_USAGE_CONTRACT.execution.idempotency,
            idempotency_key=None,
            tool_attempt=1,
            risk=DISK_USAGE_CONTRACT.risk_level.value,
            checkpoint=TaskCheckpointPayload(
                task_id=task.task_id,
                next_stage=0,
                tool_call_id=call_id,
                graph_id=graph.graph_id,
                graph_schema_version=graph.schema_version,
                graph_fencing_token=lease.fencing_token,
                current_node_id=node_id,
            ),
            lease_owner_id="api_owner",
            fencing_token=lease.fencing_token,
        )

        recovered = await contender.recover_incomplete_tool_calls(
            lease_owner_id="api_contender",
            lease_ttl_seconds=30,
        )

        assert recovered.requested_failed == 0
        assert await contender.get_tool_call_status(task.task_id, call_id) == "requested"
        current = await contender.get_effect_graph(task.task_id)
        assert current.lease_owner_id == "api_owner"
        assert current.fencing_token == lease.fencing_token
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_effect_terminal_rolls_back_ledger_transition_and_checkpoint_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'terminal-command.db').as_posix()}"
    )
    await database.migrate()
    service = TaskService(database, "/api/v1")
    try:
        task = await service.create_task(TaskCreate(goal="terminal transaction"))
        graph = await service.create_effect_graph(
            task.task_id,
            (
                EffectNodeDefinition(
                    node_key="disk",
                    step_id="disk",
                    tool_name=DISK_USAGE_CONTRACT.name,
                    tool_version=DISK_USAGE_CONTRACT.version,
                    contract_digest=DISK_USAGE_CONTRACT.digest,
                    compensation_strategy=CompensationStrategy.NONE,
                ),
            ),
        )
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="api_terminal",
            ttl_seconds=30,
        )
        node_id = graph.nodes[0].node_id
        attempt_id = effect_attempt_id(node_id, EffectAttemptKind.FORWARD)
        call_id = effect_call_id(node_id, EffectAttemptKind.FORWARD)
        checkpoint = TaskCheckpointPayload(
            task_id=task.task_id,
            next_stage=0,
            tool_call_id=call_id,
            graph_id=graph.graph_id,
            graph_schema_version=graph.schema_version,
            graph_fencing_token=lease.fencing_token,
            current_node_id=node_id,
        )
        await service.request_effect_tool_call(
            task.task_id,
            node_id,
            call_id=call_id,
            attempt_id=attempt_id,
            attempt_kind=EffectAttemptKind.FORWARD,
            step_id="disk",
            tool_name=DISK_USAGE_CONTRACT.name,
            tool_version=DISK_USAGE_CONTRACT.version,
            contract_digest=DISK_USAGE_CONTRACT.digest,
            arguments={"path": "."},
            idempotency=DISK_USAGE_CONTRACT.execution.idempotency,
            idempotency_key=None,
            tool_attempt=1,
            risk=DISK_USAGE_CONTRACT.risk_level.value,
            checkpoint=checkpoint,
            lease_owner_id="api_terminal",
            fencing_token=lease.fencing_token,
        )
        event_count = len(await service.list_events(task.task_id))

        async def fail_checkpoint(*_: object, **__: object) -> None:
            raise RuntimeError("terminal checkpoint write failed")

        monkeypatch.setattr(service, "_write_task_checkpoint", fail_checkpoint)
        with pytest.raises(RuntimeError, match="terminal checkpoint write failed"):
            await service.finish_effect_tool_call(
                task.task_id,
                node_id,
                call_id=call_id,
                attempt_id=attempt_id,
                status=ToolCallStatus.FAILED,
                target_status=EffectNodeStatus.FAILED,
                transition_kind="attempt_failed",
                event_type="effect.attempt.failed",
                attempt_status=EffectAttemptStatus.FAILED,
                graph_status=EffectGraphStatus.FAILED,
                execution_mode=EffectExecutionMode.FORWARD,
                failure_node_id=node_id,
                error_code="TEST_FAILURE",
                checkpoint=checkpoint,
                lease_owner_id="api_terminal",
                fencing_token=lease.fencing_token,
            )

        assert await service.get_tool_call_status(task.task_id, call_id) == "requested"
        after = await service.get_effect_graph(task.task_id)
        assert after.status is EffectGraphStatus.ACTIVE
        assert after.nodes[0].status is EffectNodeStatus.PENDING
        assert after.nodes[0].attempts[0].status is EffectAttemptStatus.REQUESTED
        assert len(await service.list_events(task.task_id)) == event_count
    finally:
        await database.dispose()
