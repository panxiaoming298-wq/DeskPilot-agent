"""Opt-in PostgreSQL container restart and database-time fence drill."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from asyncpg.exceptions import CannotConnectNowError
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from deskpilot.application.event_broker import EventBroker
from deskpilot.application.outbox_publisher import (
    ClaimedOutboxMessage,
    OutboxPublisher,
)
from deskpilot.application.task_service import (
    EffectGraphFenceRejectedError,
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
    OutboxMessageRecord,
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

_RESTART_ALLOW_ENV = "DESKPILOT_TEST_POSTGRESQL_RESTART_ALLOW"
_DOCKER_CLI_ENV = "DESKPILOT_TEST_DOCKER_CLI"
_CONTAINER_NAME = "deskpilot-postgres"
_COMPOSE_PROJECT = "deskpilot-storage"
_COMPOSE_SERVICE = "postgres"
_DATA_VOLUME = "deskpilot_postgres_data"


@dataclass(frozen=True, slots=True)
class _RestartTarget:
    docker_cli: str
    container_name: str


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _restart_target() -> _RestartTarget:
    if os.environ.get(_RESTART_ALLOW_ENV) != "1":
        pytest.skip(f"{_RESTART_ALLOW_ENV}=1 is required for a container restart")
    configured_cli = os.environ.get(_DOCKER_CLI_ENV)
    docker_cli = configured_cli or shutil.which("docker")
    if docker_cli is None or not Path(docker_cli).is_file():
        pytest.fail(
            f"Docker CLI is unavailable; set {_DOCKER_CLI_ENV} to its absolute path"
        )
    return _RestartTarget(docker_cli=docker_cli, container_name=_CONTAINER_NAME)


def _run_docker(target: _RestartTarget, *arguments: str) -> str:
    completed = subprocess.run(
        (target.docker_cli, *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        pytest.fail(
            "Docker command failed during PostgreSQL restart verification "
            f"(exit_code={completed.returncode})"
        )
    return completed.stdout.strip()


def _inspect_restart_target(
    target: _RestartTarget,
    *,
    database_url: str,
    require_healthy: bool = True,
) -> dict[str, Any]:
    raw_inspect = _run_docker(target, "inspect", target.container_name)
    try:
        documents = json.loads(raw_inspect)
    except json.JSONDecodeError:
        pytest.fail("Docker inspect returned malformed JSON")
    if not isinstance(documents, list) or len(documents) != 1:
        pytest.fail("Docker inspect did not identify exactly one restart target")
    inspected = documents[0]
    if not isinstance(inspected, dict):
        pytest.fail("Docker inspect target is malformed")

    config = inspected.get("Config")
    state = inspected.get("State")
    network = inspected.get("NetworkSettings")
    mounts = inspected.get("Mounts")
    if not isinstance(config, dict) or not isinstance(state, dict):
        pytest.fail("Docker inspect is missing target configuration or state")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        pytest.fail("PostgreSQL restart target has no Compose labels")
    expected_labels = {
        "com.docker.compose.project": _COMPOSE_PROJECT,
        "com.docker.compose.service": _COMPOSE_SERVICE,
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        pytest.fail("PostgreSQL restart target does not match the dedicated Compose service")
    if state.get("Status") != "running":
        pytest.fail("PostgreSQL restart target is not running")
    health = state.get("Health")
    if require_healthy and (
        not isinstance(health, dict) or health.get("Status") != "healthy"
    ):
        pytest.fail("PostgreSQL restart target is not healthy")
    if not isinstance(mounts, list) or not any(
        isinstance(mount, dict)
        and mount.get("Type") == "volume"
        and mount.get("Name") == _DATA_VOLUME
        and mount.get("Destination") == "/var/lib/postgresql/data"
        for mount in mounts
    ):
        pytest.fail("PostgreSQL restart target does not use the dedicated data volume")

    url = make_url(database_url)
    expected_port = str(url.port or 5432)
    ports = network.get("Ports") if isinstance(network, dict) else None
    bindings = ports.get("5432/tcp") if isinstance(ports, dict) else None
    if not isinstance(bindings, list) or not any(
        isinstance(binding, dict)
        and binding.get("HostIp") == "127.0.0.1"
        and binding.get("HostPort") == expected_port
        for binding in bindings
    ):
        pytest.fail("PostgreSQL restart target does not match the guarded test URL port")
    return inspected


async def _wait_for_container_healthy(
    target: _RestartTarget,
    *,
    database_url: str,
) -> dict[str, Any]:
    for _ in range(120):
        inspected = await asyncio.to_thread(
            _inspect_restart_target,
            target,
            database_url=database_url,
            require_healthy=False,
        )
        health = inspected["State"].get("Health")
        if isinstance(health, dict) and health.get("Status") == "healthy":
            return inspected
        await asyncio.sleep(0.1)
    pytest.fail("PostgreSQL restart target did not become healthy within the test bound")


def _node() -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key="postgres_restart_root",
        step_id="postgres_restart_root",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


def _claim_for_task(
    claims: list[ClaimedOutboxMessage],
    *,
    task_id: str,
) -> ClaimedOutboxMessage:
    matches = [
        claim for claim in claims if claim.payload.get("task_id") == task_id
    ]
    assert len(matches) == 1
    return matches[0]


async def _wait_for_database_ttls(
    database_url: str,
    *,
    graph_id: str,
    node_id: str,
    message_id: str,
) -> tuple[Database, datetime]:
    last_error: Exception | None = None
    for _ in range(240):
        probe = Database(database_url)
        try:
            async with probe.session() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT pg_postmaster_start_time(), "
                            "current_timestamp >= graph.lease_expires_at, "
                            "current_timestamp >= node.claim_expires_at, "
                            "current_timestamp >= message.claim_expires_at "
                            "FROM tool_effect_graphs AS graph "
                            "JOIN tool_effect_nodes AS node "
                            "ON node.graph_id = graph.graph_id "
                            "JOIN outbox_messages AS message "
                            "ON message.message_id = :message_id "
                            "WHERE graph.graph_id = :graph_id "
                            "AND node.node_id = :node_id"
                        ),
                        {
                            "graph_id": graph_id,
                            "node_id": node_id,
                            "message_id": message_id,
                        },
                    )
                ).one_or_none()
            if row is not None and row[1] is True and row[2] is True and row[3] is True:
                return probe, row[0]
        except (CannotConnectNowError, DBAPIError, OSError) as exc:
            last_error = exc
        await probe.dispose()
        await asyncio.sleep(0.1)
    if last_error is not None:
        pytest.fail(
            "PostgreSQL did not recover and expire database-time leases "
            f"({type(last_error).__name__})"
        )
    pytest.fail("database-time graph/node/outbox TTLs did not expire within the test bound")


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_container_restart_recovers_database_time_leases_and_fences_stale_writers(
) -> None:
    database_url = _postgresql_test_url()
    restart_target = _restart_target()
    _inspect_restart_target(restart_target, database_url=database_url)

    original_database = Database(database_url)
    recovered_database: Database | None = None
    stale_database: Database | None = None
    task_id: str | None = None
    victim_connection = None
    stale_outbox_claim: ClaimedOutboxMessage | None = None
    old_graph_owner = f"api_before_restart_{uuid4().hex}"
    old_node_owner = f"node_before_restart_{uuid4().hex}"
    old_publisher_owner = f"publisher_before_restart_{uuid4().hex}"
    new_graph_owner = f"api_after_restart_{uuid4().hex}"
    new_node_owner = f"node_after_restart_{uuid4().hex}"
    new_publisher_owner = f"publisher_after_restart_{uuid4().hex}"
    try:
        await original_database.migrate()
        service = TaskService(original_database, "/api/v1")
        broker = EventBroker()
        stale_publisher = OutboxPublisher(
            original_database,
            broker,
            instance_id=old_publisher_owner,
            claim_ttl_seconds=3,
        )
        task = await service.create_task(
            TaskCreate(goal=f"postgresql container restart {uuid4().hex}")
        )
        task_id = task.task_id
        graph = await service.create_effect_dag(task.task_id, (_node(),))
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id=old_graph_owner,
            ttl_seconds=3,
        )
        ready = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id=old_graph_owner,
            fencing_token=lease.fencing_token,
            page_size=1,
        )
        claimed_node = (
            await service.claim_effect_dag_nodes(
                task.task_id,
                (ready.ready_nodes[0].node_id,),
                ready_proof_digest=ready.proof_digest,
                claim_owner_id=old_node_owner,
                claim_ttl_seconds=3,
                lease_owner_id=old_graph_owner,
                fencing_token=lease.fencing_token,
            )
        )[0]
        stale_outbox_claim = _claim_for_task(
            await stale_publisher._claim_batch(),
            task_id=task.task_id,
        )
        assert lease.fencing_token == 1
        assert claimed_node.fencing_token == 1
        assert stale_outbox_claim.fencing_token == 1

        victim_connection = await original_database.engine.connect()
        postmaster_started_before = await victim_connection.scalar(
            text("SELECT pg_postmaster_start_time()")
        )
        backend_pid = await victim_connection.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(backend_pid, int)

        restart_output = await asyncio.to_thread(
            _run_docker,
            restart_target,
            "restart",
            restart_target.container_name,
        )
        assert restart_output == restart_target.container_name
        with pytest.raises(DBAPIError) as disconnected:
            await victim_connection.scalar(text("SELECT 1"))
        assert disconnected.value.connection_invalidated

        recovered_database, postmaster_started_after = await _wait_for_database_ttls(
            database_url,
            graph_id=graph.graph_id,
            node_id=claimed_node.node_id,
            message_id=stale_outbox_claim.message_id,
        )
        assert postmaster_started_after > postmaster_started_before
        inspected_after = await _wait_for_container_healthy(
            restart_target,
            database_url=database_url,
        )
        assert inspected_after["State"]["Status"] == "running"

        recovered = TaskService(recovered_database, "/api/v1")
        stale_database = Database(database_url)
        stale = TaskService(stale_database, "/api/v1")
        takeover_lease = await recovered.acquire_effect_graph_lease(
            task.task_id,
            owner_id=new_graph_owner,
            ttl_seconds=30,
        )
        assert takeover_lease.fencing_token == 2
        recovered_ready = await recovered.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id=new_graph_owner,
            fencing_token=takeover_lease.fencing_token,
            page_size=1,
        )
        reclaimed = (
            await recovered.claim_effect_dag_nodes(
                task.task_id,
                (claimed_node.node_id,),
                ready_proof_digest=recovered_ready.proof_digest,
                claim_owner_id=new_node_owner,
                claim_ttl_seconds=30,
                lease_owner_id=new_graph_owner,
                fencing_token=takeover_lease.fencing_token,
            )
        )[0]
        assert reclaimed.fencing_token == 2

        current_publisher = OutboxPublisher(
            recovered_database,
            broker,
            instance_id=new_publisher_owner,
            claim_ttl_seconds=30,
        )
        current_outbox_claim = _claim_for_task(
            await current_publisher._claim_batch(),
            task_id=task.task_id,
        )
        assert current_outbox_claim.message_id == stale_outbox_claim.message_id
        assert current_outbox_claim.fencing_token == 2

        async with recovered_database.session() as session:
            graph_before_rejections = await session.get(
                ToolEffectGraphRecord, graph.graph_id
            )
            node_before_rejections = await session.get(
                ToolEffectNodeRecord, claimed_node.node_id
            )
            outbox_before_rejections = await session.get(
                OutboxMessageRecord, stale_outbox_claim.message_id
            )
        assert graph_before_rejections is not None
        assert node_before_rejections is not None
        assert outbox_before_rejections is not None
        graph_revision = graph_before_rejections.revision
        node_revision = node_before_rejections.revision
        outbox_identity = (
            outbox_before_rejections.claim_owner_id,
            outbox_before_rejections.claim_fencing_token,
            outbox_before_rejections.delivery_id,
            outbox_before_rejections.published_at,
            outbox_before_rejections.attempt_count,
        )

        with pytest.raises(EffectGraphFenceRejectedError):
            await stale.transition_claimed_effect_node(
                task.task_id,
                claimed_node.node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="container_restart_stale_graph_owner",
                event_type="effect.node.succeeded",
                claim_owner_id=old_node_owner,
                node_fencing_token=claimed_node.fencing_token,
                lease_owner_id=old_graph_owner,
                fencing_token=lease.fencing_token,
            )
        with pytest.raises(EffectNodeFenceRejectedError):
            await stale.transition_claimed_effect_node(
                task.task_id,
                claimed_node.node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="container_restart_stale_node_fence",
                event_type="effect.node.succeeded",
                claim_owner_id=old_node_owner,
                node_fencing_token=claimed_node.fencing_token,
                lease_owner_id=new_graph_owner,
                fencing_token=takeover_lease.fencing_token,
            )
        stale_publisher_after_restart = OutboxPublisher(
            stale_database,
            broker,
            instance_id=old_publisher_owner,
            claim_ttl_seconds=30,
        )
        assert not await stale_publisher_after_restart._mark_published(
            stale_outbox_claim
        )

        async with recovered_database.session() as session:
            graph_after_rejections = await session.get(
                ToolEffectGraphRecord, graph.graph_id
            )
            node_after_rejections = await session.get(
                ToolEffectNodeRecord, claimed_node.node_id
            )
            outbox_after_rejections = await session.get(
                OutboxMessageRecord, stale_outbox_claim.message_id
            )
            tool_call_count = await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(ToolCallRecord.task_id == task.task_id)
            )
        assert graph_after_rejections is not None
        assert node_after_rejections is not None
        assert outbox_after_rejections is not None
        assert graph_after_rejections.revision == graph_revision
        assert node_after_rejections.revision == node_revision
        assert (
            outbox_after_rejections.claim_owner_id,
            outbox_after_rejections.claim_fencing_token,
            outbox_after_rejections.delivery_id,
            outbox_after_rejections.published_at,
            outbox_after_rejections.attempt_count,
        ) == outbox_identity
        assert tool_call_count == 0

        assert await current_publisher._mark_published(current_outbox_claim)
        await recovered.transition_claimed_effect_node(
            task.task_id,
            claimed_node.node_id,
            expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
            target_status=EffectNodeStatus.SUCCEEDED,
            transition_kind="container_restart_recovered_node_succeeded",
            event_type="effect.node.succeeded",
            claim_owner_id=new_node_owner,
            node_fencing_token=reclaimed.fencing_token,
            lease_owner_id=new_graph_owner,
            fencing_token=takeover_lease.fencing_token,
        )
        recovered_graph = await recovered.get_effect_graph(task.task_id)
        assert recovered_graph.nodes[0].status is EffectNodeStatus.SUCCEEDED
        events = await recovered.list_events(task.task_id)
        assert sum(event.type == "effect.node.claimed" for event in events) == 1
        assert sum(event.type == "effect.node.reclaimed" for event in events) == 1
        assert not any(event.type == "tool.requested" for event in events)
    finally:
        if victim_connection is not None:
            with suppress(Exception):
                await victim_connection.close()
        cleanup_database = recovered_database or Database(database_url)
        try:
            if task_id is not None:
                async with cleanup_database.session() as session:
                    async with session.begin():
                        await session.execute(
                            delete(TaskRecord).where(TaskRecord.task_id == task_id)
                        )
        finally:
            if recovered_database is None:
                await cleanup_database.dispose()
        await original_database.dispose()
        if recovered_database is not None:
            await recovered_database.dispose()
        if stale_database is not None:
            await stale_database.dispose()
