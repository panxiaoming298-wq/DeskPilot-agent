from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.builtin_capability_executors import (
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_execution_engine import (
    CapabilityExecutionCandidate,
    CapabilityExecutionEngine,
    VerifiedCapabilityOutput,
)
from deskpilot.application.capability_execution_runtime import (
    CapabilityExecutionEnginePort,
    CapabilityExecutionRuntime,
    CapabilityExecutionRuntimeProofRejectedError,
    CapabilityExecutionRuntimeStaleFenceError,
    CapabilityVerificationDeferredError,
)
from deskpilot.application.capability_input_binding_catalog import (
    CapabilityInputBindingCatalog,
)
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import (
    CapabilityExecutionContext,
    VerifiedCapabilityResultRef,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskExecutionNodeRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_builtin_capability_executors import (  # noqa: E402
    FakeKnowledge,
    FakeMcp,
)
from test_multi_step_plan_runtime import (  # noqa: E402
    NOW,
    ScriptedTurnPlannerProvider,
    _defer_two_offers,
    _runtimes,
    _select_two_offers,
)


class _FlakyVerificationEngine:
    def __init__(self, delegate: CapabilityExecutionEngine) -> None:
        self.delegate = delegate
        self.execute_calls = 0
        self.verify_calls = 0
        self.failures_remaining = 1

    async def execute_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
    ) -> CapabilityExecutionCandidate:
        self.execute_calls += 1
        return await self.delegate.execute_candidate(context, bound_input)

    async def verify_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
        candidate: CapabilityExecutionCandidate,
    ) -> VerifiedCapabilityOutput:
        self.verify_calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("scripted verifier interruption")
        return await self.delegate.verify_candidate(context, bound_input, candidate)


class _BlockingMcpEngine:
    def __init__(self, delegate: CapabilityExecutionEngine) -> None:
        self.delegate = delegate
        self.mcp_candidate_completed = asyncio.Event()
        self.release_mcp_candidate = asyncio.Event()

    async def execute_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
    ) -> CapabilityExecutionCandidate:
        candidate = await self.delegate.execute_candidate(context, bound_input)
        if context.capability.capability_id == "mcp.text.metrics.v1":
            self.mcp_candidate_completed.set()
            await self.release_mcp_candidate.wait()
        return candidate

    async def verify_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
        candidate: CapabilityExecutionCandidate,
    ) -> VerifiedCapabilityOutput:
        return await self.delegate.verify_candidate(context, bound_input, candidate)


class _FailingMcp(FakeMcp):
    async def invoke(self, tool_name: str, arguments: dict[str, object]) -> Any:
        self.calls.append((tool_name, arguments))
        raise RuntimeError("scripted ambiguous broker boundary")


@dataclass(slots=True)
class _RuntimeFixture:
    database: Database
    activation: TaskLoopActivationRuntime
    runtime: CapabilityExecutionRuntime
    task_id: str
    knowledge: FakeKnowledge
    mcp: FakeMcp
    engine: CapabilityExecutionEnginePort


async def _runtime_fixture(
    tmp_path: Path,
    *,
    suffix: str,
    mcp: FakeMcp | None = None,
    wrap_engine: Callable[[CapabilityExecutionEngine], CapabilityExecutionEnginePort] | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
) -> _RuntimeFixture:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / f'capability-{suffix}.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    knowledge = FakeKnowledge()
    actual_mcp = mcp or FakeMcp()
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001 - exact shared fixture
        knowledge=knowledge,
        mcp=actual_mcp,
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001 - exact shared fixture
        planner._agents,  # noqa: SLF001 - exact shared fixture
    )
    activation = TaskLoopActivationRuntime(
        database,
        task_loops,
        planner,
        planning,
        execution,
        ModelPlannerNodeBinder(
            planner._agents,  # noqa: SLF001
            registry,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
        ),
        clock=lambda: NOW,
    )
    delegate = CapabilityExecutionEngine(registry)
    engine: CapabilityExecutionEnginePort = (
        wrap_engine(delegate) if wrap_engine is not None else delegate
    )
    runtime = CapabilityExecutionRuntime(
        database,
        CapabilityInputBindingCatalog(planner._capabilities),  # noqa: SLF001
        registry,
        engine,
        clock=clock,
    )
    task_id, _fallback = await _defer_two_offers(
        database,
        planner,
        provider,
        suffix=suffix,
    )
    await task_loops.plan(task_id)
    await activation.activate(task_id)
    return _RuntimeFixture(
        database=database,
        activation=activation,
        runtime=runtime,
        task_id=task_id,
        knowledge=knowledge,
        mcp=actual_mcp,
        engine=engine,
    )


