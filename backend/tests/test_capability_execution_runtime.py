from __future__ import annotations

import asyncio
import json
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
from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.multi_step_plan_runtime import (
    MultiStepPlanConflictError,
    MultiStepPlanRuntime,
)
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationProofRejectedError,
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
    TaskLoopExecutionCoordinatorProofRejectedError,
)
from deskpilot.application.workspace_command_plan_binder import WorkspaceCommandPlanBinder
from deskpilot.application.workspace_command_plan_compiler import WorkspaceCommandPlanCompiler
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import (
    CapabilityExecutionContext,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.command_profiles import (
    CommandProfile,
    CommandProfileId,
    WorkspaceCommandRead,
    WorkspaceCommandSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskExecutionNodeRecord,
    TaskLoopCapabilityApprovalRecord,
    TaskLoopCycleEventRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    WorkspaceCommandPlanBindingRecord,
    utc_now,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_builtin_capability_executors import (  # noqa: E402
    FakeCommandSnapshots,
    FakeKnowledge,
    FakeMcp,
    FakePythonRuntime,
)
from test_multi_step_plan_runtime import (  # noqa: E402
    NOW,
    ScriptedTurnPlannerProvider,
    _defer_two_offers,
    _offer_key_for,
    _runtimes,
    _seed_turn,
    _select_two_offers,
    _task_loop_record_counts,
)


def _select_patch_offer(request: Any) -> dict[str, Any]:
    changes_json = json.dumps(
        [
            {"path": "one.txt", "old_text": "old-one", "new_text": "new-one"},
            {"path": "two.txt", "old_text": "old-two", "new_text": "new-two"},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _offer_key_for(request, "changes_json"),
                "parameters": [
                    {"name": "changes_json", "value": changes_json}
                ],
            },
            {
                "offer_key": _offer_key_for(request, "project_path"),
                "parameters": [
                    {"name": "project_path", "value": "backend"},
                    {"name": "test_path", "value": "tests/test_one.py"},
                ],
            },
        ],
    }


def _command_offer_key(request: Any, profile_id: str) -> str:
    payload = json.loads(request.messages[1].content)
    matches = [
        item["offer"]["offer_key"]
        for item in payload["offers"]
        if profile_id in item["intent_description"]
    ]
    assert len(matches) == 1
    return str(matches[0])


def _select_two_command_offers(request: Any) -> dict[str, Any]:
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _command_offer_key(request, "python.ruff.v1"),
                "parameters": [{"name": "project_path", "value": "backend"}],
            },
            {
                "offer_key": _command_offer_key(request, "python.mypy.v1"),
                "parameters": [{"name": "project_path", "value": "backend"}],
            },
        ],
    }


def _select_mixed_grouped_offers(request: Any) -> dict[str, Any]:
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _command_offer_key(request, "python.ruff.v1"),
                "parameters": [{"name": "project_path", "value": "backend"}],
            },
            {
                "offer_key": _command_offer_key(request, "node.pnpm_typecheck.v1"),
                "parameters": [{"name": "project_path", "value": "backend"}],
            },
            {
                "offer_key": _offer_key_for(request, "query"),
                "parameters": [{"name": "query", "value": "release evidence"}],
            },
            {
                "offer_key": _command_offer_key(request, "python.mypy.v1"),
                "parameters": [{"name": "project_path", "value": "frontend"}],
            },
        ],
    }


def _command_read(profile: CommandProfile, status: str) -> WorkspaceCommandRead:
    passed = status == "passed"
    output = "ok" if passed else "scripted check failure"
    material: dict[str, Any] = {
        "schema_version": "deskpilot.workspace-command-read.v1",
        "command_profile_id": profile.command_profile_id,
        "profile_digest": profile.profile_digest,
        "project_path": "backend",
        "snapshot_digest": "1" * 64,
        "toolchain_digest": "2" * 64,
        "status": status,
        "exit_code": 0 if passed else 1,
        "duration_ms": 10,
        "output_summary": output,
        "output_digest": sha256_digest({"output": output}),
        "output_truncated": False,
        "termination_reason": "completed",
        "cancellation_receipt_digest": None,
        "isolation_mode": "windows_appcontainer",
        "network_access": False,
        "temporary_snapshot": True,
        "snapshot_mutations_discarded": True,
    }
    return WorkspaceCommandRead.model_validate(
        {**material, "result_digest": sha256_digest(material)}
    )


