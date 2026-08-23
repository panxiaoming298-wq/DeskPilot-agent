"""AppContainer execution boundary for fixed workspace snapshot checks."""

import os
from pathlib import Path
from threading import Event, Lock, Timer

from pydantic import ValidationError

from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.workspace_files import WorkspaceCheckRead
from deskpilot.runner.process_isolation import (
    IsolatedProcessCancelledError,
    IsolationMode,
    IsolationPolicy,
    ProcessIsolationError,
    ProcessLauncher,
    create_process_launcher,
    worker_command,
)
from deskpilot.runner.worker_protocol import MAX_WORKER_FRAME_BYTES, WorkerRequest, WorkerResponse
from deskpilot.runner.worker_runtime import prepare_worker_runtime
from deskpilot.tools.workspace_checks import (
    WORKSPACE_CHECK_CONTRACT,
    WorkspaceCheckInput,
    WorkspaceCheckOutput,
)


class WorkspaceCheckError(RuntimeError):
    code = "WORKSPACE_CHECK_FAILED"


class WorkspaceCheckUnavailableError(WorkspaceCheckError):
    code = "WORKSPACE_CHECK_UNAVAILABLE"


class WorkspaceCheckTimeoutError(WorkspaceCheckError):
    code = "WORKSPACE_CHECK_TIMEOUT"


class WorkspaceCheckRuntime:
    """Run only the fixed parser Tool in a fresh, networkless Windows process."""

    def __init__(
        self,
        runtime_root: str,
        profile_journal_path: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        self._runtime_root = Path(runtime_root)
        self._profile_journal_path = profile_journal_path
        self._timeout_seconds = timeout_seconds
        self._launcher: ProcessLauncher | None = None
        self._command: tuple[str, ...] | None = None
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return os.name == "nt"

    def run(self, snapshot: WorkspaceCheckInput) -> WorkspaceCheckRead:
        launcher, command = self._boundary()
        request = WorkerRequest(
            call_id=f"wck_{sha256_digest(snapshot)}",
            tool_name=WORKSPACE_CHECK_CONTRACT.name,
            tool_version=WORKSPACE_CHECK_CONTRACT.version,
            contract_digest=WORKSPACE_CHECK_CONTRACT.digest,
            arguments=snapshot.model_dump(mode="json"),
        )
        frame = canonical_json_bytes(request) + b"\n"
        if len(frame) > MAX_WORKER_FRAME_BYTES:
            raise WorkspaceCheckError("Workspace check snapshot exceeds the process limit")
        cancellation = Event()
        timer = Timer(self._timeout_seconds, cancellation.set)
        timer.start()
        try:
            completed = launcher.run(
                command=command,
                input_frame=frame,
                cancellation=cancellation,
            )
        except IsolatedProcessCancelledError as error:
            raise WorkspaceCheckTimeoutError("Workspace check exceeded its time limit") from error
        except ProcessIsolationError as error:
            raise WorkspaceCheckUnavailableError(str(error)) from error
        finally:
            timer.cancel()
        if completed.return_code != 0:
            raise WorkspaceCheckError("Workspace check worker exited unsuccessfully")
        try:
            response = WorkerResponse.model_validate_json(completed.stdout)
        except ValidationError as error:
            raise WorkspaceCheckError(
                "Workspace check worker returned an invalid response"
            ) from error
        if response.call_id != request.call_id or response.status != "succeeded":
            raise WorkspaceCheckError("Workspace check worker rejected the bounded snapshot")
        if response.output is None:
            raise WorkspaceCheckError("Workspace check worker omitted its output")
        output = WorkspaceCheckOutput.model_validate(response.output)
        if len(canonical_json_bytes(output)) > WORKSPACE_CHECK_CONTRACT.execution.max_output_bytes:
            raise WorkspaceCheckError("Workspace check output exceeds its limit")
        material = {
            "schema_version": "deskpilot.workspace-check.v1",
            **output.model_dump(mode="json"),
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
        }
        return WorkspaceCheckRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )

    def _boundary(self) -> tuple[ProcessLauncher, tuple[str, ...]]:
        with self._lock:
            if self._launcher is not None and self._command is not None:
                return self._launcher, self._command
            try:
                bundle = prepare_worker_runtime(self._runtime_root)
                policy = IsolationPolicy(
                    require_windows_sandbox=True,
                    require_network_isolation=True,
                    memory_limit_bytes=134_217_728,
                    active_process_limit=1,
                    worker_runtime_bundle=str(bundle.root),
                    appcontainer_profile_journal_path=self._profile_journal_path,
                )
                launcher = create_process_launcher(policy)
                if launcher.mode is not IsolationMode.WINDOWS_APPCONTAINER:
                    raise WorkspaceCheckUnavailableError(
                        "Workspace checks require AppContainer network isolation"
                    )
                command = worker_command(
                    "deskpilot.tools.workspace_checks:create_workspace_check_executor",
                    executable=str(bundle.executable),
                )
                launcher.validate_command(command)
            except (OSError, ProcessIsolationError) as error:
                raise WorkspaceCheckUnavailableError(str(error)) from error
            self._launcher = launcher
            self._command = command
            return launcher, command


__all__ = [
    "WorkspaceCheckError",
    "WorkspaceCheckRuntime",
    "WorkspaceCheckTimeoutError",
    "WorkspaceCheckUnavailableError",
]
