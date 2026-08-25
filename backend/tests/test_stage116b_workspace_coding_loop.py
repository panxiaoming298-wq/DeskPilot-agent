from __future__ import annotations

import json
import sys
import threading
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
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog, RouteRecipeError
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationProofRejectedError,
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_agent_runtime import (
    TaskLoopAgentConflictError,
    TaskLoopAgentProofRejectedError,
    TaskLoopAgentRuntime,
)
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
)
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_files import (
    WorkspaceFileRead,
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentModelTurnRecord,
    ConversationMessageRecord,
    TaskExecutionNodeRecord,
    TaskLoopCapabilityApprovalRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskRecord,
    TurnRouteRecord,
    WorkspaceCodingAmendmentBindingRecord,
    WorkspaceCodingDeliveryRecord,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_multi_step_plan_runtime import (  # noqa: E402
    ScriptedTurnPlannerProvider,
    _runtimes,
    _seed_turn,
    _unsupported_route,
)


def _coding_loop_offer_key(request: Any) -> str:
    payload = json.loads(request.messages[1].content)
    matches = [
        item["offer"]["offer_key"]
        for item in payload["offers"]
        if {spec["parameter_name"] for spec in item["parameter_specs"]}
        == {
            "primary_path",
            "secondary_path",
            "changes_json",
            "project_path",
            "test_path",
        }
    ]
    assert len(matches) == 1
    return str(matches[0])


def _select_coding_loop(request: Any) -> dict[str, Any]:
    changes_json = json.dumps(
        [
            {
                "path": "backend/one.py",
                "old_text": "VALUE = 'old-one'",
                "new_text": "VALUE = 'new-one'",
            },
            {
                "path": "backend/two.py",
                "old_text": "VALUE = 'old-two'",
                "new_text": "VALUE = 'new-two'",
            },
        ],
        separators=(",", ":"),
    )
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _coding_loop_offer_key(request),
                "parameters": [
                    {"name": "primary_path", "value": "backend/one.py"},
                    {"name": "secondary_path", "value": "backend/two.py"},
                    {"name": "changes_json", "value": changes_json},
                    {"name": "project_path", "value": "backend"},
                    {"name": "test_path", "value": "tests/test_one.py"},
                ],
            }
        ],
    }


def _propose_bound_patch(request: Any) -> dict[str, Any]:
    path = str(request.metadata["workspace_path"])
    changes = {
        "backend/one.py": ("VALUE = 'old-one'", "VALUE = 'new-one'"),
        "backend/two.py": ("VALUE = 'old-two'", "VALUE = 'new-two'"),
    }
    old_text, new_text = changes[path]
    return {
        "schema_version": "deskpilot.agent-decision.v1",
        "kind": "submit_result",
        "patch_binding_id": request.metadata["workspace_patch_binding_id"],
        "observation_digest": request.metadata["observation_digest"],
        "changes": [
            {
                "path": path,
                "old_text": old_text,
                "new_text": new_text,
                "rationale": "Apply the exact server-offered replacement.",
            }
        ],
        "decision_summary": "Exact unprivileged patch proposal.",
    }


def _confirm_bound_coding_graph(request: Any) -> dict[str, Any]:
    nodes = request.metadata["task_graph_allowed_capabilities"]
    assert isinstance(nodes, list) and len(nodes) == 6
    return {
        "schema_version": "deskpilot.agent-decision.v1",
        "kind": "propose_task_graph",
        "nodes": nodes,
        "output_node_key": "run_fixed_test",
        "decision_summary": "Confirm the exact server-sealed coding graph.",
    }


def _tamper_bound_coding_graph(request: Any) -> dict[str, Any]:
    decision = _confirm_bound_coding_graph(request)
    nodes = json.loads(json.dumps(decision["nodes"]))
    nodes[0]["objective"] = f"{nodes[0]['objective']} unauthorized drift"
    return {**decision, "nodes": nodes}