class _ScriptedCommandRuntime:
    enabled_profile_ids: frozenset[CommandProfileId] = frozenset(
        {"python.ruff.v1", "python.mypy.v1"}
    )

    def __init__(
        self,
        profiles: CommandProfileCatalog,
        outcomes: list[tuple[CommandProfileId, str]],
    ) -> None:
        self._profiles = profiles
        self._outcomes = outcomes
        self.calls: list[CommandProfileId] = []

    def run(self, snapshot: WorkspaceCommandSnapshot) -> WorkspaceCommandRead:
        del snapshot
        profile_id, status = self._outcomes.pop(0)
        self.calls.append(profile_id)
        return _command_read(self._profiles.resolve(profile_id), status)


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


class _BlockingApprovedPatchEngine:
    def __init__(self, delegate: CapabilityExecutionEngine) -> None:
        self.delegate = delegate
        self.patch_effect_completed = asyncio.Event()
        self.release_candidate = asyncio.Event()

    async def execute_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
    ) -> CapabilityExecutionCandidate:
        return await self.delegate.execute_candidate(context, bound_input)

    async def prepare_approval(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
    ) -> Any:
        return await self.delegate.prepare_approval(context, bound_input)

    async def execute_approved_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: Any,
        preview_manifest: dict[str, Any],
    ) -> CapabilityExecutionCandidate:
        candidate = await self.delegate.execute_approved_candidate(
            context,
            bound_input,
            preview_manifest,
        )
        self.patch_effect_completed.set()
        await self.release_candidate.wait()
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


class _FlakyKnowledge(FakeKnowledge):
    async def search(self, query: str, limit: int) -> Any:
        result = await super().search(query, limit)
        if len(self.calls) == 1:
            raise RuntimeError("scripted transient knowledge boundary")
        return result


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
    knowledge: FakeKnowledge | None = None,
    mcp: FakeMcp | None = None,
    wrap_engine: Callable[[CapabilityExecutionEngine], CapabilityExecutionEnginePort] | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
) -> _RuntimeFixture:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / f'capability-{suffix}.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    actual_knowledge = knowledge or FakeKnowledge()
    actual_mcp = mcp or FakeMcp()
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001 - exact shared fixture
        knowledge=actual_knowledge,
        mcp=actual_mcp,
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001 - exact shared fixture
        planner._agents,  # noqa: SLF001 - exact shared fixture
        clock=lambda: NOW,
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
        knowledge=actual_knowledge,
        mcp=actual_mcp,
        engine=engine,
    )


