"""Short-lived conversation memory and immutable model-context proofs."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.agent_runtime import (
    INVOCATION_ID_PATTERN,
    MODEL_TURN_ID_PATTERN,
)
from deskpilot.domain.model_contracts import ModelLocation, PrivacyMode
from deskpilot.domain.task_plans import (
    CONVERSATION_ID_PATTERN,
    MESSAGE_ID_PATTERN,
    TASK_ID_PATTERN,
)

CONTEXT_REQUEST_ID_PATTERN = r"^crq_[0-9a-f]{64}$"
CONTEXT_ITEM_ID_PATTERN = r"^ctx_[0-9a-f]{64}$"
CONTEXT_MANIFEST_ID_PATTERN = r"^cmf_[0-9a-f]{64}$"
WORKING_MEMORY_ID_PATTERN = r"^wmi_[0-9a-f]{64}$"


class WorkingMemoryKind(StrEnum):
    CURRENT_GOAL = "current_goal"
    ACTIVE_CONSTRAINT = "active_constraint"
    CONFIRMED_DECISION = "confirmed_decision"
    OPEN_QUESTION = "open_question"
    SELECTED_ARTIFACT = "selected_artifact"
    TEMPORARY_FACT = "temporary_fact"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DELETED = "deleted"


class AuthorityClass(StrEnum):
    TASK_TRUTH = "task_truth"
    USER_EXPLICIT = "user_explicit"
    VERIFIED = "verified"
    WORKING_MEMORY = "working_memory"
    CONFIRMED_MEMORY = "confirmed_memory"
    DATA = "data"


class TrustClass(StrEnum):
    TRUSTED_RUNTIME = "trusted_runtime"
    TRUSTED_USER_INPUT = "trusted_user_input"
    TRUSTED_EVIDENCE = "trusted_evidence"
    UNTRUSTED_MODEL_OUTPUT = "untrusted_model_output"
    UNTRUSTED_EXTERNAL_CONTENT = "untrusted_external_content"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class EgressOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str = Field(min_length=1, max_length=200)


class ConversationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    title: str
    created_at: datetime


class CreateConversationMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant"]
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    content_ref: str | None = Field(default=None, min_length=1, max_length=500)
    task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    classification: DataClassification = DataClassification.INTERNAL

    @model_validator(mode="after")
    def content_or_reference(self) -> Self:
        if (self.content is None) == (self.content_ref is None):
            raise ValueError("Exactly one of content and content_ref is required")
        return self


class ConversationMessageRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    role: Literal["user", "assistant"]
    content: str | None
    content_ref: str | None
    classification: DataClassification
    status: MemoryStatus
    message_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    deleted_at: datetime | None


class CreateWorkingMemoryRequest(BaseModel):
    """An explicit local-user write; agents and external adapters have no write route."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: WorkingMemoryKind
    content: str = Field(min_length=1, max_length=2_000)
    classification: DataClassification = DataClassification.INTERNAL
    expires_at: datetime | None = None


class WorkingMemoryItemRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    memory_item_id: str = Field(pattern=WORKING_MEMORY_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID_PATTERN)
    kind: WorkingMemoryKind
    content: str
    source_type: Literal["user_explicit", "task_contract", "verified_claim"]
    source_ref: str
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    classification: DataClassification
    verification_status: Literal["not_required", "verified"]
    status: MemoryStatus
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: str = Field(pattern=CONTEXT_ITEM_ID_PATTERN)
    source_type: Literal[
        "task_contract",
        "handoff",
        "conversation_message",
        "working_memory",
        "verified_claim",
        "long_term_memory",
        "external_untrusted_page_snapshot",
    ]
    source_ref: str = Field(min_length=1, max_length=500)
    source_version: str = Field(min_length=1, max_length=100)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    authority_class: AuthorityClass
    trust_class: TrustClass
    classification: DataClassification
    token_count: int = Field(ge=1)
    inclusion_reason: str = Field(min_length=1, max_length=100)


class ExcludedContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: str = Field(pattern=CONTEXT_ITEM_ID_PATTERN)
    source_type: str = Field(min_length=1, max_length=100)
    source_ref: str = Field(min_length=1, max_length=500)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    reason: Literal[
        "source_not_allowed",
        "scope_denied",
        "deleted",
        "expired",
        "duplicate_task_truth",
        "egress_denied",
        "token_budget",
        "conflict",
    ]


class ContextEgressDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: EgressOutcome
    privacy_mode: PrivacyMode
    target_provider_location: ModelLocation
    denied_item_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    decision_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"decision_digest"})
        if self.decision_digest != sha256_digest(material):
            raise ValueError("Context egress decision digest does not match")
        return self


class ContextManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.context-manifest.v1"] = (
        "deskpilot.context-manifest.v1"
    )
    manifest_id: str = Field(pattern=CONTEXT_MANIFEST_ID_PATTERN)
    context_request_id: str = Field(pattern=CONTEXT_REQUEST_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    model_turn_id: str = Field(pattern=MODEL_TURN_ID_PATTERN)
    model_request_digest: str = Field(pattern=DIGEST_PATTERN)
    agent_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_package_digest: str = Field(pattern=DIGEST_PATTERN)
    handoff_digest: str = Field(pattern=DIGEST_PATTERN)
    selector_policy_id: Literal["deskpilot.context-selector.v1"] = (
        "deskpilot.context-selector.v1"
    )
    selector_policy_digest: str = Field(pattern=DIGEST_PATTERN)
    tokenizer_id: Literal["deskpilot.conservative-char4.v1"] = (
        "deskpilot.conservative-char4.v1"
    )
    renderer_version: Literal[1] = 1
    included_items: tuple[ContextItem, ...]
    excluded_items: tuple[ExcludedContextItem, ...]
    maximum_input_tokens: int = Field(ge=1)
    used_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    egress: ContextEgressDecision
    final_context_digest: str = Field(pattern=DIGEST_PATTERN)
    manifest_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.used_input_tokens != sum(item.token_count for item in self.included_items):
            raise ValueError("Context token count does not match included items")
        if self.used_input_tokens + self.reserved_output_tokens > self.maximum_input_tokens:
            raise ValueError("Context exceeds the declared input budget")
        material = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != sha256_digest(material):
            raise ValueError("Context Manifest digest does not match")
        return self


class CurrentContextRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    retained_items: tuple[WorkingMemoryItemRead, ...]
    latest_manifest: ContextManifest | None