class _PassThroughContextMemory:
    async def build_for_turn(self, *args: Any) -> tuple[None, Any]:
        return None, args[-1]


class _ParallelWorkspace(WorkspaceFileRuntime):
    def __init__(self, workspace_root: str, staging_root: str) -> None:
        super().__init__(workspace_root, staging_root)
        self._reader_barrier = threading.Barrier(2, timeout=5)
        self._call_lock = threading.Lock()
        self.parallel_read_paths: list[str] = []

    def read(self, relative_path: str) -> WorkspaceFileRead:
        if relative_path in {"backend/one.py", "backend/two.py"}:
            with self._call_lock:
                self.parallel_read_paths.append(relative_path)
            self._reader_barrier.wait()
        return super().read(relative_path)


class _RepairingPythonRuntime:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def run(self, snapshot: WorkspacePythonTestSnapshot) -> WorkspacePythonTestRead:
        self.calls += 1
        failed = self.calls == 1
        material: dict[str, object] = {
            "schema_version": "deskpilot.workspace-python-test.v1",
            "profile": "pytest-file",
            "project_path": snapshot.project_path,
            "test_path": snapshot.test_path,
            "snapshot_digest": snapshot.snapshot_digest,
            "runtime_digest": "f" * 64,
            "status": "failed" if failed else "passed",
            "exit_code": 1 if failed else 0,
            "passed_count": 0 if failed else 1,
            "failed_count": 1 if failed else 0,
            "skipped_count": 0,
            "error_count": 0,
            "duration_ms": 10,
            "output": "1 failed" if failed else "1 passed",
            "output_truncated": False,
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
            "process_limit": 1,
        }
        return WorkspacePythonTestRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )


