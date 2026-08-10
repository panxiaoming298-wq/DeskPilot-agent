"""Explicit tool allowlist and Pydantic contract validation."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from deskpilot.domain.policy import PolicyResource
from deskpilot.domain.tool_contracts import ToolContract

ResourceProjector = Callable[[BaseModel], tuple[PolicyResource, ...]]


class ToolRegistryError(LookupError):
    code = "TOOL_REGISTRY_ERROR"


class DuplicateToolError(ToolRegistryError):
    code = "TOOL_ALREADY_REGISTERED"


class UnknownToolError(ToolRegistryError):
    code = "TOOL_NOT_REGISTERED"


class ToolSchemaValidationError(ToolRegistryError):
    code = "TOOL_SCHEMA_VALIDATION_FAILED"

    def __init__(self, message: str, validation_error: ValidationError) -> None:
        super().__init__(message)
        self.validation_error = validation_error


class ToolResourceProjectionError(ToolRegistryError):
    code = "TOOL_RESOURCE_PROJECTION_FAILED"


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    contract: ToolContract
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    resource_projector: ResourceProjector


class ToolRegistry:
    """Stores only registrations created by trusted application composition code."""

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], ToolRegistration] = {}

    def register(
        self,
        contract: ToolContract,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        resource_projector: ResourceProjector,
    ) -> ToolRegistration:
        if contract.key in self._registrations:
            raise DuplicateToolError(f"Tool already registered: {contract.name}@{contract.version}")
        if contract.input_schema != input_model.model_json_schema():
            raise ValueError("Tool input model does not match the serialized contract")
        if contract.output_schema != output_model.model_json_schema():
            raise ValueError("Tool output model does not match the serialized contract")
        registration = ToolRegistration(
            contract,
            input_model,
            output_model,
            resource_projector,
        )
        self._registrations[contract.key] = registration
        return registration

    def resolve(self, name: str, version: str) -> ToolRegistration:
        try:
            return self._registrations[(name, version)]
        except KeyError as error:
            raise UnknownToolError(f"Tool is not registered: {name}@{version}") from error

    def validate_input(self, name: str, version: str, value: dict[str, Any]) -> BaseModel:
        registration = self.resolve(name, version)
        try:
            return registration.input_model.model_validate(value)
        except ValidationError as error:
            raise ToolSchemaValidationError("Tool input failed schema validation", error) from error

    def validate_output(self, name: str, version: str, value: dict[str, Any]) -> BaseModel:
        registration = self.resolve(name, version)
        try:
            return registration.output_model.model_validate(value)
        except ValidationError as error:
            raise ToolSchemaValidationError(
                "Tool output failed schema validation", error
            ) from error

    def project_resources(
        self,
        name: str,
        version: str,
        arguments: BaseModel,
    ) -> tuple[PolicyResource, ...]:
        registration = self.resolve(name, version)
        try:
            resources = registration.resource_projector(arguments)
        except Exception as error:
            raise ToolResourceProjectionError(
                "Tool resources could not be projected from validated arguments"
            ) from error
        if (
            not isinstance(resources, tuple)
            or not resources
            or any(not isinstance(resource, PolicyResource) for resource in resources)
        ):
            raise ToolResourceProjectionError(
                "Tool resource projector returned an invalid resource scope"
            )
        return resources

    def contracts(self) -> tuple[ToolContract, ...]:
        return tuple(
            registration.contract for _, registration in sorted(self._registrations.items())
        )
