"""Exact, data-bound registry for trusted capability executors."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from deskpilot.domain.capability_execution import (
    CapabilityExecutionContext,
    CapabilityExecutorManifest,
)
from deskpilot.domain.task_plans import CapabilityRef, DraftNodeKind

_RESERVED_MODEL_AUTHORITY_FIELDS = frozenset(
    {
        "executable",
        "argv",
        "cwd",
        "working_directory",
        "env",
        "environment",
        "environment_variables",
        "permission",
        "permissions",
        "authority",
        "authorization",
        "grant",
        "grants",
        "capability",
        "capabilities",
        "capability_ref",
        "capability_refs",
        "approval",
        "approval_digest",
        "result_ref",
        "result_refs",
        "upstream_result_ref",
        "upstream_result_refs",
        "claim_owner_id",
        "claim_fencing_token",
    }
)
_RESERVED_MODEL_AUTHORITY_FIELDS_COMPACT = frozenset(
    item.replace("_", "") for item in _RESERVED_MODEL_AUTHORITY_FIELDS
)


class CapabilityExecutorRegistryError(LookupError):
    code = "CAPABILITY_EXECUTOR_REGISTRY_ERROR"


class DuplicateCapabilityExecutorError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_ALREADY_REGISTERED"


class UnknownCapabilityExecutorError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_NOT_REGISTERED"


class CapabilityExecutorVersionDriftError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_VERSION_DRIFT"


class CapabilityExecutorDigestDriftError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_DIGEST_DRIFT"


class CapabilityExecutorRuntimeDisabledError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_RUNTIME_DISABLED"


class CapabilityExecutorSchemaError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_SCHEMA_MISMATCH"


class CapabilityExecutionBindingError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTION_BINDING_REJECTED"


class CapabilityModelAuthorityRejectedError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_MODEL_AUTHORITY_REJECTED"


class CapabilityExecutorInputValidationError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_INPUT_INVALID"

    def __init__(self, message: str, validation_error: ValidationError) -> None:
        super().__init__(message)
        self.validation_error = validation_error


class CapabilityExecutorOutputValidationError(CapabilityExecutorRegistryError):
    code = "CAPABILITY_EXECUTOR_OUTPUT_INVALID"

    def __init__(self, message: str, validation_error: ValidationError) -> None:
        super().__init__(message)
        self.validation_error = validation_error


class CapabilityExecutor(Protocol):
    """Runtime adapter interface. Registry construction itself performs no I/O."""

    async def execute(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel: ...

    async def verify(
        self,
        context: CapabilityExecutionContext,
        candidate: BaseModel,
    ) -> BaseModel: ...


@runtime_checkable
class ApprovalGatedCapabilityExecutor(CapabilityExecutor, Protocol):
    """Trusted adapter that separates preview from an approved side effect."""

    async def prepare_approval(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
    ) -> BaseModel: ...

    async def execute_approved(
        self,
        context: CapabilityExecutionContext,
        arguments: BaseModel,
        preview: BaseModel,
    ) -> BaseModel: ...


@dataclass(frozen=True, slots=True)
class CapabilityExecutorRegistration:
    manifest: CapabilityExecutorManifest
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    executor: CapabilityExecutor
    approval_model: type[BaseModel] | None = None


class CapabilityExecutorRegistry:
    """Resolve only exact, runtime-enabled registrations sealed by trusted code."""

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str, str], CapabilityExecutorRegistration] = {}
        self._identity_digests: dict[tuple[str, str], str] = {}
        self._versions: dict[str, set[str]] = {}

    def register(
        self,
        manifest: CapabilityExecutorManifest,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        executor: CapabilityExecutor,
        *,
        approval_model: type[BaseModel] | None = None,
    ) -> CapabilityExecutorRegistration:
        capability = manifest.capability
        exact_key = self._exact_key(capability)
        identity_key = capability.key
        if exact_key in self._registrations:
            raise DuplicateCapabilityExecutorError(
                "Capability executor exact binding is already registered"
            )
        existing_digest = self._identity_digests.get(identity_key)
        if existing_digest is not None and existing_digest != capability.digest:
            raise CapabilityExecutorDigestDriftError(
                "Capability executor identity was registered with another digest"
            )
        if not manifest.runtime_enabled:
            raise CapabilityExecutorRuntimeDisabledError(
                "Disabled capability cannot register an executor"
            )
        if manifest.input_schema != input_model.model_json_schema():
            raise CapabilityExecutorSchemaError(
                "Capability executor input model does not match its manifest"
            )
        if manifest.output_schema != output_model.model_json_schema():
            raise CapabilityExecutorSchemaError(
                "Capability executor output model does not match its manifest"
            )
        approval_required = manifest.approval_requirement.value != "none"
        if approval_required != (approval_model is not None):
            raise CapabilityExecutorSchemaError(
                "Capability approval model does not match its manifest requirement"
            )
        if approval_required and not isinstance(executor, ApprovalGatedCapabilityExecutor):
            raise CapabilityExecutorSchemaError(
                "Approval-gated capability executor omitted its trusted protocol"
            )
        reserved_schema_field = self._reserved_authority_field(manifest.input_schema)
        if reserved_schema_field is not None:
            raise CapabilityModelAuthorityRejectedError(
                f"Capability input Schema contains reserved authority field: "
                f"{reserved_schema_field}"
            )
        registration = CapabilityExecutorRegistration(
            manifest=manifest,
            input_model=input_model,
            output_model=output_model,
            executor=executor,
            approval_model=approval_model,
        )
        self._registrations[exact_key] = registration
        self._identity_digests[identity_key] = capability.digest
        self._versions.setdefault(capability.capability_id, set()).add(capability.version)
        return registration

    def resolve(self, capability: CapabilityRef) -> CapabilityExecutorRegistration:
        exact = self._registrations.get(self._exact_key(capability))
        if exact is not None:
            return exact
        versions = self._versions.get(capability.capability_id)
        if versions is None:
            raise UnknownCapabilityExecutorError("Capability executor is not registered")
        if capability.version not in versions:
            raise CapabilityExecutorVersionDriftError(
                "Capability executor version does not match an exact registration"
            )
        raise CapabilityExecutorDigestDriftError(
            "Capability executor digest does not match an exact registration"
        )

    def resolve_for_execution(
        self,
        context: CapabilityExecutionContext,
        *,
        bound_capability: CapabilityRef,
        bound_node_kind: DraftNodeKind,
    ) -> CapabilityExecutorRegistration:
        if context.capability != bound_capability:
            raise CapabilityExecutionBindingError(
                "Execution context capability does not match the persisted Plan node"
            )
        if context.node_kind is not bound_node_kind:
            raise CapabilityExecutionBindingError(
                "Execution context node kind does not match the persisted Plan node"
            )
        registration = self.resolve(bound_capability)
        manifest = registration.manifest
        if bound_node_kind not in manifest.node_kinds:
            raise CapabilityExecutionBindingError(
                "Capability executor cannot run this Plan node kind"
            )
        # A verified dependency edge is only a scheduling gate.  It does not
        # become semantic executor input unless the trusted binding explicitly
        # selects it as consumed input.
        actual_kinds = tuple(item.result_kind for item in context.consumed_result_refs)
        required_kinds = manifest.consumes
        if actual_kinds != required_kinds:
            raise CapabilityExecutionBindingError(
                "Verified upstream ResultRef kinds do not match the executor manifest"
            )
        return registration

    def validate_model_input(
        self,
        capability: CapabilityRef,
        value: dict[str, Any],
    ) -> BaseModel:
        reserved_field = self._reserved_authority_field(value)
        if reserved_field is not None:
            raise CapabilityModelAuthorityRejectedError(
                f"Model input contains reserved authority field: {reserved_field}"
            )
        registration = self.resolve(capability)
        try:
            return registration.input_model.model_validate(value)
        except ValidationError as error:
            raise CapabilityExecutorInputValidationError(
                "Capability executor input failed Schema validation",
                error,
            ) from error

    def validate_output(
        self,
        capability: CapabilityRef,
        value: dict[str, Any],
    ) -> BaseModel:
        registration = self.resolve(capability)
        try:
            return registration.output_model.model_validate(value)
        except ValidationError as error:
            raise CapabilityExecutorOutputValidationError(
                "Capability executor output failed Schema validation",
                error,
            ) from error

    def manifests(self) -> tuple[CapabilityExecutorManifest, ...]:
        return tuple(self._registrations[key].manifest for key in sorted(self._registrations))

    @staticmethod
    def _exact_key(capability: CapabilityRef) -> tuple[str, str, str]:
        return capability.capability_id, capability.version, capability.digest

    @classmethod
    def _reserved_authority_field(cls, value: object) -> str | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if (
                    normalized in _RESERVED_MODEL_AUTHORITY_FIELDS
                    or normalized.replace("_", "") in _RESERVED_MODEL_AUTHORITY_FIELDS_COMPACT
                ):
                    return normalized
                found = cls._reserved_authority_field(nested)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found = cls._reserved_authority_field(nested)
                if found is not None:
                    return found
        return None