@pytest.mark.asyncio
async def test_workspace_command_plan_groups_on_route_project_and_ecosystem_changes(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'command-plan-groups.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_mixed_grouped_offers])
    planner, _planning, _legacy_loop, composer = _runtimes(database, provider)
    workspace_root = tmp_path / "workspace"
    (workspace_root / "backend").mkdir(parents=True)
    (workspace_root / "frontend").mkdir()
    workspace = WorkspaceFileRuntime(str(workspace_root), str(tmp_path / "staging"))
    command_plans = WorkspaceCommandPlanBinder(
        WorkspaceCommandPlanCompiler(CommandProfileCatalog(), workspace)
    )
    task_loops = MultiStepPlanRuntime(
        database,
        planner,
        composer,
        command_plans=command_plans,
        clock=lambda: NOW,
    )
    task_id, fallback = await _seed_turn(
        database,
        suffix="8",
        message="run backend and frontend checks and collect release evidence",
    )
    try:
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset(
                {
                    "workspace_command_profile:python.ruff.v1",
                    "workspace_command_profile:node.pnpm_typecheck.v1",
                    "knowledge_lookup",
                    "workspace_command_profile:python.mypy.v1",
                }
            ),
        )
        interpreted = await planner.interpret(task_id)
        assert interpreted.adjudication is not None
        assert interpreted.adjudication.outcome == "multi_step_deferred", (
            interpreted.run.failure,
            interpreted.adjudication.reason_code,
        )

        planned = await task_loops.plan(task_id)
        bundle = await task_loops.get_bundle(task_id)

        assert planned.status == "planned"
        assert bundle is not None
        assert tuple(
            (
                item.command_plan.request.project_path,
                item.command_plan.ecosystem,
                item.command_plan.request.command_profile_ids,
            )
            for item in bundle.command_plans
        ) == (
            ("backend", "python", ("python.ruff.v1",)),
            ("backend", "node", ("node.pnpm_typecheck.v1",)),
            ("frontend", "python", ("python.mypy.v1",)),
        )
        assert tuple(item.group_ordinal for item in bundle.command_plans) == (1, 2, 3)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_workspace_command_plan_transaction_rolls_back_partial_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'command-plan-rollback.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_command_offers])
    planner, _planning, _legacy_loop, composer = _runtimes(database, provider)
    workspace_root = tmp_path / "workspace"
    (workspace_root / "backend").mkdir(parents=True)
    workspace = WorkspaceFileRuntime(str(workspace_root), str(tmp_path / "staging"))
    command_plans = WorkspaceCommandPlanBinder(
        WorkspaceCommandPlanCompiler(CommandProfileCatalog(), workspace)
    )
    task_loops = MultiStepPlanRuntime(
        database,
        planner,
        composer,
        command_plans=command_plans,
        clock=lambda: NOW,
    )
    original_record = task_loops._command_plan_record  # noqa: SLF001

    def broken_record(binding: Any) -> WorkspaceCommandPlanBindingRecord:
        record = original_record(binding)
        record.draft_id = f"mpd_{'f' * 64}"
        return record

    monkeypatch.setattr(task_loops, "_command_plan_record", broken_record)
    task_id, fallback = await _seed_turn(
        database,
        suffix="7",
        message="run backend checks atomically",
    )
    try:
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset(
                {
                    "workspace_command_profile:python.ruff.v1",
                    "workspace_command_profile:python.mypy.v1",
                }
            ),
        )
        await planner.interpret(task_id)

        with pytest.raises(MultiStepPlanConflictError):
            await task_loops.plan(task_id)

        assert await _task_loop_record_counts(database, task_id) == (1, 1, 0, 0)
        async with database.session() as session:
            assert int(
                await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceCommandPlanBindingRecord)
                    .where(WorkspaceCommandPlanBindingRecord.task_id == task_id)
                )
                or 0
            ) == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_workspace_command_plan_persists_fail_stop_repair_and_restart(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'command-plan.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_command_offers])
    planner, planning, _legacy_loop, composer = _runtimes(database, provider)
    workspace_root = tmp_path / "workspace"
    staging_root = tmp_path / "staging"
    (workspace_root / "backend").mkdir(parents=True)
    staging_root.mkdir()
    workspace = WorkspaceFileRuntime(str(workspace_root), str(staging_root))
    profiles = CommandProfileCatalog()
    command_plans = WorkspaceCommandPlanBinder(
        WorkspaceCommandPlanCompiler(profiles, workspace)
    )
    task_loops = MultiStepPlanRuntime(
        database,
        planner,
        composer,
        command_plans=command_plans,
        clock=lambda: NOW,
    )
    snapshots = FakeCommandSnapshots()
    commands = _ScriptedCommandRuntime(
        profiles,
        [
            ("python.ruff.v1", "failed"),
            ("python.ruff.v1", "passed"),
            ("python.mypy.v1", "passed"),
        ],
    )
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        command_profiles=profiles,
        command_snapshots=snapshots,
        command_runtime=commands,
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001
        planner._agents,  # noqa: SLF001
        clock=lambda: NOW,
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
        command_plans=command_plans,
        clock=lambda: NOW,
    )
    runtime = CapabilityExecutionRuntime(
        database,
        CapabilityInputBindingCatalog(planner._capabilities),  # noqa: SLF001
        registry,
        CapabilityExecutionEngine(registry),
        command_plans=command_plans,
        clock=lambda: NOW,
    )
    task_id, fallback = await _seed_turn(
        database,
        suffix="9",
        message="run backend checks",
    )
    try:
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset(
                {
                    "workspace_command_profile:python.ruff.v1",
                    "workspace_command_profile:python.mypy.v1",
                }
            ),
        )
        interpreted = await planner.interpret(task_id)
        assert interpreted.adjudication is not None
        assert interpreted.adjudication.outcome == "multi_step_deferred"
        await task_loops.plan(task_id)
        bundle = await task_loops.get_bundle(task_id)
        assert bundle is not None
        assert len(bundle.command_plans) == 1
        assert tuple(
            item.command_profile.command_profile_id
            for item in bundle.command_plans[0].command_plan.steps
        ) == ("python.ruff.v1", "python.mypy.v1")
        async with database.session() as session:
            assert int(
                await session.scalar(
                    select(func.count()).select_from(
                        WorkspaceCommandPlanBindingRecord
                    )
                )
                or 0
            ) == 1

        original_profile = profiles.resolve("python.ruff.v1")
        changed_profile_values = original_profile.model_dump(
            exclude={"profile_digest", "timeout_seconds"}
        )
        profiles._profiles["python.ruff.v1"] = CommandProfile.build(  # noqa: SLF001
            **changed_profile_values,
            timeout_seconds=121,
        )
        with pytest.raises(TaskLoopActivationProofRejectedError):
            await activation.activate(task_id)
        profiles._profiles["python.ruff.v1"] = original_profile  # noqa: SLF001

        project_root = workspace_root / "backend"
        drifted_root = workspace_root / "backend-drifted"
        project_root.rename(drifted_root)
        with pytest.raises(TaskLoopActivationProofRejectedError):
            await activation.activate(task_id)
        drifted_root.rename(project_root)

        await activation.activate(task_id)
        coordinator = TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=runtime,
        )
        failed = await coordinator.advance(task_id, "command-worker-1")
        command_nodes = tuple(
            item for item in failed.read.nodes if item.command_plan_id is not None
        )
        first_node, second_node = sorted(
            command_nodes,
            key=lambda item: item.command_step_sequence or 0,
        )
        assert first_node.status == "failed"
        assert first_node.verified_failure_result_count == 1
        assert first_node.verified_result_present is False
        assert second_node.status == "pending"
        assert commands.calls == ["python.ruff.v1"]

        repaired = await coordinator.advance(task_id, "command-repair")
        assert repaired.command.kind == "start_repair"
        restarted_runtime = CapabilityExecutionRuntime(
            database,
            CapabilityInputBindingCatalog(planner._capabilities),  # noqa: SLF001
            registry,
            CapabilityExecutionEngine(registry),
            command_plans=command_plans,
            clock=lambda: NOW,
        )
        restarted = TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=restarted_runtime,
        )
        passed_first = await restarted.advance(task_id, "command-worker-2")
        first_after = next(
            item
            for item in passed_first.read.nodes
            if item.command_step_sequence == 1
        )
        second_after = next(
            item
            for item in passed_first.read.nodes
            if item.command_step_sequence == 2
        )
        assert first_after.status == "verified"
        assert first_after.verified_result_present
        assert first_after.verified_failure_result_count == 1
        assert second_after.status == "ready"

        passed_second = await restarted.advance(task_id, "command-worker-3")
        assert passed_second.command.kind == "execute_capability"
        assert next(
            item
            for item in passed_second.read.nodes
            if item.command_step_sequence == 2
        ).status == "verified"
        assert commands.calls == [
            "python.ruff.v1",
            "python.ruff.v1",
            "python.mypy.v1",
        ]
        async with database.session() as session:
            results = int(
                await session.scalar(
                    select(func.count()).select_from(TaskLoopVerifiedResultRecord)
                )
                or 0
            )
        assert results == 3
    finally:
        await database.dispose()


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


