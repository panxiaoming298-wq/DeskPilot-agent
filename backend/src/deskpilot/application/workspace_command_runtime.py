"""Fixed coding Command Profiles in disposable networkless project snapshots."""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sysconfig
import time
from pathlib import Path
from threading import Event, Timer
from uuid import uuid4

from deskpilot.application.workspace_python_test_runtime import PYTEST_RUNTIME_DISTRIBUTIONS
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.command_profiles import (
    CommandProfileId,
    WorkspaceCommandRead,
    WorkspaceCommandSnapshot,
)
from deskpilot.runner.node_command_runtime import (
    NodeCommandRuntimeError,
    prepare_node_command_runtime,
)
from deskpilot.runner.process_isolation import (
    IsolatedProcessCancelledError,
    IsolationMode,
    IsolationPolicy,
    ProcessIsolationError,
    ProcessLauncher,
    create_process_launcher,
)
from deskpilot.runner.worker_runtime import WorkerRuntimeError, prepare_worker_runtime

COMMAND_RUNTIME_DISTRIBUTIONS = tuple(
    dict.fromkeys(
        PYTEST_RUNTIME_DISTRIBUTIONS
        + (
            "mypy",
            "mypy_extensions",
            "librt",
            "pathspec",
            "ruff",
        )
    )
)
MAX_COMMAND_OUTPUT_BYTES = 65_536
PYTEST_PROJECT_HARNESS = """
import os
import sys
from pathlib import Path
import pytest

os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path.cwd() / "src"))
raise SystemExit(pytest.main([
    ".",
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
]))
""".strip()
MYPY_PROJECT_HARNESS = """
from pathlib import Path
from mypy import api

excluded = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", "node_modules"}
paths = [
    item.as_posix()
    for item in sorted(Path.cwd().rglob("*.py"))
    if not any(part in excluded or part.startswith(".") for part in item.parts)
]
if not paths:
    raise SystemExit(2)
stdout, stderr, code = api.run([
    "--strict",
    "--no-incremental",
    "--cache-dir=.deskpilot-mypy-cache",
    "--config-file=NUL",
    "--show-error-codes",
    "--no-color-output",
    *paths,
])
print(stdout, end="")
print(stderr, end="")
raise SystemExit(code)
""".strip()


class WorkspaceCommandError(RuntimeError):
    code = "WORKSPACE_COMMAND_FAILED"


class WorkspaceCommandUnavailableError(WorkspaceCommandError):
    code = "WORKSPACE_COMMAND_UNAVAILABLE"


