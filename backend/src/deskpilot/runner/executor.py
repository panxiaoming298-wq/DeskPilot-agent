"""Trusted mapping from authorized calls to built-in Python implementations."""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any, Protocol

from pydantic import BaseModel

from deskpilot.application.tool_registry import ResourceProjector, ToolRegistry
from deskpilot.core.canonical_json import canonical_json_bytes
from deskpilot.domain.tool_contracts import ToolCommitProtocol, ToolContract
from deskpilot.runner.authorization import AuthorizedToolCall
from deskpilot.runner.commit_receipts import CommitReceiptStore
from deskpilot.runner.controlled_commit import ControlledCommitBoundary
from deskpilot.runner.resource_broker import ResourceBrokerError, ToolResourceBroker
from deskpilot.runner.worker_protocol import (
    BrokeredFileMove,
    BrokeredFilesystemMetadata,
    BrokeredResource,
)


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    resources: tuple[BrokeredResource, ...] = ()

    def require_filesystem_metadata(self) -> BrokeredFilesystemMetadata:
        if len(self.resources) != 1 or not isinstance(
            self.resources[0], BrokeredFilesystemMetadata
        ):
            raise ToolExecutorError(
                "Tool worker requires exactly one brokered filesystem metadata resource"
            )
        return self.resources[0]

    def require_file_move(self) -> BrokeredFileMove:
        if len(self.resources) != 1 or not isinstance(self.resources[0], BrokeredFileMove):
            raise ToolExecutorError(
                "Tool worker requires exactly one brokered file.move resource"
            )
        return self.resources[0]


ToolHandler = Callable[[BaseModel, Event, ToolExecutionContext], BaseModel]


class ToolCommitProvider(Protocol):
    contract_key: tuple[str, str]

    def recover(self, store: CommitReceiptStore) -> None: ...

    def commit(
        self,
        call: AuthorizedToolCall,
        prepared: BaseModel,
        resources: tuple[BrokeredResource, ...],
        cancellation: Event,
        boundary: ControlledCommitBoundary,
        store: CommitReceiptStore,
    ) -> BaseModel: ...


class ToolExecutorError(RuntimeError):
    code = "TOOL_EXECUTION_FAILED"


class ToolExecutionCancelledError(ToolExecutorError):
    code = "TOOL_CANCELLED"


class ToolOutputTooLargeError(ToolExecutorError):
    code = "TOOL_OUTPUT_TOO_LARGE"


class DuplicateToolHandlerError(ToolExecutorError):
    code = "TOOL_HANDLER_ALREADY_REGISTERED"


class ControlledCommitUnavailableError(ToolExecutorError):
    code = "TOOL_CONTROLLED_COMMIT_UNAVAILABLE"


class ToolResourceContextError(ToolExecutorError):
    code = "TOOL_RESOURCE_CONTEXT_INVALID"


