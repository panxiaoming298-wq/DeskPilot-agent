from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.agent_model_loop import AgentModelLoopRuntime
from deskpilot.application.builtin_capability_executors import (
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_execution_engine import CapabilityExecutionEngine
from deskpilot.application.capability_execution_runtime import CapabilityExecutionRuntime
from deskpilot.application.capability_input_binding_catalog import (
    CapabilityInputBindingCatalog,
)
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.task_loop_activation_runtime import TaskLoopActivationRuntime
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_agent_runtime import (
    TaskLoopAgentProofRejectedError,
    TaskLoopAgentRuntime,
)
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
)
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_files import (
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentModelTurnRecord,
    ModelPlannerNodeBindingRecord,
    TaskExecutionNodeRecord,
    TaskLoopCapabilityApprovalRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    WorkspaceCodingDeliveryRecord,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_multi_step_plan_runtime import (  # noqa: E402
    ScriptedTurnPlannerProvider,
    _runtimes,
    _seed_turn,
)
from test_stage116b_workspace_coding_loop import (  # noqa: E402
    _initialize_repository,
    _PassThroughContextMemory,
)

_FILE_NAMES = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
)


def _node_source(name: str, state: str) -> str:
    return f"export const VALUE = '{state}-{name}';"


def _bounded_node_parameters(file_count: int) -> tuple[str, ...]:
    paths = ["primary_path", "secondary_path"]
    paths.extend(f"file_{index:02d}_path" for index in range(3, file_count + 1))
    return (*paths, "changes_json", "project_path", "test_path")


def _select_bounded_node_loop(request: Any, *, file_count: int) -> dict[str, Any]:
    payload = json.loads(request.messages[1].content)
    parameter_names = set(_bounded_node_parameters(file_count))
    matches = [
        item["offer"]["offer_key"]
        for item in payload["offers"]
        if {spec["parameter_name"] for spec in item["parameter_specs"]}
        == parameter_names
    ]
    assert len(matches) == 1
    paths = [
        f"node_project/src/{name}.js" for name in _FILE_NAMES[:file_count]
    ]
    changes = [
        {
            "path": path,
            "old_text": _node_source(name, "old"),
            "new_text": _node_source(name, "new"),
        }
        for name, path in zip(_FILE_NAMES[:file_count], paths, strict=True)
    ]
    parameters = [
        {"name": parameter_name, "value": path}
        for parameter_name, path in zip(
            _bounded_node_parameters(file_count)[:file_count],
            paths,
            strict=True,
        )
    ]
    parameters.extend(
        (
            {
                "name": "changes_json",
                "value": json.dumps(changes, separators=(",", ":")),
            },
            {"name": "project_path", "value": "node_project"},
            {"name": "test_path", "value": "tests/coding.test.js"},
        )
    )
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": str(matches[0]),
                "parameters": parameters,
            }
        ],
    }


def _confirm_bounded_graph(request: Any, *, file_count: int) -> dict[str, Any]:
    nodes = request.metadata["task_graph_allowed_capabilities"]
    assert isinstance(nodes, list) and len(nodes) == 2 * file_count + 3
    return {
        "schema_version": "deskpilot.agent-decision.v2",
        "kind": "propose_task_graph",
        "nodes": nodes,
        "output_node_key": "commit_git",
        "decision_summary": "Confirm the exact server-sealed bounded coding graph.",
    }


def _propose_node_patch(
    request: Any,
    *,
    reject_name: str | None = None,
    unknown_name: str | None = None,
) -> dict[str, Any]:
    path = str(request.metadata["workspace_path"])
    name = Path(path).stem
    if name == unknown_name:
        return {"schema_version": "deskpilot.invalid-agent-decision.v1"}
    new_text = _node_source(name, "new")
    if name == reject_name:
        new_text = f"{new_text} unauthorized drift"
    return {
        "schema_version": "deskpilot.agent-decision.v1",
        "kind": "submit_result",
        "patch_binding_id": request.metadata["workspace_patch_binding_id"],
        "observation_digest": request.metadata["observation_digest"],
        "changes": [
            {
                "path": path,
                "old_text": _node_source(name, "old"),
                "new_text": new_text,
                "rationale": "Apply the exact server-offered replacement.",
            }
        ],
        "decision_summary": "Exact unprivileged patch proposal.",
    }


