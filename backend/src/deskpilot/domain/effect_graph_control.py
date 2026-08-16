"""Durable, owner-targeted control messages for live effect DAGs."""

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EffectGraphControlCommand(StrEnum):
    CANCEL = "cancel"


class EffectGraphControlStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    APPLIED = "applied"
    SUPERSEDED = "superseded"


def effect_graph_control_id(
    graph_id: str,
    command: EffectGraphControlCommand,
) -> str:
    material = f"{graph_id}\x1f{command.value}".encode()
    return f"egc_{hashlib.sha256(material).hexdigest()}"


class EffectGraphControlRead(BaseModel):
    """Current durable routing and acknowledgement state for one graph command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    control_id: str = Field(pattern=r"^egc_[0-9a-f]{64}$")
    task_id: str
    graph_id: str
    command: EffectGraphControlCommand
    reason: str | None = Field(default=None, max_length=500)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_by: str = Field(min_length=1, max_length=80)
    target_owner_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_fencing_token: int | None = Field(default=None, ge=1)
    status: EffectGraphControlStatus
    revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    last_error_code: str | None = Field(default=None, max_length=120)
    available_at: datetime
    applied_graph_fencing_token: int | None = Field(default=None, ge=1)
    created_at: datetime
    updated_at: datetime
    applied_at: datetime | None = None


class EffectGraphControlClaimRead(EffectGraphControlRead):
    """One mailbox delivery claimed by the exact target owner generation."""

    claim_owner_id: str = Field(min_length=1, max_length=80)
    claim_fencing_token: int = Field(ge=1)
    claim_acquired_at: datetime
    claim_expires_at: datetime


__all__ = [
    "EffectGraphControlClaimRead",
    "EffectGraphControlCommand",
    "EffectGraphControlRead",
    "EffectGraphControlStatus",
    "effect_graph_control_id",
]
