"""Opt-in PostgreSQL EXPLAIN, dual-engine contention, and recovery drill."""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.dialects import postgresql

from deskpilot.application.effect_runtime_operations import (
    EffectRuntimeOperationsService,
)
from deskpilot.application.task_service import (
    EffectGraphFenceRejectedError,
    EffectNodeFenceRejectedError,
    EffectReadySetProofRejectedError,
    TaskService,
)
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagNodeDefinition,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.effect_ready_queries import (
    build_effect_ready_page_statement,
)
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolEffectDagReadyNodeRecord,
    ToolEffectDagReadyStateRecord,
    ToolEffectNodeRecord,
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
    / "ready-v6-membership-1000-nodes.postgresql-17.json"
)
_PLAN_BASELINE_MODE_ENV = "DESKPILOT_TEST_POSTGRESQL_PLAN_BASELINE_MODE"


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _node(index: int) -> EffectDagNodeDefinition:
    node_key = f"root_{index:04d}"
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


def _postgresql_sql(statement: object) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_large_ready_keyset_dual_engine_claim_and_connection_drop_recovery() -> None:
    database_url = _postgresql_test_url()
    baseline_mode = os.environ.get(_PLAN_BASELINE_MODE_ENV, "compare")
    if baseline_mode not in {"compare", "record"}:
        pytest.fail(f"{_PLAN_BASELINE_MODE_ENV} must be 'compare' or 'record'")
    control_database = Database(database_url)
    first_database = Database(database_url)
    second_database = Database(database_url)
    recovery_database = Database(database_url)
    task_id: str | None = None
    try:
        await control_database.migrate()
        async with control_database.session() as session:
            version_column_length = await session.scalar(
                text(
                    "SELECT character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'alembic_version' "
                    "AND column_name = 'version_num'"
                )
            )
        assert version_column_length == 128
        control = TaskService(control_database, "/api/v1")
        first = TaskService(first_database, "/api/v1")
        second = TaskService(second_database, "/api/v1")
        operations_a = EffectRuntimeOperationsService(first_database)
        operations_b = EffectRuntimeOperationsService(second_database)

        task = await control.create_task(
            TaskCreate(goal=f"postgresql runtime verification {uuid4().hex}")
        )
        task_id = task.task_id
        graph = await control.create_effect_dag(
            task.task_id,
            tuple(_node(index) for index in range(1_000)),
        )
        lease = await control.acquire_effect_graph_lease(
            task.task_id,
            owner_id="postgresql_verifier",
            ttl_seconds=120,
        )

        first_page = await control.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="postgresql_verifier",
            fencing_token=lease.fencing_token,
            page_size=100,
        )
        second_page = await control.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="postgresql_verifier",
            fencing_token=lease.fencing_token,
            page_size=100,
            cursor=first_page.next_cursor,
        )
        assert first_page.next_cursor == first_page.checkpoint_id
        assert first_page.last_ordinal == 99
        assert second_page.after_ordinal == 99
        assert second_page.last_ordinal == 199

        explain_statement = build_effect_ready_page_statement(
            graph_id=graph.graph_id,
            page_size=100,
            after_ordinal=898,
        )
        explain_sql = _postgresql_sql(explain_statement)
        async with control_database.session() as session:
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
        assert "Execution Time" in plan[0]
        assert int(plan[0]["Plan"]["Actual Rows"]) == 101
        assert isinstance(postgresql_version, str)
        assert isinstance(server_version, str)
        assert isinstance(raw_server_version_num, str)
        parameterized_sql = str(explain_statement.compile(dialect=postgresql.dialect()))
        server_version_num = int(raw_server_version_num)
        captured_baseline = build_plan_baseline(
            baseline_id="ready-v6-membership-1000-nodes-pg17",
            workload={
                "graph_node_count": 1_000,
                "page_size": 100,
                "sentinel_rows": 1,
                "after_ordinal": 898,
                "expected_rows": 101,
            },
            query_shape_digest=query_shape_sha256(parameterized_sql),
            postgresql_version=postgresql_version,
            server_version=server_version,
            server_version_num=server_version_num,
            raw_plan=plan,
        )
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

        node_id = first_page.ready_nodes[0].node_id

        async def claim(service: TaskService, owner_id: str) -> object:
            return await service.claim_effect_dag_nodes(
                task.task_id,
                (node_id,),
                ready_proof_digest=first_page.proof_digest,
                claim_owner_id=owner_id,
                claim_ttl_seconds=60,
                lease_owner_id="postgresql_verifier",
                fencing_token=lease.fencing_token,
            )

        results = await asyncio.gather(
            claim(first, "postgresql_worker_a"),
            claim(second, "postgresql_worker_b"),
            return_exceptions=True,
        )
        successes = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(
            failures[0],
            (
                EffectGraphFenceRejectedError,
                EffectNodeFenceRejectedError,
                EffectReadySetProofRejectedError,
            ),
        )
        async with control_database.session() as session:
            state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
            membership = await session.get(ToolEffectDagReadyNodeRecord, node_id)
        assert state is not None
        assert state.projected_node_count == 1_000
        assert state.ready_node_count == 999
        assert membership is not None
        assert not membership.membership_ready
        winning_claim = successes[0][0]
        winning_database = first_database if results[0] is successes[0] else second_database
        await winning_database.dispose()

        async with control_database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectNodeRecord)
                    .where(ToolEffectNodeRecord.node_id == node_id)
                    .values(claim_expires_at=utc_now() - timedelta(seconds=1))
                )
        recovered_page = await control.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="postgresql_verifier",
            fencing_token=lease.fencing_token,
            page_size=100,
        )
        assert recovered_page.ready_nodes[0].status is EffectNodeStatus.ACTIVE
        recovered_claim = (
            await control.claim_effect_dag_nodes(
                task.task_id,
                (node_id,),
                ready_proof_digest=recovered_page.proof_digest,
                claim_owner_id="postgresql_recovery_worker",
                claim_ttl_seconds=60,
                lease_owner_id="postgresql_verifier",
                fencing_token=lease.fencing_token,
            )
        )[0]
        assert recovered_claim.fencing_token == winning_claim.fencing_token + 1

        recovery = TaskService(recovery_database, "/api/v1")
        with pytest.raises(EffectNodeFenceRejectedError):
            await recovery.transition_claimed_effect_node(
                task.task_id,
                node_id,
                expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="stale_worker_after_connection_drop",
                event_type="effect.node.succeeded",
                claim_owner_id=winning_claim.owner_id,
                node_fencing_token=winning_claim.fencing_token,
                lease_owner_id="postgresql_verifier",
                fencing_token=lease.fencing_token,
            )

        audit_results = await asyncio.gather(
            operations_a.sample_metrics(actor_id="postgresql_verifier_a", sample_limit=5),
            operations_b.sample_metrics(actor_id="postgresql_verifier_b", sample_limit=5),
        )
        ordered_audits = sorted(
            (result.audit_event for result in audit_results),
            key=lambda event: event.sequence,
        )
        assert ordered_audits[1].sequence == ordered_audits[0].sequence + 1
        assert ordered_audits[1].previous_event_digest == ordered_audits[0].event_digest
    finally:
        if task_id is not None:
            async with control_database.session() as session:
                async with session.begin():
                    await session.execute(delete(TaskRecord).where(TaskRecord.task_id == task_id))
        await control_database.dispose()
        await first_database.dispose()
        await second_database.dispose()
        await recovery_database.dispose()
