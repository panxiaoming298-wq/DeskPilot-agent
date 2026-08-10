"""Secret-free contracts for Provider administration commands and results."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.model_contracts import PROVIDER_ID_PATTERN
from deskpilot.domain.provider_runtime import (
    CredentialAuditDisposition,
    ProviderConfigAuditEvent,
    ProviderRuntimeConfigSnapshot,
)


class ProviderMutationAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    ENABLED = "enabled"
    DISABLED = "disabled"
    DEFAULT_CHANGED = "default_changed"
    DELETED = "deleted"


class ProviderManagementState(BaseModel):
    """Internal decrypted state used by the trusted management service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: int = Field(ge=1)
    imported_at: datetime
    default_provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    providers: tuple[ProviderRuntimeConfigSnapshot, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.imported_at.utcoffset() is None:
            raise ValueError("Provider management timestamp must be timezone-aware")
        provider_ids = [item.bundle.provider_id for item in self.providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Provider management state contains duplicate IDs")
        by_id = {item.bundle.provider_id: item for item in self.providers}
        default = by_id.get(self.default_provider_id)
        if default is None:
            raise ValueError("Default Provider is missing from management state")
        if not default.bundle.config.enabled:
            raise ValueError("Default Provider must be enabled")
        return self


class ProviderMutationResult(BaseModel):
    """Public mutation receipt; it deliberately excludes runtime configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ProviderMutationAction
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    catalog_version: int = Field(ge=1)
    config_revision: int = Field(ge=1)
    default_provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    credential_disposition: CredentialAuditDisposition
    replayed: bool = False


class ProviderConfigAuditPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[ProviderConfigAuditEvent, ...]
    next_sequence: int = Field(ge=0)
