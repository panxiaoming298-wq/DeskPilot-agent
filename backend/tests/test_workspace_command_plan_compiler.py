from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.application.workspace_command_plan_compiler import (
    WorkspaceCommandPlanCompiler,
    WorkspaceCommandPlanRejectedError,
)
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.domain.workspace_command_plans import WorkspaceCommandPlan

TASK_ID = "tsk_" + "a" * 32


def _compiler(tmp_path: Path) -> WorkspaceCommandPlanCompiler:
    (tmp_path / "project").mkdir()
    return WorkspaceCommandPlanCompiler(
        CommandProfileCatalog(),
        WorkspaceFileRuntime(str(tmp_path)),
    )


def test_compiler_builds_deterministic_content_addressed_python_chain(tmp_path: Path) -> None:
    compiler = _compiler(tmp_path)

    first = compiler.compile(
        task_id=TASK_ID,
        plan_generation=3,
        project_path="project",
        command_profile_ids=(
            "python.ruff.v1",
            "python.mypy.v1",
            "python.pytest.v1",
        ),
    )
    second = compiler.compile(
        task_id=TASK_ID,
        plan_generation=3,
        project_path="project",
        command_profile_ids=(
            "python.ruff.v1",
            "python.mypy.v1",
            "python.pytest.v1",
        ),
    )

    assert first == second
    assert first.request.project_path == "project"
    assert first.request.plan_generation == 3
    assert first.ecosystem == "python"
    assert first.total_timeout_seconds == 480
    assert tuple(step.sequence for step in first.steps) == (1, 2, 3)
    assert first.steps[0].depends_on == ()
    assert first.steps[1].depends_on == (first.steps[0].step_id,)
    assert first.steps[2].depends_on == (first.steps[1].step_id,)
    assert tuple(step.command_profile.command_profile_id for step in first.steps) == (
        "python.ruff.v1",
        "python.mypy.v1",
        "python.pytest.v1",
    )
    assert not first.network_access
    assert first.stop_on_failure
    assert first.temporary_snapshot_per_step
    assert not first.caller_supplies_process_fields

    with pytest.raises(ValidationError):
        WorkspaceCommandPlan.model_validate(
            first.model_copy(update={"plan_digest": "0" * 64}).model_dump()
        )


def test_compiler_normalizes_target_and_rejects_unsafe_or_mixed_selection(
    tmp_path: Path,
) -> None:
    compiler = _compiler(tmp_path)

    normalized = compiler.compile(
        task_id=TASK_ID,
        plan_generation=1,
        project_path=" project ",
        command_profile_ids=("python.pytest.v1",),
    )
    assert normalized.request.project_path == "project"

    with pytest.raises(WorkspaceCommandPlanRejectedError, match="safe project"):
        compiler.compile(
            task_id=TASK_ID,
            plan_generation=1,
            project_path="../outside",
            command_profile_ids=("python.pytest.v1",),
        )
    with pytest.raises(WorkspaceCommandPlanRejectedError, match="selection is invalid"):
        compiler.compile(
            task_id=TASK_ID,
            plan_generation=1,
            project_path="project",
            command_profile_ids=("python.pytest.v1", "python.pytest.v1"),
        )
    with pytest.raises(WorkspaceCommandPlanRejectedError, match="mix"):
        compiler.compile(
            task_id=TASK_ID,
            plan_generation=1,
            project_path="project",
            command_profile_ids=("python.pytest.v1", "node.pnpm_test.v1"),
        )


def test_plan_request_and_compiler_surface_never_accept_process_fields(tmp_path: Path) -> None:
    compiler = _compiler(tmp_path)
    plan = compiler.compile(
        task_id=TASK_ID,
        plan_generation=1,
        project_path="project",
        command_profile_ids=("node.pnpm_typecheck.v1", "node.pnpm_test.v1"),
    )

    input_surface = " ".join(inspect.signature(compiler.compile).parameters)
    request_schema = str(plan.request.model_json_schema())
    serialized_request = plan.request.model_dump_json()
    for forbidden in ("executable", "argv", "cwd", "environment", "shell"):
        assert forbidden not in input_surface
        assert forbidden not in request_schema
        assert forbidden not in serialized_request
