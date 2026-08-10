"""ToolExecutor-compatible adapter that delegates every invocation to one worker."""

from dataclasses import replace
from pathlib import Path
from threading import Event

from pydantic import BaseModel, ValidationError

from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import ToolCommitProtocol
from deskpilot.runner.authorization import AuthorizedToolCall
from deskpilot.runner.commit_receipts import CommitReceiptStore
from deskpilot.runner.controlled_commit import ControlledCommitBoundary
from deskpilot.runner.executor import (
    ControlledCommitUnavailableError,
    ToolExecutionCancelledError,
    ToolExecutor,
    ToolExecutorError,
    ToolOutputTooLargeError,
)
from deskpilot.runner.process_isolation import (
    IsolatedProcessCancelledError,
    IsolationMode,
    IsolationPolicy,
    NetworkIsolationMode,
    ProcessIsolationError,
    create_process_launcher,
    worker_command,
)
from deskpilot.runner.resource_broker import ResourceBrokerError, ToolResourceBroker
from deskpilot.runner.worker_protocol import (
    MAX_WORKER_FRAME_BYTES,
    WorkerRequest,
    WorkerResponse,
)
from deskpilot.runner.worker_runtime import load_worker_runtime, prepare_worker_runtime


class IsolatedToolExecutionError(ToolExecutorError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IsolatedToolExecutor:
    def __init__(
        self,
        *,
        executor: ToolExecutor,
        worker_factory: str,
        policy: IsolationPolicy,
        commit_receipt_database_path: str,
    ) -> None:
        self.registry = executor.registry
        self._trusted_executor = executor
        self._commit_receipts = CommitReceiptStore(
            Path(commit_receipt_database_path)
        )
        self._trusted_executor.recover_commit_receipts(self._commit_receipts)
        executable: str | None = None
        resolved_policy = policy
        if policy.require_network_isolation:
            if policy.worker_runtime_bundle is not None:
                bundle = load_worker_runtime(Path(policy.worker_runtime_bundle))
            else:
                runtime_root = Path(policy.worker_runtime_root or "./data/worker-runtime")
                bundle = prepare_worker_runtime(runtime_root)
            executable = str(bundle.executable)
            resolved_policy = replace(
                policy,
                worker_runtime_bundle=str(bundle.root),
            )
        self._command = worker_command(worker_factory, executable=executable)
        self._launcher = create_process_launcher(resolved_policy)
        self._launcher.validate_command(self._command)
        self._resource_broker = ToolResourceBroker()

    @property
    def isolation_mode(self) -> IsolationMode:
        return self._launcher.mode

    @property
    def network_isolation_mode(self) -> NetworkIsolationMode:
        return self._launcher.network_isolation_mode

    def execute(
        self,
        call: AuthorizedToolCall,
        cancellation: Event,
        commit_boundary: ControlledCommitBoundary | None = None,
    ) -> BaseModel:
        contract = call.registration.contract
        is_brokered = contract.execution.commit_protocol is ToolCommitProtocol.BROKERED
        if is_brokered and not self._trusted_executor.has_commit_provider(contract.key):
            raise ControlledCommitUnavailableError(
                "No trusted controlled-commit provider is registered for this Tool"
            )
        if is_brokered and commit_boundary is None:
            raise ControlledCommitUnavailableError(
                "Brokered Tool execution requires commit-boundary tracking"
            )
        try:
            resources = self._resource_broker.prepare(call)
        except ResourceBrokerError as error:
            raise IsolatedToolExecutionError(error.code, str(error)) from error
        request = WorkerRequest(
            call_id=call.request.call_id,
            tool_name=call.registration.contract.name,
            tool_version=call.registration.contract.version,
            contract_digest=call.registration.contract.digest,
            arguments=call.arguments.model_dump(mode="json"),
            resources=resources,
        )
        frame = canonical_json_bytes(request) + b"\n"
        if len(frame) > MAX_WORKER_FRAME_BYTES:
            raise ToolExecutorError("Tool worker request exceeds the process boundary limit")
        try:
            completed = self._launcher.run(
                command=self._command,
                input_frame=frame,
                cancellation=cancellation,
            )
        except IsolatedProcessCancelledError as error:
            raise ToolExecutionCancelledError(str(error)) from error
        except ProcessIsolationError as error:
            raise IsolatedToolExecutionError(error.code, str(error)) from error
        if completed.return_code != 0:
            diagnostic = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
            suffix = f": {diagnostic}" if diagnostic else ""
            raise IsolatedToolExecutionError(
                "TOOL_WORKER_EXITED",
                f"Tool worker exited with code {completed.return_code}{suffix}",
            )
        if len(completed.stdout) > MAX_WORKER_FRAME_BYTES:
            raise IsolatedToolExecutionError(
                "TOOL_WORKER_PROTOCOL_INVALID",
                "Tool worker response exceeds the process boundary limit",
            )
        try:
            response = WorkerResponse.model_validate_json(completed.stdout)
        except ValidationError as error:
            raise IsolatedToolExecutionError(
                "TOOL_WORKER_PROTOCOL_INVALID",
                "Tool worker returned an invalid response",
            ) from error
        if response.call_id != call.request.call_id:
            raise IsolatedToolExecutionError(
                "TOOL_WORKER_PROTOCOL_INVALID",
                "Tool worker response call_id does not match the invocation",
            )
        if response.status == "failed":
            if response.error is None:
                raise IsolatedToolExecutionError(
                    "TOOL_WORKER_PROTOCOL_INVALID",
                    "Tool worker failure omitted its error",
                )
            raise IsolatedToolExecutionError(response.error.code, response.error.message)
        if response.output is None:
            raise IsolatedToolExecutionError(
                "TOOL_WORKER_PROTOCOL_INVALID",
                "Tool worker success omitted its output",
            )
        if is_brokered:
            if commit_boundary is None:
                raise ControlledCommitUnavailableError(
                    "Brokered Tool execution lost its commit boundary"
                )
            prepared = self._trusted_executor.validate_prepare(
                contract.name,
                contract.version,
                response.output,
            )
            output = self._trusted_executor.commit_prepared(
                call,
                prepared,
                resources,
                cancellation,
                commit_boundary,
                self._commit_receipts,
            )
        else:
            output = self.registry.validate_output(
                contract.name,
                contract.version,
                response.output,
            )
        if (
            len(canonical_json_bytes(output))
            > call.registration.contract.execution.max_output_bytes
        ):
            raise ToolOutputTooLargeError("Tool output exceeds its Contract limit")
        return output

    def commit_receipt_for_call(self, call_id: str) -> ToolCommitReceipt | None:
        record = self._commit_receipts.get_for_call(call_id)
        if record is None or record.state != "committed":
            return None
        return record.receipt