class ToolExecutor:
    """Contains the only executable allowlist inside the Runner process."""

    def __init__(self) -> None:
        self.registry = ToolRegistry()
        self._handlers: dict[tuple[str, str], ToolHandler] = {}
        self._prepare_models: dict[tuple[str, str], type[BaseModel]] = {}
        self._commit_providers: dict[tuple[str, str], ToolCommitProvider] = {}

    def register(
        self,
        contract: ToolContract,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        resource_projector: ResourceProjector,
        handler: ToolHandler,
        *,
        prepare_model: type[BaseModel] | None = None,
        commit_provider: ToolCommitProvider | None = None,
    ) -> None:
        if contract.key in self._handlers:
            raise DuplicateToolHandlerError(
                f"Tool handler already registered: {contract.name}@{contract.version}"
            )
        is_brokered = contract.execution.commit_protocol is ToolCommitProtocol.BROKERED
        if (prepare_model is None) != (commit_provider is None):
            raise ValueError(
                "A controlled-commit provider and prepare model must be registered together"
            )
        if prepare_model is not None and not is_brokered:
            raise ValueError("Read-only Tools cannot register a prepare model")
        if commit_provider is not None:
            if not is_brokered:
                raise ValueError("Read-only Tools cannot register a commit provider")
            if commit_provider.contract_key != contract.key:
                raise ValueError("Controlled-commit provider does not match its Tool Contract")
        self.registry.register(
            contract,
            input_model,
            output_model,
            resource_projector,
        )
        self._handlers[contract.key] = handler
        if prepare_model is not None:
            self._prepare_models[contract.key] = prepare_model
        if commit_provider is not None:
            self._commit_providers[contract.key] = commit_provider

    def execute(self, call: AuthorizedToolCall, cancellation: Event) -> BaseModel:
        if call.registration.contract.execution.commit_protocol is ToolCommitProtocol.BROKERED:
            raise ControlledCommitUnavailableError(
                "Brokered Tools must execute through the isolated parent commit boundary"
            )
        try:
            resources = ToolResourceBroker().prepare(call)
        except ResourceBrokerError as error:
            raise ToolExecutorError(str(error)) from error
        return self._execute_registration(
            tool_name=call.registration.contract.name,
            tool_version=call.registration.contract.version,
            arguments=call.arguments,
            resources=resources,
            cancellation=cancellation,
        )

    def execute_worker_request(
        self,
        *,
        tool_name: str,
        tool_version: str,
        contract_digest: str,
        arguments: dict[str, Any],
        resources: tuple[BrokeredResource, ...],
        cancellation: Event,
    ) -> BaseModel:
        """Revalidate the narrow parent-to-worker request before invoking a handler."""
        registration = self.registry.resolve(tool_name, tool_version)
        if not hmac.compare_digest(contract_digest, registration.contract.digest):
            raise ToolExecutorError("Worker request contract digest does not match allowlist")
        parsed_arguments = self.registry.validate_input(
            tool_name,
            tool_version,
            arguments,
        )
        return self._execute_registration(
            tool_name=tool_name,
            tool_version=tool_version,
            arguments=parsed_arguments,
            resources=resources,
            cancellation=cancellation,
            worker_request=True,
        )

    def validate_prepare(
        self,
        tool_name: str,
        tool_version: str,
        value: dict[str, Any],
    ) -> BaseModel:
        key = (tool_name, tool_version)
        try:
            model = self._prepare_models[key]
        except KeyError as error:
            raise ControlledCommitUnavailableError(
                "No prepare model is registered for this brokered Tool"
            ) from error
        return model.model_validate(value)

    def commit_prepared(
        self,
        call: AuthorizedToolCall,
        prepared: BaseModel,
        resources: tuple[BrokeredResource, ...],
        cancellation: Event,
        boundary: ControlledCommitBoundary,
        store: CommitReceiptStore,
    ) -> BaseModel:
        try:
            provider = self._commit_providers[call.registration.contract.key]
        except KeyError as error:
            raise ControlledCommitUnavailableError(
                "No trusted controlled-commit provider is registered for this Tool"
            ) from error
        output = provider.commit(
            call,
            prepared,
            resources,
            cancellation,
            boundary,
            store,
        )
        return self.registry.validate_output(
            call.registration.contract.name,
            call.registration.contract.version,
            output.model_dump(mode="json"),
        )

    def recover_commit_receipts(self, store: CommitReceiptStore) -> None:
        for key in sorted(self._commit_providers):
            self._commit_providers[key].recover(store)

    def has_commit_provider(self, key: tuple[str, str]) -> bool:
        return key in self._commit_providers

    def _execute_registration(
        self,
        *,
        tool_name: str,
        tool_version: str,
        arguments: BaseModel,
        resources: tuple[BrokeredResource, ...],
        cancellation: Event,
        worker_request: bool = False,
    ) -> BaseModel:
        if cancellation.is_set():
            raise ToolExecutionCancelledError("Tool call was cancelled before execution")
        try:
            registration = self.registry.resolve(tool_name, tool_version)
            handler = self._handlers[registration.contract.key]
        except KeyError as error:
            raise ToolExecutorError("Authorized tool has no registered handler") from error

        contract = registration.contract
        is_brokered = contract.execution.commit_protocol is ToolCommitProtocol.BROKERED
        if is_brokered and contract.key not in self._commit_providers:
            raise ControlledCommitUnavailableError(
                "No trusted controlled-commit broker is registered for this Tool"
            )
        if is_brokered and not worker_request:
            raise ControlledCommitUnavailableError(
                "Brokered Tool prepare may run only inside an isolated worker"
            )
        expected_capabilities = tuple(sorted(set(contract.security.capabilities)))
        actual_capabilities = tuple(
            sorted(
                {
                    operation
                    for resource in resources
                    for operation in resource.operations
                }
            )
        )
        if actual_capabilities != expected_capabilities:
            raise ToolResourceContextError(
                "Brokered worker resources do not cover the Tool Contract capabilities"
            )

        raw_output = handler(arguments, cancellation, ToolExecutionContext(resources))
        if is_brokered:
            output = self.validate_prepare(
                tool_name,
                tool_version,
                raw_output.model_dump(mode="json"),
            )
        else:
            output = self.registry.validate_output(
                tool_name,
                tool_version,
                raw_output.model_dump(mode="json"),
            )
        if cancellation.is_set():
            raise ToolExecutionCancelledError("Tool call was cancelled before commit")
        if (
            len(canonical_json_bytes(output))
            > registration.contract.execution.max_output_bytes
        ):
            raise ToolOutputTooLargeError("Tool output exceeds its Contract limit")
        return output