@pytest.mark.asyncio
async def test_coordinator_reduces_capabilities_and_controls_across_restart(
    tmp_path: Path,
) -> None:
    fixture = await _runtime_fixture(tmp_path, suffix="c")
    coordinator = TaskLoopExecutionCoordinator(
        fixture.database,
        fixture.activation,
        capabilities=fixture.runtime,
    )
    try:
        first = await coordinator.advance(fixture.task_id, "coordinator-worker")
        second = await coordinator.advance(fixture.task_id, "coordinator-worker")
        final = await coordinator.advance(fixture.task_id, "coordinator-worker")

        assert first.command.kind == "execute_capability"
        assert second.command.kind == "execute_capability"
        assert final.command.kind == "reduce_control_node"
        assert final.read.execution is not None
        assert final.read.execution.status == "active"
        assert final.read.workbench.verified_result_count == 2

        restarted = TaskLoopExecutionCoordinator(
            fixture.database,
            fixture.activation,
            capabilities=fixture.runtime,
        )
        delivered = await restarted.advance(
            fixture.task_id,
            "restarted-coordinator-worker",
        )
        replay = await restarted.advance(
            fixture.task_id,
            "restarted-coordinator-worker",
        )

        assert delivered.command.kind == "reduce_control_node"
        assert delivered.read.execution is not None
        assert delivered.read.execution.status == "succeeded"
        assert delivered.read.execution.event_count == 2
        assert all(item.status == "verified" for item in delivered.read.nodes)
        assert replay.command.kind == "noop"
        assert replay.read.execution == delivered.read.execution
    finally:
        await fixture.database.dispose()


async def _nodes(database: Database) -> tuple[TaskExecutionNodeRecord, ...]:
    async with database.session() as session:
        return tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .where(TaskExecutionNodeRecord.node_kind == "capability")
                    .order_by(TaskExecutionNodeRecord.local_key)
                )
            ).all()
        )


async def _attempts(database: Database) -> tuple[TaskLoopNodeAttemptRecord, ...]:
    async with database.session() as session:
        return tuple(
            (
                await session.scalars(
                    select(TaskLoopNodeAttemptRecord).order_by(
                        TaskLoopNodeAttemptRecord.created_at,
                        TaskLoopNodeAttemptRecord.attempt_id,
                    )
                )
            ).all()
        )


async def _result_count(database: Database) -> int:
    async with database.session() as session:
        return int(
            await session.scalar(select(func.count()).select_from(TaskLoopVerifiedResultRecord))
            or 0
        )


@pytest.mark.asyncio
async def test_exact_capabilities_persist_verified_refs_before_unlock(
    tmp_path: Path,
) -> None:
    fixture = await _runtime_fixture(tmp_path, suffix="e")
    try:
        first = await fixture.runtime.run_once(fixture.task_id, "capability-worker")

        assert first is not None and first.status == "verified"
        assert first.result_ref is not None
        assert first.result_ref.capability.capability_id == "knowledge.local.v1"
        nodes = await _nodes(fixture.database)
        assert tuple(item.status for item in nodes) == ("verified", "ready")
        assert await _result_count(fixture.database) == 1

        second = await fixture.runtime.run_once(fixture.task_id, "capability-worker")

        assert second is not None and second.status == "verified"
        assert second.result_ref is not None
        assert second.result_ref.capability.capability_id == "mcp.text.metrics.v1"
        assert tuple(item.status for item in await _nodes(fixture.database)) == (
            "verified",
            "verified",
        )
        assert await _result_count(fixture.database) == 2
        assert fixture.knowledge.calls == [("cats", 10)]
        assert len(fixture.mcp.calls) == 1
    finally:
        await fixture.database.dispose()