@pytest.mark.asyncio
async def test_coding_loop_parallel_join_patch_repair_restart_and_delivery(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'coding-loop.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider(
        [
            _select_coding_loop,
            _confirm_bound_coding_graph,
            _propose_bound_patch,
            _propose_bound_patch,
        ]
    )
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    workspace_root = tmp_path / "workspace"
    backend = workspace_root / "backend"
    tests = backend / "tests"
    tests.mkdir(parents=True)
    (backend / "one.py").write_text("VALUE = 'old-one'\n", encoding="utf-8")
    (backend / "two.py").write_text("VALUE = 'old-two'\n", encoding="utf-8")
    (tests / "test_one.py").write_text(
        "def test_one():\n    assert True\n",
        encoding="utf-8",
    )
    workspace = _ParallelWorkspace(
        str(workspace_root),
        str(tmp_path / "staging"),
    )
    python_tests = _RepairingPythonRuntime()
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        workspace=workspace,
        python_tests=python_tests,
        workspace_patches=workspace,
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
    agents = TaskLoopAgentRuntime(
        database,
        execution,
        adapters,
        workspace=workspace,
        model_loop=model_loop,
    )
    coordinator = TaskLoopExecutionCoordinator(
        database,
        activation,
        capabilities=capabilities,
        agents=agents,
        turn_planner=planner,
    )
    changes_json = json.dumps(
        [
            {
                "path": "backend/one.py",
                "old_text": "VALUE = 'old-one'",
                "new_text": "VALUE = 'new-one'",
            },
            {
                "path": "backend/two.py",
                "old_text": "VALUE = 'old-two'",
                "new_text": "VALUE = 'new-two'",
            },
        ],
        separators=(",", ":"),
    )
    message = (
        "inspect backend/one.py and backend/two.py apply "
        f"{changes_json} in backend then test tests/test_one.py"
    )
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="b",
            message=message,
        )
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"workspace_coding_loop:python"}),
        )
        interpreted = await planner.interpret(task_id)
        assert interpreted.adjudication is not None
        assert interpreted.adjudication.outcome == "single_step", (
            interpreted.run.status,
            interpreted.run.failure,
            interpreted.binding,
        )
        await task_loops.plan(task_id)
        await coordinator.advance(task_id, "activate-worker")

        coordinated = await coordinator.advance(task_id, "coordinator-worker")
        assert coordinated.command.kind == "execute_agent"
        coordination_verified = await coordinator.advance(
            task_id,
            "coordinator-verifier",
        )
        assert coordination_verified.command.kind == "verify_candidate"

        parallel = await coordinator.advance(task_id, "parallel-reader-worker")
        assert parallel.command.kind == "execute_agent_batch"
        assert len(parallel.command.node_ids) == 2
        assert sorted(workspace.parallel_read_paths) == [
            "backend/one.py",
            "backend/two.py",
        ]
        assert sum(node.candidate_present for node in parallel.read.nodes) == 2

        restarted_agents = TaskLoopAgentRuntime(
            database,
            execution,
            adapters,
            workspace=workspace,
            model_loop=model_loop,
        )
        reader_restart = TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=capabilities,
            agents=restarted_agents,
            turn_planner=planner,
        )
        first_verified = await reader_restart.advance(task_id, "reader-verifier-1")
        second_verified = await reader_restart.advance(task_id, "reader-verifier-2")
        assert first_verified.command.kind == "verify_candidate"
        assert second_verified.command.kind == "verify_candidate"

        planned_patches = await reader_restart.advance(
            task_id,
            "patch-planner-workers",
        )
        assert planned_patches.command.kind == "execute_agent_batch"
        assert len(planned_patches.command.node_ids) == 2
        first_proposal = await reader_restart.advance(
            task_id,
            "patch-proposal-verifier-1",
        )
        second_proposal = await reader_restart.advance(
            task_id,
            "patch-proposal-verifier-2",
        )
        assert first_proposal.command.kind == "verify_candidate"
        assert second_proposal.command.kind == "verify_candidate"

        waiting = await reader_restart.advance(task_id, "patch-prepare-worker")
        assert waiting.command.kind == "execute_capability"
        assert waiting.read.workspace_patch is not None
        assert waiting.read.execution is not None
        assert waiting.read.execution.status == "awaiting_user"

        await coordinator.approve_workspace_patch(
            task_id,
            waiting.read.workspace_patch.confirmation_digest,
            expected_execution_revision=waiting.read.execution.revision,
        )
        restarted = TaskLoopExecutionCoordinator(
            database,
            activation,
            capabilities=capabilities,
            agents=agents,
            turn_planner=planner,
        )
        patched = await restarted.advance(task_id, "patch-commit-worker")
        assert patched.command.kind == "execute_capability"
        assert (backend / "one.py").read_text(encoding="utf-8") == (
            "VALUE = 'new-one'\n"
        )
        assert (backend / "two.py").read_text(encoding="utf-8") == (
            "VALUE = 'new-two'\n"
        )

        failed_test = await restarted.advance(task_id, "test-worker-1")
        assert failed_test.command.kind == "execute_capability"
        assert next(
            node
            for node in failed_test.read.nodes
            if node.local_key.endswith("run_fixed_test")
        ).status == "failed"
        repair = await restarted.advance(task_id, "repair-worker")
        assert repair.command.kind == "start_repair"
        passed_test = await restarted.advance(task_id, "test-worker-2")
        assert passed_test.command.kind == "execute_capability"
        assert python_tests.calls == 2

        final_verify = await restarted.advance(task_id, "final-verifier")
        delivered = await restarted.advance(task_id, "delivery-worker")
        assert final_verify.command.kind == "reduce_control_node"
        assert delivered.command.kind == "reduce_control_node"
        assert delivered.read.execution is not None
        assert delivered.read.execution.status == "succeeded"
        assert delivered.read.coding_delivery is not None
        assert delivered.read.coding_delivery.changed_files == (
            "backend/one.py",
            "backend/two.py",
        )
        public_delivery = delivered.read.workbench.coding_delivery
        assert public_delivery is not None
        public_payload = public_delivery.model_dump(mode="json")
        assert "backup_relative_path" not in json.dumps(public_payload)
        assert [item["status"] for item in public_payload["tests"]] == [
            "failed",
            "passed",
        ]
        assert len(public_payload["patch_planner_evidence"]) == 2
        assert public_payload["coordinator_evidence"] == {
            "agent_id": "builtin.workspace_coordinator",
            "agent_version": "1.1.0",
            "node_count": 6,
            "output_node_key": "run_fixed_test",
            "graph_digest": public_payload["coordinator_evidence"]["graph_digest"],
            "decision_digest": public_payload["coordinator_evidence"][
                "decision_digest"
            ],
            "verification_digest": public_payload["coordinator_evidence"][
                "verification_digest"
            ],
        }

        async with database.session() as session:
            attempts = tuple(
                (
                    await session.scalars(
                        select(TaskLoopNodeAttemptRecord).order_by(
                            TaskLoopNodeAttemptRecord.node_id,
                            TaskLoopNodeAttemptRecord.attempt,
                        )
                    )
                ).all()
            )
            results = tuple(
                (
                    await session.scalars(
                        select(TaskLoopVerifiedResultRecord).order_by(
                            TaskLoopVerifiedResultRecord.created_at
                        )
                    )
                ).all()
            )
            approval = await session.scalar(select(TaskLoopCapabilityApprovalRecord))
            coding_delivery = await session.scalar(
                select(WorkspaceCodingDeliveryRecord)
            )
            model_turns = tuple(
                (await session.scalars(select(AgentModelTurnRecord))).all()
            )
        assert approval is not None and approval.status == "consumed"
        assert coding_delivery is not None
        assert coding_delivery.changed_file_count == 2
        assert coding_delivery.test_run_count == 2
        assert coding_delivery.failure_count == 1
        assert coding_delivery.rollback_available is True
        assert coding_delivery.manifest["changed_files"] == [
            "backend/one.py",
            "backend/two.py",
        ]
        assert coding_delivery.manifest["remaining_risks"] == [
            "local_fake_model_quality_unproven",
            "git_commit_not_created",
        ]
        assert len(model_turns) == 3
        assert all(item.status == "succeeded" for item in model_turns)
        assert sorted(item.result_kind for item in results) == [
            "coordination_plan",
            "patch_proposal",
            "patch_proposal",
            "patch_receipt",
            "python_test",
            "python_test",
            "workspace_file",
            "workspace_file",
        ]
        test_attempts = [
            item for item in attempts if item.node_id == failed_test.command.node_id
        ]
        patch_attempt = next(
            item for item in attempts if item.node_id == patched.command.node_id
        )
        dependency_refs = patch_attempt.input_manifest["dependency_result_refs"]
        assert isinstance(dependency_refs, list) and len(dependency_refs) == 2
        assert [item.status for item in test_attempts] == ["failed", "verified"]
        assert test_attempts[0].receipt_digest is not None
        assert test_attempts[0].receipt_manifest["result_ref_digest"] == next(
            item.result_ref_digest
            for item in results
            if item.attempt_id == test_attempts[0].attempt_id
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_coding_coordinator_rejects_graph_drift_before_readers(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'coordinator-drift.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider(
        [_select_coding_loop, _tamper_bound_coding_graph]
    )
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    workspace_root = tmp_path / "workspace"
    (workspace_root / "backend" / "tests").mkdir(parents=True)
    (workspace_root / "backend" / "one.py").write_text(
        "VALUE = 'old-one'\n",
        encoding="utf-8",
    )
    (workspace_root / "backend" / "two.py").write_text(
        "VALUE = 'old-two'\n",
        encoding="utf-8",
    )
    (workspace_root / "backend" / "tests" / "test_one.py").write_text(
        "def test_one():\n    assert True\n",
        encoding="utf-8",
    )
    workspace = WorkspaceFileRuntime(
        str(workspace_root),
        str(tmp_path / "staging"),
    )
    capability_registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        workspace=workspace,
        python_tests=_RepairingPythonRuntime(),
        workspace_patches=workspace,
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
            capability_registry,
            adapters,
        ),
    )
    agents = TaskLoopAgentRuntime(
        database,
        execution,
        adapters,
        workspace=workspace,
        model_loop=model_loop,
    )
    coordinator = TaskLoopExecutionCoordinator(
        database,
        activation,
        agents=agents,
        turn_planner=planner,
    )
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="d",
            message=(
                "inspect backend/one.py and backend/two.py apply "
                "[{\"path\":\"backend/one.py\",\"old_text\":\"VALUE = 'old-one'\","
                "\"new_text\":\"VALUE = 'new-one'\"},{\"path\":\"backend/two.py\","
                "\"old_text\":\"VALUE = 'old-two'\",\"new_text\":"
                "\"VALUE = 'new-two'\"}] in backend then test tests/test_one.py"
            ),
        )
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"workspace_coding_loop:python"}),
        )
        await planner.interpret(task_id)
        await task_loops.plan(task_id)
        await coordinator.advance(task_id, "activate-worker")
        with pytest.raises(
            TaskLoopAgentProofRejectedError,
            match="server-sealed graph",
        ):
            await coordinator.advance(task_id, "coordinator-worker")

        async with database.session() as session:
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).order_by(
                            TaskExecutionNodeRecord.local_key
                        )
                    )
                ).all()
            )
            turns = tuple(
                (await session.scalars(select(AgentModelTurnRecord))).all()
            )
            results = tuple(
                (await session.scalars(select(TaskLoopVerifiedResultRecord))).all()
            )
        coordinate = next(
            item for item in nodes if item.local_key.endswith("coordinate_coding")
        )
        readers = tuple(
            item
            for item in nodes
            if item.local_key.endswith(("inspect_primary", "inspect_secondary"))
        )
        assert coordinate.status == "failed"
        assert len(readers) == 2 and all(item.status == "pending" for item in readers)
        assert len(turns) == 1 and turns[0].status == "failed"
        assert results == ()
    finally:
        await database.dispose()


