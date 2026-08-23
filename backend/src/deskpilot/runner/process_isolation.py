"""Per-invocation process isolation selected by the persistent Runner broker."""

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event

from deskpilot.runner.worker_protocol import MAX_WORKER_FRAME_BYTES


class IsolationMode(StrEnum):
    WINDOWS_RESTRICTED = "windows_restricted"
    WINDOWS_APPCONTAINER = "windows_appcontainer"
    PROCESS_ONLY = "process_only"


class NetworkIsolationMode(StrEnum):
    NONE = "none"
    APPCONTAINER = "appcontainer"


class ProcessIsolationError(RuntimeError):
    code = "RUNNER_ISOLATION_FAILED"


class ProcessIsolationUnavailableError(ProcessIsolationError):
    code = "RUNNER_ISOLATION_UNAVAILABLE"


class IsolatedProcessCancelledError(ProcessIsolationError):
    code = "TOOL_CANCELLED"

    def __init__(
        self,
        message: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class IsolationPolicy:
    require_windows_sandbox: bool = False
    require_network_isolation: bool = False
    memory_limit_bytes: int = 268_435_456
    active_process_limit: int = 1
    worker_runtime_root: str | None = None
    worker_runtime_bundle: str | None = None
    appcontainer_profile_journal_path: str | None = None
    working_directory: str | None = None
    appcontainer_read_paths: tuple[str, ...] = ()
    appcontainer_temp_path: str | None = None
    appcontainer_mirror_workspace: bool = False

    def __post_init__(self) -> None:
        if self.memory_limit_bytes < 67_108_864:
            raise ValueError("Worker memory limit must be at least 64 MiB")
        if not 1 <= self.active_process_limit <= 16:
            raise ValueError("Worker active process limit must be between 1 and 16")
        for value in (
            self.worker_runtime_root,
            self.worker_runtime_bundle,
            self.appcontainer_profile_journal_path,
            self.working_directory,
            self.appcontainer_temp_path,
        ):
            if value is not None and (not value or "\x00" in value or len(value) > 32_767):
                raise ValueError("Worker isolation path is invalid")
        if self.working_directory is not None and not Path(self.working_directory).is_absolute():
            raise ValueError("Worker isolation working directory must be absolute")
        if len(self.appcontainer_read_paths) > 8:
            raise ValueError("Worker isolation accepts at most eight read-only paths")
        for value in self.appcontainer_read_paths:
            if not value or "\x00" in value or len(value) > 32_767 or not Path(value).is_absolute():
                raise ValueError("Worker AppContainer read path is invalid")
        if (
            self.appcontainer_temp_path is not None
            and not Path(self.appcontainer_temp_path).is_absolute()
        ):
            raise ValueError("Worker AppContainer temp path must be absolute")
        if self.appcontainer_mirror_workspace and (
            not self.require_network_isolation
            or self.worker_runtime_bundle is None
            or self.working_directory is None
        ):
            raise ValueError(
                "AppContainer workspace mirroring requires a runtime bundle, "
                "working directory, and network isolation"
            )


@dataclass(frozen=True, slots=True)
class IsolatedProcessResult:
    return_code: int
    stdout: bytes
    stderr: bytes


def sanitized_worker_environment(*, runtime_root: Path | None = None) -> dict[str, str]:
    """Keep interpreter essentials while excluding application credentials and secrets."""
    allowed = (
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "DESKPILOT_WORKER_MODE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if runtime_root is None:
        environment["PYTHONPATH"] = os.pathsep.join(
            dict.fromkeys(path for path in sys.path if path)
        )
    else:
        system_root = Path(os.environ["SYSTEMROOT"])
        environment.update(
            {
                "PATH": os.pathsep.join(
                    (str(runtime_root), str(system_root / "System32"), str(system_root))
                ),
                "PYTHONHOME": str(runtime_root),
            }
        )
    return environment


class ProcessLauncher:
    mode = IsolationMode.PROCESS_ONLY
    network_isolation_mode = NetworkIsolationMode.NONE

    def validate(self) -> None:
        return

    def validate_command(self, command: tuple[str, ...]) -> None:
        del command

    def run(
        self,
        *,
        command: tuple[str, ...],
        input_frame: bytes,
        cancellation: Event,
    ) -> IsolatedProcessResult:
        raise NotImplementedError


class PortableProcessLauncher(ProcessLauncher):
    """Compatibility path: process separation without a Windows kernel sandbox."""

    def __init__(self, policy: IsolationPolicy | None = None) -> None:
        self._cwd = policy.working_directory if policy is not None else None

    def run(
        self,
        *,
        command: tuple[str, ...],
        input_frame: bytes,
        cancellation: Event,
    ) -> IsolatedProcessResult:
        process = subprocess.Popen(  # noqa: S603 - fixed executable and argument tuple
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd or os.getcwd(),
            env=sanitized_worker_environment(),
            close_fds=True,
            start_new_session=os.name != "nt",
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        stdout = b""
        stderr = b""
        pending_input: bytes | None = input_frame
        try:
            while True:
                if cancellation.is_set():
                    kill_process_group = getattr(
                        os,
                        "".join(("kill", "pg")),
                        None,
                    )
                    if os.name != "nt" and callable(kill_process_group):
                        kill_process_group(process.pid, 9)
                    else:
                        process.kill()
                    process.wait()
                    raise IsolatedProcessCancelledError("Tool worker was cancelled")
                try:
                    stdout, stderr = process.communicate(input=pending_input, timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
            return IsolatedProcessResult(
                return_code=process.returncode,
                stdout=stdout[: MAX_WORKER_FRAME_BYTES + 1],
                stderr=stderr[:4_096],
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def create_process_launcher(policy: IsolationPolicy) -> ProcessLauncher:
    if os.name == "nt":
        from deskpilot.runner.windows_sandbox import WindowsRestrictedProcessLauncher

        launcher: ProcessLauncher = WindowsRestrictedProcessLauncher(policy)
        try:
            launcher.validate()
        except ProcessIsolationError:
            if policy.require_windows_sandbox or policy.require_network_isolation:
                raise
            launcher = PortableProcessLauncher(policy)
            launcher.validate()
        return launcher
    else:
        if policy.require_windows_sandbox or policy.require_network_isolation:
            raise ProcessIsolationUnavailableError(
                "Required Windows process or network isolation is unavailable on this Runner"
            )
        launcher = PortableProcessLauncher(policy)
    launcher.validate()
    return launcher


def worker_command(
    factory_path: str,
    *,
    executable: str | None = None,
) -> tuple[str, ...]:
    base_executable = executable or getattr(sys, "_base_executable", sys.executable)
    if not isinstance(base_executable, str):
        raise ProcessIsolationUnavailableError("Python base executable is unavailable")
    return (base_executable, "-m", "deskpilot.runner.worker", factory_path)
