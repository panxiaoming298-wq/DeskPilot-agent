"""Immutable lineage for same-conversation workspace coding amendments."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.task_plans import (
    CONVERSATION_ID_PATTERN,
    MESSAGE_ID_PATTERN,
    TASK_ID_PATTERN,
)

WORKSPACE_CODING_AMENDMENT_ID_PATTERN = r"^wca_[0-9a-f]{64}$"
TASK_LOOP_EXECUTION_ID_PATTERN = r"^tlx_[0-9a-f]{64}$"


class WorkspaceCodingAmendmentBinding(BaseModel):
    """Bind a terminal source generation to one immutable successor user turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-amendment-binding.v1"] = (
        "deskpilot.workspace-coding-amendment-binding.v1"
    )
    amendment_id: str = Field(pattern=WORKSPACE_CODING_AMENDMENT_ID_PATTERN)
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    source_task_id: str = Field(pattern=TASK_ID_PATTERN)
    source_execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    source_contract_version: int = Field(ge=1)
    source_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    source_plan_generation: int = Field(ge=1)
    source_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    source_execution_digest: str = Field(pattern=DIGEST_PATTERN)
    source_execution_event_digest: str = Field(pattern=DIGEST_PATTERN)
    successor_task_id: str = Field(pattern=TASK_ID_PATTERN)
    successor_user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    successor_user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    reason_code: Literal["user_conversation_amendment"] = (
        "user_conversation_amendment"
    )
    changed_fields: tuple[Literal["goal_ref", "normalized_objective"], ...] = (
        "goal_ref",
        "normalized_objective",
    )
    created_at: datetime
    amendment_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def scope_and_digest_match(self) -> Self:
        if (
            self.source_task_id == self.successor_task_id
            or self.changed_fields != ("goal_ref", "normalized_objective")
            or self.created_at.tzinfo is None
        ):
            raise ValueError("Workspace coding amendment scope is invalid")
        material = self.model_dump(mode="json", exclude={"amendment_digest"})
        if self.amendment_digest != sha256_digest(material):
            raise ValueError("Workspace coding amendment digest changed")
        identity_material = {
            key: value
            for key, value in material.items()
            if key not in {"amendment_id", "created_at"}
        }
        if self.amendment_id != f"wca_{sha256_digest(identity_material)}":
            raise ValueError("Workspace coding amendment identity changed")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material = {
            "schema_version": "deskpilot.workspace-coding-amendment-binding.v1",
            **values,
        }
        material.setdefault("reason_code", "user_conversation_amendment")
        material.setdefault(
            "changed_fields",
            ("goal_ref", "normalized_objective"),
        )
        identity_material = {
            key: value
            for key, value in material.items()
            if key not in {"amendment_id", "created_at"}
        }
        material["amendment_id"] = f"wca_{sha256_digest(identity_material)}"
        return cls(**material, amendment_digest=sha256_digest(material))