def test_coding_loop_recipe_has_two_parallel_readers_and_verified_patch_join() -> None:
    from deskpilot.application.capability_catalog import create_builtin_capability_catalog

    capabilities = create_builtin_capability_catalog()
    contract, draft = RouteRecipeCatalog.compile(
        task_id="tsk_" + "c" * 32,
        route_id="workspace_coding_loop",
        parameters={"test_kind": "python"},
        capabilities=capabilities,
    )
    by_key = {item.local_key: item for item in draft.nodes}
    assert by_key["coordinate_coding"].depends_on == ()
    assert by_key["coordinate_coding"].agent_selector == (
        "builtin.workspace_coordinator"
    )
    assert by_key["inspect_primary"].depends_on == ("coordinate_coding",)
    assert by_key["inspect_secondary"].depends_on == ("coordinate_coding",)
    assert by_key["plan_primary_patch"].depends_on == ("inspect_primary",)
    assert by_key["plan_secondary_patch"].depends_on == ("inspect_secondary",)
    assert set(by_key["apply_patch"].depends_on) == {
        "plan_primary_patch",
        "plan_secondary_patch",
    }
    assert by_key["run_fixed_test"].budget.retries == 1
    assert tuple(
        item.value for item in contract.privacy_policy.allowed_provider_locations
    ) == ("local",)


