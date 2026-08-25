"""Trusted startup-only Agent Registry, strict loaders and exact reference validator."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deskpilot.application.tool_registry import ToolRegistry, UnknownToolError
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import (
    AGENT_ID_PATTERN,
    AgentContract,
    AgentDescriptor,
    AgentRegistrySnapshot,
    AgentRegistryStatus,
)
from deskpilot.domain.model_contracts import (
    ModelLocation,
    ModelProviderDescriptor,
    ModelRequest,
    PrivacyMode,
    ToolCallingMode,
)
from deskpilot.domain.tool_contracts import SEMVER_PATTERN


class AgentRegistryError(RuntimeError):
    code = "AGENT_REGISTRY_REJECTED"


class AgentNotRegisteredError(AgentRegistryError):
    code = "AGENT_NOT_REGISTERED"


class AgentAlreadyRegisteredError(AgentRegistryError):
    code = "AGENT_ALREADY_REGISTERED"


class AgentRegistryFrozenError(AgentRegistryError):
    code = "AGENT_REGISTRY_FROZEN"


class AgentContractInvalidError(AgentRegistryError):
    code = "AGENT_CONTRACT_INVALID"


class AgentModelRouteNotAllowedError(AgentRegistryError):
    code = "AGENT_MODEL_ROUTE_NOT_ALLOWED"


class AgentPromptDigestMismatchError(AgentRegistryError):
    code = "AGENT_PROMPT_DIGEST_MISMATCH"


class AgentIoSchemaMismatchError(AgentRegistryError):
    code = "AGENT_IO_SCHEMA_MISMATCH"


class AgentToolContractMismatchError(AgentRegistryError):
    code = "AGENT_TOOL_CONTRACT_MISMATCH"


class AgentHandoffNotAllowedError(AgentRegistryError):
    code = "AGENT_HANDOFF_NOT_ALLOWED"


class AgentDisabledError(AgentRegistryError):
    code = "AGENT_DISABLED"


class AgentRevokedError(AgentRegistryError):
    code = "AGENT_REVOKED"


class PromptManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = Field(pattern=r"^deskpilot\.prompt-package\.v1$")
    package_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    renderer_version: int = Field(ge=1)
    instruction_file: str = Field(pattern=r"^[a-z][a-z0-9_-]*\.txt$")
    variables: tuple[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")], ...
    ] = ()


@dataclass(frozen=True, slots=True)
class PromptPackage:
    manifest: PromptManifest
    instruction: str
    digest: str


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    contract: AgentContract
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    prompt_package: PromptPackage
    source: str = "builtin"
    status: AgentRegistryStatus = AgentRegistryStatus.ENABLED
    requires_release_activation: bool = False


class AgentModelAdmissionPolicy(Protocol):
    def allows(
        self,
        contract: AgentContract,
        prompt_package_digest: str,
        provider: ModelProviderDescriptor,
    ) -> bool: ...


class AgentReleaseActivationPolicyPort(Protocol):
    def allows(
        self,
        contract: AgentContract,
        prompt_package_digest: str,
    ) -> bool: ...


_SECRET = re.compile(r"(?i)(api[_-]?key|authorization\s*:|bearer\s+|password\s*=)")
_RISK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _validate_unique_json(payload: bytes) -> None:
    json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)


def load_agent_contract(payload: bytes) -> AgentContract:
    """Parse a bounded immutable Contract without accepting unknown/coerced fields."""

    if not payload or len(payload) > 262_144:
        raise AgentContractInvalidError("Agent Contract size is invalid")
    try:
        _validate_unique_json(payload)
        return AgentContract.model_validate_json(payload, strict=True)
    except (UnicodeDecodeError, ValidationError, ValueError) as error:
        raise AgentContractInvalidError("Agent Contract failed strict validation") from error


def load_prompt_package(root: Path, manifest_name: str) -> PromptPackage:
    try:
        trusted_root = root.resolve(strict=True)
        manifest_candidate = trusted_root / manifest_name
        if manifest_candidate.is_symlink():
            raise AgentContractInvalidError("Prompt manifest cannot be a symbolic link")
        manifest_path = manifest_candidate.resolve(strict=True)
        if manifest_path.parent != trusted_root:
            raise AgentContractInvalidError("Prompt manifest escaped its trusted root")
    except AgentRegistryError:
        raise
    except OSError as error:
        raise AgentContractInvalidError("Prompt manifest is unavailable") from error
    try:
        payload = manifest_path.read_bytes()
        if not payload or len(payload) > 16_384:
            raise AgentContractInvalidError("Prompt manifest size is invalid")
        _validate_unique_json(payload)
        manifest = PromptManifest.model_validate_json(payload, strict=True)
        instruction_candidate = trusted_root / manifest.instruction_file
        if instruction_candidate.is_symlink():
            raise AgentContractInvalidError("Prompt instruction cannot be a symbolic link")
        instruction_path = instruction_candidate.resolve(strict=True)
        if instruction_path.parent != trusted_root:
            raise AgentContractInvalidError("Prompt instruction escaped its trusted root")
        raw_instruction = instruction_path.read_bytes()
        if not raw_instruction or len(raw_instruction) > 32_768:
            raise AgentContractInvalidError("Prompt instruction size is invalid")
        instruction = raw_instruction.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as error:
        if isinstance(error, AgentRegistryError):
            raise
        raise AgentContractInvalidError("Prompt Package failed strict validation") from error
    if _SECRET.search(instruction):
        raise AgentContractInvalidError("Prompt Package contains a forbidden secret marker")
    if len(manifest.variables) != len(set(manifest.variables)):
        raise AgentContractInvalidError("Prompt variables must be unique")
    material: dict[str, Any] = {
        "manifest": manifest.model_dump(mode="json"),
        "instruction": instruction,
    }
    return PromptPackage(manifest, instruction, sha256_digest(material))


class AgentRegistry:
    def __init__(
        self,
        model_admissions: AgentModelAdmissionPolicy | None = None,
        release_activations: AgentReleaseActivationPolicyPort | None = None,
    ) -> None:
        self._registrations: dict[tuple[str, str], AgentRegistration] = {}
        self._statuses: dict[tuple[str, str], tuple[AgentRegistryStatus, str | None]] = {}
        self._model_admissions = model_admissions
        self._release_activations = release_activations
        self._frozen = False
        self._snapshot: AgentRegistrySnapshot | None = None

    def register(self, registration: AgentRegistration) -> None:
        if self._frozen:
            raise AgentRegistryFrozenError("Agent Registry is frozen")
        key = registration.contract.key
        if key in self._registrations:
            raise AgentAlreadyRegisteredError("Agent version is already registered")
        self._registrations[key] = registration
        self._statuses[key] = (registration.status, None)

    def freeze(
        self,
        tool_registry: ToolRegistry,
        model_descriptors: tuple[ModelProviderDescriptor, ...],
    ) -> AgentRegistrySnapshot:
        if self._frozen:
            raise AgentRegistryFrozenError("Agent Registry is already frozen")
        candidate_statuses = dict(self._statuses)
        for registration in self._registrations.values():
            self._validate_registration(registration, tool_registry)
            current_status, _ = candidate_statuses[registration.contract.key]
            if (
                current_status is not AgentRegistryStatus.REVOKED
                and registration.requires_release_activation
                and not self._release_allows(registration)
            ):
                candidate_statuses[registration.contract.key] = (
                    AgentRegistryStatus.DISABLED,
                    "release_not_activated",
                )
            elif current_status is not AgentRegistryStatus.REVOKED and not any(
                self._model_satisfies(registration.contract, descriptor)
                and self._admission_allows(registration, descriptor)
                for descriptor in model_descriptors
            ):
                candidate_statuses[registration.contract.key] = (
                    AgentRegistryStatus.DISABLED,
                    "model_requirements_unsatisfied",
                )
        activated_release = tuple(
            registration
            for registration in self._registrations.values()
            if registration.requires_release_activation
            and self._release_allows(registration)
        )
        if activated_release and any(
            candidate_statuses[item.contract.key][0] is not AgentRegistryStatus.ENABLED
            for item in activated_release
        ):
            for registration in activated_release:
                if (
                    candidate_statuses[registration.contract.key][0]
                    is not AgentRegistryStatus.REVOKED
                ):
                    candidate_statuses[registration.contract.key] = (
                        AgentRegistryStatus.DISABLED,
                        "release_cohort_unsatisfied",
                    )
        self._validate_handoffs()
        descriptors = tuple(
            self._descriptor(self._registrations[key], candidate_statuses)
            for key in sorted(self._registrations)
        )
        material = {
            "schema_version": "deskpilot.agent-registry-snapshot.v1",
            "agents": [item.model_dump(mode="json") for item in descriptors],
        }
        self._snapshot = AgentRegistrySnapshot(
            snapshot_digest=sha256_digest(material), agents=descriptors
        )
        self._statuses = candidate_statuses
        self._frozen = True
        return self._snapshot

    def resolve_exact(
        self,
        agent_id: str,
        version: str,
        *,
        contract_digest: str | None = None,
        prompt_package_digest: str | None = None,
    ) -> AgentRegistration:
        self._require_frozen()
        try:
            registration = self._registrations[(agent_id, version)]
        except KeyError as error:
            raise AgentNotRegisteredError("Exact Agent version is not registered") from error
        status, _ = self._statuses[registration.contract.key]
        if status is AgentRegistryStatus.REVOKED:
            raise AgentRevokedError("Agent version is revoked")
        if status is AgentRegistryStatus.DISABLED:
            raise AgentDisabledError("Agent version is disabled")
        if contract_digest is not None and contract_digest != registration.contract.digest:
            raise AgentContractInvalidError("Agent Contract digest does not match")
        if (
            prompt_package_digest is not None
            and prompt_package_digest != registration.prompt_package.digest
        ):
            raise AgentPromptDigestMismatchError("Prompt Package digest does not match")
        return registration

    def resolve_preferred(self, agent_id: str) -> AgentRegistration:
        self._require_frozen()
        candidates = [
            registration
            for key, registration in self._registrations.items()
            if key[0] == agent_id
            and self._statuses[key][0] is AgentRegistryStatus.ENABLED
        ]
        if not candidates:
            raise AgentNotRegisteredError("No enabled Agent version is registered")
        return max(candidates, key=lambda item: tuple(map(int, item.contract.version.split("."))))

    def resolve_preferred_compatible(
        self,
        agent_id: str,
        *,
        allowed_locations: tuple[ModelLocation, ...],
        allowed_privacy_modes: tuple[PrivacyMode, ...],
    ) -> AgentRegistration:
        """Resolve a preferred version only inside the Task's privacy authority."""

        candidates = [
            registration
            for key, registration in self._registrations.items()
            if key[0] == agent_id
            and self._statuses[key][0] is AgentRegistryStatus.ENABLED
            and any(
                mode in registration.contract.model_policy.allowed_privacy_modes
                for mode in allowed_privacy_modes
            )
            and any(
                location in registration.contract.model_policy.allowed_locations
                for location in allowed_locations
            )
        ]
        if not candidates:
            raise AgentNotRegisteredError(
                "No enabled Agent version matches the Task privacy authority"
            )
        return max(
            candidates,
            key=lambda item: tuple(map(int, item.contract.version.split("."))),
        )

    def validate_model_route(
        self,
        agent_id: str,
        version: str,
        *,
        contract_digest: str,
        prompt_package_digest: str,
        request: ModelRequest,
        provider: ModelProviderDescriptor,
    ) -> AgentRegistration:
        """Revalidate one selected Provider against the exact bound Agent Contract."""

        registration = self.resolve_exact(
            agent_id,
            version,
            contract_digest=contract_digest,
            prompt_package_digest=prompt_package_digest,
        )
        policy = registration.contract.model_policy
        if request.privacy_mode not in policy.allowed_privacy_modes:
            raise AgentModelRouteNotAllowedError(
                "Model privacy mode is not allowed by the Agent Contract"
            )
        requirements = policy.requirements
        requested = request.requirements
        expected_identity = {
            "agent_id": registration.contract.agent_id,
            "agent_version": registration.contract.version,
            "agent_contract_digest": registration.contract.digest,
            "agent_prompt_package_digest": registration.prompt_package.digest,
        }
        boolean_requirements = (
            "streaming",
            "structured_output",
            "strict_json_schema",
            "tool_calling",
            "parallel_tool_calls",
            "vision",
        )
        if (
            any(request.metadata.get(key) != value for key, value in expected_identity.items())
            or request.messages[0].role != "system"
            or request.messages[0].content != registration.prompt_package.instruction
            or request.role is not policy.role
            or any(
                getattr(requirements, field) and not getattr(requested, field)
                for field in boolean_requirements
            )
            or requested.min_context_tokens < requirements.min_context_tokens
            or request.output_schema is None
            or request.output_schema.json_schema != registration.contract.output_schema
            or (requirements.strict_json_schema and not request.output_schema.strict)
        ):
            raise AgentModelRouteNotAllowedError(
                "Model request does not satisfy the Agent Contract or Prompt Package"
            )
        if not self._model_satisfies(registration.contract, provider):
            raise AgentModelRouteNotAllowedError(
                "Selected Provider does not satisfy the Agent Contract"
            )
        if not self._admission_allows(registration, provider):
            raise AgentModelRouteNotAllowedError(
                "Selected Provider lacks an approved Agent model admission"
            )
        return registration

    def list_public(
        self,
        *,
        status: AgentRegistryStatus | None = None,
        kind: str | None = None,
        capability: str | None = None,
    ) -> tuple[AgentDescriptor, ...]:
        snapshot = self.snapshot()
        return tuple(
            item
            for item in snapshot.agents
            if (status is None or item.status is status)
            and (kind is None or item.kind.value == kind)
            and (capability is None or capability in item.provides)
        )

    def descriptor_exact(self, agent_id: str, version: str) -> AgentDescriptor:
        """Return a redacted descriptor regardless of its non-invocable status."""

        self._require_frozen()
        try:
            registration = self._registrations[(agent_id, version)]
        except KeyError as error:
            raise AgentNotRegisteredError("Exact Agent version is not registered") from error
        return self._descriptor(registration)

    def snapshot(self) -> AgentRegistrySnapshot:
        self._require_frozen()
        if self._snapshot is None:
            raise AgentRegistryError("Agent Registry snapshot is unavailable")
        return self._snapshot

    def _validate_registration(
        self, registration: AgentRegistration, tool_registry: ToolRegistry
    ) -> None:
        contract = registration.contract
        if contract.input_schema != registration.input_model.model_json_schema():
            raise AgentIoSchemaMismatchError("Agent input Schema does not match")
        if contract.output_schema != registration.output_model.model_json_schema():
            raise AgentIoSchemaMismatchError("Agent output Schema does not match")
        prompt = registration.prompt_package
        reference = contract.prompt_package
        if (
            reference.package_id != prompt.manifest.package_id
            or reference.version != prompt.manifest.version
            or reference.renderer_version != prompt.manifest.renderer_version
            or reference.digest != prompt.digest
        ):
            raise AgentPromptDigestMismatchError("Prompt Package reference does not match")
        for grant in contract.tool_policy.grants:
            try:
                tool = tool_registry.resolve(grant.name, grant.version).contract
            except UnknownToolError as error:
                raise AgentToolContractMismatchError("Agent Tool is not registered") from error
            if tool.digest != grant.contract_digest:
                raise AgentToolContractMismatchError("Agent Tool digest does not match")
            if _RISK[tool.risk_level.value] > _RISK[contract.tool_policy.max_risk_level.value]:
                raise AgentToolContractMismatchError("Agent Tool exceeds the risk ceiling")

    def _validate_handoffs(self) -> None:
        graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for registration in self._registrations.values():
            source = registration.contract.key
            targets = {item.key for item in registration.contract.handoff_policy.may_delegate_to}
            graph[source] = targets
            if len(targets) > registration.contract.handoff_policy.max_outgoing_handoffs:
                raise AgentHandoffNotAllowedError("Agent handoff edges exceed their limit")
            for target in targets:
                target_registration = self._registrations.get(target)
                if target_registration is None:
                    raise AgentHandoffNotAllowedError("Agent handoff target is not registered")
                allowed_sources = {
                    item.key
                    for item in target_registration.contract.handoff_policy.may_receive_from
                }
                if source not in allowed_sources:
                    raise AgentHandoffNotAllowedError("Agent handoff reverse edge is missing")
        visiting: set[tuple[str, str]] = set()
        visited: set[tuple[str, str]] = set()

        def visit(node: tuple[str, str]) -> None:
            if node in visiting:
                raise AgentHandoffNotAllowedError("Agent handoff graph contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for target in graph.get(node, set()):
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    @staticmethod
    def _model_satisfies(contract: AgentContract, descriptor: ModelProviderDescriptor) -> bool:
        policy = contract.model_policy
        requirements = policy.requirements
        capabilities = descriptor.capabilities
        return (
            descriptor.location in policy.allowed_locations
            and (not requirements.streaming or capabilities.streaming)
            and (not requirements.structured_output or capabilities.structured_output)
            and (not requirements.strict_json_schema or capabilities.strict_json_schema)
            and (
                not requirements.tool_calling
                or capabilities.tool_calling is not ToolCallingMode.NONE
            )
            and (
                not requirements.parallel_tool_calls or capabilities.parallel_tool_calls
            )
            and (not requirements.vision or capabilities.vision)
            and capabilities.max_context_tokens >= requirements.min_context_tokens
        )

    def _admission_allows(
        self,
        registration: AgentRegistration,
        descriptor: ModelProviderDescriptor,
    ) -> bool:
        return bool(
            descriptor.location is ModelLocation.LOCAL
            or (
                self._model_admissions is not None
                and self._model_admissions.allows(
                    registration.contract,
                    registration.prompt_package.digest,
                    descriptor,
                )
            )
        )

    def _release_allows(self, registration: AgentRegistration) -> bool:
        return self._release_activations is not None and self._release_activations.allows(
            registration.contract,
            registration.prompt_package.digest,
        )

    def _descriptor(
        self,
        registration: AgentRegistration,
        statuses: dict[
            tuple[str, str], tuple[AgentRegistryStatus, str | None]
        ] | None = None,
    ) -> AgentDescriptor:
        contract = registration.contract
        status, reason = (statuses or self._statuses)[contract.key]
        return AgentDescriptor(
            agent_id=contract.agent_id,
            version=contract.version,
            kind=contract.kind,
            display_name=contract.display_name,
            description=contract.description,
            status=status,
            status_reason=reason,
            source=registration.source,
            contract_digest=contract.digest,
            prompt_package=contract.prompt_package,
            provides=contract.provides,
            tool_policy=contract.tool_policy,
            handoff_policy=contract.handoff_policy,
            model_policy=contract.model_policy,
            context_policy=contract.context_policy,
            budget_policy=contract.budget_policy,
            result_policy=contract.result_policy,
            input_schema_digest=sha256_digest(contract.input_schema),
            output_schema_digest=sha256_digest(contract.output_schema),
        )

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise AgentRegistryError("Agent Registry is not frozen")
