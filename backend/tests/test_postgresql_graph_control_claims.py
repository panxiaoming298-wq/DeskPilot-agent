"""Opt-in PostgreSQL graph-control batch claim, takeover, and plan gates."""

import asyncio
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

from deskpilot.application.effect_graph_control_router import (
    EffectGraphControlFenceRejectedError,
    EffectGraphControlStore,
)
from deskpilot.application.task_service import TaskService
from deskpilot.domain.effect_graph import CompensationStrategy, EffectDagNodeDefinition
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolEffectGraphControlRecord,
    ToolEffectGraphRecord,
)
from deskpilot.infrastructure.postgresql_claims import (
    build_postgresql_graph_control_claim_statement,
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
    / "graph-control-claim-v1-16000-controls.postgresql-17.json"
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


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_postgresql_graph_control_batch_claim_and_ttl_takeover() -> None:
    database_url = _postgresql_test_url()
    databases = tuple(Database(database_url) for _ in range(4))
    control_database = databases[0]
    owner_id = "postgresql_graph_control_owner_a"
    task_ids: list[str] = []
    try:
        await control_database.migrate()
        service = TaskService(control_database, "/api/v1")
        stores = tuple(EffectGraphControlStore(database) for database in databases)
        for index in range(12):
            task = await service.create_task(
                TaskCreate(goal=f"postgresql graph control claim {index}")
            )
            task_ids.append(task.task_id)
            await service.create_effect_dag(task.task_id, (_node(index),))
            await service.acquire_effect_graph_lease(
                task.task_id,
                owner_id=owner_id,
                ttl_seconds=120,
            )
            await stores[0].request_cancel(
                task.task_id,
                reason="postgresql batch claim",
                requested_by="postgresql_requester",
            )

        claimed_batches = await asyncio.gather(
            stores[0].claim_for_owner(owner_id, ttl_seconds=30, limit=8),
            stores[1].claim_for_owner(owner_id, ttl_seconds=30, limit=8),
        )
        claims = tuple(claim for batch in claimed_batches for claim in batch)
        assert len(claims) == 12
        assert len({claim.control_id for claim in claims}) == 12
        assert {claim.claim_fencing_token for claim in claims} == {1}
        assert all(claim.claim_owner_id == owner_id for claim in claims)

        stale = claims[0]
        async with control_database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectGraphControlRecord)
                    .where(ToolEffectGraphControlRecord.control_id == stale.control_id)
                    .values(claim_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"))
                )
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await stores[2].mark_applied(stale)
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await stores[2].renew_claim(stale, ttl_seconds=30)
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await stores[2].retry(
                stale,
                error_code="GRAPH_CONTROL_HANDLER_RETRY",
                superseded=False,
            )

        await stores[2].route_pending()
        current = (await stores[3].claim_for_owner(owner_id, ttl_seconds=30, limit=8))[0]
        assert current.control_id == stale.control_id
        assert current.claim_fencing_token == stale.claim_fencing_token + 1

        async with control_database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectGraphControlRecord)
                    .where(ToolEffectGraphControlRecord.control_id == current.control_id)
                    .values(claim_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"))
                )
        assert current.target_fencing_token is not None
        await service.release_effect_graph_lease(
            current.task_id,
            owner_id=owner_id,
            fencing_token=current.target_fencing_token,
        )
        new_owner_id = "postgresql_graph_control_owner_b"
        new_lease = await service.acquire_effect_graph_lease(
            current.task_id,
            owner_id=new_owner_id,
            ttl_seconds=120,
        )
        await stores[0].route_pending()
        assert await stores[1].claim_for_owner(owner_id, ttl_seconds=30, limit=8) == ()
        replacement = (await stores[1].claim_for_owner(new_owner_id, ttl_seconds=30, limit=8))[0]
        assert replacement.control_id == current.control_id
        assert replacement.target_owner_id == new_owner_id
        assert replacement.target_fencing_token == new_lease.fencing_token
        assert replacement.claim_fencing_token == current.claim_fencing_token + 1
        with pytest.raises(EffectGraphControlFenceRejectedError):
            await stores[0].mark_applied(current)
        applied = await stores[0].mark_applied(replacement)
        assert applied.applied_graph_fencing_token == new_lease.fencing_token
    finally:
        if task_ids:
            async with control_database.session() as session:
                async with session.begin():
                    await session.execute(
                        delete(TaskRecord).where(TaskRecord.task_id.in_(task_ids))
                    )
        await asyncio.gather(*(database.dispose() for database in databases))


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_postgresql_graph_control_claim_plan_baseline() -> None:
    database_url = _postgresql_test_url()
    baseline_mode = os.environ.get(_PLAN_BASELINE_MODE_ENV, "compare")
    if baseline_mode not in {"compare", "record"}:
        pytest.fail(f"{_PLAN_BASELINE_MODE_ENV} must be 'compare' or 'record'")
    database = Database(database_url)
    prefix = uuid4().hex[:8]
    task_prefix = f"tsk_gcplan_{prefix}_"
    try:
        await database.migrate()
        # Plan buffers are compared against an immutable fresh-database
        # baseline.  DELETE leaves random-key B-tree pages behind across local
        # reruns, so reset this guarded disposable database's task projection.
        async with database.session() as session:
            async with session.begin():
                await session.execute(text("TRUNCATE TABLE tasks CASCADE"))
        async with database.session() as session:
            async with session.begin():
                database_time = await session.scalar(text("SELECT CURRENT_TIMESTAMP"))
                assert database_time is not None
                owners = tuple(f"postgresql_plan_owner_{index:02d}" for index in range(16))
                task_rows: list[dict[str, object]] = []
                graph_rows: list[dict[str, object]] = []
                control_rows: list[dict[str, object]] = []
                for index in range(16_000):
                    owner_id = owners[index // 1_000]
                    task_id = f"{task_prefix}{index:05d}"
                    graph_id = f"teg_gcplan_{prefix}_{index:05d}"
                    control_id = "egc_" + hashlib.sha256(f"{prefix}:{index}".encode()).hexdigest()
                    task_rows.append(
                        {
                            "task_id": task_id,
                            "conversation_id": None,
                            "goal": "postgresql graph control plan",
                            "status": "created",
                            "mode": "fake_model",
                            "privacy_mode": "local_preferred",
                            "constraints": [],
                            "last_event_seq": 0,
                            "created_at": database_time,
                            "updated_at": database_time,
                        }
                    )
                    graph_rows.append(
                        {
                            "graph_id": graph_id,
                            "task_id": task_id,
                            "schema_version": "plan.v1",
                            "status": "active",
                            "execution_mode": "forward",
                            "current_node_id": None,
                            "failure_node_id": None,
                            "lease_owner_id": owner_id,
                            "lease_acquired_at": database_time,
                            "lease_heartbeat_at": database_time,
                            "lease_expires_at": database_time + timedelta(minutes=5),
                            "cancel_requested_at": None,
                            "fencing_token": 1,
                            "revision": 1,
                            "last_event_seq": 1,
                            "created_at": database_time,
                            "updated_at": database_time,
                        }
                    )
                    control_rows.append(
                        {
                            "control_id": control_id,
                            "task_id": task_id,
                            "graph_id": graph_id,
                            "command": "cancel",
                            "reason": None,
                            "request_digest": hashlib.sha256(control_id.encode()).hexdigest(),
                            "requested_by": "postgresql_plan_requester",
                            "target_owner_id": owner_id,
                            "target_fencing_token": 1,
                            "status": "pending",
                            "revision": 1,
                            "attempt_count": 0,
                            "last_error_code": None,
                            "available_at": database_time - timedelta(seconds=1),
                            "claim_owner_id": None,
                            "claim_acquired_at": None,
                            "claim_expires_at": None,
                            "claim_fencing_token": 0,
                            "applied_graph_fencing_token": None,
                            "created_at": database_time,
                            "updated_at": database_time,
                            "applied_at": None,
                        }
                    )
                await session.execute(insert(TaskRecord), task_rows)
                await session.execute(insert(ToolEffectGraphRecord), graph_rows)
                await session.execute(insert(ToolEffectGraphControlRecord), control_rows)

        async with database.session() as session:
            await session.execute(text("ANALYZE tool_effect_graphs"))
            await session.execute(text("ANALYZE tool_effect_graph_controls"))
            database_time = await session.scalar(text("SELECT CURRENT_TIMESTAMP"))
            assert database_time is not None
            statement = build_postgresql_graph_control_claim_statement(
                owner_id="postgresql_plan_owner_00",
                database_now=database_time,
                expires_at=database_time + timedelta(seconds=30),
                batch_size=101,
            )
            raw_plan = await session.scalar(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {_postgresql_sql(statement)}")
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
        parameterized_sql = str(statement.compile(dialect=_POSTGRESQL_DIALECT))
        server_version_num = int(raw_server_version_num)
        captured_baseline = build_plan_baseline(
            baseline_id="graph-control-claim-v1-16000-controls-pg17",
            workload={
                "control_count": 16_000,
                "owner_count": 16,
                "controls_per_owner": 1_000,
                "batch_size": 101,
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
        summary = captured_baseline["summary"]
        assert isinstance(summary, dict)
        assert summary["scan_actual_rows"] == 303
        assert summary["rows_removed"] == 0
        assert summary["index_names"] == [
            "ix_tool_effect_graph_controls_route",
            "tool_effect_graph_controls_pkey",
            "tool_effect_graphs_pkey",
        ]
        node_type_counts = summary["node_type_counts"]
        assert isinstance(node_type_counts, dict)
        assert "Sort" not in node_type_counts
        assert "Bitmap Heap Scan" not in node_type_counts
        assert "Seq Scan" not in node_type_counts
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
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    delete(TaskRecord).where(TaskRecord.task_id.like(f"{task_prefix}%"))
                )
        await database.dispose()