class _PassingNodeRuntime:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def run(self, snapshot: WorkspaceNodeTestSnapshot) -> WorkspaceNodeTestRead:
        self.calls += 1
        material: dict[str, object] = {
            "schema_version": "deskpilot.workspace-node-test.v1",
            "profile": "node-test-file",
            "project_path": snapshot.project_path,
            "test_path": snapshot.test_path,
            "snapshot_digest": snapshot.snapshot_digest,
            "runtime_digest": "e" * 64,
            "status": "passed",
            "exit_code": 0,
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "duration_ms": 10,
            "output": "1 passed",
            "output_truncated": False,
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
            "process_limit": 1,
        }
        return WorkspaceNodeTestRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )


def _prepare_node_repository(
    tmp_path: Path,
    *,
    file_count: int,
) -> tuple[WorkspaceFileRuntime, Path, tuple[str, ...], str]:
    workspace_root = tmp_path / "workspace"
    project = workspace_root / "node_project"
    source = project / "src"
    tests = project / "tests"
    source.mkdir(parents=True)
    tests.mkdir(parents=True)
    paths = tuple(
        f"node_project/src/{name}.js" for name in _FILE_NAMES[:file_count]
    )
    for name in _FILE_NAMES[:file_count]:
        (source / f"{name}.js").write_text(
            f"{_node_source(name, 'old')}\n",
            encoding="utf-8",
        )
    (tests / "coding.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "test('coding', () => assert.equal(1, 1));\n",
        encoding="utf-8",
    )
    _initialize_repository(project)
    changes = [
        {
            "path": path,
            "old_text": _node_source(name, "old"),
            "new_text": _node_source(name, "new"),
        }
        for name, path in zip(_FILE_NAMES[:file_count], paths, strict=True)
    ]
    message = (
        f"inspect {' '.join(paths)} apply "
        f"{json.dumps(changes, separators=(',', ':'))} in node_project "
        "then test tests/coding.test.js"
    )
    return (
        WorkspaceFileRuntime(
            str(workspace_root),
            str(tmp_path / "staging"),
        ),
        project,
        paths,
        message,
    )


def _build_harness(
    database: Database,
    provider: ScriptedTurnPlannerProvider,
    workspace: WorkspaceFileRuntime,
    node_tests: _PassingNodeRuntime,
) -> tuple[
    Any,
    Any,
    TaskLoopActivationRuntime,
    Callable[[], TaskLoopAgentRuntime],
    Callable[[], TaskLoopExecutionCoordinator],
]:
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        workspace=workspace,
        node_tests=node_tests,
        workspace_patches=workspace,
        workspace_coding=WorkspaceCodingRuntime(workspace, shutil.which("git")),
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001
        planner._agents,  # noqa: SLF001
        max_parallel=2,
    )
    model_loop = AgentModelLoopRuntime(
        database,
        execution,
        planner._agents,  # noqa: SLF001
        planner._gateway,  # noqa: SLF001
        _PassThroughContextMemory(),  # type: ignore[arg-type]
    )
    adapters = create_task_loop_agent_adapter_registry(
        research_available=False,
        workspace_file_available=True,
        workspace_coding_loop_available=True,
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
            adapters,
        ),
    )
    capabilities = CapabilityExecutionRuntime(
        database,
        CapabilityInputBindingCatalog(planner._capabilities),  # noqa: SLF001
        registry,
        CapabilityExecutionEngine(registry),
    )

    def new_agents() -> TaskLoopAgentRuntime:
        return TaskLoopAgentRuntime(
            database,
            execution,
            adapters,
            workspace=workspace,
            model_loop=model_loop,
        )

    def new_coordinator() -> TaskLoopExecutionCoordinator:
        return TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=capabilities,
            agents=new_agents(),
            turn_planner=planner,
        )

    return planner, task_loops, activation, new_agents, new_coordinator


