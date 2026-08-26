from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from deskpilot.agents.builtins import create_builtin_agent_registry
from deskpilot.application.capability_catalog import (
    create_builtin_capability_catalog,
)
from deskpilot.application.plan_compilation_service import PlanCompilationService
from deskpilot.application.plan_compiler import PlanCompiler
from deskpilot.application.workspace_coding_exploration_binder import (
    WorkspaceCodingExplorationBinder,
    WorkspaceCodingExplorationProofRejectedError,
)
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingExplorationCandidateFile,
    WorkspaceCodingExplorationDecision,
    WorkspaceCodingExplorationSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    ConversationRecord,
    TaskContractVersionRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    WorkspaceCodingExplorationProposalRecord,
    WorkspaceCodingExplorationSnapshotRecord,
    WorkspaceCodingFileSetPlanBindingRecord,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)


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
) -> tuple[Database, WorkspaceCodingExplorationBinder, PlanCompilationService]:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'exploration.db').as_posix()}"
    )
    await database.migrate()
    binder, planning = _new_binder(database, tmp_path)
    return database, binder, planning


def _new_binder(
    database: Database,
    tmp_path: Path,
) -> tuple[WorkspaceCodingExplorationBinder, PlanCompilationService]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(
        tools,
        (FakeModelProvider().descriptor,),
    )
    planning = PlanCompilationService(
        database,
        PlanCompiler(agents, tools, capabilities),
    )
    coding = WorkspaceCodingRuntime(WorkspaceFileRuntime(str(tmp_path / "workspace")))
    return (
        WorkspaceCodingExplorationBinder(
            database,
            coding,
            agents,
            capabilities,
            planning,
            clock=lambda: NOW,
        ),
        planning,
    )


async def _seed_task(
    database: Database,
    *,
    suffix: str,
    conversation_id: str,
    goal: str,
    create_conversation: bool,
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
        "created_at": NOW,
    }
    async with database.session() as session, session.begin():
        if create_conversation:
            session.add(
                ConversationRecord(
                    conversation_id=conversation_id,
                    title="Workspace exploration test",
                    created_at=NOW,
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
                created_at=NOW,
                updated_at=NOW,
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
                created_at=NOW,
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
    database, binder, planning = await _services(tmp_path)
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
        proposal = await binder.submit_proposal(snapshot.snapshot_id, _decision(snapshot))
        proposal_workbench = await binder.get_workbench(source_task_id)
        assert proposal_workbench is not None
        assert proposal_workbench.phase == "proposal_ready"
        assert proposal_workbench.requires_user_confirmation is True
        assert proposal_workbench.confirmation_text == (
            f"确认候选文件集：{proposal.proposal_id}"
        )
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
        assert tuple(
            item.capability_id for item in binding.task_contract.capabilities
        ) == ("workspace.file.read.v1",)
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

        restarted, _restarted_planning = _new_binder(database, tmp_path)
        recovered = await restarted.get_binding(proposal_id=proposal.proposal_id)
        assert recovered == binding
        repeated = await restarted.confirm(
            proposal.proposal_id,
            successor_task_id=successor_task_id,
            confirmation_message_id=confirmation_message_id,
        )
        assert repeated == binding
        async with database.session() as session:
            assert await session.scalar(
                select(func.count()).select_from(TaskPlanGenerationRecord)
            ) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_wrong_confirmation_rolls_back_plan_and_catalog_drift_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_project(workspace)
    database, binder, _planning = await _services(tmp_path)
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
        proposal = await binder.submit_proposal(snapshot.snapshot_id, _decision(snapshot))
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
                WorkspaceCodingFileSetPlanBindingRecord,
            ):
                assert await session.scalar(
                    select(func.count()).select_from(record_type)
                ) == 0

        (workspace / "project" / "src" / "a.py").write_text(
            "VALUE_A = 99\n",
            encoding="utf-8",
        )
        with pytest.raises(
            WorkspaceCodingExplorationProofRejectedError,
            match="catalog drifted",
        ):
            await binder.submit_proposal(snapshot.snapshot_id, _decision(snapshot))
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
    database, binder, _planning = await _services(tmp_path)
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
        proposal = await binder.submit_proposal(snapshot.snapshot_id, _decision(snapshot))
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

        async with database.session() as session, session.begin():
            await session.execute(
                update(WorkspaceCodingExplorationProposalRecord)
                .where(
                    WorkspaceCodingExplorationProposalRecord.proposal_id
                    == proposal.proposal_id
                )
                .values(proposal_digest="f" * 64)
            )
        with pytest.raises(
            WorkspaceCodingExplorationProofRejectedError,
            match="columns diverged",
        ):
            await binder.get_binding(proposal_id=proposal.proposal_id)

        async with database.session() as session:
            assert await session.scalar(
                select(func.count()).select_from(
                    WorkspaceCodingExplorationSnapshotRecord
                )
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(
                    WorkspaceCodingFileSetPlanBindingRecord
                )
            ) == 1
    finally:
        await database.dispose()
