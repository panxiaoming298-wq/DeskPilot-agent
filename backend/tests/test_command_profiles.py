from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

from deskpilot.application.command_profile_catalog import (
    CommandProfileCatalog,
    CommandProfileNotFoundError,
)
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_command_runtime import WorkspaceCommandRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.domain.command_profiles import WorkspaceCommandSnapshot
from deskpilot.runner.node_command_runtime import (
    NodeCommandRuntimeIntegrityError,
    prepare_node_command_runtime,
)
from deskpilot.runner.process_isolation import IsolatedProcessCancelledError


def test_catalog_exposes_only_six_fixed_process_free_profiles() -> None:
    catalog = CommandProfileCatalog()

    assert catalog.ids() == (
        "node.pnpm_build.v1",
        "node.pnpm_test.v1",
        "node.pnpm_typecheck.v1",
        "python.mypy.v1",
        "python.pytest.v1",
        "python.ruff.v1",
    )
    assert len({item.profile_digest for item in catalog.list()}) == 6
    assert all(not item.network_access for item in catalog.list())
    assert all(item.temporary_snapshot for item in catalog.list())
    assert all(item.model_selects_only_profile_id for item in catalog.list())
    assert all(not item.caller_supplies_process_fields for item in catalog.list())
    serialized = "".join(item.model_dump_json() for item in catalog.list())
    for forbidden in ("executable", "argv", "cwd", "environment", "shell"):
        assert forbidden not in serialized

    with pytest.raises(CommandProfileNotFoundError):
        catalog.resolve("python.shell.v1")


@pytest.mark.parametrize(
    ("profile_id", "expected_paths"),
    (
        (
            "python.pytest.v1",
            ("pyproject.toml", "src/app.py", "tests/test_app.py"),
        ),
        (
            "node.pnpm_build.v1",
            ("package.json", "pnpm-lock.yaml", "src/app.ts"),
        ),
    ),
)
def test_command_snapshot_is_project_scoped_bounded_and_content_addressed(
    tmp_path: Path,
    profile_id: str,
    expected_paths: tuple[str, ...],
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "node_modules").mkdir()
    (project / "pyproject.toml").write_bytes(b"[tool.pytest.ini_options]\n")
    (project / "src" / "app.py").write_bytes(b"VALUE = 1\n")
    (project / "tests" / "test_app.py").write_bytes(b"def test_value(): assert True\n")
    (project / "package.json").write_bytes(b'{"scripts":{"build":"vite build"}}\n')
    (project / "pnpm-lock.yaml").write_bytes(b"lockfileVersion: '9.0'\n")
    (project / "src" / "app.ts").write_bytes(b"export const value = 1\n")
    (project / "node_modules" / "hidden.js").write_bytes(b"throw new Error()\n")
    runtime = WorkspaceCodingRuntime(WorkspaceFileRuntime(str(tmp_path)))
    profile = CommandProfileCatalog().resolve(profile_id)

    snapshot = runtime.prepare_command_snapshot("project", profile)

    assert tuple(item.relative_path for item in snapshot.files) == expected_paths
    assert snapshot.command_profile == profile
    assert snapshot.project_path == "project"
    assert snapshot.total_byte_count == sum(item.byte_count for item in snapshot.files)
    with pytest.raises(ValidationError):
        WorkspaceCommandSnapshot.model_validate(
            snapshot.model_copy(update={"snapshot_digest": "0" * 64}).model_dump()
        )


def test_cancelled_command_produces_content_addressed_receipt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_bytes(b"VALUE = 1\n")
    profile = CommandProfileCatalog().resolve("python.pytest.v1")
    snapshot = WorkspaceCodingRuntime(
        WorkspaceFileRuntime(str(tmp_path))
    ).prepare_command_snapshot("project", profile)
    cancellation = Event()
    cancellation.set()

    class CancelledLauncher:
        @staticmethod
        def run(**values: object) -> object:
            signal = values["cancellation"]
            assert isinstance(signal, Event)
            assert signal.is_set()
            raise IsolatedProcessCancelledError(
                "cancelled",
                stdout=b"cancelled output",
            )

    receipt = WorkspaceCommandRuntime(str(tmp_path / "runtime"), "profiles.json")._execute(  # noqa: SLF001
        snapshot,
        "a" * 64,
        tmp_path / "snapshot",
        tmp_path / "toolchain",
        CancelledLauncher(),
        ("server-owned.exe",),
        cancellation,
    )

    assert receipt.status == "cancelled"
    assert receipt.exit_code is None
    assert receipt.termination_reason == "cancelled"
    assert receipt.cancellation_receipt_digest is not None
    assert receipt.output_summary == "cancelled output"


@pytest.mark.skipif(os.name != "nt", reason="Windows protected runtime ACL test")
def test_node_command_bundle_tampering_fails_closed(tmp_path: Path) -> None:
    node = tmp_path / "node.exe"
    pnpm = tmp_path / "pnpm-package"
    (pnpm / "bin").mkdir(parents=True)
    (pnpm / "dist").mkdir()
    node.write_bytes(b"fixed-node-fixture")
    (pnpm / "bin" / "pnpm.mjs").write_bytes(b"export {}\n")
    (pnpm / "dist" / "pnpm.mjs").write_bytes(b"export {}\n")

    bundle = prepare_node_command_runtime(tmp_path / "runtime", node, pnpm)
    bundle.harness.write_bytes(bundle.harness.read_bytes() + b"// changed\n")

    with pytest.raises(NodeCommandRuntimeIntegrityError, match="failed verification"):
        prepare_node_command_runtime(tmp_path / "runtime", node, pnpm)
