"""Opt-in PostgreSQL backend-kill, lock-timeout, and idempotency drills."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from deskpilot.application.effect_runtime_operations import (
    EffectRuntimeOperationsService,
)
from deskpilot.application.task_service import TaskService
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagNodeDefinition,
    EffectNodeStatus,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolEffectGraphRecord,
    ToolEffectNodeRecord,
)
from deskpilot.infrastructure.postgresql_claims import (
    build_postgresql_node_claim_statement,
    build_postgresql_node_lock_statement,
)
from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _node(index: int) -> EffectDagNodeDefinition:
    node_key = f"fault_root_{index}"
    return EffectDagNodeDefinition(
        node_key=node_key,
        step_id=node_key,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


async def _rollback_and_close(
    session: AsyncSession | None,
    transaction: AsyncSessionTransaction | None,
) -> None:
    if transaction is not None:
        try:
            await transaction.rollback()
        except SQLAlchemyError:
            pass
    if session is not None:
        try:
            await session.close()
        except SQLAlchemyError:
            pass


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_backend_kill_rolls_back_claim_and_lock_timeout_stays_claim_free() -> None:
    database_url = _postgresql_test_url()
    control_database = Database(database_url)
    victim_database = Database(database_url)
    admin_database = Database(database_url)
    blocker_database = Database(database_url)
    contender_database = Database(database_url)
    victim_session: AsyncSession | None = None
    victim_transaction: AsyncSessionTransaction | None = None
    blocker_session: AsyncSession | None = None
    blocker_transaction: AsyncSessionTransaction | None = None
    contender_session: AsyncSession | None = None
    contender_transaction: AsyncSessionTransaction | None = None
    task_id: str | None = None
    try:
        await control_database.migrate()
        service = TaskService(control_database, "/api/v1")
        task = await service.create_task(
            TaskCreate(goal=f"postgresql fault injection {uuid4().hex}")
        )
        task_id = task.task_id
        graph = await service.create_effect_dag(task.task_id, (_node(0), _node(1)))
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="postgresql_fault_verifier",
            ttl_seconds=3_600,
        )
        first_page = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="postgresql_fault_verifier",
            fencing_token=lease.fencing_token,
            page_size=2,
        )
        first_node_id, second_node_id = (
            proof.node_id for proof in first_page.ready_nodes
        )

        async with control_database.session() as session:
            graph_before_kill = await session.get(ToolEffectGraphRecord, graph.graph_id)
            assert graph_before_kill is not None
            graph_revision_before_kill = graph_before_kill.revision

        victim_session = victim_database.session_factory()
        victim_transaction = await victim_session.begin()
        victim_pid = int(
            (await victim_session.scalar(text("SELECT pg_backend_pid()"))) or 0
        )
        assert victim_pid > 0
        database_now = await database_utc_now(victim_session)
        graph_result = await victim_session.execute(
            update(ToolEffectGraphRecord)
            .where(
                ToolEffectGraphRecord.graph_id == graph.graph_id,
                ToolEffectGraphRecord.revision == graph_revision_before_kill,
                ToolEffectGraphRecord.lease_owner_id == "postgresql_fault_verifier",
                ToolEffectGraphRecord.fencing_token == lease.fencing_token,
                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
            )
            .values(
                revision=graph_revision_before_kill + 1,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        assert int(getattr(graph_result, "rowcount", 0)) == 1
        locked_ids = tuple(
            (
                await victim_session.scalars(
                    build_postgresql_node_lock_statement(
                        graph_id=graph.graph_id,
                        node_ids=(first_node_id,),
                        database_now=database_now,
                    )
                )
            ).all()
        )
        assert locked_ids == (first_node_id,)
        claimed_rows = tuple(
            (
                await victim_session.execute(
                    build_postgresql_node_claim_statement(
                        graph_id=graph.graph_id,
                        node_ids=(first_node_id,),
                        owner_id="postgresql_killed_worker",
                        database_now=database_now,
                        expires_at=database_now + timedelta(seconds=60),
                    )
                )
            ).mappings()
        )
        assert len(claimed_rows) == 1
        assert int(claimed_rows[0]["claim_fencing_token"]) == 1

        async with admin_database.session() as session:
            target_is_owned = await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE pid = :pid AND datname = current_database() "
                    "AND usename = current_user AND pid <> pg_backend_pid())"
                ),
                {"pid": victim_pid},
            )
            assert target_is_owned is True
            terminated = await session.scalar(
                text("SELECT pg_terminate_backend(:pid)"),
                {"pid": victim_pid},
            )
            assert terminated is True

        with pytest.raises(DBAPIError):
            await victim_session.execute(text("SELECT 1"))
        await _rollback_and_close(victim_session, victim_transaction)
        victim_session = None
        victim_transaction = None
        await victim_database.dispose()

        async with control_database.session() as session:
            graph_after_kill = await session.get(ToolEffectGraphRecord, graph.graph_id)
            node_after_kill = await session.get(ToolEffectNodeRecord, first_node_id)
            assert graph_after_kill is not None
            assert node_after_kill is not None
            assert graph_after_kill.revision == graph_revision_before_kill
            assert EffectNodeStatus(node_after_kill.status) is EffectNodeStatus.PENDING
            assert node_after_kill.revision == 1
            assert node_after_kill.claim_owner_id is None
            assert node_after_kill.claim_fencing_token == 0

        first_claim = (
            await service.claim_effect_dag_nodes(
                task.task_id,
                (first_node_id,),
                ready_proof_digest=first_page.proof_digest,
                claim_owner_id="postgresql_after_kill_worker",
                claim_ttl_seconds=60,
                lease_owner_id="postgresql_fault_verifier",
                fencing_token=lease.fencing_token,
            )
        )[0]
        assert first_claim.fencing_token == 1

        second_page = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="postgresql_fault_verifier",
            fencing_token=lease.fencing_token,
            page_size=2,
        )
        assert tuple(proof.node_id for proof in second_page.ready_nodes) == (
            second_node_id,
        )
        async with control_database.session() as session:
            graph_before_timeout = await session.get(
                ToolEffectGraphRecord,
                graph.graph_id,
            )
            assert graph_before_timeout is not None
            graph_revision_before_timeout = graph_before_timeout.revision

        blocker_session = blocker_database.session_factory()
        blocker_transaction = await blocker_session.begin()
        blocked_graph_id = await blocker_session.scalar(
            select(ToolEffectGraphRecord.graph_id)
            .where(ToolEffectGraphRecord.graph_id == graph.graph_id)
            .with_for_update()
        )
        assert blocked_graph_id == graph.graph_id

        contender_session = contender_database.session_factory()
        contender_transaction = await contender_session.begin()
        await contender_session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        contender_now = await database_utc_now(contender_session)
        with pytest.raises(DBAPIError) as timeout_error:
            await contender_session.execute(
                update(ToolEffectGraphRecord)
                .where(
                    ToolEffectGraphRecord.graph_id == graph.graph_id,
                    ToolEffectGraphRecord.revision == graph_revision_before_timeout,
                )
                .values(
                    revision=graph_revision_before_timeout + 1,
                    updated_at=contender_now,
                )
                .execution_options(synchronize_session=False)
            )
        assert getattr(timeout_error.value.orig, "sqlstate", None) == "55P03"
        await _rollback_and_close(contender_session, contender_transaction)
        contender_session = None
        contender_transaction = None
        await _rollback_and_close(blocker_session, blocker_transaction)
        blocker_session = None
        blocker_transaction = None

        async with control_database.session() as session:
            graph_after_timeout = await session.get(ToolEffectGraphRecord, graph.graph_id)
            node_after_timeout = await session.get(ToolEffectNodeRecord, second_node_id)
            assert graph_after_timeout is not None
            assert node_after_timeout is not None
            assert graph_after_timeout.revision == graph_revision_before_timeout
            assert EffectNodeStatus(node_after_timeout.status) is EffectNodeStatus.PENDING
            assert node_after_timeout.claim_owner_id is None
            assert node_after_timeout.claim_fencing_token == 0

        second_claim = (
            await service.claim_effect_dag_nodes(
                task.task_id,
                (second_node_id,),
                ready_proof_digest=second_page.proof_digest,
                claim_owner_id="postgresql_after_timeout_worker",
                claim_ttl_seconds=60,
                lease_owner_id="postgresql_fault_verifier",
                fencing_token=lease.fencing_token,
            )
        )[0]
        assert second_claim.fencing_token == 1

        operations_a = EffectRuntimeOperationsService(control_database)
        operations_b = EffectRuntimeOperationsService(admin_database)
        idempotency_key = f"postgresql-retention-{uuid4().hex}"
        retention_results = await asyncio.gather(
            operations_a.run_retention(
                actor_id="postgresql_retention_a",
                idempotency_key=idempotency_key,
                retention_days=30,
            ),
            operations_b.run_retention(
                actor_id="postgresql_retention_b",
                idempotency_key=idempotency_key,
                retention_days=30,
            ),
        )
        assert (
            retention_results[0].audit_event.event_id
            == retention_results[1].audit_event.event_id
        )
        assert (
            retention_results[0].audit_event.event_digest
            == retention_results[1].audit_event.event_digest
        )
        assert retention_results[0].manifest_digest == retention_results[1].manifest_digest
    finally:
        await _rollback_and_close(contender_session, contender_transaction)
        await _rollback_and_close(blocker_session, blocker_transaction)
        await _rollback_and_close(victim_session, victim_transaction)
        if task_id is not None:
            async with control_database.session() as session:
                async with session.begin():
                    await session.execute(
                        delete(TaskRecord).where(TaskRecord.task_id == task_id)
                    )
        await control_database.dispose()
        await victim_database.dispose()
        await admin_database.dispose()
        await blocker_database.dispose()
        await contender_database.dispose()
