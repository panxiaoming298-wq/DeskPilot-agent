"""Deterministic, source-bound context compaction proofs."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.context_memory import (
    AuthorityClass,
    DataClassification,
)
from deskpilot.domain.task_plans import CONVERSATION_ID_PATTERN, TASK_ID_PATTERN

COMPACTION_SNAPSHOT_ID_PATTERN = r"^cps_[0-9a-f]{64}$"


class CompactionStatus(StrEnum):
    ACTIVE = "active"
    CONFLICT = "conflict"
    STALE = "stale"


class CompactionSourceStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    DELETED = "deleted"
    OUT_OF_SCOPE = "out_of_scope"


class CoverageStatus(StrEnum):
    COVERED = "covered"
    CONFLICT = "conflict"
    STALE = "stale"


class CompactionStructuredFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    goals: tuple[str, ...] = Field(default=(), max_length=100)
    active_constraints: tuple[str, ...] = Field(default=(), max_length=200)
    confirmed_decisions: tuple[str, ...] = Field(default=(), max_length=200)
    open_questions: tuple[str, ...] = Field(default=(), max_length=200)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=200)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=200)
    active_memory_refs: tuple[str, ...] = Field(default=(), max_length=200)


class CompactionSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_type: str = Field(min_length=1, max_length=100)
    source_ref: str = Field(min_length=1, max_length=500)
    source_version: str = Field(min_length=1, max_length=100)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    authority_class: AuthorityClass
    classification: DataClassification
    status: CompactionSourceStatus = CompactionSourceStatus.ACTIVE


class CompactionCoverageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field_kind: Literal[
        "goal",
        "active_constraint",
        "confirmed_decision",
        "open_question",
        "artifact_ref",
        "evidence_ref",
        "active_memory_ref",
    ]
    value_digest: str = Field(pattern=DIGEST_PATTERN)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=1_000)
    status: CoverageStatus


class CompactionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.compaction-snapshot.v1"] = "deskpilot.compaction-snapshot.v1"
    snapshot_id: str = Field(pattern=COMPACTION_SNAPSHOT_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID_PATTERN)
    parent_snapshot_id: str | None = Field(default=None, pattern=COMPACTION_SNAPSHOT_ID_PATTERN)
    source_refs: tuple[CompactionSourceRef, ...] = Field(min_length=1)
    source_set_digest: str = Field(pattern=DIGEST_PATTERN)
    structured_fields: CompactionStructuredFields
    narrative_summary: None = None
    coverage_items: tuple[CompactionCoverageItem, ...] = Field(min_length=1)
    compressor_version: Literal["deskpilot.deterministic-compactor.v1"] = (
        "deskpilot.deterministic-compactor.v1"
    )
    classification: DataClassification
    status: CompactionStatus
    created_at: datetime
    stale_at: datetime | None = None
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def proof_matches(self) -> Self:
        source_material = [
            item.model_dump(mode="json", exclude={"status"}) for item in self.source_refs
        ]
        if self.source_set_digest != sha256_digest({"sources": source_material}):
            raise ValueError("Compaction source-set digest does not match")
        material = self.model_dump(
            mode="json", exclude={"snapshot_digest", "created_at", "stale_at"}
        )
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Compaction Snapshot digest does not match")
        if self.status is CompactionStatus.ACTIVE:
            if any(item.status is not CompactionSourceStatus.ACTIVE for item in self.source_refs):
                raise ValueError("Active Compaction Snapshot has an inactive source")
            if any(item.status is not CoverageStatus.COVERED for item in self.coverage_items):
                raise ValueError("Active Compaction Snapshot has incomplete coverage")
            if self.stale_at is not None:
                raise ValueError("Active Compaction Snapshot cannot be stale")
        return self


class CreateCompactionSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    parent_snapshot_id: str | None = Field(default=None, pattern=COMPACTION_SNAPSHOT_ID_PATTERN)


class CompactionSnapshotPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[CompactionSnapshot, ...]
