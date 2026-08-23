import hashlib
from pathlib import Path
from threading import Event

import pytest

from deskpilot.application.turn_router import TurnRouter
from deskpilot.application.workspace_file_runtime import (
    WorkspaceFileConflictError,
    WorkspaceFilePathRejectedError,
    WorkspaceFileRuntime,
    WorkspacePatchPartialError,
)
from deskpilot.runner.executor import ToolExecutionContext
from deskpilot.tools.workspace_checks import WorkspaceCheckOutput, execute_workspace_check


def test_workspace_file_read_and_confirmed_replace_keep_backup(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("alpha old omega", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(tmp_path))

    read = runtime.read("notes.md")
    assert read.content == "alpha old omega"
    assert read.byte_count == 15

    preview = runtime.prepare_replace(
        task_id="tsk_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        relative_path="notes.md",
        old_text="old",
        new_text="new",
    )
    assert target.read_text(encoding="utf-8") == "alpha old omega"

    receipt = runtime.commit_replace(preview)
    assert target.read_text(encoding="utf-8") == "alpha new omega"
    backup = tmp_path / receipt.backup_relative_path
    assert backup.read_text(encoding="utf-8") == "alpha old omega"
    assert runtime.commit_replace(preview).receipt_digest == receipt.receipt_digest


def test_workspace_file_boundary_rejects_escape_and_ambiguous_replace(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("same same", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(tmp_path))

    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.read("../outside.md")
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.read(str((tmp_path / "notes.md").resolve()))
    with pytest.raises(WorkspaceFileConflictError):
        runtime.prepare_replace(
            task_id="tsk_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            relative_path="notes.md",
            old_text="same",
            new_text="changed",
        )


def test_workspace_replace_recovers_before_and_after_atomic_boundary(tmp_path: Path) -> None:
    task_id = "tsk_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    runtime = WorkspaceFileRuntime(str(tmp_path))

    staged_target = tmp_path / "staged.md"
    staged_target.write_text("old", encoding="utf-8")
    staged = runtime.prepare_replace(
        task_id=task_id,
        relative_path="staged.md",
        old_text="old",
        new_text="new",
    )
    staged_temp = tmp_path / ".staged.md.deskpilot-0123456789abcdef.replacement"
    runtime._stage_replacement(staged_temp, b"new")
    assert runtime.commit_replace(staged).content_digest
    assert staged_target.read_text(encoding="utf-8") == "new"

    replaced_target = tmp_path / "replaced.md"
    replaced_target.write_text("old", encoding="utf-8")
    replaced = runtime.prepare_replace(
        task_id=task_id,
        relative_path="replaced.md",
        old_text="old",
        new_text="new",
    )
    replacement = tmp_path / ".replaced.md.deskpilot-0123456789abcdef.replacement"
    backup = tmp_path / ".replaced.md.deskpilot-0123456789abcdef.backup"
    runtime._stage_replacement(replacement, b"new")
    runtime._replace_file_windows(replaced_target, replacement, backup)
    recovered = runtime.commit_replace(replaced)
    assert recovered.backup_relative_path == backup.name
    assert backup.read_text(encoding="utf-8") == "old"


def test_turn_router_uses_explicit_workspace_command_grammar() -> None:
    read = TurnRouter.classify("读取工作区文件：README.md")
    assert read.route_id == "workspace_file_read"
    assert read.parameters == {"path": "README.md"}

    edit = TurnRouter.classify('在工作区文件 README.md 中把 "旧文本" 替换为 "新文本"')
    assert edit.route_id == "workspace_file_replace"
    assert edit.parameters == {
        "path": "README.md",
        "old_text": "旧文本",
        "new_text": "新文本",
    }

    patch = TurnRouter.classify(
        '批量修改工作区文件：在工作区文件 a.md 中把 "a" 替换为 "A"；'
        '在工作区文件 b.md 中把 "b" 替换为 "B"'
    )
    assert patch.route_id == "workspace_patch_bundle"
    assert '"path":"a.md"' in patch.parameters["changes_json"]

    agent_patch = TurnRouter.classify(
        '修复并测试工作区：文件："frontend/src/sample.js" '
        'Node项目："frontend" Node测试："tests/sample.test.js" 目标：修复断言'
    )
    assert agent_patch.route_id == "workspace_agent_patch_test"
    assert agent_patch.parameters == {
        "path": "frontend/src/sample.js",
        "project_path": "frontend",
        "test_path": "tests/sample.test.js",
        "test_kind": "node",
        "objective": "修复断言",
    }

    create = TurnRouter.classify('新建工作区文件："notes/todo.md" 内容："第一行\n第二行"')
    assert create.route_id == "workspace_file_create"
    assert create.parameters == {
        "target_path": "notes/todo.md",
        "content": "第一行\n第二行",
    }

    rename = TurnRouter.classify('将工作区文件 "notes/old.md" 重命名为 "notes/new.md"')
    assert rename.route_id == "workspace_file_rename"
    assert rename.parameters == {
        "source_path": "notes/old.md",
        "target_path": "notes/new.md",
    }

    directory = TurnRouter.classify("列出工作区目录：backend/src")
    assert directory.route_id == "workspace_directory_list"
    assert directory.parameters == {"path": "backend/src"}

    check = TurnRouter.classify("运行工作区测试：python-syntax backend/src")
    assert check.route_id == "workspace_snapshot_check"
    assert check.parameters == {"profile": "python-syntax", "path": "backend/src"}

    unclear = TurnRouter.classify("修改工作区文件")
    assert unclear.route_id is None
    assert unclear.reason_code == "WORKSPACE_COMMAND_INVALID"


@pytest.mark.parametrize(
    ("message", "route_id", "parameters"),
    (
        ("帮我看看 README.md", "workspace_file_read", {"path": "README.md"}),
        (
            '把 README.md 里的 "旧文本" 改成 "新文本"',
            "workspace_file_replace",
            {"path": "README.md", "old_text": "旧文本", "new_text": "新文本"},
        ),
        (
            "创建 notes/todo.md，内容是“第一行”",
            "workspace_file_create",
            {"target_path": "notes/todo.md", "content": "第一行"},
        ),
        (
            "把 notes/old.md 改名成 notes/new.md",
            "workspace_file_rename",
            {"source_path": "notes/old.md", "target_path": "notes/new.md"},
        ),
        (
            "看看 backend/src 目录里有什么",
            "workspace_directory_list",
            {"path": "backend/src"},
        ),
        (
            "帮我检查 backend/src 里的 Python 语法",
            "workspace_snapshot_check",
            {"profile": "python-syntax", "path": "backend/src"},
        ),
        (
            "验证 config/settings.json 是不是合法 JSON",
            "workspace_snapshot_check",
            {"profile": "json-parse", "path": "config/settings.json"},
        ),
        (
            "在 backend 里运行 tests/test_sample.py",
            "workspace_python_test",
            {"project_path": "backend", "test_path": "tests/test_sample.py"},
        ),
        (
            "帮我跑一下 frontend 里的 tests/sample.test.js",
            "workspace_node_test",
            {"project_path": "frontend", "test_path": "tests/sample.test.js"},
        ),
        (
            "帮我查一下量子计算，整理成 PDF 报告",
            "research_to_html",
            {"goal": "量子计算"},
        ),
        (
            "给我做一份关于量子计算的 Markdown 报告",
            "research_to_html",
            {"goal": "量子计算"},
        ),
    ),
)
def test_turn_router_extracts_parameters_from_natural_phrasing(
    message: str,
    route_id: str,
    parameters: dict[str, str],
) -> None:
    candidate = TurnRouter.classify(message)
    assert candidate.route_id == route_id
    assert candidate.parameters == parameters


def test_natural_parameter_extraction_keeps_ambiguous_effects_closed() -> None:
    export = TurnRouter.classify("把报告导出到 D:/tmp/report.pdf")
    assert export.route_id is None
    assert export.reason_code == "NO_TRUSTED_ROUTE"

    unbounded_test = TurnRouter.classify("帮我运行 backend 里的所有测试")
    assert unbounded_test.route_id is None
    assert unbounded_test.reason_code == "WORKSPACE_COMMAND_INVALID"


def test_workspace_directory_and_fixed_snapshot_checks_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "valid.py").write_text("value = 1\n", encoding="utf-8")
    (source / "broken.py").write_text("if True print('x')\n", encoding="utf-8")
    (source / "data.json").write_text('{"ok": true}', encoding="utf-8")
    (source / "ignored.bin").write_bytes(b"binary")
    (source / ".secret.py").write_text("secret = True\n", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(tmp_path))

    listed = runtime.list_directory("src")
    assert [item.name for item in listed.entries] == ["broken.py", "data.json", "valid.py"]
    assert listed.truncated is False

    snapshot = runtime.prepare_check("python-syntax", "src")
    assert [item.relative_path for item in snapshot.files] == [
        "src/broken.py",
        "src/valid.py",
    ]
    output = execute_workspace_check(snapshot, Event(), ToolExecutionContext())
    checked = WorkspaceCheckOutput.model_validate(output)
    assert checked.status == "failed"
    assert checked.issues[0].relative_path == "src/broken.py"
    assert checked.issues[0].code == "PYTHON_SYNTAX_INVALID"

    json_snapshot = runtime.prepare_check("json-parse", "src/data.json")
    json_output = WorkspaceCheckOutput.model_validate(
        execute_workspace_check(json_snapshot, Event(), ToolExecutionContext())
    )
    assert json_output.status == "passed"

    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.list_directory("../outside")
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.prepare_check("python-syntax", "src/data.json")


def test_python_test_snapshot_is_project_relative_and_bounded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (project / "tests" / "test_sample.py").write_text(
        "from sample import value\n\ndef test_value():\n    assert value == 1\n",
        encoding="utf-8",
    )
    (project / ".env").write_text("SECRET=not-copied\n", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(tmp_path))

    snapshot = runtime.prepare_python_test("project", "tests/test_sample.py")

    assert snapshot.project_path == "project"
    assert snapshot.test_path == "tests/test_sample.py"
    assert [item.relative_path for item in snapshot.files] == [
        "src/sample.py",
        "tests/test_sample.py",
    ]
    assert all("SECRET" not in item.content for item in snapshot.files)
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.prepare_python_test("project", "src/sample.py")


def test_node_test_snapshot_excludes_dependencies_and_requires_explicit_js(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "node_modules" / "ignored").mkdir(parents=True)
    (project / "src" / "math.js").write_text("exports.add = (a, b) => a + b\n", encoding="utf-8")
    (project / "tests" / "math.test.js").write_text(
        "const test = require('node:test')\n", encoding="utf-8"
    )
    (project / "node_modules" / "ignored" / "index.js").write_text(
        "throw new Error('must not be copied')\n", encoding="utf-8"
    )
    (project / ".env").write_text("SECRET=not-copied\n", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(tmp_path))

    snapshot = runtime.prepare_node_test("project", "tests/math.test.js")

    assert snapshot.profile == "node-test-file"
    assert [item.relative_path for item in snapshot.files] == [
        "src/math.js",
        "tests/math.test.js",
    ]
    assert all("SECRET" not in item.content for item in snapshot.files)
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.prepare_node_test("project", "src/math.js")
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.prepare_node_test("project", "tests/math.test.mjs")


def test_workspace_patch_stages_then_commits_multiple_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    staging = workspace / "internal"
    workspace.mkdir()
    first = workspace / "first.md"
    second = workspace / "second.py"
    first.write_text("alpha old", encoding="utf-8")
    second.write_text("value = 'before'\n", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(workspace), str(staging))
    task_id = "tsk_" + "1" * 64

    preview = runtime.prepare_patch(
        task_id=task_id,
        changes=(
            {"path": "first.md", "old_text": "old", "new_text": "new"},
            {"path": "second.py", "old_text": "before", "new_text": "after"},
        ),
    )
    assert first.read_text(encoding="utf-8") == "alpha old"
    assert second.read_text(encoding="utf-8") == "value = 'before'\n"
    staged = staging / preview.staging_workspace_ref
    assert (staged / "before" / "first.md").read_text(encoding="utf-8") == "alpha old"
    assert (staged / "after" / "first.md").read_text(encoding="utf-8") == "alpha new"
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.read(f"internal/{preview.staging_workspace_ref}/after/first.md")

    receipt = runtime.commit_patch(preview)
    assert receipt.status == "committed"
    assert len(receipt.change_receipts) == 2
    assert first.read_text(encoding="utf-8") == "alpha new"
    assert second.read_text(encoding="utf-8") == "value = 'after'\n"
    assert runtime.commit_patch(preview).receipt_digest == receipt.receipt_digest


def test_workspace_patch_generations_keep_distinct_staging_and_backups(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    workspace.mkdir()
    first = workspace / "first.md"
    second = workspace / "second.py"
    first.write_text("alpha old", encoding="utf-8")
    second.write_text("value = 'old'\n", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(workspace), str(staging))
    task_id = "tsk_" + "a" * 64

    generation_one = runtime.prepare_patch(
        task_id=task_id,
        changes=(
            {"path": "first.md", "old_text": "old", "new_text": "middle"},
            {"path": "second.py", "old_text": "old", "new_text": "middle"},
        ),
    )
    receipt_one = runtime.commit_patch(generation_one)
    generation_two = runtime.prepare_patch(
        task_id=task_id,
        changes=(
            {"path": "first.md", "old_text": "middle", "new_text": "new"},
            {"path": "second.py", "old_text": "middle", "new_text": "new"},
        ),
    )
    receipt_two = runtime.commit_patch(generation_two)

    assert generation_two.staging_workspace_ref != generation_one.staging_workspace_ref
    assert generation_two.confirmation_digest != generation_one.confirmation_digest
    assert {
        item.backup_relative_path for item in receipt_one.change_receipts
    }.isdisjoint(item.backup_relative_path for item in receipt_two.change_receipts)
    assert first.read_text(encoding="utf-8") == "alpha new"
    assert second.read_text(encoding="utf-8") == "value = 'new'\n"
    assert runtime.commit_patch(generation_two).receipt_digest == receipt_two.receipt_digest


def test_workspace_patch_preflight_prevents_writes_and_partial_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    workspace.mkdir()
    first = workspace / "first.md"
    second = workspace / "second.md"
    first.write_text("first old", encoding="utf-8")
    second.write_text("second old", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(workspace), str(staging))
    preview = runtime.prepare_patch(
        task_id="tsk_" + "2" * 64,
        changes=(
            {"path": "first.md", "old_text": "old", "new_text": "new"},
            {"path": "second.md", "old_text": "old", "new_text": "new"},
        ),
    )

    second.write_text("external change", encoding="utf-8")
    with pytest.raises(WorkspaceFileConflictError):
        runtime.commit_patch(preview)
    assert first.read_text(encoding="utf-8") == "first old"

    second.write_text("second old", encoding="utf-8")
    preview = runtime.prepare_patch(
        task_id="tsk_" + "3" * 64,
        changes=(
            {"path": "first.md", "old_text": "old", "new_text": "new"},
            {"path": "second.md", "old_text": "old", "new_text": "new"},
        ),
    )
    original_commit = runtime.commit_replace
    calls = 0

    def fail_second(item: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WorkspaceFileConflictError("simulated concurrent change")
        return original_commit(item)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime, "commit_replace", fail_second)
    with pytest.raises(WorkspacePatchPartialError) as caught:
        runtime.commit_patch(preview)
    assert caught.value.receipt.status == "partial"
    assert first.read_text(encoding="utf-8") == "first new"
    assert second.read_text(encoding="utf-8") == "second old"

    monkeypatch.setattr(runtime, "commit_replace", original_commit)
    recovered = runtime.commit_patch(preview)
    assert recovered.status == "committed"
    assert second.read_text(encoding="utf-8") == "second new"


def test_workspace_create_is_confirmed_non_overwriting_and_recoverable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    (workspace / "notes").mkdir(parents=True)
    runtime = WorkspaceFileRuntime(str(workspace), str(staging))
    task_id = "tsk_" + "4" * 64

    preview = runtime.prepare_create(
        task_id=task_id,
        target_path="notes/new.md",
        content="first\nsecond\n",
    )
    assert not (workspace / "notes" / "new.md").exists()
    receipt = runtime.commit_path_operation(preview)
    assert (workspace / "notes" / "new.md").read_text(encoding="utf-8") == "first\nsecond\n"
    assert runtime.commit_path_operation(preview).receipt_digest == receipt.receipt_digest
    assert runtime._creation_manifest_path(task_id).is_file()

    with pytest.raises(WorkspaceFileConflictError):
        runtime.prepare_create(
            task_id="tsk_" + "5" * 64,
            target_path="notes/new.md",
            content="must not overwrite",
        )
    with pytest.raises(WorkspaceFilePathRejectedError):
        runtime.prepare_create(
            task_id="tsk_" + "6" * 64,
            target_path="missing/new.md",
            content="no implicit directory",
        )

    recovered_preview = runtime.prepare_create(
        task_id="tsk_" + "7" * 64,
        target_path="notes/recovered.md",
        content="recover me",
    )
    staged = runtime._creation_staging_path(recovered_preview.task_id)
    runtime._stage_replacement(staged, b"recover me")
    runtime._ensure_creation_manifest(recovered_preview)
    staged.rename(workspace / "notes" / "recovered.md")
    recovered = runtime.commit_path_operation(recovered_preview)
    assert recovered.target_path == "notes/recovered.md"
    assert recovered.content_digest == hashlib.sha256(b"recover me").hexdigest()


def test_workspace_rename_preserves_identity_and_rejects_stale_or_colliding_targets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    (workspace / "notes").mkdir(parents=True)
    source = workspace / "notes" / "old.md"
    source.write_text("unchanged", encoding="utf-8")
    runtime = WorkspaceFileRuntime(str(workspace), str(staging))

    preview = runtime.prepare_rename(
        task_id="tsk_" + "8" * 64,
        source_path="notes/old.md",
        target_path="notes/new.md",
    )
    receipt = runtime.commit_path_operation(preview)
    assert not source.exists()
    assert (workspace / "notes" / "new.md").read_text(encoding="utf-8") == "unchanged"
    assert receipt.version_digest == preview.expected_source_version_digest
    assert runtime.commit_path_operation(preview).receipt_digest == receipt.receipt_digest

    stale_source = workspace / "notes" / "stale.md"
    stale_source.write_text("before", encoding="utf-8")
    stale = runtime.prepare_rename(
        task_id="tsk_" + "9" * 64,
        source_path="notes/stale.md",
        target_path="notes/stale-new.md",
    )
    stale_source.write_text("after", encoding="utf-8")
    with pytest.raises(WorkspaceFileConflictError):
        runtime.commit_path_operation(stale)

    collision_source = workspace / "notes" / "collision.md"
    collision_source.write_text("source", encoding="utf-8")
    collision = runtime.prepare_rename(
        task_id="tsk_" + "a" * 64,
        source_path="notes/collision.md",
        target_path="notes/collision-new.md",
    )
    (workspace / "notes" / "collision-new.md").write_text("external", encoding="utf-8")
    with pytest.raises(WorkspaceFileConflictError):
        runtime.commit_path_operation(collision)

    recover_source = workspace / "notes" / "recover-old.md"
    recover_source.write_text("recover rename", encoding="utf-8")
    recover = runtime.prepare_rename(
        task_id="tsk_" + "b" * 64,
        source_path="notes/recover-old.md",
        target_path="notes/recover-new.md",
    )
    recover_source.rename(workspace / "notes" / "recover-new.md")
    recovered = runtime.commit_path_operation(recover)
    assert recovered.version_digest == recover.expected_source_version_digest
