"""Protected, versioned long-term memory contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.context_memory import DataClassification

MEMORY_ID_PATTERN = r"^mem_[0-9a-f]{64}$"
MEMORY_PROPOSAL_ID_PATTERN = r"^mpr_[0-9a-f]{64}$"
MEMORY_CONFLICT_ID_PATTERN = r"^mcf_[0-9a-f]{64}$"
MEMORY_USAGE_ID_PATTERN = r"^mus_[0-9a-f]{64}$"


class LongTermMemoryKind(StrEnum):
    PREFERENCE = "preference"
    RESTRICTIVE_PERMISSION = "restrictive_permission"
    USER_CONFIRMED_FACT = "user_confirmed_fact"
    VERIFIED_EPISODE = "verified_episode"
    SKILL_TEMPLATE = "skill_template"


class LongTermMemoryStatus(StrEnum):
    PROPOSAL = "proposal"
    PENDING_CONFIRMATION = "pending_confirmation"
    ACTIVE = "active"
    CONFLICT = "conflict"
    EXPIRED = "expired"
    DELETED = "deleted"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class MemorySourceType(StrEnum):
    USER_EXPLICIT = "user_explicit"
    AGENT_RESULT = "agent_result"
    VERIFIED_DELIVERY = "verified_delivery"


class MemoryCreatedBy(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class CreateLongTermMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    kind: LongTermMemoryKind
    value: str = Field(min_length=1, max_length=4_000)
    classification: DataClassification = DataClassification.INTERNAL
    expires_at: datetime | None = None
    verified_delivery_id: str | None = Field(default=None, pattern=r"^dlv_[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verified_source_matches_kind(self) -> Self:
        needs_delivery = self.kind in {
            LongTermMemoryKind.VERIFIED_EPISODE,
            LongTermMemoryKind.SKILL_TEMPLATE,
        }
        if needs_delivery != (self.verified_delivery_id is not None):
            raise ValueError("Verified episode and skill template require a delivery proof")
        return self


class EditLongTermMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: str = Field(min_length=1, max_length=4_000)
    classification: DataClassification | None = None
    expires_at: datetime | None = None


class ResolveMemoryConflictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selected_memory_id: str = Field(pattern=MEMORY_ID_PATTERN)


class MemoryProposalRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    proposal_id: str = Field(pattern=MEMORY_PROPOSAL_ID_PATTERN)
    key: str
    kind: LongTermMemoryKind
    value: str | None
    source_type: MemorySourceType
    source_id: str
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    created_by: MemoryCreatedBy
    scope: Literal["user"] = "user"
    classification: DataClassification
    confidence: float = Field(ge=0, le=1)
    status: LongTermMemoryStatus
    value_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    expires_at: datetime | None
    decided_at: datetime | None


class LongTermMemoryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    memory_id: str = Field(pattern=MEMORY_ID_PATTERN)
    proposal_id: str = Field(pattern=MEMORY_PROPOSAL_ID_PATTERN)
    key: str
    version: int = Field(ge=1)
    kind: LongTermMemoryKind
    value: str | None
    source_type: MemorySourceType
    source_id: str
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    created_by: MemoryCreatedBy
    scope: Literal["user"] = "user"
    classification: DataClassification
    confidence: float = Field(ge=0, le=1)
    status: LongTermMemoryStatus
    value_digest: str = Field(pattern=DIGEST_PATTERN)
    item_digest: str = Field(pattern=DIGEST_PATTERN)
    supersedes_memory_id: str | None = Field(default=None, pattern=MEMORY_ID_PATTERN)
    created_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None


class MemoryConflictRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conflict_id: str = Field(pattern=MEMORY_CONFLICT_ID_PATTERN)
    key: str
    kind: LongTermMemoryKind
    memory_ids: tuple[str, ...] = Field(min_length=2)
    status: Literal["open", "resolved"]
    selected_memory_id: str | None = Field(default=None, pattern=MEMORY_ID_PATTERN)
    conflict_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    resolved_at: datetime | None


class MemoryUsageRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    usage_id: str = Field(pattern=MEMORY_USAGE_ID_PATTERN)
    memory_id: str = Field(pattern=MEMORY_ID_PATTERN)
    memory_version: int = Field(ge=1)
    task_id: str
    invocation_id: str
    context_manifest_id: str
    agent_id: str
    provider_id: str
    provider_location: str
    purpose: str
    supplied_at: datetime
    policy_reference: str
    deleted_after_use: bool


class LongTermMemoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[LongTermMemoryRead, ...]
    proposals: tuple[MemoryProposalRead, ...]
    conflicts: tuple[MemoryConflictRead, ...]
    usage: tuple[MemoryUsageRead, ...]


class LongTermMemoryExport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.long-term-memory-export.v1"] = (
        "deskpilot.long-term-memory-export.v1"
    )
    exported_at: datetime
    items: tuple[LongTermMemoryRead, ...]
    proposals: tuple[MemoryProposalRead, ...]
    conflicts: tuple[MemoryConflictRead, ...]
    usage: tuple[MemoryUsageRead, ...]
    tombstones: tuple[dict[str, object], ...]
    export_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"export_digest", "exported_at"})
        if self.export_digest != sha256_digest(material):
            raise ValueError("Memory export digest does not match")
        return self