@pytest.mark.asyncio
async def test_bounded_repair_recovers_started_marker_without_replaying_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_compile = RouteRecipeCatalog.compile

    def compile_with_retry_budget(
        cls: type[RouteRecipeCatalog],
        **kwargs: Any,
    ) -> Any:
        del cls
        contract, draft = original_compile(**kwargs)
        if kwargs["route_id"] != "knowledge_lookup":
            return contract, draft
        contract_material = contract.model_dump(mode="json")
        contract_material["budget"]["max_tool_calls"] = 3
        contract_material["budget"]["max_retries"] = 2
        draft_material = draft.model_dump(mode="json")
        for node in draft_material["nodes"]:
            if node["local_key"] == "knowledge_lookup":
                node["budget"]["tool_calls"] = 3
                node["budget"]["retries"] = 2
        return (
            type(contract).model_validate(contract_material),
            type(draft).model_validate(draft_material),
        )

    monkeypatch.setattr(
        RouteRecipeCatalog,
        "compile",
        classmethod(compile_with_retry_budget),
    )
    knowledge = _FlakyKnowledge()
    fixture = await _runtime_fixture(
        tmp_path,
        suffix="b",
        knowledge=knowledge,
    )
    coordinator = TaskLoopExecutionCoordinator(
        fixture.database,
        fixture.activation,
        capabilities=fixture.runtime,
    )
    try:
        failed = await coordinator.advance(fixture.task_id, "repair-first-worker")
        failed_node = next(item for item in failed.read.nodes if item.status == "failed")
        snapshot = await coordinator._snapshot(failed.read)  # noqa: SLF001
        repair_command = coordinator._reducer.decide(snapshot)  # noqa: SLF001

        assert repair_command.kind == "start_repair"
        assert repair_command.node_id == failed_node.node_id
        assert len(knowledge.calls) == 1

        await coordinator._record_cycle_event(  # noqa: SLF001
            failed.read,
            repair_command,
            kind="repair_started",
        )
        restarted = TaskLoopExecutionCoordinator(
            fixture.database,
            fixture.activation,
            capabilities=fixture.runtime,
        )
        recovered = await restarted.advance(
            fixture.task_id,
            "repair-restarted-worker",
        )

        assert recovered.command.kind == "execute_capability"
        assert len(knowledge.calls) == 2
        assert recovered.read.execution is not None
        assert recovered.read.execution.event_count == 3
        assert recovered.read.cycle is not None
        assert recovered.read.cycle.repair_count == 1
        assert recovered.read.cycle.latest_event_kind == "repair_completed"
        assert failed_node.node_id in {
            item.node_id for item in recovered.read.nodes if item.status == "verified"
        }
        async with fixture.database.session() as session:
            events = tuple(
                (
                    await session.scalars(
                        select(TaskLoopCycleEventRecord).order_by(
                            TaskLoopCycleEventRecord.sequence
                        )
                    )
                ).all()
            )
        assert [item.kind for item in events] == [
            "repair_started",
            "repair_completed",
        ]
    finally:
        await fixture.database.dispose()


