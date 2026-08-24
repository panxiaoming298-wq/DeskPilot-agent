"""Pure execute/verify orchestration for exact Capability bindings.

Persistence, claiming and retry policy remain owned by the stage-112B task-loop
runtime.  This engine intentionally cannot mint a verified ResultRef: it first
returns a candidate and only a separate verification call returns a sealed
verification object.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from deskpilot.application.builtin_capability_executors import (
    CapabilityAdapterVerification,
)
from deskpilot.application.capability_executor_registry import (
    ApprovalGatedCapabilityExecutor,
    CapabilityExecutionBindingError,
    CapabilityExecutorRegistration,
    CapabilityExecutorRegistry,
)
from deskpilot.application.capability_input_binding_catalog import BoundCapabilityInput
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.capability_execution import (
    CapabilityApprovalRequirement,
    CapabilityExecutionContext,
    CapabilityResultKind,
)
from deskpilot.domain.task_plans import PLAN_NODE_ID_PATTERN, TASK_ID_PATTERN, CapabilityRef


class CapabilityExecutionEngineError(RuntimeError):
    code = "CAPABILITY_EXECUTION_ENGINE_REJECTED"


class CapabilityCandidateBindingRejectedError(CapabilityExecutionEngineError):
    code = "CAPABILITY_CANDIDATE_BINDING_REJECTED"


class CapabilityVerificationRejectedError(CapabilityExecutionEngineError):
    code = "CAPABILITY_VERIFICATION_REJECTED"


class CapabilityExecutionCandidate(BaseModel):
    """Unverified server execution output; never usable as a ResultRef."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.capability-execution-candidate.v1"] = (
        "deskpilot.capability-execution-candidate.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    context_digest: str = Field(pattern=DIGEST_PATTERN)
    capability: CapabilityRef
    input_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    arguments_digest: str = Field(pattern=DIGEST_PATTERN)
    result_kind: CapabilityResultKind
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    output_manifest: dict[str, JsonValue]
    result_digest: str = Field(pattern=DIGEST_PATTERN)
    candidate_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> CapabilityExecutionCandidate:
        if self.output_manifest.get("result_digest") != self.result_digest:
            raise ValueError("Capability candidate output digest does not match")
        material = self.model_dump(mode="json", exclude={"candidate_digest"})
        if self.candidate_digest != sha256_digest(material):
            raise ValueError("Capability candidate digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
        registration: CapabilityExecutorRegistration,
        output: BaseModel,
    ) -> CapabilityExecutionCandidate:
        output_manifest = output.model_dump(mode="json")
        result_digest = output_manifest.get("result_digest")
        if not isinstance(result_digest, str):
            raise CapabilityCandidateBindingRejectedError(
                "Capability output omitted its result digest"
            )
        values: dict[str, Any] = {
            "schema_version": "deskpilot.capability-execution-candidate.v1",
            "task_id": context.task_id,
            "node_id": context.node_id,
            "context_digest": context.context_digest,
            "capability": context.capability,
            "input_binding_digest": bound_input.binding_digest,
            "arguments_digest": bound_input.arguments_digest,
            "result_kind": registration.manifest.produces,
            "output_schema_digest": sha256_digest(registration.output_model.model_json_schema()),
            "output_manifest": output_manifest,
            "result_digest": result_digest,
        }
        return cls(**values, candidate_digest=sha256_digest(values))


class VerifiedCapabilityOutput(BaseModel):
    """Sealed verification result; persistence may later mint one ResultRef."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.verified-capability-output.v1"] = (
        "deskpilot.verified-capability-output.v1"
    )
    candidate: CapabilityExecutionCandidate
    adapter_verification: CapabilityAdapterVerification
    verification_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digest_match(self) -> VerifiedCapabilityOutput:
        proof = self.adapter_verification
        if (
            proof.context_digest != self.candidate.context_digest
            or proof.capability != self.candidate.capability
            or proof.result_kind is not self.candidate.result_kind
            or proof.result_schema_digest != self.candidate.output_schema_digest
            or proof.result_digest != self.candidate.result_digest
        ):
            raise ValueError("Capability verification does not match its candidate")
        material = self.model_dump(mode="json", exclude={"verification_digest"})
        if self.verification_digest != sha256_digest(material):
            raise ValueError("Verified capability output digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        candidate: CapabilityExecutionCandidate,
        adapter_verification: CapabilityAdapterVerification,
    ) -> VerifiedCapabilityOutput:
        values = {
            "schema_version": "deskpilot.verified-capability-output.v1",
            "candidate": candidate,
            "adapter_verification": adapter_verification,
        }
        return cls.model_validate({**values, "verification_digest": sha256_digest(values)})


class CapabilityExecutionEngine:
    """Execute and verify only registry-resolved, server-bound inputs."""

    def __init__(self, registry: CapabilityExecutorRegistry) -> None:
        self._registry = registry

    async def execute_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
    ) -> CapabilityExecutionCandidate:
        registration = self._registration(context, bound_input)
        arguments = registration.input_model.model_validate(
            bound_input.arguments.model_dump(mode="json")
        )
        raw_output = await registration.executor.execute(context, arguments)
        output = registration.output_model.model_validate(raw_output.model_dump(mode="json"))
        return CapabilityExecutionCandidate.build(
            context=context,
            bound_input=bound_input,
            registration=registration,
            output=output,
        )

    async def prepare_approval(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
    ) -> BaseModel:
        registration = self._registration(context, bound_input)
        executor = registration.executor
        approval_model = registration.approval_model
        if (
            registration.manifest.approval_requirement
            is not CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST
            or approval_model is None
            or not isinstance(executor, ApprovalGatedCapabilityExecutor)
        ):
            raise CapabilityExecutionBindingError(
                "Capability is not registered for an approval preview"
            )
        arguments = registration.input_model.model_validate(
            bound_input.arguments.model_dump(mode="json")
        )
        raw_preview = await executor.prepare_approval(context, arguments)
        return approval_model.model_validate(raw_preview.model_dump(mode="json"))

    async def execute_approved_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
        preview_manifest: dict[str, Any],
    ) -> CapabilityExecutionCandidate:
        registration = self._registration(context, bound_input)
        executor = registration.executor
        approval_model = registration.approval_model
        if (
            registration.manifest.approval_requirement
            is not CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST
            or approval_model is None
            or not isinstance(executor, ApprovalGatedCapabilityExecutor)
        ):
            raise CapabilityExecutionBindingError(
                "Capability has no exact approved execution path"
            )
        arguments = registration.input_model.model_validate(
            bound_input.arguments.model_dump(mode="json")
        )
        preview = approval_model.model_validate(preview_manifest)
        raw_output = await executor.execute_approved(context, arguments, preview)
        output = registration.output_model.model_validate(raw_output.model_dump(mode="json"))
        return CapabilityExecutionCandidate.build(
            context=context,
            bound_input=bound_input,
            registration=registration,
            output=output,
        )

    async def verify_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
        candidate: CapabilityExecutionCandidate,
    ) -> VerifiedCapabilityOutput:
        registration = self._registration(context, bound_input)
        if (
            candidate.task_id != context.task_id
            or candidate.node_id != context.node_id
            or candidate.context_digest != context.context_digest
            or candidate.capability != context.capability
            or candidate.input_binding_digest != bound_input.binding_digest
            or candidate.arguments_digest != bound_input.arguments_digest
            or candidate.result_kind is not registration.manifest.produces
            or candidate.output_schema_digest
            != sha256_digest(registration.output_model.model_json_schema())
        ):
            raise CapabilityCandidateBindingRejectedError(
                "Capability candidate changed its exact execution binding"
            )
        output = registration.output_model.model_validate(candidate.output_manifest)
        raw_verification = await registration.executor.verify(context, output)
        if not isinstance(raw_verification, CapabilityAdapterVerification):
            raise CapabilityVerificationRejectedError(
                "Capability executor returned no adapter verification proof"
            )
        return VerifiedCapabilityOutput.build(
            candidate=candidate,
            adapter_verification=raw_verification,
        )

    def _registration(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
    ) -> CapabilityExecutorRegistration:
        if (
            context.task_id != bound_input.task_id
            or context.node_id != bound_input.node_id
            or context.node_spec_digest != bound_input.node_spec_digest
            or context.node_binding_id != bound_input.node_binding_id
            or context.node_binding_digest != bound_input.node_binding_digest
            or context.effective_authority_digest != bound_input.effective_authority_digest
            or context.runtime_eligibility_digest != bound_input.runtime_eligibility_digest
            or context.capability != bound_input.capability
            or context.step_input_digest != bound_input.binding_digest
            or context.upstream_result_refs != bound_input.dependency_result_refs
            or context.consumed_result_refs != bound_input.consumed_result_refs
        ):
            raise CapabilityExecutionBindingError(
                "Execution context does not match the server-bound capability input"
            )
        return self._registry.resolve_for_execution(
            context,
            bound_capability=bound_input.capability,
            bound_node_kind=context.node_kind,
        )


__all__ = [
    "CapabilityCandidateBindingRejectedError",
    "CapabilityExecutionCandidate",
    "CapabilityExecutionEngine",
    "CapabilityExecutionEngineError",
    "CapabilityVerificationRejectedError",
    "VerifiedCapabilityOutput",
]
