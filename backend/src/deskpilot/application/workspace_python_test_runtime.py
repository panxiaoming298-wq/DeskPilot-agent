"""Read-only AppContainer runtime for one fixed pytest file profile."""

import os
import re
import shutil
import time
from pathlib import Path
from threading import Event, Timer
from uuid import uuid4

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_files import (
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.runner.process_isolation import (
    IsolatedProcessCancelledError,
    IsolationMode,
    IsolationPolicy,
    ProcessIsolationError,
    create_process_launcher,
)
from deskpilot.runner.worker_runtime import (
    WORKER_DISTRIBUTIONS,
    prepare_worker_runtime,
)

PYTEST_RUNTIME_DISTRIBUTIONS = tuple(
    dict.fromkeys(
        WORKER_DISTRIBUTIONS
        + (
            "aio-pika",
            "aiormq",
            "aiosqlite",
            "alembic",
            "annotated-doc",
            "anyio",
            "asyncpg",
            "certifi",
            "click",
            "colorama",
            "fastapi",
            "greenlet",
            "h11",
            "httpcore",
            "httptools",
            "httpx",
            "idna",
            "iniconfig",
            "Mako",
            "MarkupSafe",
            "multidict",
            "opentelemetry-api",
            "opentelemetry-sdk",
            "opentelemetry-semantic-conventions",
            "packaging",
            "pamqp",
            "pluggy",
            "propcache",
            "pydantic-settings",
            "Pygments",
            "pytest",
            "pytest-asyncio",
            "python-dotenv",
            "PyYAML",
            "SQLAlchemy",
            "starlette",
            "uvicorn",
            "watchfiles",
            "websockets",
            "yarl",
        )
    )
)
MAX_TEST_OUTPUT_BYTES = 32_768
PYTEST_HARNESS = """
import os
import sys
import traceback
from pathlib import Path

root = Path.cwd()
target = Path(sys.argv[1])
if target.is_absolute() or ".." in target.parts:
    raise SystemExit(4)
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
sys.path.insert(0, str(root / "src"))
import pytest

try:
    code = pytest.main([
        str(target),
        "-o", "addopts=",
        "--rootdir=.",
        "--confcutdir=.",
        "-q",
        "--tb=short",
        "--maxfail=20",
        "--color=no",
        "--disable-warnings",
        "-p", "no:cacheprovider",
        "-p", "pytest_asyncio.plugin",
    ])
except BaseException:
    print(traceback.format_exc()[-16000:])
    code = 3
raise SystemExit(code)
""".strip()


class WorkspacePythonTestError(RuntimeError):
    code = "WORKSPACE_PYTHON_TEST_FAILED"


class WorkspacePythonTestUnavailableError(WorkspacePythonTestError):
    code = "WORKSPACE_PYTHON_TEST_UNAVAILABLE"


class WorkspacePythonTestTimeoutError(WorkspacePythonTestError):
    code = "WORKSPACE_PYTHON_TEST_TIMEOUT"


class WorkspacePythonTestRuntime:
    def __init__(
        self,
        runtime_root: str,
        profile_journal_path: str,
        *,
        timeout_seconds: float = 60,
    ) -> None:
        self._runtime_root = Path(runtime_root)
        self._snapshot_root = self._runtime_root / ".pytest-snapshots"
        self._temp_root = self._runtime_root / ".pytest-temp"
        self._profile_journal_path = profile_journal_path
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return os.name == "nt"

    def run(self, snapshot: WorkspacePythonTestSnapshot) -> WorkspacePythonTestRead:
        snapshot_root = self._publish_snapshot(snapshot)
        try:
            scratch_root = self._create_scratch()
        except Exception:
            self._remove_snapshot(snapshot_root)
            raise
        try:
            bundle = prepare_worker_runtime(
                self._runtime_root,
                distributions=PYTEST_RUNTIME_DISTRIBUTIONS,
                include_deskpilot=False,
            )
            policy = IsolationPolicy(
                require_windows_sandbox=True,
                require_network_isolation=True,
                memory_limit_bytes=536_870_912,
                active_process_limit=1,
                worker_runtime_bundle=str(bundle.root),
                appcontainer_profile_journal_path=self._profile_journal_path,
                working_directory=str(snapshot_root),
                appcontainer_read_paths=(str(snapshot_root.parent),),
                appcontainer_temp_path=str(scratch_root),
            )
            launcher = create_process_launcher(policy)
            if launcher.mode is not IsolationMode.WINDOWS_APPCONTAINER:
                raise WorkspacePythonTestUnavailableError(
                    "Python tests require AppContainer network isolation"
                )
            probe = (str(bundle.executable), "-I", "-c", "import pytest; print('ready')")
            launcher.validate_command(probe)
            command = (
                str(bundle.executable),
                "-I",
                "-c",
                PYTEST_HARNESS,
                snapshot.test_path,
            )
            cancellation = Event()
            timer = Timer(self._timeout_seconds, cancellation.set)
            started = time.monotonic()
            timer.start()
            try:
                completed = launcher.run(
                    command=command,
                    input_frame=b"",
                    cancellation=cancellation,
                )
            except IsolatedProcessCancelledError as error:
                raise WorkspacePythonTestTimeoutError(
                    "Python test exceeded its time limit"
                ) from error
            except ProcessIsolationError as error:
                raise WorkspacePythonTestUnavailableError(str(error)) from error
            finally:
                timer.cancel()
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            output, output_truncated = self._output(
                completed.stdout,
                completed.stderr,
                snapshot_root,
                bundle.root,
            )
            exit_code = completed.return_code
            status = "passed" if exit_code == 0 else ("failed" if exit_code == 1 else "error")
            counts = {
                name: self._count(output, name)
                for name in ("passed", "failed", "skipped", "error")
            }
            material = {
                "schema_version": "deskpilot.workspace-python-test.v1",
                "profile": "pytest-file",
                "project_path": snapshot.project_path,
                "test_path": snapshot.test_path,
                "snapshot_digest": snapshot.snapshot_digest,
                "runtime_digest": bundle.digest,
                "status": status,
                "exit_code": exit_code,
                "passed_count": counts["passed"],
                "failed_count": counts["failed"],
                "skipped_count": counts["skipped"],
                "error_count": counts["error"],
                "duration_ms": duration_ms,
                "output": output,
                "output_truncated": output_truncated,
                "isolation_mode": "windows_appcontainer",
                "network_access": False,
                "process_limit": 1,
            }
            return WorkspacePythonTestRead.model_validate(
                {**material, "result_digest": sha256_digest(material)}
            )
        except WorkspacePythonTestError:
            raise
        except (OSError, ProcessIsolationError) as error:
            raise WorkspacePythonTestUnavailableError(str(error)) from error
        finally:
            try:
                self._remove_scratch(scratch_root)
            finally:
                self._remove_snapshot(snapshot_root)

    def _publish_snapshot(self, snapshot: WorkspacePythonTestSnapshot) -> Path:
        self._snapshot_root.mkdir(parents=True, exist_ok=True)
        container = self._snapshot_root / f"snapshot-{uuid4().hex}"
        target = container / "workspace"
        target.mkdir(parents=True, exist_ok=False)
        try:
            for item in snapshot.files:
                destination = target.joinpath(*Path(item.relative_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(item.content)
                    stream.flush()
                    os.fsync(stream.fileno())
            return target.resolve(strict=True)
        except Exception:
            self._remove_snapshot(target)
            raise

    def _remove_snapshot(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        expected_parent = self._snapshot_root.resolve(strict=False)
        container = resolved.parent
        if (
            resolved.name != "workspace"
            or container.parent != expected_parent
            or not container.name.startswith("snapshot-")
        ):
            raise WorkspacePythonTestError("Refusing to remove an unexpected test snapshot")
        if container.exists():
            shutil.rmtree(container)

    def _create_scratch(self) -> Path:
        self._temp_root.mkdir(parents=True, exist_ok=True)
        target = self._temp_root / f"temp-{uuid4().hex}"
        target.mkdir(exist_ok=False)
        return target.resolve(strict=True)

    def _remove_scratch(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        expected_parent = self._temp_root.resolve(strict=False)
        if resolved.parent != expected_parent or not resolved.name.startswith("temp-"):
            raise WorkspacePythonTestError("Refusing to remove an unexpected test scratch")
        if resolved.exists():
            shutil.rmtree(resolved)

    @staticmethod
    def _count(output: str, name: str) -> int:
        pattern = rf"(?<!\d)(\d+) {re.escape(name)}(?:s)?\b"
        matches = re.findall(pattern, output, flags=re.IGNORECASE)
        return int(matches[-1]) if matches else 0

    @staticmethod
    def _output(
        stdout: bytes,
        stderr: bytes,
        snapshot_root: Path,
        runtime_root: Path,
    ) -> tuple[str, bool]:
        raw = stdout + (b"\n" if stdout and stderr else b"") + stderr
        text = raw.decode("utf-8", errors="replace")
        replacements = (
            (str(snapshot_root), "<workspace>"),
            (str(runtime_root), "<runtime>"),
            (os.environ.get("USERPROFILE", ""), "<user>"),
        )
        for source, replacement in replacements:
            if source:
                text = text.replace(source, replacement).replace(
                    source.replace("\\", "/"), replacement
                )
        encoded = text.encode("utf-8")
        if len(encoded) <= MAX_TEST_OUTPUT_BYTES:
            return text, False
        marker = b"\n... <output truncated> ...\n"
        half = (MAX_TEST_OUTPUT_BYTES - len(marker)) // 2
        bounded = encoded[:half] + marker + encoded[-half:]
        return bounded.decode("utf-8", errors="ignore"), True


__all__ = [
    "WorkspacePythonTestError",
    "WorkspacePythonTestRuntime",
    "WorkspacePythonTestTimeoutError",
    "WorkspacePythonTestUnavailableError",
]