@pytest.mark.asyncio
async def test_workspace_patch_waits_for_exact_revision_and_recovers_after_approval(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'capability-patch.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_patch_offer])
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    knowledge = FakeKnowledge()
    workspace_root = tmp_path / "workspace"
    staging_root = tmp_path / "staging"
    workspace_root.mkdir()
    staging_root.mkdir()
    first_path = workspace_root / "one.txt"
    second_path = workspace_root / "two.txt"
    first_path.write_text("before old-one after", encoding="utf-8")
    second_path.write_text("before old-two after", encoding="utf-8")
    test_path = workspace_root / "backend" / "tests" / "test_one.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    workspace = WorkspaceFileRuntime(str(workspace_root), str(staging_root))
    python_tests = FakePythonRuntime()
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        knowledge=knowledge,
        workspace=workspace,
        python_tests=python_tests,
        workspace_patches=workspace,
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001
        planner._agents,  # noqa: SLF001
        clock=lambda: NOW,
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
    engine = _BlockingApprovedPatchEngine(CapabilityExecutionEngine(registry))
    runtime = CapabilityExecutionRuntime(
        database,
        CapabilityInputBindingCatalog(planner._capabilities),  # noqa: SLF001
        registry,
        engine,
        clock=lambda: NOW,
    )
    changes_json = json.dumps(
        [
            {"path": "one.txt", "old_text": "old-one", "new_text": "new-one"},
            {"path": "two.txt", "old_text": "old-two", "new_text": "new-two"},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="f",
            message=(
                f"apply {changes_json} then test project backend "
                "file tests/test_one.py"
            ),
        )
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"workspace_patch_bundle", "workspace_python_test"}),
        )
        interpreted = await planner.interpret(task_id)
        assert interpreted.adjudication is not None
        assert interpreted.adjudication.outcome == "multi_step_deferred"
        await task_loops.plan(task_id)
        await activation.activate(task_id)
        coordinator = TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=runtime,
        )

        waiting = await coordinator.advance(task_id, "patch-worker")

        assert waiting.command.kind == "execute_capability"
        assert waiting.read.execution is not None
        assert waiting.read.execution.status == "awaiting_user"
        assert waiting.read.workspace_patch is not None
        assert waiting.read.workspace_patch.schema_version == (
            "deskpilot.workspace-patch-preview.v1"
        )
        assert first_path.read_text(encoding="utf-8") == "before old-one after"
        assert second_path.read_text(encoding="utf-8") == "before old-two after"

        with pytest.raises(TaskLoopExecutionCoordinatorProofRejectedError):
            await coordinator.approve_workspace_patch(
                task_id,
                waiting.read.workspace_patch.confirmation_digest,
                expected_execution_revision=waiting.read.execution.revision - 1,
            )
        approved_preview = await coordinator.approve_workspace_patch(
            task_id,
            waiting.read.workspace_patch.confirmation_digest,
            expected_execution_revision=waiting.read.execution.revision,
        )
        assert approved_preview == waiting.read.workspace_patch
        assert first_path.read_text(encoding="utf-8") == "before old-one after"

        restarted = TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=runtime,
        )
        interrupted = asyncio.create_task(
            restarted.advance(task_id, "interrupted-patch-worker")
        )
        await engine.patch_effect_completed.wait()
        assert first_path.read_text(encoding="utf-8") == "before new-one after"
        async with database.session() as session, session.begin():
            running_attempt = await session.scalar(
                select(TaskLoopNodeAttemptRecord).where(
                    TaskLoopNodeAttemptRecord.status == "running"
                )
            )
            assert running_attempt is not None
            attempt_model = runtime._attempt_from_record(  # noqa: SLF001
                running_attempt
            )
            expired = runtime._replace_attempt(  # noqa: SLF001
                attempt_model,
                revision=attempt_model.revision + 1,
                claim_expires_at=NOW,
                updated_at=NOW,
            )
            runtime._apply_attempt(running_attempt, expired)  # noqa: SLF001
            running_node = await session.get(
                TaskExecutionNodeRecord,
                running_attempt.node_id,
            )
            assert running_node is not None
            running_node.claim_expires_at = NOW
            running_node.updated_at = NOW
        engine.release_candidate.set()
        with pytest.raises(CapabilityExecutionRuntimeStaleFenceError):
            await interrupted

        committed = await restarted.advance(task_id, "restarted-patch-worker")
        assert committed.command.kind == "execute_capability"
        assert committed.read.workspace_patch is not None
        assert committed.read.workspace_patch.schema_version == (
            "deskpilot.workspace-patch-receipt.v1"
        )
        assert first_path.read_text(encoding="utf-8") == "before new-one after"
        assert second_path.read_text(encoding="utf-8") == "before new-two after"
        fixed_test = await restarted.advance(task_id, "fixed-test-worker")
        assert fixed_test.command.kind == "execute_capability"
        assert python_tests.calls == 1

        async with database.session() as session:
            approval = await session.scalar(select(TaskLoopCapabilityApprovalRecord))
            result = await session.scalar(
                select(TaskLoopVerifiedResultRecord).where(
                    TaskLoopVerifiedResultRecord.result_kind == "patch_receipt"
                )
            )
            test_result = await session.scalar(
                select(TaskLoopVerifiedResultRecord).where(
                    TaskLoopVerifiedResultRecord.result_kind == "python_test"
                )
            )
        assert approval is not None and approval.status == "consumed"
        assert result is not None
        assert test_result is not None
        assert result.output_manifest["receipt"]["confirmation_digest"] == (
            approved_preview.confirmation_digest
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_coordinator_persists_three_no_progress_observations_across_restart(
    tmp_path: Path,
) -> None:
    fixture = await _runtime_fixture(tmp_path, suffix="e")
    try:
        async with fixture.database.session() as session, session.begin():
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).with_for_update()
                    )
                ).all()
            )
            now = utc_now()
            for node in nodes:
                node.status = "pending"
                node.revision += 1
                node.updated_at = now

        for expected_count in (1, 2, 3):
            coordinator = TaskLoopExecutionCoordinator(
                fixture.database,
                fixture.activation,
                capabilities=fixture.runtime,
            )
            observed = await coordinator.advance(
                fixture.task_id,
                f"no-progress-worker-{expected_count}",
            )
            assert observed.command.kind == "record_no_progress"
            async with fixture.database.session() as session:
                count = int(
                    await session.scalar(
                        select(func.count()).select_from(TaskLoopCycleEventRecord)
                    )
                    or 0
                )
            assert count == expected_count

        restarted = TaskLoopExecutionCoordinator(
            fixture.database,
            fixture.activation,
            capabilities=fixture.runtime,
        )
        terminal = await restarted.advance(
            fixture.task_id,
            "no-progress-terminal-worker",
        )
        assert terminal.command.kind == "terminate_no_progress"
        assert terminal.read.execution is not None
        assert terminal.read.execution.status == "failed"

        async with fixture.database.session() as session:
            events = tuple(
                (
                    await session.scalars(
                        select(TaskLoopCycleEventRecord).order_by(
                            TaskLoopCycleEventRecord.sequence
                        )
                    )
                ).all()
            )
        assert [item.kind for item in events] == [
            "no_progress_observed",
            "no_progress_observed",
            "no_progress_observed",
            "no_progress_terminated",
        ]
        assert [item.evidence_manifest["observation_count"] for item in events] == [
            1,
            2,
            3,
            0,
        ]
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


