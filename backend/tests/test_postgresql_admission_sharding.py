"""Opt-in PostgreSQL admission shard contention and TTL verification."""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

from deskpilot.application.effect_dag_admission import EffectDagAdmissionRequest
from deskpilot.application.effect_dag_cluster_admission import (
    EffectDagAdmissionFenceRejectedError,
    EffectDagClusterAdmissionStatus,
    EffectDagClusterAdmissionStore,
)
from deskpilot.application.task_service import TaskService
from deskpilot.domain.effect_graph import CompensationStrategy, EffectDagNodeDefinition
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.admission_shard_queries import (
    build_postgresql_admission_candidate_statement,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolEffectDagAdmissionRecord,
    ToolEffectDagAdmissionStateRecord,
    utc_now,
)
from deskpilot.infrastructure.postgresql_plan_baseline import (
    build_plan_baseline,
    compare_plan_baseline,
    load_plan_baseline,
    query_shape_sha256,
    write_plan_baseline,
)
from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT

_PLAN_BASELINE_PATH = (
    Path(__file__).parent
    / "baselines"
    / "postgresql"
    / "admission-shard-v1-16000-tickets.postgresql-17.json"
)
_PLAN_BASELINE_MODE_ENV = "DESKPILOT_TEST_POSTGRESQL_PLAN_BASELINE_MODE"
_POSTGRESQL_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _node(index: int) -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key=f"root_{index}",
        step_id=f"root_{index}",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


def _postgresql_sql(statement: ClauseElement) -> str:
    return str(
        statement.compile(
            dialect=_POSTGRESQL_DIALECT,
            compile_kwargs={"literal_binds": True},
        )
    )


