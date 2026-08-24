"""Immutable contracts for exact capability executor dispatch."""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.agent_runtime import RUN_ID_PATTERN
from deskpilot.domain.task_plans import (
    PLAN_ID_PATTERN,
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    CapabilityPack,
    CapabilityRef,
    DraftNodeKind,
    PlanNodeBudget,
)

CAPABILITY_EXECUTOR_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"
CAPABILITY_NODE_BINDING_ID_PATTERN = r"^mnb_[0-9a-f]{64}$"


class CapabilityResultKind(StrEnum):
    """Server-known result kinds that may cross a verified Plan edge."""

    RESEARCH_EVIDENCE = "research_evidence"
    VERIFIED_CLAIMS = "verified_claims"
    ARTIFACT = "artifact"
    BROWSER_VERIFICATION = "browser_verification"
    KNOWLEDGE = "knowledge"
    MCP = "mcp"
    WORKSPACE_FILE = "workspace_file"
    WORKSPACE_DIRECTORY = "workspace_directory"
    WORKSPACE_CHECK = "workspace_check"
    PYTHON_TEST = "python_test"
    NODE_TEST = "node_test"
    PROJECT_SEARCH = "project_search"
    PROJECT_BATCH_READ = "project_batch_read"
    GIT_INSPECTION = "git_inspection"
    PATCH_PROPOSAL = "patch_proposal"
    PATCH_RECEIPT = "patch_receipt"
    PATCH_TEST = "patch_test"
    DELIVERY = "delivery"


