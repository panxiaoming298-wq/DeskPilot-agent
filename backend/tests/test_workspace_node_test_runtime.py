import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.application.workspace_node_test_runtime import WorkspaceNodeTestRuntime

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("node") is None,
    reason="Windows AppContainer Node test integration test",
)


def test_fixed_node_test_file_runs_in_networkless_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    source = project / "src" / "math.js"
    source.write_text("exports.add = (left, right) => left + right\n", encoding="utf-8")
    original_path = json.dumps(str(source))
    (project / "tests" / "math.test.js").write_text(
        "const assert = require('node:assert/strict')\n"
        "const fs = require('node:fs')\n"
        "const test = require('node:test')\n"
        "const { add } = require('../src/math.js')\n\n"
        "test('local module works', () => assert.equal(add(2, 3), 5))\n"
        "test('original workspace is hidden', () => {\n"
        f"  assert.throws(() => fs.readFileSync({original_path}, 'utf8'))\n"
        "})\n",
        encoding="utf-8",
    )
    files = WorkspaceFileRuntime(str(tmp_path))
    local_data = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    runtime_root = local_data / "DeskPilot" / f"dp83-{uuid4().hex[:8]}"
    tests = WorkspaceNodeTestRuntime(
        str(runtime_root),
        str(runtime_root / "profiles.json"),
        None,
    )

    try:
        result = tests.run(files.prepare_node_test("project", "tests/math.test.js"))
    finally:
        if runtime_root.exists():
            shutil.rmtree(runtime_root)

    assert result.status == "passed", result.output
    assert result.passed_count == 2
    assert result.failed_count == 0
    assert result.network_access is False
    assert result.isolation_mode == "windows_appcontainer"
    assert result.process_limit == 1
    assert str(tmp_path) not in result.output