async def _plan_bounded_node_task(
    database: Database,
    planner: Any,
    task_loops: Any,
    *,
    file_count: int,
    message: str,
    suffix: str,
) -> str:
    task_id, fallback = await _seed_turn(
        database,
        suffix=suffix,
        message=message,
    )
    await planner.prepare(
        task_id,
        fallback.user_message_id,
        fallback,
        frozenset({f"workspace_coding_loop:node:{file_count}"}),
    )
    interpreted = await planner.interpret(task_id)
    assert interpreted.adjudication is not None
    assert interpreted.adjudication.outcome == "single_step"
    await task_loops.plan(task_id)
    return task_id


@pytest.mark.asyncio
async def test_eight_file_node_loop_recovers_every_parallel_wave_and_delivers(
    tmp_path: Path,
) -> None:
    file_count = 8
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'node-eight.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider(
        [
            partial(_select_bounded_node_loop, file_count=file_count),
            partial(_confirm_bounded_graph, file_count=file_count),
            *[_propose_node_patch for _ in range(file_count)],
        ]
    )
    workspace, project, paths, message = _prepare_node_repository(
        tmp_path,
        file_count=file_count,
    )
    node_tests = _PassingNodeRuntime()
    planner, task_loops, _activation, _new_agents, new_coordinator = _build_harness(
        database,
        provider,
        workspace,
        node_tests,
    )
    try:
        task_id = await _plan_bounded_node_task(
            database,
            planner,
            task_loops,
            file_count=file_count,
            message=message,
            suffix="a",
        )
        coordinator = new_coordinator()
        await coordinator.advance(task_id, "node-eight-activate")

        batch_node_ids: list[str] = []
        delivered = None
        for index in range(100):
            result = await coordinator.advance(task_id, f"node-eight-{index}")
            if result.command.kind == "execute_agent_batch":
                assert len(result.command.node_ids) == 2
                batch_node_ids.extend(result.command.node_ids)
                coordinator = new_coordinator()
            execution = result.read.execution
            if execution is not None and execution.status == "awaiting_user":
                if (
                    result.read.workspace_patch is not None
                    and result.read.workspace_patch.schema_version
                    == "deskpilot.workspace-patch-preview.v1"
                    and result.read.git_commit is None
                ):
                    await coordinator.approve_workspace_patch(
                        task_id,
                        result.read.workspace_patch.confirmation_digest,
                        expected_execution_revision=execution.revision,
                    )
                elif (
                    result.read.git_commit is not None
                    and result.read.git_commit.schema_version
                    == "deskpilot.git-commit-preview.v1"
                ):
                    await coordinator.approve_git_commit(
                        task_id,
                        result.read.git_commit.confirmation_digest,
                        expected_execution_revision=execution.revision,
                    )
            if execution is not None and execution.status == "succeeded":
                delivered = result.read
                break

        assert delivered is not None
        assert delivered.coding_delivery is not None
        assert delivered.coding_delivery.changed_files == tuple(sorted(paths))
        assert delivered.coding_delivery.coordinator_evidence.node_count == 19
        assert delivered.coding_delivery.coordinator_evidence.agent_id == (
            "builtin.workspace_bounded_coordinator"
        )
        assert delivered.coding_delivery.coordinator_evidence.agent_version == "1.1.0"
        assert len(batch_node_ids) == 16
        assert len(set(batch_node_ids)) == 16
        assert node_tests.calls == 1
        assert all(
            (project / "src" / f"{name}.js").read_text(encoding="utf-8")
            == f"{_node_source(name, 'new')}\n"
            for name in _FILE_NAMES
        )

        async with database.session() as session:
            model_turns = tuple(
                (await session.scalars(select(AgentModelTurnRecord))).all()
            )
            results = tuple(
                (await session.scalars(select(TaskLoopVerifiedResultRecord))).all()
            )
            delivery = await session.scalar(select(WorkspaceCodingDeliveryRecord))
        assert len(model_turns) == 9
        assert all(item.status == "succeeded" for item in model_turns)
        assert Counter(item.result_kind for item in results) == Counter(
            {
                "coordination_plan": 1,
                "workspace_file": 8,
                "patch_proposal": 8,
                "patch_receipt": 1,
                "node_test": 1,
                "git_commit": 1,
            }
        )
        assert delivery is not None
        assert delivery.changed_file_count == 8
        assert delivery.manifest["schema_version"] == (
            "deskpilot.workspace-coding-delivery.v3"
        )
        assert delivery.manifest["file_count"] == 8
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_eight_file_restart_rejects_pending_reader_binding_tamper(
    tmp_path: Path,
) -> None:
    file_count = 8
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'node-eight-tamper.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider(
        [
            partial(_select_bounded_node_loop, file_count=file_count),
            partial(_confirm_bounded_graph, file_count=file_count),
        ]
    )
    workspace, _project, _paths, message = _prepare_node_repository(
        tmp_path,
        file_count=file_count,
    )
    node_tests = _PassingNodeRuntime()
    planner, task_loops, _activation, new_agents, new_coordinator = _build_harness(
        database,
        provider,
        workspace,
        node_tests,
    )
    try:
        task_id = await _plan_bounded_node_task(
            database,
            planner,
            task_loops,
            file_count=file_count,
            message=message,
            suffix="b",
        )
        coordinator = new_coordinator()
        await coordinator.advance(task_id, "tamper-activate")
        await coordinator.advance(task_id, "tamper-coordinate")
        await coordinator.advance(task_id, "tamper-coordinate-verify")
        first_batch = await coordinator.advance(task_id, "tamper-read-batch")
        assert first_batch.command.kind == "execute_agent_batch"
        await coordinator.advance(task_id, "tamper-read-verify-1")
        verified = await coordinator.advance(task_id, "tamper-read-verify-2")
        execution = verified.read.execution
        assert execution is not None
        target = next(
            item
            for item in verified.read.nodes
            if item.local_key.endswith("inspect_file_05")
        )

        async with database.session() as session, session.begin():
            binding = await session.scalar(
                select(ModelPlannerNodeBindingRecord).where(
                    ModelPlannerNodeBindingRecord.execution_id
                    == execution.execution_id,
                    ModelPlannerNodeBindingRecord.composite_node_id == target.node_id,
                )
            )
            assert binding is not None
            changed = dict(binding.bound_input_manifest)
            assert changed["file_05_path"] == "node_project/src/five.js"
            changed["file_05_path"] = "node_project/src/unauthorized.js"
            binding.bound_input_manifest = changed

        with pytest.raises(TaskLoopAgentProofRejectedError):
            await new_agents().claim_next(
                execution.execution_id,
                "tampered-reader-worker",
                node_id=target.node_id,
            )

        async with database.session() as session:
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).where(
                            TaskExecutionNodeRecord.run_id == execution.run_id
                        )
                    )
                ).all()
            )
            target_attempts = tuple(
                (
                    await session.scalars(
                        select(TaskLoopNodeAttemptRecord).where(
                            TaskLoopNodeAttemptRecord.node_id == target.node_id
                        )
                    )
                ).all()
            )
            results = tuple(
                (
                    await session.scalars(
                        select(TaskLoopVerifiedResultRecord).where(
                            TaskLoopVerifiedResultRecord.execution_id
                            == execution.execution_id
                        )
                    )
                ).all()
            )
        readers = tuple(item for item in nodes if "inspect_" in item.local_key)
        assert sum(item.status == "verified" for item in readers) == 2
        assert next(item for item in nodes if item.node_id == target.node_id).status == (
            "ready"
        )
        assert target_attempts == ()
        assert Counter(item.result_kind for item in results) == Counter(
            {"coordination_plan": 1, "workspace_file": 2}
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_later_planner_batch_failure_settles_sibling_and_blocks_patch(
    tmp_path: Path,
) -> None:
    file_count = 8
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'node-eight-failure.db').as_posix()}"
    )
    await database.migrate()
    proposal = partial(
        _propose_node_patch,
        reject_name="five",
        unknown_name="six",
    )
    provider = ScriptedTurnPlannerProvider(
        [
            partial(_select_bounded_node_loop, file_count=file_count),
            partial(_confirm_bounded_graph, file_count=file_count),
            *[proposal for _ in range(file_count)],
        ]
    )
    workspace, project, _paths, message = _prepare_node_repository(
        tmp_path,
        file_count=file_count,
    )
    node_tests = _PassingNodeRuntime()
    planner, task_loops, _activation, _new_agents, new_coordinator = _build_harness(
        database,
        provider,
        workspace,
        node_tests,
    )
    try:
        task_id = await _plan_bounded_node_task(
            database,
            planner,
            task_loops,
            file_count=file_count,
            message=message,
            suffix="f",
        )
        coordinator = new_coordinator()
        await coordinator.advance(task_id, "failure-activate")
        rejected = False
        for index in range(80):
            try:
                await coordinator.advance(task_id, f"failure-{index}")
            except TaskLoopAgentProofRejectedError:
                rejected = True
                break
        assert rejected is True

        async with database.session() as session:
            nodes = tuple(
                (await session.scalars(select(TaskExecutionNodeRecord))).all()
            )
            results = tuple(
                (await session.scalars(select(TaskLoopVerifiedResultRecord))).all()
            )
            turns = tuple(
                (await session.scalars(select(AgentModelTurnRecord))).all()
            )
            attempts = tuple(
                (await session.scalars(select(TaskLoopNodeAttemptRecord))).all()
            )
            approvals = tuple(
                (await session.scalars(select(TaskLoopCapabilityApprovalRecord))).all()
            )
            delivery = await session.scalar(select(WorkspaceCodingDeliveryRecord))
        planner_five = next(
            item for item in nodes if item.local_key.endswith("plan_file_05_patch")
        )
        planner_six = next(
            item for item in nodes if item.local_key.endswith("plan_file_06_patch")
        )
        patch = next(item for item in nodes if item.local_key.endswith("apply_patch"))
        planners = tuple(item for item in nodes if "plan_" in item.local_key)
        verified_planner_count = sum(
            item.status == "verified" for item in planners
        )
        planner_five_attempt = next(
            item for item in attempts if item.node_id == planner_five.node_id
        )
        planner_six_attempt = next(
            item for item in attempts if item.node_id == planner_six.node_id
        )
        assert planner_five.status == "failed"
        assert planner_five_attempt.status == "failed"
        assert planner_six.status == "failed"
        assert planner_six_attempt.status == "outcome_unknown"
        assert planner_six_attempt.error_code == "AGENT_MODEL_OUTCOME_UNKNOWN"
        assert verified_planner_count >= 2
        assert patch.status == "pending"
        assert Counter(item.result_kind for item in results)["patch_proposal"] == (
            verified_planner_count
        )
        assert sum(item.status == "failed" for item in turns) == 1
        assert sum(item.status == "outcome_unknown" for item in turns) == 1
        assert sum(item.status == "succeeded" for item in turns) == (
            verified_planner_count + 1
        )
        assert approvals == ()
        assert delivery is None
        assert node_tests.calls == 0
        assert all(
            (project / "src" / f"{name}.js").read_text(encoding="utf-8")
            == f"{_node_source(name, 'old')}\n"
            for name in _FILE_NAMES
        )
    finally:
        await database.dispose()