class CapabilityEffectClass(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    USER_PATH_WRITE = "user_path_write"
    EXECUTION_CONTROL = "execution_control"


class CapabilityApprovalRequirement(StrEnum):
    NONE = "none"
    EXACT_CONFIRMATION_DIGEST = "exact_confirmation_digest"


class CapabilityRecoveryPolicy(StrEnum):
    DETERMINISTIC_RETRY = "deterministic_retry"
    RECEIPT_RECONCILE = "receipt_reconcile"
    NO_AUTOMATIC_REPLAY = "no_automatic_replay"


class CapabilityExecutorManifest(BaseModel):
    """Data-only registration manifest; it contains no callable or import path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.capability-executor-manifest.v1"] = (
        "deskpilot.capability-executor-manifest.v1"
    )
    executor_id: str = Field(pattern=CAPABILITY_EXECUTOR_ID_PATTERN)
    capability: CapabilityRef
    runtime_enabled: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    node_kinds: tuple[DraftNodeKind, ...] = Field(min_length=1, max_length=2)
    consumes: tuple[CapabilityResultKind, ...] = Field(default=(), max_length=16)
    produces: CapabilityResultKind
    effect_class: CapabilityEffectClass
    approval_requirement: CapabilityApprovalRequirement
    recovery_policy: CapabilityRecoveryPolicy
    manifest_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def declarations_and_digest_match(self) -> Self:
        if len(self.node_kinds) != len(set(self.node_kinds)):
            raise ValueError("Capability executor node kinds must be unique")
        if any(
            item not in {DraftNodeKind.AGENT, DraftNodeKind.CAPABILITY} for item in self.node_kinds
        ):
            raise ValueError("Capability executors cannot bind control nodes")
        if len(self.consumes) != len(set(self.consumes)):
            raise ValueError("Capability executor consumed result kinds must be unique")
        material = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != sha256_digest(material):
            raise ValueError("Capability executor manifest digest does not match")
        return self

    @classmethod
    def from_pack(
        cls,
        *,
        executor_id: str,
        pack: CapabilityPack,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        node_kinds: tuple[DraftNodeKind, ...],
        consumes: tuple[CapabilityResultKind, ...] = (),
        produces: CapabilityResultKind,
        effect_class: CapabilityEffectClass,
        approval_requirement: CapabilityApprovalRequirement,
        recovery_policy: CapabilityRecoveryPolicy,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.capability-executor-manifest.v1",
            "executor_id": executor_id,
            "capability": CapabilityRef(
                capability_id=pack.capability_id,
                version=pack.version,
                digest=pack.digest,
            ),
            "runtime_enabled": pack.runtime_enabled,
            "input_schema": input_model.model_json_schema(),
            "output_schema": output_model.model_json_schema(),
            "node_kinds": node_kinds,
            "consumes": consumes,
            "produces": produces,
            "effect_class": effect_class,
            "approval_requirement": approval_requirement,
            "recovery_policy": recovery_policy,
        }
        return cls(**values, manifest_digest=sha256_digest(values))


class VerifiedCapabilityResultRef(BaseModel):
    """Server-authored reference to one already verified capability result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.verified-capability-result-ref.v1"] = (
        "deskpilot.verified-capability-result-ref.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    producer_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    producer_attempt: int = Field(ge=1)
    capability: CapabilityRef
    result_kind: CapabilityResultKind
    result_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_digest: str = Field(pattern=DIGEST_PATTERN)
    verification_digest: str = Field(pattern=DIGEST_PATTERN)
    result_ref_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_ref_digest"})
        if self.result_ref_digest != sha256_digest(material):
            raise ValueError("Verified capability ResultRef digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        run_id: str,
        plan_generation: int,
        producer_node_id: str,
        producer_attempt: int,
        capability: CapabilityRef,
        result_kind: CapabilityResultKind,
        result_schema_digest: str,
        result_digest: str,
        verification_digest: str,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.verified-capability-result-ref.v1",
            "task_id": task_id,
            "run_id": run_id,
            "plan_generation": plan_generation,
            "producer_node_id": producer_node_id,
            "producer_attempt": producer_attempt,
            "capability": capability,
            "result_kind": result_kind,
            "result_schema_digest": result_schema_digest,
            "result_digest": result_digest,
            "verification_digest": verification_digest,
        }
        return cls(**values, result_ref_digest=sha256_digest(values))


class CapabilityExecutionContext(BaseModel):
    """Fence-bound context assembled by the server, never by a planner model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.capability-execution-context.v1"] = (
        "deskpilot.capability-execution-context.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    node_kind: DraftNodeKind
    node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    node_binding_id: str = Field(pattern=CAPABILITY_NODE_BINDING_ID_PATTERN)
    node_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    effective_authority_digest: str = Field(pattern=DIGEST_PATTERN)
    runtime_eligibility_digest: str = Field(pattern=DIGEST_PATTERN)
    node_attempt: int = Field(ge=1)
    claim_owner_id: str = Field(min_length=1, max_length=128)
    claim_fencing_token: int = Field(ge=1)
    capability: CapabilityRef
    step_input_digest: str = Field(pattern=DIGEST_PATTERN)
    upstream_result_refs: tuple[VerifiedCapabilityResultRef, ...] = Field(
        default=(),
        max_length=20,
    )
    consumed_result_refs: tuple[VerifiedCapabilityResultRef, ...] = Field(
        default=(),
        max_length=16,
    )
    budget: PlanNodeBudget
    context_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def refs_and_digest_match(self) -> Self:
        ref_digests = tuple(item.result_ref_digest for item in self.upstream_result_refs)
        if len(ref_digests) != len(set(ref_digests)):
            raise ValueError("Capability execution context contains duplicate ResultRefs")
        if any(item.task_id != self.task_id for item in self.upstream_result_refs):
            raise ValueError("Capability execution context crosses Task scope")
        consumed_digests = tuple(item.result_ref_digest for item in self.consumed_result_refs)
        if len(consumed_digests) != len(set(consumed_digests)):
            raise ValueError("Capability execution context contains duplicate consumed ResultRefs")
        if not set(consumed_digests).issubset(ref_digests):
            raise ValueError(
                "Capability semantic ResultRefs must also satisfy a verified dependency edge"
            )
        material = self.model_dump(mode="json", exclude={"context_digest"})
        if self.context_digest != sha256_digest(material):
            raise ValueError("Capability execution context digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        run_id: str,
        plan_id: str,
        plan_generation: int,
        plan_manifest_digest: str,
        node_id: str,
        node_kind: DraftNodeKind,
        node_spec_digest: str,
        node_binding_id: str,
        node_binding_digest: str,
        effective_authority_digest: str,
        runtime_eligibility_digest: str,
        node_attempt: int,
        claim_owner_id: str,
        claim_fencing_token: int,
        capability: CapabilityRef,
        step_input_digest: str,
        upstream_result_refs: tuple[VerifiedCapabilityResultRef, ...],
        consumed_result_refs: tuple[VerifiedCapabilityResultRef, ...] = (),
        budget: PlanNodeBudget,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.capability-execution-context.v1",
            "task_id": task_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "plan_generation": plan_generation,
            "plan_manifest_digest": plan_manifest_digest,
            "node_id": node_id,
            "node_kind": node_kind,
            "node_spec_digest": node_spec_digest,
            "node_binding_id": node_binding_id,
            "node_binding_digest": node_binding_digest,
            "effective_authority_digest": effective_authority_digest,
            "runtime_eligibility_digest": runtime_eligibility_digest,
            "node_attempt": node_attempt,
            "claim_owner_id": claim_owner_id,
            "claim_fencing_token": claim_fencing_token,
            "capability": capability,
            "step_input_digest": step_input_digest,
            "upstream_result_refs": upstream_result_refs,
            "consumed_result_refs": consumed_result_refs,
            "budget": budget,
        }
        return cls(**values, context_digest=sha256_digest(values))
