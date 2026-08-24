"""Durable cycle evidence for stage-112C task-loop termination and repair.

Cycle events deliberately live beside, rather than inside, the immutable
stage-112B execution manifest.  A no-progress observation is scoped to one
exact semantic progress digest.  When any execution or node truth changes the
digest changes and the consecutive counter resets without deleting history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.task_plans import TASK_ID_PATTERN

TASK_LOOP_EXECUTION_ID_PATTERN = r"^tlx_[0-9a-f]{64}$"
TASK_LOOP_CYCLE_EVENT_ID_PATTERN = r"^tce_[0-9a-f]{64}$"

TaskLoopCycleEventKind = Literal[
    "no_progress_observed",
    "no_progress_terminated",
    "budget_exhausted",
    "repair_started",
    "repair_completed",
]


class TaskLoopCycleRead(BaseModel):
    """Payload-free cycle summary safe for the Workbench projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-cycle-read.v1"] = (
        "deskpilot.task-loop-cycle-read.v1"
    )
    no_progress_count: int = Field(ge=0, le=3)
    no_progress_limit: Literal[3] = 3
    repair_count: int = Field(ge=0, le=2)
    maximum_plan_generations: Literal[3] = 3
    budget_exhausted: bool
    latest_event_kind: TaskLoopCycleEventKind | None = None
    latest_event_sequence: int = Field(ge=0)
    summary_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def summary_matches(self) -> Self:
        if (self.latest_event_sequence == 0) != (self.latest_event_kind is None):
            raise ValueError("Task-loop cycle summary event pointer is incomplete")
        material = self.model_dump(mode="json", exclude={"summary_digest"})
        if self.summary_digest != sha256_digest(material):
            raise ValueError("Task-loop cycle summary digest does not match")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material = {
            "schema_version": "deskpilot.task-loop-cycle-read.v1",
            "no_progress_limit": 3,
            "maximum_plan_generations": 3,
            **values,
        }
        return cls(**material, summary_digest=sha256_digest(material))


class TaskLoopCycleEvent(BaseModel):
    """One content-addressed observation in a per-execution digest chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-cycle-event.v1"] = (
        "deskpilot.task-loop-cycle-event.v1"
    )
    event_id: str = Field(pattern=TASK_LOOP_CYCLE_EVENT_ID_PATTERN)
    execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    sequence: int = Field(ge=1)
    previous_event_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    kind: TaskLoopCycleEventKind
    plan_generation: int = Field(ge=1, le=3)
    source_progress_digest: str = Field(pattern=DIGEST_PATTERN)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    evidence_manifest: dict[str, Any]
    evidence_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    event_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def chain_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Task-loop cycle event timestamp must be timezone-aware")
        if (self.sequence == 1) != (self.previous_event_digest is None):
            raise ValueError("Task-loop cycle event chain root is invalid")
        if self.evidence_digest != sha256_digest(self.evidence_manifest):
            raise ValueError("Task-loop cycle evidence digest does not match")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"event_id", "event_digest"}
        }
        if self.event_id != f"tce_{sha256_digest(identity)}":
            raise ValueError("Task-loop cycle event id does not match")
        digest_material = {
            key: value for key, value in values.items() if key != "event_digest"
        }
        if self.event_digest != sha256_digest(digest_material):
            raise ValueError("Task-loop cycle event digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        execution_id: str,
        task_id: str,
        sequence: int,
        previous_event_digest: str | None,
        kind: TaskLoopCycleEventKind,
        plan_generation: int,
        source_progress_digest: str,
        reason_code: str,
        evidence_manifest: dict[str, Any],
        created_at: datetime,
    ) -> Self:
        material: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-cycle-event.v1",
            "execution_id": execution_id,
            "task_id": task_id,
            "sequence": sequence,
            "previous_event_digest": previous_event_digest,
            "kind": kind,
            "plan_generation": plan_generation,
            "source_progress_digest": source_progress_digest,
            "reason_code": reason_code,
            "evidence_manifest": evidence_manifest,
            "evidence_digest": sha256_digest(evidence_manifest),
            "created_at": created_at,
        }
        identity = dict(material)
        event_id = f"tce_{sha256_digest(identity)}"
        digest_material = {**material, "event_id": event_id}
        return cls(
            **digest_material,
            event_digest=sha256_digest(digest_material),
        )


__all__ = [
    "TASK_LOOP_CYCLE_EVENT_ID_PATTERN",
    "TaskLoopCycleRead",
    "TaskLoopCycleEvent",
    "TaskLoopCycleEventKind",
]
