from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update

from deskpilot.agents.builtins import create_builtin_agent_registry
from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.agent_model_loop import (
    AgentModelLoopOutcomeUnknownError,
    AgentModelLoopRuntime,
)
from deskpilot.application.builtin_capability_executors import (
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_catalog import (
    CapabilityCatalog,
    create_builtin_capability_catalog,
)
from deskpilot.application.model_gateway import ModelGateway
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.plan_compilation_service import PlanCompilationService
from deskpilot.application.plan_compiler import PlanCompiler
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationProofRejectedError,
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_agent_runtime import TaskLoopAgentRuntime
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
)
from deskpilot.application.task_workbench_service import TaskWorkbenchService
from deskpilot.application.workspace_coding_change_runtime import (
    WorkspaceCodingChangeConflictError,
    WorkspaceCodingChangeProofRejectedError,
    WorkspaceCodingChangeRuntime,
)
from deskpilot.application.workspace_coding_exploration_binder import (
    WorkspaceCodingExplorationBinder,
    WorkspaceCodingExplorationProofRejectedError,
)
from deskpilot.application.workspace_coding_explorer_runtime import (
    WorkspaceCodingExplorerConflictError,
    WorkspaceCodingExplorerProofRejectedError,
    WorkspaceCodingExplorerRuntime,
)
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import ExecutionRunPage
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.task_workbench import WorkbenchAction
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingExplorationCandidateFile,
    WorkspaceCodingExplorationDecision,
    WorkspaceCodingExplorationSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    ConversationMessageRecord,
    ConversationRecord,
    ModelPlannerNodeBindingRecord,
    TaskContractVersionRecord,
    TaskExecutionRunRecord,
    TaskLoopExecutionRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    WorkspaceAgentResultRecord,
    WorkspaceCodingChangeProposalRecord,
    WorkspaceCodingChangeRunBindingRecord,
    WorkspaceCodingChangeTurnProofRecord,
    WorkspaceCodingExplorationProposalRecord,
    WorkspaceCodingExplorationSnapshotRecord,
    WorkspaceCodingExplorerRunBindingRecord,
    WorkspaceCodingExplorerTurnProofRecord,
    WorkspaceCodingFileSetPlanBindingRecord,
    WorkspaceCodingWritePlanBindingRecord,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)


def _alembic_config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        (Path(__file__).parents[1] / "src/deskpilot/infrastructure/migrations").as_posix(),
    )
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{path.as_posix()}",
    )
    return config


def _write_python_project(root: Path) -> None:
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "a.py").write_text("VALUE_A = 1\n", encoding="utf-8")
    (project / "src" / "b.py").write_text("VALUE_B = 2\n", encoding="utf-8")
    (project / "src" / "c.py").write_text("VALUE_C = 3\n", encoding="utf-8")
    (project / "src" / "ignored.ts").write_text("export {};\n", encoding="utf-8")
    (project / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )


async def _services(
    tmp_path: Path,
) -> tuple[
    Database,
    WorkspaceCodingExplorationBinder,
    PlanCompilationService,
    WorkspaceCodingExplorerRuntime,
]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'exploration.db').as_posix()}")
    await database.migrate()
    binder, planning, explorer = _new_binder(database, tmp_path)
    return database, binder, planning, explorer


def _new_binder(
    database: Database,
    tmp_path: Path,
    *,
    provider: FakeModelProvider | None = None,
) -> tuple[
    WorkspaceCodingExplorationBinder,
    PlanCompilationService,
    WorkspaceCodingExplorerRuntime,
]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    provider = provider or FakeModelProvider()
    agents = create_builtin_agent_registry(
        tools,
        (provider.descriptor,),
    )
    gateway = ModelGateway(
        default_provider_id=provider.descriptor.provider_id,
        policy=ModelGatewayPolicy(
            provider_pricing=(ModelProviderPricing(provider_id=provider.descriptor.provider_id),),
        ),
    )
    gateway.register(provider)
    compiler = PlanCompiler(agents, tools, capabilities)
    planning = PlanCompilationService(
        database,
        compiler,
    )
    coding = WorkspaceCodingRuntime(WorkspaceFileRuntime(str(tmp_path / "workspace")))
    binder = WorkspaceCodingExplorationBinder(
        database,
        coding,
        agents,
        capabilities,
        planning,
        clock=lambda: NOW,
    )
    execution = AgentExecutionRuntime(database, compiler, agents)
    loop = AgentModelLoopRuntime(
        database,
        execution,
        agents,
        gateway,
        _PassThroughContextMemory(),  # type: ignore[arg-type]
    )
    return (
        binder,
        planning,
        WorkspaceCodingExplorerRuntime(
            database,
            binder,
            agents,
            planning,
            execution,
            loop,
        ),
    )


class _PassThroughContextMemory:
    async def build_for_turn(self, *args: object) -> tuple[None, object]:
        return None, args[-1]


class _CountingFailureProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__(failure_message="unknown explorer outcome")
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return await super().complete(request)


class _ChangeOnlyFailureProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.change_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if (
            request.output_schema is not None
            and request.output_schema.name == "workspace_coding_change_decision"
        ):
            self.change_calls += 1
            raise RuntimeError("unknown change proposer outcome")
        return await super().complete(request)


