from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from deskpilot.application.workspace_coding_runtime import (
    MAX_BATCH_FILES,
    MAX_GIT_OUTPUT_BYTES,
    WorkspaceCodingRuntime,
    WorkspaceGitRejectedError,
)
from deskpilot.application.workspace_file_runtime import (
    WorkspaceFilePathRejectedError,
    WorkspaceFileRuntime,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.coding_tools import GitCommitPreview, ProjectSearchRead


def _runtime(root: Path) -> WorkspaceCodingRuntime:
    return WorkspaceCodingRuntime(WorkspaceFileRuntime(str(root)), shutil.which("git"))


def _git(project: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is unavailable")
    subprocess.run(  # noqa: S603 - test invokes a discovered tool with fixed arguments
        (executable, *arguments),
        cwd=project,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )


def _repository(project: Path) -> None:
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "deskpilot-test@example.invalid")
    _git(project, "config", "user.name", "DeskPilot Test")
    _git(project, "config", "core.autocrlf", "false")
    (project / "README.md").write_text("alpha\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "initial")


def test_project_search_is_recursive_sorted_bounded_and_content_addressed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "b.py").write_bytes(b"needle two\n")
    (project / "src" / "a.py").write_bytes(b"one needle needle\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "hidden.js").write_text("needle\n", encoding="utf-8")
    (project / ".hidden.py").write_text("needle\n", encoding="utf-8")

    result = _runtime(tmp_path).search("project", "needle")

    assert [
        (item.relative_path, item.line, item.column) for item in result.matches
    ] == [
        ("project/src/a.py", 1, 5),
        ("project/src/a.py", 1, 12),
        ("project/src/b.py", 1, 1),
    ]
    assert result.scanned_file_count == 2
    assert result.scanned_byte_count == len(b"one needle needle\nneedle two\n")
    assert result.truncated is False
    with pytest.raises(ValidationError):
        ProjectSearchRead.model_validate(
            {**result.model_dump(mode="json"), "query_digest": "0" * 64}
        )


def test_project_search_rejects_reparse_points_instead_of_following_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    linked = project / "linked"
    linked.mkdir(parents=True)
    (linked / "secret.py").write_text("needle\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    original = runtime._is_reparse_point

    def fake_reparse(path: Path, value: os.stat_result | None = None) -> bool:
        return path.name == "linked" or original(path, value)

    monkeypatch.setattr(runtime, "_is_reparse_point", fake_reparse)
    with pytest.raises(WorkspaceFilePathRejectedError, match="reparse"):
        runtime.search("project", "needle")


def test_project_batch_read_is_project_scoped_sorted_and_bounded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "b.py").write_bytes(b"b\n")
    (project / "a.py").write_bytes(b"a\n")
    (tmp_path / "outside.py").write_text("outside\n", encoding="utf-8")
    runtime = _runtime(tmp_path)

    result = runtime.read_many("project", ("b.py", "a.py"))

    assert [item.relative_path for item in result.files] == [
        "project/a.py",
        "project/b.py",
    ]
    assert result.total_byte_count == 4
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.read_many("project", ("../outside.py",))
    with pytest.raises(WorkspaceFilePathRejectedError, match="unique"):
        runtime.read_many("project", ("a.py", "a.py"))
    with pytest.raises(WorkspaceFilePathRejectedError, match="count"):
        runtime.read_many("project", tuple("a.py" for _ in range(MAX_BATCH_FILES + 1)))


@pytest.mark.parametrize("operation", ["status", "diff", "log"])
def test_git_inspection_uses_only_fixed_read_only_profiles(
    tmp_path: Path,
    operation: str,
) -> None:
    project = tmp_path / "project"
    _repository(project)
    (project / "README.md").write_text("alpha\nbeta\n", encoding="utf-8")

    result = _runtime(tmp_path).inspect_git(project.name, operation)  # type: ignore[arg-type]

    assert result.operation == operation
    assert result.exit_code == 0
    assert result.head_oid is not None
    assert result.hooks_disabled is True
    assert result.external_diff_disabled is True
    assert result.textconv_disabled is True
    assert result.pager_disabled is True
    assert result.optional_locks_disabled is True
    if operation == "status":
        assert "README.md" in result.output
    elif operation == "diff":
        assert "+beta" in result.output
    else:
        assert "initial" in result.output


def test_git_inspection_truncates_output_and_rejects_external_git_directories(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _repository(project)
    (project / "README.md").write_text("x" * (MAX_GIT_OUTPUT_BYTES + 8_192), encoding="utf-8")
    runtime = _runtime(tmp_path)

    result = runtime.inspect_git("project", "diff")

    assert result.output_truncated is True
    assert len(result.output.encode("utf-8")) <= MAX_GIT_OUTPUT_BYTES
    (project / ".git" / "objects" / "info" / "alternates").write_text(
        "C:/external/objects\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceGitRejectedError, match="external object"):
        runtime.inspect_git("project", "status")


def test_git_inspection_rejects_worktree_pointer_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").write_text("gitdir: ../outside\n", encoding="utf-8")

    with pytest.raises(WorkspaceGitRejectedError, match="worktree"):
        _runtime(tmp_path).inspect_git("project", "status")


def test_git_commit_is_exact_hook_free_and_reconcilable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _repository(project)
    (project / "notes.md").write_text("before\n", encoding="utf-8")
    _git(project, "add", "notes.md")
    _git(project, "commit", "-m", "add notes")
    (project / "README.md").write_text("alpha\nafter\n", encoding="utf-8")
    (project / "notes.md").write_text("after\n", encoding="utf-8")
    (project / ".README.md.deskpilot-1111111111111111.backup").write_text(
        "alpha\n", encoding="utf-8"
    )
    (project / ".notes.md.deskpilot-2222222222222222.backup").write_text(
        "before\n", encoding="utf-8"
    )
    hook_marker = tmp_path / "hook-ran"
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf ran > '{hook_marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(project, "config", "commit.gpgSign", "true")
    _git(project, "config", "user.signingKey", "not-a-real-key")
    runtime = _runtime(tmp_path)
    task_id = f"tsk_{'a' * 32}"

    preview = runtime.prepare_git_commit(
        task_id=task_id,
        project_path="project",
        paths=("project/notes.md", "project/README.md"),
    )
    receipt = runtime.commit_git(preview)
    reconciled = runtime.commit_git(preview)

    assert receipt == reconciled
    assert receipt.target_branch == f"codex/deskpilot-{'a' * 16}"
    assert receipt.commit_oid != receipt.expected_head_oid
    assert [item.relative_path for item in receipt.paths] == ["notes.md", "README.md"]
    assert receipt.push_disabled is True
    assert receipt.rollback_available is True
    assert not hook_marker.exists()


@pytest.mark.parametrize("partial_state", ["branch_created", "files_staged"])
def test_git_commit_reconciles_exact_partial_state(
    tmp_path: Path,
    partial_state: str,
) -> None:
    project = tmp_path / "project"
    _repository(project)
    for name in ("a.py", "b.py"):
        (project / name).write_text("before\n", encoding="utf-8")
    _git(project, "add", "a.py", "b.py")
    _git(project, "commit", "-m", "add files")
    for name in ("a.py", "b.py"):
        (project / name).write_text("after\n", encoding="utf-8")
        (project / f".{name}.deskpilot-8888888888888888.backup").write_text(
            "before\n", encoding="utf-8"
        )
    runtime = _runtime(tmp_path)
    preview = runtime.prepare_git_commit(
        task_id=f"tsk_{'e' * 32}",
        project_path="project",
        paths=("project/a.py", "project/b.py"),
    )
    _git(project, "switch", "--create", preview.target_branch)
    if partial_state == "files_staged":
        _git(project, "add", "--", "a.py", "b.py")

    receipt = runtime.commit_git(preview)

    assert receipt.target_branch == preview.target_branch
    assert receipt.expected_head_oid == preview.expected_head_oid
    assert receipt.commit_oid != preview.expected_head_oid


def test_git_commit_rejects_extra_changes_and_post_approval_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _repository(project)
    for name in ("a.py", "b.py", "extra.py"):
        (project / name).write_text("before\n", encoding="utf-8")
    _git(project, "add", "a.py", "b.py", "extra.py")
    _git(project, "commit", "-m", "add files")
    (project / "a.py").write_text("after\n", encoding="utf-8")
    (project / "b.py").write_text("after\n", encoding="utf-8")
    (project / "extra.py").write_text("unexpected\n", encoding="utf-8")
    (project / ".a.py.deskpilot-3333333333333333.backup").write_text(
        "before\n", encoding="utf-8"
    )
    (project / ".b.py.deskpilot-4444444444444444.backup").write_text(
        "before\n", encoding="utf-8"
    )
    runtime = _runtime(tmp_path)

    with pytest.raises(WorkspaceGitRejectedError, match="outside"):
        runtime.prepare_git_commit(
            task_id=f"tsk_{'b' * 32}",
            project_path="project",
            paths=("project/a.py", "project/b.py"),
        )

    _git(project, "restore", "extra.py")
    preview = runtime.prepare_git_commit(
        task_id=f"tsk_{'b' * 32}",
        project_path="project",
        paths=("project/a.py", "project/b.py"),
    )
    wrong_branch = preview.model_dump(mode="json")
    wrong_branch["target_branch"] = f"codex/deskpilot-{'c' * 16}"
    wrong_branch["confirmation_digest"] = sha256_digest(
        {key: value for key, value in wrong_branch.items() if key != "confirmation_digest"}
    )
    with pytest.raises(ValidationError, match="Task authority"):
        GitCommitPreview.model_validate(wrong_branch)
    escaped_backup = preview.model_dump(mode="json")
    escaped_proof = escaped_backup["excluded_backups"][0]
    escaped_proof["relative_path"] = "../outside.backup"
    escaped_proof["proof_digest"] = sha256_digest(
        {key: value for key, value in escaped_proof.items() if key != "proof_digest"}
    )
    escaped_backup["confirmation_digest"] = sha256_digest(
        {key: value for key, value in escaped_backup.items() if key != "confirmation_digest"}
    )
    with pytest.raises(ValidationError, match="beneath its project"):
        GitCommitPreview.model_validate(escaped_backup)
    (project / "a.py").write_text("drift\n", encoding="utf-8")
    with pytest.raises(WorkspaceGitRejectedError, match="content changed"):
        runtime.commit_git(preview)


def test_git_commit_rejects_repository_content_filters(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _repository(project)
    (project / ".gitattributes").write_text("*.py filter=unsafe\n", encoding="utf-8")
    (project / "a.py").write_text("before\n", encoding="utf-8")
    (project / "b.py").write_text("before\n", encoding="utf-8")
    _git(project, "add", ".gitattributes", "a.py", "b.py")
    _git(project, "commit", "-m", "add filtered files")
    (project / "a.py").write_text("after\n", encoding="utf-8")
    (project / "b.py").write_text("after\n", encoding="utf-8")
    (project / ".a.py.deskpilot-5555555555555555.backup").write_text(
        "before\n", encoding="utf-8"
    )
    (project / ".b.py.deskpilot-6666666666666666.backup").write_text(
        "before\n", encoding="utf-8"
    )

    with pytest.raises(WorkspaceGitRejectedError, match="content filters"):
        _runtime(tmp_path).prepare_git_commit(
            task_id=f"tsk_{'c' * 32}",
            project_path="project",
            paths=("project/a.py", "project/b.py"),
        )


def test_git_commit_disables_repository_configured_external_attributes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _repository(project)
    for name in ("a.py", "b.py"):
        (project / name).write_text("before\n", encoding="utf-8")
    _git(project, "add", "a.py", "b.py")
    _git(project, "commit", "-m", "add files")
    attributes = tmp_path / "external.attributes"
    attributes.write_text("*.py filter=unsafe\n", encoding="utf-8")
    _git(project, "config", "core.attributesFile", str(attributes))
    _git(project, "config", "filter.unsafe.required", "true")
    _git(project, "config", "filter.unsafe.clean", "missing-deskpilot-filter-command")
    for name in ("a.py", "b.py"):
        (project / name).write_text("after\n", encoding="utf-8")
        (project / f".{name}.deskpilot-7777777777777777.backup").write_text(
            "before\n", encoding="utf-8"
        )
    runtime = _runtime(tmp_path)

    preview = runtime.prepare_git_commit(
        task_id=f"tsk_{'d' * 32}",
        project_path="project",
        paths=("project/a.py", "project/b.py"),
    )
    receipt = runtime.commit_git(preview)

    assert receipt.target_branch == f"codex/deskpilot-{'d' * 16}"
    assert [item.relative_path for item in receipt.paths] == ["a.py", "b.py"]