@pytest.mark.asyncio
async def test_persisted_candidate_restart_runs_only_verification(
    tmp_path: Path,
) -> None:
    holder: dict[str, _FlakyVerificationEngine] = {}

    def wrap(delegate: CapabilityExecutionEngine) -> CapabilityExecutionEnginePort:
        engine = _FlakyVerificationEngine(delegate)
        holder["engine"] = engine
        return engine

    fixture = await _runtime_fixture(tmp_path, suffix="f", wrap_engine=wrap)
    engine = holder["engine"]
    try:
        with pytest.raises(CapabilityVerificationDeferredError):
            await fixture.runtime.run_once(fixture.task_id, "first-process")

        attempts = await _attempts(fixture.database)
        assert len(attempts) == 1
        assert attempts[0].status == "awaiting_verification"
        assert attempts[0].candidate_digest is not None
        assert attempts[0].verification_digest is None
        assert tuple(item.status for item in await _nodes(fixture.database)) == (
            "awaiting_verification",
            "pending",
        )
        assert await _result_count(fixture.database) == 0

        outcome = await fixture.runtime.run_once(fixture.task_id, "restarted-process")

        assert outcome is not None and outcome.status == "verified"
        assert engine.execute_calls == 1
        assert engine.verify_calls == 2
        assert fixture.knowledge.calls == [("cats", 10)]
    finally:
        await fixture.database.dispose()


@pytest.mark.asyncio
async def test_candidate_tampering_is_rejected_without_reexecution(
    tmp_path: Path,
) -> None:
    holder: dict[str, _FlakyVerificationEngine] = {}

    def wrap(delegate: CapabilityExecutionEngine) -> CapabilityExecutionEnginePort:
        engine = _FlakyVerificationEngine(delegate)
        holder["engine"] = engine
        return engine

    fixture = await _runtime_fixture(tmp_path, suffix="a", wrap_engine=wrap)
    engine = holder["engine"]
    try:
        with pytest.raises(CapabilityVerificationDeferredError):
            await fixture.runtime.run_once(fixture.task_id, "first-process")
        async with fixture.database.session() as session, session.begin():
            record = await session.scalar(select(TaskLoopNodeAttemptRecord))
            assert record is not None and record.candidate_manifest is not None
            record.candidate_manifest = {
                **record.candidate_manifest,
                "result_digest": "0" * 64,
            }

        with pytest.raises(CapabilityExecutionRuntimeProofRejectedError):
            await fixture.runtime.run_once(fixture.task_id, "restarted-process")

        assert engine.execute_calls == 1
        assert engine.verify_calls == 1
        assert await _result_count(fixture.database) == 0
    finally:
        await fixture.database.dispose()


@pytest.mark.asyncio
async def test_mcp_failure_is_outcome_unknown_and_never_replayed(
    tmp_path: Path,
) -> None:
    mcp = _FailingMcp()
    fixture = await _runtime_fixture(tmp_path, suffix="b", mcp=mcp)
    try:
        first = await fixture.runtime.run_once(fixture.task_id, "capability-worker")
        assert first is not None and first.status == "verified"

        unknown = await fixture.runtime.run_once(fixture.task_id, "capability-worker")

        assert unknown is not None and unknown.status == "outcome_unknown"
        assert unknown.error_code == "CAPABILITY_OUTCOME_UNKNOWN"
        attempts = await _attempts(fixture.database)
        assert {item.status for item in attempts} == {
            "verified",
            "outcome_unknown",
        }
        unknown_attempt = next(item for item in attempts if item.status == "outcome_unknown")
        assert unknown_attempt.candidate_digest is None
        assert (await _nodes(fixture.database))[1].status == "failed"
        assert (
            await fixture.runtime.run_once(
                fixture.task_id,
                "another-worker",
            )
            is None
        )
        assert len(mcp.calls) == 1
    finally:
        await fixture.database.dispose()