class _NoModelTaskLoops:
    async def get_bundle(self, _task_id: str) -> None:
        return None


class _CountingWorkspaceFileRuntime(WorkspaceFileRuntime):
    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.read_paths: list[str] = []

    def read(self, relative_path: str):  # type: ignore[no-untyped-def]
        self.read_paths.append(relative_path)
        return super().read(relative_path)


def _confirmed_reader_coordinator(
    database: Database,
    binder: WorkspaceCodingExplorationBinder,
    planning: PlanCompilationService,
    explorer: WorkspaceCodingExplorerRuntime,
    workspace: WorkspaceFileRuntime,
) -> tuple[TaskLoopActivationRuntime, TaskLoopExecutionCoordinator]:
    executors = create_builtin_capability_executor_registry(binder._capabilities)  # noqa: SLF001
    adapters = create_task_loop_agent_adapter_registry(
        research_available=False,
        workspace_file_available=True,
    )
    activation = TaskLoopActivationRuntime(
        database,
        _NoModelTaskLoops(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        planning,
        explorer._execution,  # noqa: SLF001
        ModelPlannerNodeBinder(
            binder._agents,  # noqa: SLF001
            executors,
            adapters,
        ),
        workspace_coding_explorations=binder,
        clock=lambda: NOW,
    )
    agents = TaskLoopAgentRuntime(
        database,
        explorer._execution,  # noqa: SLF001
        adapters,
        workspace=workspace,
    )
    return activation, TaskLoopExecutionCoordinator(
        database,
        activation,
        agents=agents,
    )


async def _verified_reader_change_fixture(
    tmp_path: Path,
    *,
    suffix: str,
    provider: FakeModelProvider | None = None,
) -> tuple[
    Database,
    WorkspaceCodingExplorationBinder,
    PlanCompilationService,
    WorkspaceCodingExplorerRuntime,
    WorkspaceCodingChangeRuntime,
    str,
    str,
]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    _write_python_project(workspace_root)
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / f'change-{suffix}.db').as_posix()}"
    )
    await database.migrate()
    binder, planning, explorer = _new_binder(database, tmp_path, provider=provider)
    conversation_id = f"cnv_{suffix * 32}"
    source_task_id, source_message_id = await _seed_task(
        database,
        suffix=suffix,
        conversation_id=conversation_id,
        goal="从项目证据形成无写权限变更提案",
        create_conversation=True,
    )
    snapshot = await binder.prepare(
        task_id=source_task_id,
        user_message_id=source_message_id,
        project_path="project",
        ecosystem="python",
        test_path="tests/test_app.py",
    )
    file_proposal = await explorer.run(snapshot.snapshot_id)
    reader_suffix = chr(ord(suffix) + 1)
    reader_task_id, confirmation_id = await _seed_task(
        database,
        suffix=reader_suffix,
        conversation_id=conversation_id,
        goal=f"确认候选文件集：{file_proposal.proposal_id}",
        create_conversation=False,
    )
    await binder.confirm(
        file_proposal.proposal_id,
        successor_task_id=reader_task_id,
        confirmation_message_id=confirmation_id,
    )
    _activation, coordinator = _confirmed_reader_coordinator(
        database,
        binder,
        planning,
        explorer,
        WorkspaceFileRuntime(str(workspace_root)),
    )
    for index in range(10):
        advanced = await coordinator.advance(reader_task_id, f"fixture-reader-{index}")
        if advanced.read.execution is not None and advanced.read.execution.status == "succeeded":
            break
    else:
        pytest.fail("Fixture Reader TaskLoop did not succeed")
    changes = WorkspaceCodingChangeRuntime(
        database,
        binder,
        binder._agents,  # noqa: SLF001
        binder._capabilities,  # noqa: SLF001
        planning,
        explorer._execution,  # noqa: SLF001
        explorer._model_loop,  # noqa: SLF001
    )
    return (
        database,
        binder,
        planning,
        explorer,
        changes,
        conversation_id,
        reader_task_id,
    )