class WorkspaceCommandRuntime:
    """Execute only server-owned Profiles; no process field crosses this API."""

    def __init__(
        self,
        runtime_root: str,
        profile_journal_path: str,
        node_executable: str | None = None,
        pnpm_executable: str | None = None,
        pnpm_store_path: str | None = None,
    ) -> None:
        self._runtime_root = Path(runtime_root)
        self._snapshot_root = self._runtime_root / ".command-snapshots"
        self._profile_journal_path = profile_journal_path
        node = node_executable or shutil.which("node")
        pnpm = pnpm_executable or shutil.which("pnpm")
        self._node_executable = Path(node).resolve(strict=True) if node else None
        self._pnpm_executable = Path(pnpm).resolve(strict=True) if pnpm else None
        self._pnpm_root = self._resolve_pnpm_root(self._pnpm_executable)
        self._pnpm_store = self._resolve_pnpm_store(
            self._pnpm_executable,
            pnpm_store_path,
        )

    @property
    def enabled_profile_ids(self) -> frozenset[CommandProfileId]:
        if os.name != "nt":
            return frozenset()
        result: set[CommandProfileId] = set()
        if self._ruff_executable() is not None:
            required = ("pytest", "mypy", "ruff")
            try:
                for name in required:
                    importlib.metadata.distribution(name)
            except importlib.metadata.PackageNotFoundError:
                pass
            else:
                result.update(
                    {
                        "python.pytest.v1",
                        "python.ruff.v1",
                        "python.mypy.v1",
                    }
                )
        if (
            self._node_executable is not None
            and self._node_executable.is_file()
            and self._pnpm_root is not None
            and self._pnpm_store is not None
        ):
            result.update(
                {
                    "node.pnpm_test.v1",
                    "node.pnpm_typecheck.v1",
                    "node.pnpm_build.v1",
                }
            )
        return frozenset(result)

    def run(
        self,
        snapshot: WorkspaceCommandSnapshot,
        cancellation: Event | None = None,
    ) -> WorkspaceCommandRead:
        profile = snapshot.command_profile
        if profile.command_profile_id not in self.enabled_profile_ids:
            raise WorkspaceCommandUnavailableError("Command Profile runtime is unavailable")
        snapshot_root = self._publish_snapshot(snapshot)
        try:
            if profile.ecosystem == "python":
                return self._run_python(snapshot, snapshot_root, cancellation)
            return self._run_node(snapshot, snapshot_root, cancellation)
        finally:
            self._remove_snapshot(snapshot_root)

    def _run_node(
        self,
        snapshot: WorkspaceCommandSnapshot,
        snapshot_root: Path,
        cancellation: Event | None,
    ) -> WorkspaceCommandRead:
        if (
            self._node_executable is None
            or self._pnpm_root is None
            or self._pnpm_store is None
        ):
            raise WorkspaceCommandUnavailableError("Node/pnpm Command Profiles are unavailable")
        try:
            bundle = prepare_node_command_runtime(
                self._runtime_root,
                self._node_executable,
                self._pnpm_root,
            )
            policy = IsolationPolicy(
                require_windows_sandbox=True,
                require_network_isolation=True,
                memory_limit_bytes=1_073_741_824,
                active_process_limit=snapshot.command_profile.max_processes,
                worker_runtime_bundle=str(bundle.root),
                appcontainer_profile_journal_path=self._profile_journal_path,
                working_directory=str(snapshot_root),
                appcontainer_read_paths=(str(self._pnpm_store),),
                appcontainer_mirror_workspace=True,
            )
            launcher = create_process_launcher(policy)
            if launcher.mode is not IsolationMode.WINDOWS_APPCONTAINER:
                raise WorkspaceCommandUnavailableError(
                    "Node/pnpm Profiles require AppContainer network isolation"
                )
            probe = (
                str(bundle.executable),
                "--preserve-symlinks",
                "--preserve-symlinks-main",
                str(bundle.harness),
                "--probe",
            )
            probe_result = launcher.run(
                command=probe,
                input_frame=b"",
                cancellation=Event(),
            )
            if probe_result.return_code != 0 or not probe_result.stdout:
                output, _ = self._output(
                    probe_result.stdout,
                    probe_result.stderr,
                    snapshot_root,
                    bundle.root,
                )
                detail = output.strip() or "no output"
                raise WorkspaceCommandUnavailableError(
                    "Node runtime probe failed "
                    f"(exit {probe_result.return_code}): {detail}"
                )
            command = (
                str(bundle.executable),
                "--preserve-symlinks",
                "--preserve-symlinks-main",
                str(bundle.harness),
                snapshot.command_profile.command_profile_id,
                str(self._pnpm_store),
            )
            return self._execute(
                snapshot,
                bundle.digest,
                snapshot_root,
                bundle.root,
                launcher,
                command,
                cancellation,
                redacted_paths=(self._pnpm_store,),
            )
        except WorkspaceCommandError:
            raise
        except (OSError, NodeCommandRuntimeError, ProcessIsolationError) as error:
            raise WorkspaceCommandUnavailableError(str(error)) from error

    def _run_python(
        self,
        snapshot: WorkspaceCommandSnapshot,
        snapshot_root: Path,
        cancellation: Event | None,
    ) -> WorkspaceCommandRead:
        ruff_executable = self._ruff_executable()
        if ruff_executable is None:
            raise WorkspaceCommandUnavailableError("Ruff executable is unavailable")
        try:
            bundle = prepare_worker_runtime(
                self._runtime_root / "python-command-runtime",
                distributions=COMMAND_RUNTIME_DISTRIBUTIONS,
                include_deskpilot=False,
                additional_executables=(ruff_executable,),
            )
            policy = IsolationPolicy(
                require_windows_sandbox=True,
                require_network_isolation=True,
                memory_limit_bytes=1_073_741_824,
                active_process_limit=snapshot.command_profile.max_processes,
                worker_runtime_bundle=str(bundle.root),
                appcontainer_profile_journal_path=self._profile_journal_path,
                working_directory=str(snapshot_root),
                appcontainer_mirror_workspace=True,
            )
            launcher = create_process_launcher(policy)
            if launcher.mode is not IsolationMode.WINDOWS_APPCONTAINER:
                raise WorkspaceCommandUnavailableError(
                    "Command Profiles require AppContainer network isolation"
                )
            probe = (str(bundle.executable), "-I", "-c", "print('ready')")
            launcher.validate_command(probe)
            command = self._python_command(
                snapshot.command_profile.command_profile_id,
                bundle.executable,
                bundle.root / ruff_executable.name,
            )
            return self._execute(
                snapshot,
                bundle.digest,
                snapshot_root,
                bundle.root,
                launcher,
                command,
                cancellation,
            )
        except WorkspaceCommandError:
            raise
        except (OSError, ProcessIsolationError, WorkerRuntimeError) as error:
            raise WorkspaceCommandUnavailableError(str(error)) from error

    @staticmethod
    def _python_command(
        command_profile_id: CommandProfileId,
        python_executable: Path,
        ruff_executable: Path,
    ) -> tuple[str, ...]:
        commands: dict[CommandProfileId, tuple[str, ...]] = {
            "python.pytest.v1": (
                str(python_executable),
                "-I",
                "-c",
                PYTEST_PROJECT_HARNESS,
            ),
            "python.ruff.v1": (
                str(ruff_executable),
                "check",
                ".",
                "--no-cache",
                "--output-format=concise",
            ),
            "python.mypy.v1": (
                str(python_executable),
                "-I",
                "-c",
                MYPY_PROJECT_HARNESS,
            ),
        }
        try:
            return commands[command_profile_id]
        except KeyError as error:
            raise WorkspaceCommandUnavailableError(
                "Command Profile has no registered Python command"
            ) from error

    def _execute(
        self,
        snapshot: WorkspaceCommandSnapshot,
        toolchain_digest: str,
        snapshot_root: Path,
        runtime_root: Path,
        launcher: ProcessLauncher,
        command: tuple[str, ...],
        cancellation: Event | None,
        *,
        redacted_paths: tuple[Path, ...] = (),
    ) -> WorkspaceCommandRead:
        signal = cancellation or Event()
        timed_out = Event()

        def expire() -> None:
            timed_out.set()
            signal.set()

        timer = Timer(snapshot.command_profile.timeout_seconds, expire)
        started = time.monotonic()
        timer.start()
        try:
            try:
                completed = launcher.run(
                    command=command,
                    input_frame=b"",
                    cancellation=signal,
                )
            except IsolatedProcessCancelledError as error:
                duration_ms = max(0, round((time.monotonic() - started) * 1000))
                output, truncated = self._output(
                    error.stdout,
                    error.stderr,
                    snapshot_root,
                    runtime_root,
                    redacted_paths,
                )
                status = "timed_out" if timed_out.is_set() else "cancelled"
                reason = "timeout" if timed_out.is_set() else "cancelled"
                cancellation_digest = sha256_digest(
                    {
                        "command_profile_id": snapshot.command_profile.command_profile_id,
                        "profile_digest": snapshot.command_profile.profile_digest,
                        "snapshot_digest": snapshot.snapshot_digest,
                        "reason": reason,
                        "duration_ms": duration_ms,
                        "output_digest": sha256_digest({"output": output}),
                    }
                )
                return self._receipt(
                    snapshot,
                    toolchain_digest,
                    status=status,
                    exit_code=None,
                    duration_ms=duration_ms,
                    output=output,
                    output_truncated=truncated,
                    termination_reason=reason,
                    cancellation_receipt_digest=cancellation_digest,
                )
        finally:
            timer.cancel()
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        output, truncated = self._output(
            completed.stdout,
            completed.stderr,
            snapshot_root,
            runtime_root,
            redacted_paths,
        )
        exit_code = completed.return_code
        if exit_code > 2_147_483_647:
            exit_code = 4_294_967_296 - exit_code
        status = (
            "passed"
            if exit_code == 0
            else ("failed" if exit_code == 1 else "error")
        )
        return self._receipt(
            snapshot,
            toolchain_digest,
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=output,
            output_truncated=truncated,
            termination_reason="completed",
            cancellation_receipt_digest=None,
        )

    @staticmethod
    def _receipt(
        snapshot: WorkspaceCommandSnapshot,
        toolchain_digest: str,
        *,
        status: str,
        exit_code: int | None,
        duration_ms: int,
        output: str,
        output_truncated: bool,
        termination_reason: str,
        cancellation_receipt_digest: str | None,
    ) -> WorkspaceCommandRead:
        material = {
            "schema_version": "deskpilot.workspace-command-read.v1",
            "command_profile_id": snapshot.command_profile.command_profile_id,
            "profile_digest": snapshot.command_profile.profile_digest,
            "project_path": snapshot.project_path,
            "snapshot_digest": snapshot.snapshot_digest,
            "toolchain_digest": toolchain_digest,
            "status": status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "output_summary": output,
            "output_digest": sha256_digest({"output": output}),
            "output_truncated": output_truncated,
            "termination_reason": termination_reason,
            "cancellation_receipt_digest": cancellation_receipt_digest,
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
            "temporary_snapshot": True,
            "snapshot_mutations_discarded": True,
        }
        return WorkspaceCommandRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )

    def _publish_snapshot(self, snapshot: WorkspaceCommandSnapshot) -> Path:
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
            raise WorkspaceCommandError("Refusing to remove an unexpected command snapshot")
        if container.exists():
            shutil.rmtree(container)

    @staticmethod
    def _ruff_executable() -> Path | None:
        scripts = Path(sysconfig.get_path("scripts"))
        candidate = scripts / ("ruff.exe" if os.name == "nt" else "ruff")
        return candidate.resolve(strict=True) if candidate.is_file() else None

    @staticmethod
    def _resolve_pnpm_root(executable: Path | None) -> Path | None:
        if executable is None:
            return None
        candidates = (
            executable.parent / "node_modules" / "pnpm",
            executable.parent.parent / "node_modules" / "pnpm",
            executable.parent.parent.parent / "node" / "node_modules" / "pnpm",
        )
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if (
                (resolved / "bin" / "pnpm.mjs").is_file()
                and (resolved / "dist" / "pnpm.mjs").is_file()
            ):
                return resolved.resolve(strict=True)
        return None

    @staticmethod
    def _resolve_pnpm_store(
        executable: Path | None,
        configured: str | None,
    ) -> Path | None:
        candidate = configured
        if candidate is None and executable is not None:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed server-owned pnpm query
                    (str(executable), "store", "path", "--silent"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                    close_fds=True,
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                    ),
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if completed.returncode == 0:
                candidate = completed.stdout.decode("utf-8", errors="ignore").strip()
        if not candidate:
            return None
        path = Path(candidate).resolve(strict=False)
        if (
            not path.is_dir()
            or not re.fullmatch(r"v[1-9]\d*", path.name.casefold())
            or path.parent.name.casefold() != ".pnpm-store"
        ):
            return None
        return path.resolve(strict=True)

    @staticmethod
    def _output(
        stdout: bytes,
        stderr: bytes,
        snapshot_root: Path,
        runtime_root: Path,
        additional_paths: tuple[Path, ...] = (),
    ) -> tuple[str, bool]:
        raw = stdout + (b"\n" if stdout and stderr else b"") + stderr
        text = raw.decode("utf-8", errors="replace")
        for source, replacement in (
            (str(snapshot_root), "<workspace>"),
            (str(runtime_root), "<runtime>"),
            (os.environ.get("USERPROFILE", ""), "<user>"),
            *((str(path), "<dependency-store>") for path in additional_paths),
        ):
            if source:
                text = text.replace(source, replacement).replace(
                    source.replace("\\", "/"), replacement
                )
        encoded = text.encode("utf-8")
        if len(encoded) <= MAX_COMMAND_OUTPUT_BYTES:
            return text, False
        marker = b"\n... <output truncated> ...\n"
        half = (MAX_COMMAND_OUTPUT_BYTES - len(marker)) // 2
        bounded = encoded[:half] + marker + encoded[-half:]
        return bounded.decode("utf-8", errors="ignore"), True


__all__ = [
    "WorkspaceCommandError",
    "WorkspaceCommandRuntime",
    "WorkspaceCommandUnavailableError",
]