@pytest.mark.asyncio
async def test_completed_mcp_call_with_stale_fence_recovers_outcome_unknown(
    tmp_path: Path,
) -> None:
    holder: dict[str, _BlockingMcpEngine] = {}

    def wrap(delegate: CapabilityExecutionEngine) -> CapabilityExecutionEnginePort:
        engine = _BlockingMcpEngine(delegate)
        holder["engine"] = engine
        return engine

    fixture = await _runtime_fixture(tmp_path, suffix="c", wrap_engine=wrap)
    engine = holder["engine"]
    try:
        first = await fixture.runtime.run_once(fixture.task_id, "capability-worker")
        assert first is not None and first.status == "verified"
        pending = asyncio.create_task(fixture.runtime.run_once(fixture.task_id, "mcp-worker"))
        await engine.mcp_candidate_completed.wait()

        async with fixture.database.session() as session, session.begin():
            attempt_record = await session.scalar(
                select(TaskLoopNodeAttemptRecord).where(
                    TaskLoopNodeAttemptRecord.status == "running"
                )
            )
            assert attempt_record is not None
            attempt = fixture.runtime._attempt_from_record(attempt_record)  # noqa: SLF001
            replacement = fixture.runtime._replace_attempt(  # noqa: SLF001
                attempt,
                revision=attempt.revision + 1,
                claim_owner_id="replacement-worker",
                claim_fencing_token=attempt.claim_fencing_token + 1,
                claim_expires_at=NOW,
                updated_at=NOW,
            )
            fixture.runtime._apply_attempt(attempt_record, replacement)  # noqa: SLF001
            node = await session.get(TaskExecutionNodeRecord, attempt.node_id)
            assert node is not None
            node.claim_owner_id = "replacement-worker"
            node.claim_fencing_token = replacement.claim_fencing_token
            node.claim_expires_at = NOW
            node.revision += 1
            node.updated_at = NOW

        engine.release_mcp_candidate.set()
        with pytest.raises(CapabilityExecutionRuntimeStaleFenceError):
            await pending

        assert (
            await fixture.runtime.run_once(
                fixture.task_id,
                "recovery-worker",
            )
            is None
        )
        attempts = await _attempts(fixture.database)
        unknown_attempt = next(item for item in attempts if item.status == "outcome_unknown")
        assert unknown_attempt.candidate_digest is None
        assert len(fixture.mcp.calls) == 1
    finally:
        await fixture.database.dispose()


@pytest.mark.parametrize("scope", ["task", "run"])
@pytest.mark.asyncio
async def test_cross_scope_verified_result_ref_is_rejected(
    tmp_path: Path,
    scope: str,
) -> None:
    fixture = await _runtime_fixture(
        tmp_path,
        suffix="d" if scope == "task" else "a",
    )
    try:
        first = await fixture.runtime.run_once(fixture.task_id, "capability-worker")
        assert first is not None and first.status == "verified"
        async with fixture.database.session() as session, session.begin():
            record = await session.scalar(select(TaskLoopVerifiedResultRecord))
            assert record is not None
            ref = VerifiedCapabilityResultRef.model_validate(record.result_ref_manifest)
            values = ref.model_dump(mode="json", exclude={"result_ref_digest"})
            if scope == "task":
                values["task_id"] = f"tsk_{'0' * 32}"
            else:
                values["run_id"] = f"run_{'0' * 64}"
            changed = VerifiedCapabilityResultRef.model_validate(
                {**values, "result_ref_digest": sha256_digest(values)}
            )
            record.result_ref_manifest = changed.model_dump(mode="json")
            record.result_ref_digest = changed.result_ref_digest

        with pytest.raises(CapabilityExecutionRuntimeProofRejectedError):
            await fixture.runtime.run_once(fixture.task_id, "capability-worker")

        assert len(fixture.mcp.calls) == 0
    finally:
        await fixture.database.dispose()