def test_coding_loop_offers_expose_their_fixed_test_ecosystem() -> None:
    from deskpilot.application.capability_catalog import create_builtin_capability_catalog

    offers = RouteRecipeCatalog.offers_for(
        task_id="tsk_" + "d" * 32,
        capabilities=create_builtin_capability_catalog(),
        eligible_variant_keys=frozenset(
            {"workspace_coding_loop:python", "workspace_coding_loop:node"}
        ),
    )

    descriptions = {
        item.variant_key: RouteRecipeCatalog.intent_description(item) for item in offers
    }
    assert descriptions["workspace_coding_loop:python"].endswith(
        "Fixed test ecosystem: python."
    )
    assert descriptions["workspace_coding_loop:node"].endswith(
        "Fixed test ecosystem: node."
    )


def test_coding_loop_rejects_unread_or_out_of_project_patch_paths() -> None:
    valid_changes = json.dumps(
        [
            {"path": "backend/one.py", "old_text": "one", "new_text": "ONE"},
            {"path": "backend/two.py", "old_text": "two", "new_text": "TWO"},
        ],
        separators=(",", ":"),
    )
    base = {
        "primary_path": "backend/one.py",
        "secondary_path": "backend/two.py",
        "changes_json": valid_changes,
        "project_path": "backend",
        "test_path": "tests/test_one.py",
    }

    def bind(proposed: dict[str, str]) -> None:
        RouteRecipeCatalog.bind_parameters(
            "workspace_coding_loop",
            "\n".join(proposed.values()),
            proposed,
            fixed_parameters={"test_kind": "python"},
        )

    bind(base)
    unread = {
        **base,
        "changes_json": valid_changes.replace("backend/two.py", "backend/three.py"),
    }
    with pytest.raises(RouteRecipeError, match="exactly match both Reader"):
        bind(unread)

    escaped = {
        **base,
        "primary_path": "outside/one.py",
        "changes_json": valid_changes.replace("backend/one.py", "outside/one.py"),
    }
    with pytest.raises(RouteRecipeError, match="stay inside the project"):
        bind(escaped)

    traversal = {**base, "test_path": "../tests/test_one.py"}
    with pytest.raises(RouteRecipeError, match="safe relative path"):
        bind(traversal)