async def _schedule_wave(
    stores: tuple[EffectDagClusterAdmissionStore, ...],
    *,
    expected_active: int,
) -> None:
    for _ in range(12):
        await asyncio.gather(
            *(
                store.schedule(
                    global_limit=2,
                    per_graph_limit=1,
                    default_tool_limit=2,
                    tool_limits={"tool_a": 1, "tool_b": 1},
                )
                for store in stores
            )
        )
        if (await stores[0].snapshot()).active_total == expected_active:
            return
    raise AssertionError("PostgreSQL admission wave did not converge")


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_postgresql_shards_preserve_capacity_fairness_and_ttl_fences() -> None:
    database_url = _postgresql_test_url()
    baseline_mode = os.environ.get(_PLAN_BASELINE_MODE_ENV, "compare")
    if baseline_mode not in {"compare", "record"}:
        pytest.fail(f"{_PLAN_BASELINE_MODE_ENV} must be 'compare' or 'record'")
    databases = tuple(Database(database_url) for _ in range(4))
    control_database = databases[0]
    task_ids: list[str] = []
    try:
        await control_database.migrate()
        service = TaskService(control_database, "/api/v1")
        stores = tuple(EffectDagClusterAdmissionStore(database) for database in databases)
        await stores[0].ensure_configuration(
            global_limit=2,
            per_graph_limit=1,
            default_tool_limit=2,
            tool_limits={"tool_a": 1, "tool_b": 1},
        )
        async with control_database.session() as session:
            state = await session.get(ToolEffectDagAdmissionStateRecord, "global")
        assert state is not None
        configuration_revision = state.revision

        selected: dict[int, tuple[str, str]] = {}
        for index in range(32):
            task = await service.create_task(TaskCreate(goal=f"postgresql admission shard {index}"))
            task_ids.append(task.task_id)
            graph = await service.create_effect_dag(task.task_id, (_node(index),))
            shard_id = stores[0]._scheduling_shard(graph.graph_id)
            selected.setdefault(
                shard_id,
                (graph.graph_id, graph.nodes[0].node_id),
            )
            if len(selected) == 4:
                break
        assert len(selected) == 4

        batches: list[str] = []
        original_shards = tuple(sorted(selected))
        for index, shard_id in enumerate(original_shards):
            graph_id, node_id = selected[shard_id]
            batches.append(
                await stores[index].register_batch(
                    graph_id,
                    (
                        EffectDagAdmissionRequest(
                            node_id=node_id,
                            tool_name="tool_a" if index % 2 == 0 else "tool_b",
                        ),
                    ),
                    owner_id=f"postgresql_admission_owner_{index}",
                    lease_ttl_seconds=30,
                )
            )

        await _schedule_wave(stores, expected_active=2)
        first_entries = [
            entry for batch_id in batches for entry in await stores[0].read_batch(batch_id)
        ]
        first_granted = [
            entry
            for entry in first_entries
            if entry.status is EffectDagClusterAdmissionStatus.GRANTED
        ]
        assert len(first_granted) == 2
        assert {entry.request.tool_name for entry in first_granted} == {
            "tool_a",
            "tool_b",
        }
        assert (await stores[0].snapshot()).active_total == 2
        for entry in first_granted:
            assert await stores[0].release_permit(
                entry.admission_id,
                owner_id=entry.owner_id,
                fencing_token=entry.fencing_token,
            )

        await _schedule_wave(stores, expected_active=2)
        second_entries = [
            entry for batch_id in batches for entry in await stores[0].read_batch(batch_id)
        ]
        second_granted = [
            entry
            for entry in second_entries
            if entry.status is EffectDagClusterAdmissionStatus.GRANTED
        ]
        assert len(second_granted) == 2
        served_ids = {entry.admission_id for entry in (*first_granted, *second_granted)}
        assert len(served_ids) == 4

        stale = second_granted[0]
        async with control_database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagAdmissionRecord)
                    .where(ToolEffectDagAdmissionRecord.admission_id == stale.admission_id)
                    .values(expires_at=utc_now() - timedelta(seconds=1))
                )
        replacement_shard = stores[0]._scheduling_shard(stale.graph_id)
        replacement_graph = stale.graph_id
        replacement_node = stale.request.node_id
        replacement_batch = await stores[0].register_batch(
            replacement_graph,
            (
                EffectDagAdmissionRequest(
                    node_id=replacement_node,
                    tool_name=stale.request.tool_name,
                ),
            ),
            owner_id="postgresql_admission_replacement",
            lease_ttl_seconds=30,
        )
        await _schedule_wave(stores, expected_active=2)
        replacement = (await stores[0].read_batch(replacement_batch))[0]
        assert replacement.status is EffectDagClusterAdmissionStatus.GRANTED
        assert replacement.grant_sequence is not None
        assert stale.grant_sequence is not None
        assert replacement.grant_sequence > stale.grant_sequence
        assert not await stores[0].release_permit(
            stale.admission_id,
            owner_id=stale.owner_id,
            fencing_token=stale.fencing_token,
        )
        with pytest.raises(EffectDagAdmissionFenceRejectedError):
            await stores[0].renew_permit(
                stale.admission_id,
                owner_id=stale.owner_id,
                fencing_token=stale.fencing_token,
                lease_ttl_seconds=30,
            )

        async with control_database.session() as session:
            state = await session.get(ToolEffectDagAdmissionStateRecord, "global")
            scheduling_shards = tuple(
                (
                    await session.scalars(
                        select(ToolEffectDagAdmissionRecord.scheduling_shard).where(
                            ToolEffectDagAdmissionRecord.batch_id.in_((*batches, replacement_batch))
                        )
                    )
                ).all()
            )
        assert state is not None
        assert state.revision == configuration_revision
        assert set(scheduling_shards) == {*original_shards, replacement_shard}

        for entry in (*second_granted[1:], replacement):
            await stores[0].release_permit(
                entry.admission_id,
                owner_id=entry.owner_id,
                fencing_token=entry.fencing_token,
            )

        plan_shard = original_shards[0]
        plan_graph_id, plan_node_id = selected[plan_shard]
        plan_prefix = uuid4().hex[:8]
        async with control_database.session() as session:
            async with session.begin():
                database_time = await session.scalar(text("SELECT CURRENT_TIMESTAMP"))
                assert database_time is not None
                await session.execute(
                    insert(ToolEffectDagAdmissionRecord),
                    [
                        {
                            "admission_id": f"adp_{plan_prefix}_{shard_id:02d}_{index:04d}",
                            "batch_id": f"btp_{plan_prefix}_{shard_id:02d}_{index:04d}",
                            "graph_id": plan_graph_id,
                            "node_id": plan_node_id,
                            "tool_name": "plan_tool",
                            "owner_id": "postgresql_plan_owner",
                            "status": "pending",
                            "scheduling_shard": shard_id,
                            "lease_ttl_seconds": 300,
                            "revision": 1,
                            "fencing_token": 0,
                            "grant_sequence": None,
                            "created_at": database_time,
                            "updated_at": database_time,
                            "granted_at": None,
                            "heartbeat_at": None,
                            "expires_at": database_time + timedelta(minutes=5),
                            "released_at": None,
                        }
                        for shard_id in range(16)
                        for index in range(1_000)
                    ],
                )
        async with control_database.session() as session:
            await session.execute(text("ANALYZE tool_effect_dag_admissions"))
            database_time = await session.scalar(text("SELECT CURRENT_TIMESTAMP"))
            assert database_time is not None
            explain_statement = build_postgresql_admission_candidate_statement(
                shard_id=plan_shard,
                database_time=database_time,
                candidate_limit=101,
            )
            explain_sql = _postgresql_sql(explain_statement)
            raw_plan = await session.scalar(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {explain_sql}")
            )
            postgresql_version = await session.scalar(text("SELECT version()"))
            server_version = await session.scalar(text("SELECT current_setting('server_version')"))
            raw_server_version_num = await session.scalar(
                text("SELECT current_setting('server_version_num')")
            )
        plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
        assert isinstance(plan, list)
        assert plan and isinstance(plan[0], dict)
        assert int(plan[0]["Plan"]["Actual Rows"]) == 101
        assert isinstance(postgresql_version, str)
        assert isinstance(server_version, str)
        assert isinstance(raw_server_version_num, str)
        parameterized_sql = str(explain_statement.compile(dialect=_POSTGRESQL_DIALECT))
        server_version_num = int(raw_server_version_num)
        captured_baseline = build_plan_baseline(
            baseline_id="admission-shard-v1-16000-tickets-pg17",
            workload={
                "shard_count": 16,
                "pending_ticket_count": 16_000,
                "pending_tickets_per_shard": 1_000,
                "candidate_limit": 101,
                "expected_rows": 101,
            },
            query_shape_digest=query_shape_sha256(parameterized_sql),
            postgresql_version=postgresql_version,
            server_version=server_version,
            server_version_num=server_version_num,
            raw_plan=plan,
        )
        comparison_policy = captured_baseline["comparison_policy"]
        assert isinstance(comparison_policy, dict)
        comparison_policy["shared_blocks_slack"] = 128
        if baseline_mode == "record":
            if server_version_num // 10_000 != 17:
                pytest.fail(
                    "PostgreSQL major changed; add a separately named baseline path "
                    "instead of overwriting the PostgreSQL 17 baseline"
                )
            write_plan_baseline(_PLAN_BASELINE_PATH, captured_baseline)
        else:
            stored_baseline = load_plan_baseline(_PLAN_BASELINE_PATH)
            regressions = compare_plan_baseline(stored_baseline, captured_baseline)
            assert regressions == (), "PostgreSQL plan regression:\n- " + "\n- ".join(regressions)
    finally:
        if task_ids:
            async with control_database.session() as session:
                async with session.begin():
                    await session.execute(
                        delete(TaskRecord).where(TaskRecord.task_id.in_(task_ids))
                    )
        await asyncio.gather(*(database.dispose() for database in databases))
