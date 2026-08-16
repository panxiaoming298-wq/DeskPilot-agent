"""Opt-in PostgreSQL timeout, deadlock, and uncertain-commit drills."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.task_service import (
    TaskService,
    ToolCallStatus,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import DataEgress
from deskpilot.domain.effect_graph import (
    CompensationStrategy,
    EffectDagNodeDefinition,
    EffectNodeStatus,
)
from deskpilot.domain.policy import (
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.schemas import TaskCreate, TaskStatus
from deskpilot.domain.tool_contracts import ToolIdempotency, ToolRiskLevel
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import (
    TaskEventRecord,
    TaskRecord,
    ToolCallRecord,
    ToolEffectGraphRecord,
    ToolEffectNodeRecord,
    ToolReconciliationRecord,
)
from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


@dataclass(frozen=True, slots=True)
class _DeadlockResult:
    owner_id: str
    outcome: str
    sqlstate: str | None = None


class _CommitInterruptingTransaction:
    def __init__(
        self,
        transaction: AsyncSessionTransaction,
        session: AsyncSession,
        database: _CommitInterruptingDatabase,
    ) -> None:
        self._transaction = transaction
        self._session = session
        self._database = database

    async def __aenter__(self) -> AsyncSessionTransaction:
        return await self._transaction.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if exc_type is None:
            await self._database.terminate_transaction_backend(self._session)
        await self._transaction.__aexit__(exc_type, exc_value, traceback)
        return None


class _CommitInterruptingSession:
    def __init__(
        self,
        session: AsyncSession,
        database: _CommitInterruptingDatabase,
    ) -> None:
        self._session = session
        self._database = database

    def begin(self) -> _CommitInterruptingTransaction:
        return _CommitInterruptingTransaction(
            self._session.begin(),
            self._session,
            self._database,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class _CommitInterruptingDatabase(Database):
    def __init__(self, url: str, *, admin_database: Database) -> None:
        super().__init__(url)
        self._admin_database = admin_database
        self.backend_pid: int | None = None
        self.backend_terminated = False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as raw_session:
            proxy = _CommitInterruptingSession(raw_session, self)
            yield cast(AsyncSession, proxy)

    async def terminate_transaction_backend(self, session: AsyncSession) -> None:
        if self.backend_terminated:
            raise RuntimeError("Terminal commit backend was already interrupted")
        backend_pid = int((await session.scalar(text("SELECT pg_backend_pid()"))) or 0)
        assert backend_pid > 0
        async with self._admin_database.session() as admin_session:
            target_is_owned = await admin_session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE pid = :pid AND datname = current_database() "
                    "AND usename = current_user AND pid <> pg_backend_pid())"
                ),
                {"pid": backend_pid},
            )
            assert target_is_owned is True
            terminated = await admin_session.scalar(
                text("SELECT pg_terminate_backend(:pid)"),
                {"pid": backend_pid},
            )
            assert terminated is True
        self.backend_pid = backend_pid
        self.backend_terminated = True


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _node(index: int) -> EffectDagNodeDefinition:
    node_key = f"transaction_fault_root_{index}"
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


async def _delete_task(database: Database, task_id: str | None) -> None:
    if task_id is None:
        return
    async with database.session() as session:
        async with session.begin():
            await session.execute(delete(TaskRecord).where(TaskRecord.task_id == task_id))


async def _running_task(service: TaskService, *, goal: str) -> str:
    task = await service.create_task(TaskCreate(goal=goal))
    await service.transition_task(
        task.task_id,
        TaskStatus.CLASSIFYING,
        command="fault_injection_setup",
        requested_by="system",
    )
    await service.transition_task(
        task.task_id,
        TaskStatus.RUNNING,
        command="fault_injection_setup",
        requested_by="system",
    )
    return task.task_id


async def _start_running_tool_call(
    service: TaskService,
    *,
    task_id: str,
    call_id: str,
) -> None:
    step_id = "connection_interruption_step"
    arguments = {"path": "."}
    expected_resource_versions: dict[str, str] = {}
    await service.record_tool_requested(
        task_id,
        call_id=call_id,
        step_id=step_id,
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        arguments=arguments,
        idempotency=ToolIdempotency.IDEMPOTENT,
        risk=ToolRiskLevel.R0.value,
    )
    resource = PolicyResource(
        kind="filesystem_path",
        identifier=".",
        operations=("filesystem.metadata.read",),
        display_name="Current directory",
    )
    request = ToolAuthorizationRequest(
        task_id=task_id,
        step_id=step_id,
        call_id=call_id,
        actor="postgresql_fault_test",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        arguments_digest=sha256_digest(arguments),
        risk_level=ToolRiskLevel.R0,
        capabilities=("filesystem.metadata.read",),
        resources=(resource,),
        expected_resource_versions_digest=sha256_digest(expected_resource_versions),
    )
    decision = BuiltinPolicyEngine(
        allowed_resource_scopes=(resource.scope_key,),
    ).evaluate(request)
    assert decision.effect is PolicyEffect.ALLOW
    approval = await service.apply_policy_decision(
        task_id,
        call_id,
        request=request,
        decision=decision,
        title="Inspect disk capacity",
        purpose="Exercise PostgreSQL uncertain terminal persistence",
        consequences=(),
        data_egress=DataEgress(enabled=False),
        expected_resource_versions=expected_resource_versions,
    )
    assert approval is None
    authorization = ToolAuthorizationGrant.issue(
        decision_id=decision.decision_id,
        request_digest=decision.request_digest,
        task_id=task_id,
        step_id=step_id,
        call_id=call_id,
        actor_id=request.actor,
        origin=request.origin,
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        contract_digest=request.contract_digest,
        policy_revision=decision.policy_revision,
        rule_id=decision.rule_id,
        reason_code=decision.reason_code,
        effective_risk=decision.effective_risk,
        arguments_digest=request.arguments_digest,
        resource_scope_digest=request.resource_scope_digest,
        expected_resource_versions_digest=request.expected_resource_versions_digest,
        capabilities=request.capabilities,
        network_access=request.network_access,
        data_egress=request.data_egress,
        side_effects=request.side_effects,
        reversible=request.reversible,
        resources=request.resources,
        interactive=request.interactive,
        batch_count=request.batch_count,
    )
    await service.start_tool_call(
        task_id,
        call_id,
        runner_id="runner_before_connection_loss",
        authorization=authorization,
        arguments=arguments,
        expected_resource_versions=expected_resource_versions,
    )


async def _run_deadlock_worker(
    database: Database,
    *,
    witness_task_id: str,
    first_node_id: str,
    second_node_id: str,
    owner_id: str,
    first_locked: asyncio.Event,
    peer_locked: asyncio.Event,
) -> _DeadlockResult:
    session = database.session_factory()
    transaction = await session.begin()
    try:
        await session.execute(text("SET LOCAL statement_timeout = '5s'"))
        witness = await session.execute(
            update(TaskRecord)
            .where(TaskRecord.task_id == witness_task_id)
            .values(goal=f"deadlock committed by {owner_id}")
            .execution_options(synchronize_session=False)
        )
        assert int(getattr(witness, "rowcount", 0)) == 1
        database_now = await database_utc_now(session)
        first = await session.execute(
            update(ToolEffectNodeRecord)
            .where(ToolEffectNodeRecord.node_id == first_node_id)
            .values(
                status=EffectNodeStatus.ACTIVE.value,
                revision=2,
                claim_owner_id=owner_id,
                claim_acquired_at=database_now,
                claim_expires_at=database_now + timedelta(seconds=60),
                claim_fencing_token=1,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        assert int(getattr(first, "rowcount", 0)) == 1
        first_locked.set()
        await asyncio.wait_for(peer_locked.wait(), timeout=5)
        second = await session.execute(
            update(ToolEffectNodeRecord)
            .where(ToolEffectNodeRecord.node_id == second_node_id)
            .values(
                status=EffectNodeStatus.ACTIVE.value,
                revision=2,
                claim_owner_id=owner_id,
                claim_acquired_at=database_now,
                claim_expires_at=database_now + timedelta(seconds=60),
                claim_fencing_token=1,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        assert int(getattr(second, "rowcount", 0)) == 1
        await transaction.commit()
        return _DeadlockResult(owner_id=owner_id, outcome="committed")
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        await _rollback_and_close(session, transaction)
        return _DeadlockResult(
            owner_id=owner_id,
            outcome="deadlocked" if sqlstate == "40P01" else "database_error",
            sqlstate=sqlstate,
        )
    finally:
        await _rollback_and_close(session, transaction)


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_statement_timeout_rolls_back_graph_and_node_claim_transaction() -> None:
    database_url = _postgresql_test_url()
    control_database = Database(database_url)
    timeout_database = Database(database_url)
    timeout_session: AsyncSession | None = None
    timeout_transaction: AsyncSessionTransaction | None = None
    task_id: str | None = None
    try:
        await control_database.migrate()
        service = TaskService(control_database, "/api/v1")
        task = await service.create_task(
            TaskCreate(goal=f"postgresql statement timeout {uuid4().hex}")
        )
        task_id = task.task_id
        graph = await service.create_effect_dag(task.task_id, (_node(0),))
        lease_owner = f"statement_timeout_owner_{uuid4().hex}"
        lease = await service.acquire_effect_graph_lease(
            task.task_id,
            owner_id=lease_owner,
            ttl_seconds=60,
        )
        ready = await service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id=lease_owner,
            fencing_token=lease.fencing_token,
            page_size=1,
        )
        node_id = ready.ready_nodes[0].node_id
        async with control_database.session() as session:
            graph_before = await session.get(ToolEffectGraphRecord, graph.graph_id)
            node_before = await session.get(ToolEffectNodeRecord, node_id)
        assert graph_before is not None
        assert node_before is not None
        initial_revisions = (graph_before.revision, node_before.revision)

        timeout_session = timeout_database.session_factory()
        timeout_transaction = await timeout_session.begin()
        await timeout_session.execute(text("SET LOCAL statement_timeout = '250ms'"))
        database_now = await database_utc_now(timeout_session)
        graph_write = await timeout_session.execute(
            update(ToolEffectGraphRecord)
            .where(
                ToolEffectGraphRecord.graph_id == graph.graph_id,
                ToolEffectGraphRecord.revision == initial_revisions[0],
            )
            .values(
                revision=initial_revisions[0] + 1,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        node_write = await timeout_session.execute(
            update(ToolEffectNodeRecord)
            .where(
                ToolEffectNodeRecord.node_id == node_id,
                ToolEffectNodeRecord.revision == initial_revisions[1],
            )
            .values(
                status=EffectNodeStatus.ACTIVE.value,
                revision=initial_revisions[1] + 1,
                claim_owner_id="statement_timeout_staged_owner",
                claim_acquired_at=database_now,
                claim_expires_at=database_now + timedelta(seconds=60),
                claim_fencing_token=1,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        assert int(getattr(graph_write, "rowcount", 0)) == 1
        assert int(getattr(node_write, "rowcount", 0)) == 1
        with pytest.raises(DBAPIError) as timeout_error:
            await timeout_session.execute(text("SELECT pg_sleep(2)"))
        assert getattr(timeout_error.value.orig, "sqlstate", None) == "57014"
        await _rollback_and_close(timeout_session, timeout_transaction)
        timeout_session = None
        timeout_transaction = None

        async with control_database.session() as session:
            graph_after = await session.get(ToolEffectGraphRecord, graph.graph_id)
            node_after = await session.get(ToolEffectNodeRecord, node_id)
            tool_call_count = await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(ToolCallRecord.task_id == task.task_id)
            )
        assert graph_after is not None
        assert node_after is not None
        assert (graph_after.revision, node_after.revision) == initial_revisions
        assert EffectNodeStatus(node_after.status) is EffectNodeStatus.PENDING
        assert node_after.claim_owner_id is None
        assert node_after.claim_fencing_token == 0
        assert tool_call_count == 0
        assert not any(
            event.type == "effect.node.claimed"
            for event in await service.list_events(task.task_id)
        )

        claim = (
            await service.claim_effect_dag_nodes(
                task.task_id,
                (node_id,),
                ready_proof_digest=ready.proof_digest,
                claim_owner_id="statement_timeout_recovery_owner",
                claim_ttl_seconds=60,
                lease_owner_id=lease_owner,
                fencing_token=lease.fencing_token,
            )
        )[0]
        assert claim.fencing_token == 1
    finally:
        await _rollback_and_close(timeout_session, timeout_transaction)
        await _delete_task(control_database, task_id)
        await control_database.dispose()
        await timeout_database.dispose()


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_multi_row_deadlock_aborts_one_whole_transaction_without_ghost_owner() -> None:
    database_url = _postgresql_test_url()
    control_database = Database(database_url)
    first_database = Database(database_url)
    second_database = Database(database_url)
    task_id: str | None = None
    first_witness_task_id: str | None = None
    second_witness_task_id: str | None = None
    try:
        await control_database.migrate()
        service = TaskService(control_database, "/api/v1")
        task = await service.create_task(
            TaskCreate(goal=f"postgresql multi row deadlock {uuid4().hex}")
        )
        task_id = task.task_id
        graph = await service.create_effect_dag(task.task_id, (_node(0), _node(1)))
        first_witness = await service.create_task(
            TaskCreate(goal=f"deadlock witness a {uuid4().hex}")
        )
        second_witness = await service.create_task(
            TaskCreate(goal=f"deadlock witness b {uuid4().hex}")
        )
        first_witness_task_id = first_witness.task_id
        second_witness_task_id = second_witness.task_id
        graph_before = await service.get_effect_graph(task.task_id)
        node_ids = tuple(node.node_id for node in graph_before.nodes)
        assert len(node_ids) == 2
        first_locked = asyncio.Event()
        second_locked = asyncio.Event()
        first_owner = f"deadlock_owner_a_{uuid4().hex}"
        second_owner = f"deadlock_owner_b_{uuid4().hex}"

        results = await asyncio.gather(
            _run_deadlock_worker(
                first_database,
                witness_task_id=first_witness.task_id,
                first_node_id=node_ids[0],
                second_node_id=node_ids[1],
                owner_id=first_owner,
                first_locked=first_locked,
                peer_locked=second_locked,
            ),
            _run_deadlock_worker(
                second_database,
                witness_task_id=second_witness.task_id,
                first_node_id=node_ids[1],
                second_node_id=node_ids[0],
                owner_id=second_owner,
                first_locked=second_locked,
                peer_locked=first_locked,
            ),
        )
        committed = [result for result in results if result.outcome == "committed"]
        deadlocked = [result for result in results if result.outcome == "deadlocked"]
        assert len(committed) == 1
        assert len(deadlocked) == 1
        assert deadlocked[0].sqlstate == "40P01"
        winner = committed[0].owner_id
        loser = deadlocked[0].owner_id

        async with control_database.session() as session:
            graph_after = await session.get(ToolEffectGraphRecord, graph.graph_id)
            nodes_after = tuple(
                (
                    await session.scalars(
                        select(ToolEffectNodeRecord)
                        .where(ToolEffectNodeRecord.graph_id == graph.graph_id)
                        .order_by(ToolEffectNodeRecord.ordinal)
                    )
                ).all()
            )
            tool_call_count = await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(ToolCallRecord.task_id == task.task_id)
            )
            witness_rows = tuple(
                (
                    await session.scalars(
                        select(TaskRecord)
                        .where(
                            TaskRecord.task_id.in_(
                                (first_witness.task_id, second_witness.task_id)
                            )
                        )
                        .order_by(TaskRecord.task_id)
                    )
                ).all()
            )
        assert graph_after is not None
        assert graph_after.revision == graph_before.revision
        assert len(nodes_after) == 2
        assert all(node.status == EffectNodeStatus.ACTIVE.value for node in nodes_after)
        assert all(node.revision == 2 for node in nodes_after)
        assert all(node.claim_fencing_token == 1 for node in nodes_after)
        assert {node.claim_owner_id for node in nodes_after} == {winner}
        assert loser not in {node.claim_owner_id for node in nodes_after}
        assert tool_call_count == 0
        witness_by_id = {row.task_id: row for row in witness_rows}
        witness_id_by_owner = {
            first_owner: first_witness.task_id,
            second_owner: second_witness.task_id,
        }
        assert witness_by_id[witness_id_by_owner[winner]].goal == (
            f"deadlock committed by {winner}"
        )
        assert witness_by_id[witness_id_by_owner[loser]].goal == (
            first_witness.goal if loser == first_owner else second_witness.goal
        )
        events = await service.list_events(task.task_id)
        assert not any(event.type.startswith("effect.node.") for event in events)
    finally:
        await _delete_task(control_database, task_id)
        await _delete_task(control_database, first_witness_task_id)
        await _delete_task(control_database, second_witness_task_id)
        await control_database.dispose()
        await first_database.dispose()
        await second_database.dispose()


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_connection_loss_during_terminal_commit_recovers_unknown_without_replay() -> None:
    database_url = _postgresql_test_url()
    control_database = Database(database_url)
    admin_database = Database(database_url)
    victim_database = _CommitInterruptingDatabase(
        database_url,
        admin_database=admin_database,
    )
    recovery_database = Database(database_url)
    task_id: str | None = None
    call_id = f"call_connection_loss_{uuid4().hex}"
    try:
        await control_database.migrate()
        service = TaskService(control_database, "/api/v1")
        task_id = await _running_task(
            service,
            goal=f"postgresql uncertain terminal commit {uuid4().hex}",
        )
        await _start_running_tool_call(service, task_id=task_id, call_id=call_id)
        task_before = await service.get_task(task_id)
        victim_service = TaskService(victim_database, "/api/v1")
        with pytest.raises(DBAPIError) as interrupted_commit:
            await victim_service.finish_tool_call(
                task_id,
                call_id,
                status=ToolCallStatus.SUCCEEDED,
                result={"staged_before_connection_loss": True},
                fail_task=False,
            )
        assert interrupted_commit.value.connection_invalidated
        assert victim_database.backend_pid is not None
        assert victim_database.backend_terminated

        async with control_database.session() as session:
            call_after_loss = await session.get(ToolCallRecord, call_id)
            task_after_loss = await session.get(TaskRecord, task_id)
            completed_count = await session.scalar(
                select(func.count())
                .select_from(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == task_id,
                    TaskEventRecord.type == "tool.completed",
                )
            )
        assert call_after_loss is not None
        assert task_after_loss is not None
        assert call_after_loss.status == ToolCallStatus.RUNNING.value
        assert call_after_loss.finished_at is None
        assert call_after_loss.terminal_event_id is None
        assert task_after_loss.status == TaskStatus.RUNNING.value
        assert task_after_loss.last_event_seq == task_before.last_event_seq
        assert completed_count == 0

        recovery = TaskService(recovery_database, "/api/v1")
        recovered = await recovery.recover_incomplete_tool_calls()
        events_after_recovery = await recovery.list_events(task_id)
        event_count = len(events_after_recovery)
        repeated = await recovery.recover_incomplete_tool_calls()
        assert recovered.requested_failed == 0
        assert recovered.running_unknown == 1
        assert recovered.events_created == 2
        assert repeated.requested_failed == 0
        assert repeated.running_unknown == 0
        assert repeated.events_created == 0
        assert len(await recovery.list_events(task_id)) == event_count

        async with recovery_database.session() as session:
            final_call = await session.get(ToolCallRecord, call_id)
            reconciliation = await session.scalar(
                select(ToolReconciliationRecord).where(
                    ToolReconciliationRecord.call_id == call_id
                )
            )
            task_call_count = await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(ToolCallRecord.task_id == task_id)
            )
        assert final_call is not None
        assert reconciliation is not None
        assert final_call.status == ToolCallStatus.UNKNOWN.value
        assert final_call.error_code == "TOOL_RESULT_UNCERTAIN_AFTER_RESTART"
        assert final_call.resolution_source == "startup_recovery"
        assert reconciliation.status == "pending"
        assert task_call_count == 1
        event_types = [event.type for event in events_after_recovery]
        assert event_types.count("tool.requested") == 1
        assert event_types.count("tool.started") == 1
        assert event_types.count("tool.completed") == 0
        assert event_types.count("tool.unknown") == 1
        assert event_types.count("task.waiting_reconciliation") == 1
        assert (await recovery.get_task(task_id)).status is (
            TaskStatus.WAITING_RECONCILIATION
        )
    finally:
        await _delete_task(control_database, task_id)
        await control_database.dispose()
        await victim_database.dispose()
        await admin_database.dispose()
        await recovery_database.dispose()