@pytest.mark.asyncio
async def test_coding_loop_amendment_fences_old_claim_and_late_result(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'amendment.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider(
        [_select_coding_loop, _confirm_bound_coding_graph]
    )
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    workspace_root = tmp_path / "workspace"
    (workspace_root / "backend" / "tests").mkdir(parents=True)
    (workspace_root / "backend" / "one.py").write_text(
        "VALUE = 'old-one'\n",
        encoding="utf-8",
    )
    (workspace_root / "backend" / "two.py").write_text(
        "VALUE = 'old-two'\n",
        encoding="utf-8",
    )
    (workspace_root / "backend" / "tests" / "test_one.py").write_text(
        "def test_one():\n    assert True\n",
        encoding="utf-8",
    )
    workspace = WorkspaceFileRuntime(
        str(workspace_root),
        str(tmp_path / "staging"),
    )
    registry = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        workspace=workspace,
        python_tests=_RepairingPythonRuntime(),
        workspace_patches=workspace,
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
    agents = TaskLoopAgentRuntime(
        database,
        execution,
        adapters,
        workspace=workspace,
        model_loop=model_loop,
    )
    coordinator = TaskLoopExecutionCoordinator(
        database,
        activation,
        agents=agents,
        turn_planner=planner,
    )
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="a",
            message=(
                "inspect backend/one.py and backend/two.py apply "
                "[{\"path\":\"backend/one.py\",\"old_text\":\"VALUE = 'old-one'\","
                "\"new_text\":\"VALUE = 'new-one'\"},{\"path\":\"backend/two.py\","
                "\"old_text\":\"VALUE = 'old-two'\",\"new_text\":"
                "\"VALUE = 'new-two'\"}] in backend then test tests/test_one.py"
            ),
        )
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"workspace_coding_loop:python"}),
        )
        await planner.interpret(task_id)
        await task_loops.plan(task_id)
        activated = await activation.activate(task_id)
        active_read = await activation.get(task_id)
        assert active_read is not None
        coordinate = next(
            item
            for item in active_read.nodes
            if item.local_key.endswith("coordinate_coding")
        )
        stale = await agents.claim_next(
            activated.execution_id,
            "old-generation-worker",
            node_id=coordinate.node_id,
        )
        assert stale is not None

        sealed = await coordinator.cancel_for_amendment(task_id)
        assert sealed is not None and sealed.execution is not None
        assert sealed.execution.status == "cancelled"
        assert sealed.recoverable is False
        run = await execution.get(activated.run_id)
        assert run.status.value == "cancelled"
        cancelled_node = next(
            item for item in run.nodes if item.node_id == coordinate.node_id
        )
        assert cancelled_node.status.value == "cancelled"
        assert cancelled_node.claim_fencing_token > stale.claimed.claim_fencing_token
        with pytest.raises(TaskLoopAgentConflictError):
            await agents.run_coding_coordinator_candidate(stale)

        successor_task_id = f"tsk_{'b' * 32}"
        successor_message_id = f"msg_{'b' * 32}"
        successor_message = "revise the same workspace coding task"
        successor_material = {
            "message_id": successor_message_id,
            "conversation_id": fallback.conversation_id,
            "task_id": successor_task_id,
            "role": "user",
            "content": successor_message,
            "content_ref": None,
            "classification": "internal",
            "created_at": fallback.created_at,
        }
        successor_message_digest = sha256_digest(successor_material)
        successor_route = _unsupported_route(
            task_id=successor_task_id,
            conversation_id=fallback.conversation_id,
            message_id=successor_message_id,
            message_digest=successor_message_digest,
        )
        async with database.session() as session, session.begin():
            session.add(
                TaskRecord(
                    task_id=successor_task_id,
                    conversation_id=fallback.conversation_id,
                    goal=successor_message,
                    status="ready",
                    mode="fake",
                    privacy_mode="local_only",
                    constraints=[],
                    last_event_seq=0,
                    created_at=fallback.created_at,
                    updated_at=fallback.updated_at,
                )
            )
            await session.flush()
            session.add(
                ConversationMessageRecord(
                    **successor_material,
                    status="active",
                    message_digest=successor_message_digest,
                    deleted_at=None,
                )
            )
            await session.flush()
            session.add(
                TurnRouteRecord(
                    task_id=successor_task_id,
                    conversation_id=fallback.conversation_id,
                    user_message_id=successor_message_id,
                    decision=successor_route.decision.value,
                    route_id=None,
                    route_version=None,
                    route_manifest_digest=None,
                    candidate_digest=successor_route.candidate_digest,
                    parameters={},
                    parameter_digest=successor_route.parameter_digest,
                    resolved_from_task_id=None,
                    resolution_rule=None,
                    resolution_digest=None,
                    turn_planning_adjudication_id=None,
                    turn_plan_binding_id=None,
                    turn_plan_binding_digest=None,
                    turn_planning_provenance_digest=None,
                    reason_code=successor_route.reason_code,
                    status=successor_route.status.value,
                    result_manifest=None,
                    result_digest=None,
                    error_code=None,
                    revision=1,
                    created_at=fallback.created_at,
                    updated_at=fallback.updated_at,
                )
            )
        binding = await coordinator.bind_conversation_amendment(
            task_id,
            successor_task_id,
        )
        assert binding == await coordinator.bind_conversation_amendment(
            task_id,
            successor_task_id,
        )
        assert binding.source_execution_id == activated.execution_id
        assert binding.source_execution_event_digest == sealed.execution.latest_event_digest
        assert binding.successor_user_message_digest == successor_message_digest
        async with database.session() as session:
            record = await session.scalar(
                select(WorkspaceCodingAmendmentBindingRecord).where(
                    WorkspaceCodingAmendmentBindingRecord.source_execution_id
                    == activated.execution_id
                )
            )
            assert record is not None
            assert record.amendment_digest == binding.amendment_digest
        async with database.session() as session, session.begin():
            message_record = await session.get(
                ConversationMessageRecord,
                successor_message_id,
            )
            assert message_record is not None
            message_record.message_digest = "f" * 64
        with pytest.raises(TaskLoopActivationProofRejectedError):
            await coordinator.bind_conversation_amendment(
                task_id,
                successor_task_id,
            )
    finally:
        await database.dispose()