@pytest.mark.asyncio
async def test_running_capability_renews_short_lease_until_candidate_returns(
    tmp_path: Path,
) -> None:
    holder: dict[str, _BlockingMcpEngine] = {}

    def wrap(delegate: CapabilityExecutionEngine) -> CapabilityExecutionEnginePort:
        engine = _BlockingMcpEngine(delegate)
        holder["engine"] = engine
        return engine

    fixture = await _runtime_fixture(
        tmp_path,
        suffix="d",
        wrap_engine=wrap,
        clock=utc_now,
    )
    engine = holder["engine"]
    try:
        first = await fixture.runtime.run_once(
            fixture.task_id,
            "capability-worker",
            lease_seconds=5,
        )
        assert first is not None and first.status == "verified"

        pending = asyncio.create_task(
            fixture.runtime.run_once(
                fixture.task_id,
                "heartbeat-worker",
                lease_seconds=5,
            )
        )
        await engine.mcp_candidate_completed.wait()
        initial = (await _attempts(fixture.database))[-1]
        assert initial.status == "running"
        assert initial.claim_expires_at is not None

        await asyncio.sleep(5.5)
        renewed = (await _attempts(fixture.database))[-1]
        assert renewed.status == "running"
        assert renewed.revision > initial.revision
        assert renewed.claim_expires_at is not None
        assert renewed.claim_expires_at > initial.claim_expires_at

        engine.release_mcp_candidate.set()
        outcome = await pending
        assert outcome is not None and outcome.status == "verified"
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
