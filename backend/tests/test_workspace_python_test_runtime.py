import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.application.workspace_python_test_runtime import WorkspacePythonTestRuntime

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows AppContainer Python test integration test",
)


def test_fixed_pytest_file_runs_from_read_only_networkless_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "sample.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    test_file = project / "tests" / "test_sample.py"
    test_file.write_text(
        "from pathlib import Path\n\n"
        "from sample import add\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_original_workspace_is_not_readable():\n"
        "    try:\n"
        f"        Path({str(project / 'src' / 'sample.py')!r}).read_text()\n"
        "    except OSError:\n"
        "        return\n"
        "    raise AssertionError('original workspace was readable')\n",
        encoding="utf-8",
    )
    files = WorkspaceFileRuntime(str(tmp_path))
    runtime_root = Path(tempfile.gettempdir()) / f"dp82-{uuid4().hex[:8]}"
    tests = WorkspacePythonTestRuntime(
        str(runtime_root),
        str(runtime_root / "profiles.json"),
    )

    try:
        result = tests.run(files.prepare_python_test("project", "tests/test_sample.py"))
    finally:
        if runtime_root.exists():
            shutil.rmtree(runtime_root)

    assert result.status == "passed", result.output
    assert result.passed_count == 2
    assert result.network_access is False
    assert result.isolation_mode == "windows_appcontainer"
    assert result.process_limit == 1
    assert str(tmp_path) not in result.output
    assert not (project / ".pytest_cache").exists()
