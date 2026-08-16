"""Opt-in API process-kill drill at the committed node-claim boundary."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text

from deskpilot.application.task_service import (
    EffectGraphFenceRejectedError,
    EffectGraphLeaseUnavailableError,
    EffectNodeFenceRejectedError,
    TaskService,
)
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagNodeDefinition,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolCallRecord,
    ToolEffectGraphRecord,
    ToolEffectNodeRecord,
)
from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT

_FAULT_MODULE = "tests.fault_injection.api_claim_after_commit"
_FAULT_CHECKPOINT = "api_claim_after_commit"


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _node() -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key="api_kill_root",
        step_id="api_kill_root",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


async def _read_fault_checkpoint(
    process: asyncio.subprocess.Process,
) -> Mapping[str, Any]:
    if process.stdout is None:
        pytest.fail("fault injector stdout is unavailable")
    try:
        raw_line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
    except TimeoutError:
        pytest.fail("fault injector did not reach claim-after-commit checkpoint")
    if not raw_line:
        pytest.fail(
            "fault injector exited before claim-after-commit checkpoint "
            f"(returncode={process.returncode})"
        )
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        pytest.fail("fault injector emitted malformed checkpoint JSON")
    if not isinstance(payload, dict):
        pytest.fail("fault injector checkpoint must be a JSON object")
    return payload


async def _wait_for_database_ttl(
    database: Database,
    *,
    graph_id: str,
    node_id: str,
) -> None:
    for _ in range(160):
        async with database.session() as session:
            expired = await session.scalar(
                text(
                    "SELECT current_timestamp >= "
                    "GREATEST(graph.lease_expires_at, node.claim_expires_at) "
                    "FROM tool_effect_graphs AS graph "
                    "JOIN tool_effect_nodes AS node ON node.graph_id = graph.graph_id "
                    "WHERE graph.graph_id = :graph_id AND node.node_id = :node_id"
                ),
                {"graph_id": graph_id, "node_id": node_id},
            )
        if expired is True:
            return
        await asyncio.sleep(0.05)
    pytest.fail("database-time graph/node TTL did not expire within the test bound")


def _process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


async def _kill_fault_process(
    process: asyncio.subprocess.Process,
    *,
    process_id: int,
) -> None:
    if process_id == process.pid:
        process.kill()
    else:
        os.kill(process_id, signal.SIGTERM)
    await asyncio.wait_for(process.wait(), timeout=5)
    for _ in range(100):
        if not _process_is_alive(process_id):
            break
        await asyncio.sleep(0.01)
    assert not _process_is_alive(process_id)


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_api_process_kill_after_claim_is_ttl_reclaimed_without_ghost_fences() -> None:
    database_url = _postgresql_test_url()
    database = Database(database_url)
    contender_database = Database(database_url)
    stale_database = Database(database_url)
    process: asyncio.subprocess.Process | None = None
    fault_process_id: int | None = None
    task_id: str | None = None
    child_graph_owner = f"api_killed_{uuid4().hex}"
    child_node_owner = f"node_killed_{uuid4().hex}"
    takeover_graph_owner = f"api_takeover_{uuid4().hex}"
    takeover_node_owner = f"node_takeover_{uuid4().hex}"
    try:
        await database.migrate()
        control = TaskService(database, "/api/v1")
        contender = TaskService(contender_database, "/api/v1")
        stale = TaskService(stale_database, "/api/v1")
        task = await control.create_task(
            TaskCreate(goal=f"api process kill after claim {uuid4().hex}")
        )
        task_id = task.task_id
        graph = await control.create_effect_dag(task.task_id, (_node(),))

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _FAULT_MODULE,
            "--task-id",
            task.task_id,
            "--graph-owner-id",
            child_graph_owner,
            "--node-owner-id",
            child_node_owner,
            "--ttl-seconds",
            "3",
            cwd=Path(__file__).parents[1],
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        checkpoint = await _read_fault_checkpoint(process)
        assert checkpoint.get("checkpoint") == _FAULT_CHECKPOINT, checkpoint.get(
            "error_code"
        )
        fault_process_id = checkpoint.get("process_id")
        assert isinstance(fault_process_id, int)
        if os.name == "nt":
            assert checkpoint.get("parent_process_id") == process.pid
        else:
            assert fault_process_id == process.pid
        assert checkpoint.get("task_id") == task.task_id
        assert checkpoint.get("graph_id") == graph.graph_id
        assert checkpoint.get("graph_owner_id") == child_graph_owner
        assert checkpoint.get("graph_fencing_token") == 1
        node_id = checkpoint.get("node_id")
        assert isinstance(node_id, str)
        assert checkpoint.get("node_owner_id") == child_node_owner
        assert checkpoint.get("node_fencing_token") == 1

        await _kill_fault_process(process, process_id=fault_process_id)
        assert process.returncode != 0

        async with database.session() as session:
            graph_after_kill = await session.get(ToolEffectGraphRecord, graph.graph_id)
            node_after_kill = await session.get(ToolEffectNodeRecord, node_id)
            tool_call_count = await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(ToolCallRecord.task_id == task.task_id)
            )
        assert graph_after_kill is not None
        assert node_after_kill is not None
        assert graph_after_kill.lease_owner_id == child_graph_owner
        assert graph_after_kill.fencing_token == 1
        assert node_after_kill.status == EffectNodeStatus.ACTIVE.value
        assert node_after_kill.claim_owner_id == child_node_owner
        assert node_after_kill.claim_fencing_token == 1
        assert tool_call_count == 0
        revisions_after_kill = (graph_after_kill.revision, node_after_kill.revision)
        events_after_kill = await control.list_events(task.task_id)
        assert sum(event.type == "effect.node.claimed" for event in events_after_kill) == 1
        assert not any(event.type == "tool.requested" for event in events_after_kill)

        with pytest.raises(EffectGraphLeaseUnavailableError):
            await contender.acquire_effect_graph_lease(
                task.task_id,
                owner_id=takeover_graph_owner,
                ttl_seconds=30,
            )

        await _wait_for_database_ttl(
            database,
            graph_id=graph.graph_id,
            node_id=node_id,
        )
        takeover_lease = await contender.acquire_effect_graph_lease(
            task.task_id,
            owner_id=takeover_graph_owner,
            ttl_seconds=30,
        )
        assert takeover_lease.fencing_token == 2
        recovered_ready = await contender.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id=takeover_graph_owner,
            fencing_token=takeover_lease.fencing_token,
            page_size=1,
        )
        assert len(recovered_ready.ready_nodes) == 1
        assert recovered_ready.ready_nodes[0].node_id == node_id
        assert recovered_ready.ready_nodes[0].status is EffectNodeStatus.ACTIVE
        reclaimed = (
            await contender.claim_effect_dag_nodes(
                task.task_id,
                (node_id,),
                ready_proof_digest=recovered_ready.proof_digest,
                claim_owner_id=takeover_node_owner,
                claim_ttl_seconds=30,
                lease_owner_id=takeover_graph_owner,
                fencing_token=takeover_lease.fencing_token,
            )
        )[0]
        assert reclaimed.fencing_token == 2

        with pytest.raises(EffectGraphFenceRejectedError):
            await stale.transition_claimed_effect_node(
                task.task_id,
                node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="killed_api_stale_graph_fence",
                event_type="effect.node.succeeded",
                claim_owner_id=child_node_owner,
                node_fencing_token=1,
                lease_owner_id=child_graph_owner,
                fencing_token=1,
            )
        with pytest.raises(EffectNodeFenceRejectedError):
            await stale.transition_claimed_effect_node(
                task.task_id,
                node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="killed_api_stale_node_fence",
                event_type="effect.node.succeeded",
                claim_owner_id=child_node_owner,
                node_fencing_token=1,
                lease_owner_id=takeover_graph_owner,
                fencing_token=takeover_lease.fencing_token,
            )

        async with database.session() as session:
            graph_after_rejections = await session.get(
                ToolEffectGraphRecord,
                graph.graph_id,
            )
            node_after_rejections = await session.get(ToolEffectNodeRecord, node_id)
            tool_call_count = await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(ToolCallRecord.task_id == task.task_id)
            )
        assert graph_after_rejections is not None
        assert node_after_rejections is not None
        assert graph_after_rejections.revision == revisions_after_kill[0] + 2
        assert node_after_rejections.revision == revisions_after_kill[1] + 1
        assert graph_after_rejections.fencing_token == 2
        assert node_after_rejections.claim_fencing_token == 2
        assert node_after_rejections.claim_owner_id == takeover_node_owner
        assert tool_call_count == 0

        events_after_reclaim = await control.list_events(task.task_id)
        assert sum(event.type == "effect.node.claimed" for event in events_after_reclaim) == 1
        assert sum(event.type == "effect.node.reclaimed" for event in events_after_reclaim) == 1
        assert not any(event.type == "tool.requested" for event in events_after_reclaim)

        await contender.transition_claimed_effect_node(
            task.task_id,
            node_id,
            expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
            target_status=EffectNodeStatus.SUCCEEDED,
            transition_kind="api_kill_recovered_node_succeeded",
            event_type="effect.node.succeeded",
            claim_owner_id=takeover_node_owner,
            node_fencing_token=reclaimed.fencing_token,
            lease_owner_id=takeover_graph_owner,
            fencing_token=takeover_lease.fencing_token,
        )
        recovered_graph = await contender.get_effect_graph(task.task_id)
        assert recovered_graph.nodes[0].status is EffectNodeStatus.SUCCEEDED
    finally:
        if fault_process_id is not None and _process_is_alive(fault_process_id):
            os.kill(fault_process_id, signal.SIGTERM)
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if task_id is not None:
            async with database.session() as session:
                async with session.begin():
                    await session.execute(
                        delete(TaskRecord).where(TaskRecord.task_id == task_id)
                    )
        await database.dispose()
        await contender_database.dispose()
        await stale_database.dispose()
