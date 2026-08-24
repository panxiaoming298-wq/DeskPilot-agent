import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_command_runtime import WorkspaceCommandRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows AppContainer Command Profile integration test",
)


@pytest.mark.parametrize(
    "profile_id",
    ("python.pytest.v1", "python.ruff.v1", "python.mypy.v1"),
)
def test_python_command_profiles_run_in_disposable_networkless_snapshot(
    tmp_path: Path,
    profile_id: str,
    command_runtime: WorkspaceCommandRuntime,
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "sample.py").write_bytes(
        b"def add(left: int, right: int) -> int:\n    return left + right\n"
    )
    (project / "tests" / "test_sample.py").write_bytes(
        b"def add(left: int, right: int) -> int:\n    return left + right\n\n\n"
        b"def test_add() -> None:\n    assert add(2, 3) == 5\n"
    )
    files = WorkspaceFileRuntime(str(tmp_path))
    coding = WorkspaceCodingRuntime(files)
    profile = CommandProfileCatalog().resolve(profile_id)
    snapshot = coding.prepare_command_snapshot("project", profile)
    result = command_runtime.run(snapshot)

    assert result.status == "passed", result.output_summary
    assert result.exit_code == 0
    assert result.command_profile_id == profile_id
    assert result.profile_digest == profile.profile_digest
    assert result.snapshot_digest == snapshot.snapshot_digest
    assert result.network_access is False
    assert result.temporary_snapshot
    assert result.snapshot_mutations_discarded
    assert result.termination_reason == "completed"
    assert result.cancellation_receipt_digest is None
    assert str(tmp_path) not in result.output_summary
    assert not (project / ".pytest_cache").exists()
    assert not (project / ".mypy_cache").exists()
    assert not (project / ".ruff_cache").exists()


@pytest.mark.parametrize(
    "profile_id",
    ("node.pnpm_test.v1", "node.pnpm_typecheck.v1", "node.pnpm_build.v1"),
)
def test_node_pnpm_profiles_run_frozen_offline_and_discard_snapshot_writes(
    tmp_path: Path,
    profile_id: str,
    command_runtime: WorkspaceCommandRuntime,
) -> None:
    if profile_id not in command_runtime.enabled_profile_ids:
        pytest.skip("Node/pnpm Command Profile runtime is unavailable")
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "scripts").mkdir()
    original_package = project / "package.json"
    original_path = json.dumps(str(original_package))
    original_package.write_text(
        json.dumps(
            {
                "name": "deskpilot-command-profile-fixture",
                "private": True,
                    "packageManager": "pnpm@11.19.0",
                    "scripts": {
                        "test": "node tests/sample.test.js",
                    "type-check": "node scripts/type-check.js",
                    "build": "node scripts/build.js",
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n"
        "settings:\n"
        "  autoInstallPeers: true\n"
        "  excludeLinksFromLockfile: false\n"
        "importers:\n"
        "  .: {}\n",
        encoding="utf-8",
    )
    (project / "tests" / "sample.test.js").write_text(
        "const assert = require('node:assert/strict')\n"
        "const fs = require('node:fs')\n"
        "const test = require('node:test')\n"
        "test('snapshot is isolated', () => "
        f"assert.throws(() => fs.readFileSync({original_path})))\n",
        encoding="utf-8",
    )
    (project / "scripts" / "type-check.js").write_bytes(
        b"process.stdout.write('type-check passed\\n')\n"
    )
    (project / "scripts" / "build.js").write_bytes(
        b"require('node:fs').mkdirSync('dist', { recursive: true })\n"
        b"require('node:fs').writeFileSync('dist/output.txt', 'built')\n"
    )
    profile = CommandProfileCatalog().resolve(profile_id)
    snapshot = WorkspaceCodingRuntime(
        WorkspaceFileRuntime(str(tmp_path))
    ).prepare_command_snapshot("project", profile)

    result = command_runtime.run(snapshot)

    assert result.status == "passed", result.output_summary
    assert result.exit_code == 0
    assert result.command_profile_id == profile_id
    assert result.network_access is False
    assert result.snapshot_mutations_discarded
    assert str(tmp_path) not in result.output_summary
    assert not (project / "node_modules").exists()
    assert not (project / "dist").exists()


@pytest.fixture(scope="module")
def command_runtime() -> Iterator[WorkspaceCommandRuntime]:
    runtime_root = (
        Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
        / "DeskPilot"
        / f"dp113-{uuid4().hex[:8]}"
    )
    runtime = WorkspaceCommandRuntime(
        str(runtime_root),
        str(runtime_root / "profiles.json"),
    )
    try:
        yield runtime
    finally:
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