async def _seed_task(
    database: Database,
    *,
    suffix: str,
    conversation_id: str,
    goal: str,
    create_conversation: bool,
    created_at: datetime = NOW,
) -> tuple[str, str]:
    task_id = f"tsk_{suffix * 32}"
    message_id = f"msg_{suffix * 32}"
    material = {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "role": "user",
        "content": goal,
        "content_ref": None,
        "classification": "internal",
        "created_at": created_at,
    }
    async with database.session() as session, session.begin():
        if create_conversation:
            session.add(
                ConversationRecord(
                    conversation_id=conversation_id,
                    title="Workspace exploration test",
                    created_at=created_at,
                )
            )
        session.add(
            TaskRecord(
                task_id=task_id,
                conversation_id=conversation_id,
                goal=goal,
                status="ready",
                mode="fake",
                privacy_mode="local_only",
                constraints=[],
                last_event_seq=0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.flush()
        session.add(
            ConversationMessageRecord(
                message_id=message_id,
                conversation_id=conversation_id,
                task_id=task_id,
                role="user",
                content=goal,
                content_ref=None,
                classification="internal",
                status="active",
                message_digest=sha256_digest(material),
                created_at=created_at,
                deleted_at=None,
            )
        )
    return task_id, message_id


def _decision(
    snapshot: WorkspaceCodingExplorationSnapshot,
) -> WorkspaceCodingExplorationDecision:
    selected = tuple(item for item in snapshot.files if item.relative_path.startswith("src/"))[:2]
    return WorkspaceCodingExplorationDecision(
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=snapshot.snapshot_digest,
        files=tuple(
            WorkspaceCodingExplorationCandidateFile(
                relative_path=item.relative_path,
                source_file_proof_digest=item.proof_digest,
                rationale=f"检查 {item.relative_path}",
            )
            for item in selected
        ),
        decision_summary="两个相互独立的候选实现文件。",
    )


@pytest.mark.asyncio
async def test_confirmation_atomically_persists_exact_read_only_reader_plan_and_recovers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_project(workspace)
    database, binder, planning, explorer = await _services(tmp_path)
    conversation_id = f"cnv_{'1' * 32}"
    source_task_id, source_message_id = await _seed_task(
        database,
        suffix="1",
        conversation_id=conversation_id,
        goal="分析项目中相关实现文件",
        create_conversation=True,
    )
    try:
        snapshot = await binder.prepare(
            task_id=source_task_id,
            user_message_id=source_message_id,
            project_path="project",
            ecosystem="python",
            test_path="tests/test_app.py",
        )
        assert tuple(item.relative_path for item in snapshot.files) == (
            "src/a.py",
            "src/b.py",
            "src/c.py",
            "tests/test_app.py",
        )
        snapshot_workbench = await binder.get_workbench(source_task_id)
        assert snapshot_workbench is not None
        assert snapshot_workbench.phase == "snapshot_ready"
        assert snapshot_workbench.requires_user_confirmation is False
        proposal = await explorer.run(snapshot.snapshot_id)
        expected_decision = _decision(snapshot)
        assert proposal.decision.snapshot_id == expected_decision.snapshot_id
        assert proposal.decision.snapshot_digest == expected_decision.snapshot_digest
        assert proposal.decision.files == expected_decision.files
        explorer_binding = await explorer.get_binding(snapshot_id=snapshot.snapshot_id)
        assert explorer_binding is not None
        turn_proof = await explorer.get_turn_proof(proposal_id=proposal.proposal_id)
        assert turn_proof.run_binding_id == explorer_binding.binding_id
        proposal_workbench = await binder.get_workbench(source_task_id)
        assert proposal_workbench is not None
        assert proposal_workbench.phase == "proposal_ready"
        assert proposal_workbench.requires_user_confirmation is True
        assert proposal_workbench.explorer_run_id == explorer_binding.run_id
        assert proposal_workbench.explorer_invocation_id == turn_proof.invocation_id
        assert proposal_workbench.explorer_turn_id == turn_proof.turn_id
        assert proposal_workbench.confirmation_text == (f"确认候选文件集：{proposal.proposal_id}")
        confirmation_goal = f"确认候选文件集：{proposal.proposal_id}"
        successor_task_id, confirmation_message_id = await _seed_task(
            database,
            suffix="2",
            conversation_id=conversation_id,
            goal=confirmation_goal,
            create_conversation=False,
        )

        binding = await binder.confirm(
            proposal.proposal_id,
            successor_task_id=successor_task_id,
            confirmation_message_id=confirmation_message_id,
        )

        assert binding.proposal_digest == proposal.proposal_digest
        assert binding.expected_plan.plan_generation == 1
        assert binding.task_contract.max_risk_level.value == "R0"
        assert tuple(item.capability_id for item in binding.task_contract.capabilities) == (
            "workspace.file.read.v1",
        )
        assert tuple(item.relative_path for item in binding.mappings) == (
            "src/a.py",
            "src/b.py",
        )
        nodes = {item.local_key: item for item in binding.expected_plan.nodes}
        assert set(nodes) == {
            "inspect_candidate_01",
            "inspect_candidate_02",
            "final_acceptance",
            "delivery",
        }
        assert nodes["inspect_candidate_01"].depends_on == ()
        assert nodes["inspect_candidate_02"].depends_on == ()
        assert {
            "no_patch_authority",
            "no_test_authority",
            "no_git_authority",
            "no_shell",
        } <= set(binding.task_contract.constraints)
        state = await planning.get_state(successor_task_id)
        assert state.active_plan_generation == 1
        assert state.active_plan_digest == binding.expected_plan_manifest_digest
        source_workbench = await binder.get_workbench(source_task_id)
        successor_workbench = await binder.get_workbench(successor_task_id)
        assert source_workbench == successor_workbench
        assert source_workbench is not None
        assert source_workbench.phase == "confirmed_read_only_plan"
        assert source_workbench.requires_user_confirmation is False
        assert source_workbench.plan_id == binding.expected_plan.plan_id

        restarted, _restarted_planning, restarted_explorer = _new_binder(database, tmp_path)
        recovered = await restarted.get_binding(proposal_id=proposal.proposal_id)
        assert recovered == binding
        assert await restarted_explorer.get_turn_proof(proposal_id=proposal.proposal_id)
        repeated = await restarted.confirm(
            proposal.proposal_id,
            successor_task_id=successor_task_id,
            confirmation_message_id=confirmation_message_id,
        )
        assert repeated == binding
        async with database.session() as session:
            assert (
                await session.scalar(select(func.count()).select_from(TaskPlanGenerationRecord))
                == 2
            )
            for record_type in (
                WorkspaceCodingExplorerRunBindingRecord,
                WorkspaceCodingExplorerTurnProofRecord,
                AgentInvocationRecord,
                AgentModelTurnRecord,
                AgentDecisionRecord,
            ):
                assert await session.scalar(select(func.count()).select_from(record_type)) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_confirmed_reader_plan_uses_persistent_task_loop_and_restart_skips_verified_files(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _write_python_project(workspace_root)
    database, binder, planning, explorer = await _services(tmp_path)
    conversation_id = f"cnv_{'8' * 32}"
    source_task_id, source_message_id = await _seed_task(
        database,
        suffix="8",
        conversation_id=conversation_id,
        goal="确认候选后持久读取，不授予修改权限",
        create_conversation=True,
    )
    workspace = _CountingWorkspaceFileRuntime(str(workspace_root))
    try:
        snapshot = await binder.prepare(
            task_id=source_task_id,
            user_message_id=source_message_id,
            project_path="project",
            ecosystem="python",
            test_path="tests/test_app.py",
        )
        proposal = await explorer.run(snapshot.snapshot_id)
        successor_task_id, confirmation_message_id = await _seed_task(
            database,
            suffix="9",
            conversation_id=conversation_id,
            goal=f"确认候选文件集：{proposal.proposal_id}",
            create_conversation=False,
        )
        binding = await binder.confirm(
            proposal.proposal_id,
            successor_task_id=successor_task_id,
            confirmation_message_id=confirmation_message_id,
        )
        activation, coordinator = _confirmed_reader_coordinator(
            database,
            binder,
            planning,
            explorer,
            workspace,
        )

        pre_execution = await activation.get(successor_task_id)
        assert pre_execution is not None
        assert pre_execution.source_kind == "confirmed_file_set"
        assert pre_execution.execution is None
        assert pre_execution.phase == "plan"
        assert await activation.recoverable_task_ids() == (successor_task_id,)

        activated = await coordinator.advance(successor_task_id, "reader-activator")
        assert activated.command.kind == "activate_plan"
        assert activated.read.execution is not None
        assert activated.read.execution.source_binding_id == binding.binding_id
        async with database.session() as session:
            node_binding_records = tuple(
                (
                    await session.scalars(
                        select(ModelPlannerNodeBindingRecord).where(
                            ModelPlannerNodeBindingRecord.execution_id
                            == activated.read.execution.execution_id
                        )
                    )
                ).all()
            )
        assert len(node_binding_records) == 2
        assert all(item.source_kind == "confirmed_file_set" for item in node_binding_records)
        assert all(item.draft_id is None for item in node_binding_records)
        assert all(item.step_binding_id is None for item in node_binding_records)
        assert all(
            item.workspace_reader_node_proof_manifest is not None for item in node_binding_records
        )

        source_a = workspace_root / "project" / "src" / "a.py"
        source_a_stat = source_a.stat()
        source_a.write_text("VALUE_A = 99\n", encoding="utf-8")
        with pytest.raises(
            TaskLoopActivationProofRejectedError,
            match="Confirmed Reader source proof",
        ):
            await coordinator.advance(successor_task_id, "drifted-reader-worker")
        source_a.write_text("VALUE_A = 1\n", encoding="utf-8")
        os.utime(
            source_a,
            ns=(source_a_stat.st_atime_ns, source_a_stat.st_mtime_ns),
        )

        executed = await coordinator.advance(successor_task_id, "reader-workers")
        assert executed.command.kind == "execute_agent_batch"
        assert len(executed.command.node_ids) == 2
        assert sorted(workspace.read_paths) == [
            "project/src/a.py",
            "project/src/b.py",
        ]

        restarted_activation, restarted = _confirmed_reader_coordinator(
            database,
            binder,
            planning,
            explorer,
            workspace,
        )
        recovered = await restarted_activation.get(successor_task_id)
        assert recovered is not None
        assert recovered.execution == executed.read.execution
        for index in range(8):
            advanced = await restarted.advance(
                successor_task_id,
                f"reader-restart-{index}",
            )
            if advanced.read.execution is not None and (
                advanced.read.execution.status == "succeeded"
            ):
                break
        else:
            pytest.fail("Confirmed Reader Plan did not reach terminal success")

        assert sorted(workspace.read_paths) == [
            "project/src/a.py",
            "project/src/b.py",
        ]
        assert advanced.read.execution is not None
        assert advanced.read.execution.status == "succeeded"
        assert sum(item.verified_result_present for item in advanced.read.nodes) == 2
        assert all(
            item.status == "verified" for item in advanced.read.nodes if item.kind.value == "agent"
        )
        assert await restarted_activation.recoverable_task_ids() == ()
        async with database.session() as session:
            execution_record = await session.scalar(
                select(TaskLoopExecutionRecord).where(
                    TaskLoopExecutionRecord.task_id == successor_task_id
                )
            )
            assert execution_record is not None
            assert execution_record.source_kind == "confirmed_file_set"
            assert execution_record.loop_id is None
            assert execution_record.draft_id is None
            assert execution_record.source_binding_id == binding.binding_id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskExecutionRunRecord)
                    .where(TaskExecutionRunRecord.task_id == successor_task_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskLoopNodeAttemptRecord)
                    .where(TaskLoopNodeAttemptRecord.execution_id == execution_record.execution_id)
                )
                == 2
            )
            attempts = tuple(
                (
                    await session.scalars(
                        select(TaskLoopNodeAttemptRecord).where(
                            TaskLoopNodeAttemptRecord.execution_id == execution_record.execution_id
                        )
                    )
                ).all()
            )
            proof_digests = {
                item.workspace_reader_node_proof_digest for item in node_binding_records
            }
            assert None not in proof_digests
            assert {
                item.input_manifest["workspace_reader_node_proof_digest"] for item in attempts
            } == proof_digests
            assert {
                item.context_manifest["workspace_reader_node_proof_digest"] for item in attempts
            } == proof_digests
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskLoopVerifiedResultRecord)
                    .where(
                        TaskLoopVerifiedResultRecord.execution_id == execution_record.execution_id
                    )
                )
                == 2
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceAgentResultRecord)
                    .where(WorkspaceAgentResultRecord.run_id == execution_record.run_id)
                )
                == 2
            )
        tampered = node_binding_records[0]
        async with database.session() as session, session.begin():
            await session.execute(
                update(ModelPlannerNodeBindingRecord)
                .where(ModelPlannerNodeBindingRecord.node_binding_id == tampered.node_binding_id)
                .values(workspace_reader_node_proof_digest="f" * 64)
            )
        with pytest.raises(
            TaskLoopActivationProofRejectedError,
            match="node binding",
        ):
            await restarted_activation.get(successor_task_id)

        await database.dispose()
        with pytest.raises(
            RuntimeError,
            match=r"DESKPILOT_DOWNGRADE_UNSAFE.*Restore the reviewed stage backup",
        ):
            await asyncio.to_thread(
                command.downgrade,
                _alembic_config(tmp_path / "exploration.db"),
                "0062_workspace_coding_explorer_turns",
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_wrong_confirmation_rolls_back_plan_and_catalog_drift_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_project(workspace)
    database, binder, _planning, explorer = await _services(tmp_path)
    conversation_id = f"cnv_{'3' * 32}"
    source_task_id, source_message_id = await _seed_task(
        database,
        suffix="3",
        conversation_id=conversation_id,
        goal="寻找需要并行调查的文件",
        create_conversation=True,
    )
    try:
        snapshot = await binder.prepare(
            task_id=source_task_id,
            user_message_id=source_message_id,
            project_path="project",
            ecosystem="python",
            test_path="tests/test_app.py",
        )
        proposal = await explorer.run(snapshot.snapshot_id)
        successor_task_id, confirmation_message_id = await _seed_task(
            database,
            suffix="4",
            conversation_id=conversation_id,
            goal="我没有确认这个文件集",
            create_conversation=False,
        )

        with pytest.raises(WorkspaceCodingExplorationProofRejectedError):
            await binder.confirm(
                proposal.proposal_id,
                successor_task_id=successor_task_id,
                confirmation_message_id=confirmation_message_id,
            )
        async with database.session() as session:
            for record_type in (
                TaskPlanningStateRecord,
                TaskContractVersionRecord,
                TaskPlanGenerationRecord,
            ):
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(record_type)
                        .where(record_type.task_id == successor_task_id)
                    )
                    == 0
                )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceCodingFileSetPlanBindingRecord)
                    .where(
                        WorkspaceCodingFileSetPlanBindingRecord.successor_task_id
                        == successor_task_id
                    )
                )
                == 0
            )

        (workspace / "project" / "src" / "a.py").write_text(
            "VALUE_A = 99\n",
            encoding="utf-8",
        )
        with pytest.raises(
            WorkspaceCodingExplorationProofRejectedError,
            match="catalog drifted",
        ):
            await explorer.run(snapshot.snapshot_id)
        with pytest.raises(WorkspaceCodingExplorationProofRejectedError):
            await binder.confirm(
                proposal.proposal_id,
                successor_task_id=successor_task_id,
                confirmation_message_id=confirmation_message_id,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_restart_rejects_cross_table_binding_tamper(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_project(workspace)
    database, binder, _planning, explorer = await _services(tmp_path)
    conversation_id = f"cnv_{'5' * 32}"
    source_task_id, source_message_id = await _seed_task(
        database,
        suffix="5",
        conversation_id=conversation_id,
        goal="准备候选文件集合",
        create_conversation=True,
    )
    try:
        snapshot = await binder.prepare(
            task_id=source_task_id,
            user_message_id=source_message_id,
            project_path="project",
            ecosystem="python",
            test_path="tests/test_app.py",
        )
        proposal = await explorer.run(snapshot.snapshot_id)
        successor_task_id, confirmation_message_id = await _seed_task(
            database,
            suffix="6",
            conversation_id=conversation_id,
            goal=f"确认候选文件集：{proposal.proposal_id}",
            create_conversation=False,
        )
        await binder.confirm(
            proposal.proposal_id,
            successor_task_id=successor_task_id,
            confirmation_message_id=confirmation_message_id,
        )
        turn_proof = await explorer.get_turn_proof(proposal_id=proposal.proposal_id)
        run_binding = await explorer.get_binding(snapshot_id=snapshot.snapshot_id)
        assert run_binding is not None

        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingExplorerTurnProofRecord)
                .where(WorkspaceCodingExplorerTurnProofRecord.proposal_id == proposal.proposal_id)
                .values(model_request_digest="e" * 64)
            )
        with pytest.raises(
            WorkspaceCodingExplorationProofRejectedError,
            match="Invocation/Model Turn proof",
        ):
            await binder.get_proposal(proposal_id=proposal.proposal_id)
        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingExplorerTurnProofRecord)
                .where(WorkspaceCodingExplorerTurnProofRecord.proposal_id == proposal.proposal_id)
                .values(model_request_digest=turn_proof.model_request_digest)
            )

        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingExplorerRunBindingRecord)
                .where(WorkspaceCodingExplorerRunBindingRecord.binding_id == run_binding.binding_id)
                .values(explorer_node_spec_digest="d" * 64)
            )
        with pytest.raises(
            WorkspaceCodingExplorerProofRejectedError,
            match="columns diverged",
        ):
            await explorer.get_binding(binding_id=run_binding.binding_id)
        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingExplorerRunBindingRecord)
                .where(WorkspaceCodingExplorerRunBindingRecord.binding_id == run_binding.binding_id)
                .values(explorer_node_spec_digest=run_binding.explorer_node_spec_digest)
            )

        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingExplorationProposalRecord)
                .where(WorkspaceCodingExplorationProposalRecord.proposal_id == proposal.proposal_id)
                .values(proposal_digest="f" * 64)
            )
        with pytest.raises(
            WorkspaceCodingExplorationProofRejectedError,
            match="columns diverged",
        ):
            await binder.get_binding(proposal_id=proposal.proposal_id)

        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingExplorationSnapshotRecord)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingFileSetPlanBindingRecord)
                )
                == 1
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unverified_ingress_is_rejected_and_unknown_turn_is_not_replayed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_project(workspace)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'explorer-unknown.db').as_posix()}")
    await database.migrate()
    provider = _CountingFailureProvider()
    binder, _planning, explorer = _new_binder(
        database,
        tmp_path,
        provider=provider,
    )
    conversation_id = f"cnv_{'7' * 32}"
    task_id, message_id = await _seed_task(
        database,
        suffix="7",
        conversation_id=conversation_id,
        goal="从持久快照提议候选文件",
        create_conversation=True,
    )
    try:
        snapshot = await binder.prepare(
            task_id=task_id,
            user_message_id=message_id,
            project_path="project",
            ecosystem="python",
            test_path="tests/test_app.py",
        )
        with pytest.raises(
            WorkspaceCodingExplorationProofRejectedError,
            match="verified persistent Invocation/Model Turn",
        ):
            await binder.submit_proposal(snapshot.snapshot_id, _decision(snapshot))
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingExplorationProposalRecord)
                )
                == 0
            )

        with pytest.raises(AgentModelLoopOutcomeUnknownError):
            await explorer.run(snapshot.snapshot_id)
        assert provider.calls == 1
        restarted_binder, _restarted_planning, restarted_explorer = _new_binder(
            database,
            tmp_path,
            provider=provider,
        )
        with pytest.raises(
            WorkspaceCodingExplorerConflictError,
            match="never replayed",
        ):
            await restarted_explorer.run(snapshot.snapshot_id)
        assert provider.calls == 1
        workbench = await restarted_binder.get_workbench(task_id)
        assert workbench is not None
        assert workbench.phase == "explorer_blocked"
        assert workbench.explorer_run_status == "active"
        assert workbench.explorer_turn_status == "outcome_unknown"
        assert workbench.explorer_turn_proof_digest is None
        async with database.session() as session:
            turn = await session.scalar(select(AgentModelTurnRecord))
            assert turn is not None
            assert turn.status == "outcome_unknown"
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingExplorerTurnProofRecord)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingExplorationProposalRecord)
                )
                == 0
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_verified_readers_produce_no_write_proposal_and_confirmed_successor_plan(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _write_python_project(workspace_root)
    database, binder, planning, explorer = await _services(tmp_path)
    conversation_id = f"cnv_{'a' * 32}"
    source_task_id, source_message_id = await _seed_task(
        database,
        suffix="a",
        conversation_id=conversation_id,
        goal="分析并形成有证据的多文件修改提案",
        create_conversation=True,
    )
    workspace = _CountingWorkspaceFileRuntime(str(workspace_root))
    try:
        snapshot = await binder.prepare(
            task_id=source_task_id,
            user_message_id=source_message_id,
            project_path="project",
            ecosystem="python",
            test_path="tests/test_app.py",
        )
        file_proposal = await explorer.run(snapshot.snapshot_id)
        reader_task_id, file_confirmation_id = await _seed_task(
            database,
            suffix="b",
            conversation_id=conversation_id,
            goal=f"确认候选文件集：{file_proposal.proposal_id}",
            create_conversation=False,
        )
        file_binding = await binder.confirm(
            file_proposal.proposal_id,
            successor_task_id=reader_task_id,
            confirmation_message_id=file_confirmation_id,
        )
        activation, coordinator = _confirmed_reader_coordinator(
            database,
            binder,
            planning,
            explorer,
            workspace,
        )
        for index in range(10):
            advanced = await coordinator.advance(reader_task_id, f"reader-{index}")
            if (
                advanced.read.execution is not None
                and advanced.read.execution.status == "succeeded"
            ):
                break
        else:
            pytest.fail("Reader TaskLoop did not succeed")

        changes = WorkspaceCodingChangeRuntime(
            database,
            binder,
            binder._agents,  # noqa: SLF001
            binder._capabilities,  # noqa: SLF001
            planning,
            explorer._execution,  # noqa: SLF001
            explorer._model_loop,  # noqa: SLF001
        )
        before = await changes.get_workbench(reader_task_id)
        assert before is not None
        assert before.phase == "reader_succeeded"
        change_proposal = await changes.run(reader_task_id)
        assert len(change_proposal.decision.changes) == 2
        assert tuple(item.relative_path for item in change_proposal.decision.changes) == (
            "src/a.py",
            "src/b.py",
        )
        assert all(item.old_text != item.new_text for item in change_proposal.decision.changes)
        assert (workspace_root / "project" / "src" / "a.py").read_text(
            encoding="utf-8"
        ) == "VALUE_A = 1\n"
        state = await planning.get_state(reader_task_id)
        assert state.active_contract_version == 2
        assert state.active_plan_generation == 2
        historical_reader = await activation.get(reader_task_id)
        assert historical_reader is not None
        assert historical_reader.execution is not None
        assert historical_reader.execution.status == "succeeded"
        proposal_workbench = await changes.get_workbench(reader_task_id)
        assert proposal_workbench is not None
        assert proposal_workbench.phase == "proposal_ready"
        assert proposal_workbench.confirmation_text == (
            f"确认变更提案：{change_proposal.proposal_id}"
        )
        assert proposal_workbench.requires_user_confirmation is True

        restarted = WorkspaceCodingChangeRuntime(
            database,
            binder,
            binder._agents,  # noqa: SLF001
            binder._capabilities,  # noqa: SLF001
            planning,
            explorer._execution,  # noqa: SLF001
            explorer._model_loop,  # noqa: SLF001
        )
        assert await restarted.run(reader_task_id) == change_proposal
        write_task_id, write_confirmation_id = await _seed_task(
            database,
            suffix="c",
            conversation_id=conversation_id,
            goal=f"确认变更提案：{change_proposal.proposal_id}",
            create_conversation=False,
            created_at=change_proposal.created_at + timedelta(seconds=1),
        )
        write_binding = await restarted.confirm(
            change_proposal.proposal_id,
            successor_task_id=write_task_id,
            confirmation_message_id=write_confirmation_id,
        )
        assert write_binding.expected_plan.plan_generation == 1
        assert write_binding.parameters["project_path"] == "project"
        assert write_binding.parameters["primary_path"] == "project/src/a.py"
        assert write_binding.parameters["secondary_path"] == "project/src/b.py"
        assert json.loads(write_binding.parameters["changes_json"]) == [
            {
                "path": "project/src/a.py",
                "old_text": "VALUE_A = 1",
                "new_text": "VALUE_A = 1 # DeskPilot proposed change",
            },
            {
                "path": "project/src/b.py",
                "old_text": "VALUE_B = 2",
                "new_text": "VALUE_B = 2 # DeskPilot proposed change",
            },
        ]
        assert await restarted.confirm(
            change_proposal.proposal_id,
            successor_task_id=write_task_id,
            confirmation_message_id=write_confirmation_id,
        ) == write_binding
        competing_task_id, competing_confirmation_id = await _seed_task(
            database,
            suffix="d",
            conversation_id=conversation_id,
            goal=f"确认变更提案：{change_proposal.proposal_id}",
            create_conversation=False,
            created_at=change_proposal.created_at + timedelta(seconds=2),
        )
        with pytest.raises(
            WorkspaceCodingChangeConflictError,
            match="different successor Plan",
        ):
            await restarted.confirm(
                change_proposal.proposal_id,
                successor_task_id=competing_task_id,
                confirmation_message_id=competing_confirmation_id,
            )
        successor_workbench = await restarted.get_workbench(write_task_id)
        assert successor_workbench is not None
        assert successor_workbench.phase == "confirmed_write_plan"
        assert successor_workbench.requires_user_confirmation is False
        successor_actions = TaskWorkbenchService._actions(  # noqa: SLF001
            ExecutionRunPage(runs=()),
            await planning.get_current_contract(write_task_id),
            None,
            None,
            False,
            None,
            None,
            None,
            None,
            successor_workbench,
        )
        start_execution = next(
            item for item in successor_actions if item.action is WorkbenchAction.START_EXECUTION
        )
        assert start_execution.enabled is False
        drifted_catalog_runtime = WorkspaceCodingChangeRuntime(
            database,
            binder,
            binder._agents,  # noqa: SLF001
            CapabilityCatalog(()),
            planning,
            explorer._execution,  # noqa: SLF001
            explorer._model_loop,  # noqa: SLF001
        )
        with pytest.raises(
            WorkspaceCodingChangeProofRejectedError,
            match="current Catalog",
        ):
            await drifted_catalog_runtime.get_write_plan_binding(
                successor_task_id=write_task_id
            )
        assert file_binding.expected_plan.plan_generation == 1
        async with database.session() as session:
            for record_type in (
                WorkspaceCodingChangeRunBindingRecord,
                WorkspaceCodingChangeProposalRecord,
                WorkspaceCodingChangeTurnProofRecord,
                WorkspaceCodingWritePlanBindingRecord,
            ):
                assert await session.scalar(select(func.count()).select_from(record_type)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskPlanGenerationRecord)
                    .where(TaskPlanGenerationRecord.task_id == reader_task_id)
                )
                == 2
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskExecutionRunRecord)
                    .where(TaskExecutionRunRecord.task_id == write_task_id)
                )
                == 0
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_change_confirmation_is_fresh_and_failure_rolls_back_successor_plan(
    tmp_path: Path,
) -> None:
    (
        database,
        _binder,
        _planning,
        _explorer,
        changes,
        conversation_id,
        reader_task_id,
    ) = await _verified_reader_change_fixture(tmp_path, suffix="d")
    try:
        proposal = await changes.run(reader_task_id)
        successor_task_id, wrong_message_id = await _seed_task(
            database,
            suffix="f",
            conversation_id=conversation_id,
            goal=f"确认变更提案：{proposal.proposal_id}",
            create_conversation=False,
        )
        with pytest.raises(
            WorkspaceCodingChangeProofRejectedError,
            match="fresh exact same-conversation",
        ):
            await changes.confirm(
                proposal.proposal_id,
                successor_task_id=successor_task_id,
                confirmation_message_id=wrong_message_id,
            )
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskPlanGenerationRecord)
                    .where(TaskPlanGenerationRecord.task_id == successor_task_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingWritePlanBindingRecord)
                )
                == 0
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_change_proposer_unknown_outcome_is_not_replayed_after_restart(
    tmp_path: Path,
) -> None:
    provider = _ChangeOnlyFailureProvider()
    (
        database,
        binder,
        planning,
        explorer,
        changes,
        _conversation_id,
        reader_task_id,
    ) = await _verified_reader_change_fixture(
        tmp_path,
        suffix="1",
        provider=provider,
    )
    try:
        with pytest.raises(AgentModelLoopOutcomeUnknownError):
            await changes.run(reader_task_id)
        assert provider.change_calls == 1
        restarted = WorkspaceCodingChangeRuntime(
            database,
            binder,
            binder._agents,  # noqa: SLF001
            binder._capabilities,  # noqa: SLF001
            planning,
            explorer._execution,  # noqa: SLF001
            explorer._model_loop,  # noqa: SLF001
        )
        with pytest.raises(
            WorkspaceCodingChangeConflictError,
            match="never automatically replayed",
        ):
            await restarted.run(reader_task_id)
        assert provider.change_calls == 1
        workbench = await restarted.get_workbench(reader_task_id)
        assert workbench is not None
        assert workbench.phase == "proposal_blocked"
        assert workbench.turn_status == "outcome_unknown"
        assert reader_task_id not in await restarted.recoverable_task_ids()
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingChangeProposalRecord)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkspaceCodingChangeTurnProofRecord)
                )
                == 0
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_change_proposal_rejects_reader_drift_and_turn_proof_tamper(
    tmp_path: Path,
) -> None:
    (
        database,
        _binder,
        _planning,
        _explorer,
        changes,
        _conversation_id,
        reader_task_id,
    ) = await _verified_reader_change_fixture(tmp_path, suffix="3")
    target = tmp_path / "workspace" / "project" / "src" / "a.py"
    try:
        original = target.read_text(encoding="utf-8")
        original_stat = target.stat()
        target.write_text("VALUE_A = 99\n", encoding="utf-8")
        with pytest.raises(
            WorkspaceCodingChangeProofRejectedError,
            match="Confirmed Reader source proof",
        ):
            await changes.run(reader_task_id)
        target.write_text(original, encoding="utf-8")
        os.utime(
            target,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        proposal = await changes.run(reader_task_id)
        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingChangeTurnProofRecord)
                .where(
                    WorkspaceCodingChangeTurnProofRecord.proposal_id == proposal.proposal_id
                )
                .values(model_request_digest="f" * 64)
            )
        with pytest.raises(
            WorkspaceCodingChangeProofRejectedError,
            match="columns diverged",
        ):
            await changes.get_proposal(proposal_id=proposal.proposal_id)
    finally:
        await database.dispose()
