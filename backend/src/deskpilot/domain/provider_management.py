"""Secret-free read models for Provider management APIs."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.model_contracts import (
    PROVIDER_ID_PATTERN,
    ModelProviderDescriptor,
    ProviderHealthStatus,
)


class ProviderHealthCacheStatus(StrEnum):
    FRESH = "fresh"
    CACHED = "cached"
    COALESCED = "coalesced"


class ProviderHealthSnapshot(BaseModel):
    """Public health result; upstream details are deliberately excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    status: ProviderHealthStatus
    checked_at: datetime
    latency_ms: int | None = Field(default=None, ge=0)
    cache_status: ProviderHealthCacheStatus
    expires_at: datetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> "ProviderHealthSnapshot":
        if self.checked_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("Provider health timestamps must be timezone-aware")
        if self.expires_at < self.checked_at:
            raise ValueError("Provider health expiration cannot precede its check")
        return self


class ProviderCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    descriptor: ModelProviderDescriptor
    enabled: bool
    is_default: bool
    cached_health: ProviderHealthSnapshot | None = None


class ProviderCatalogDefinitionEntry(BaseModel):
    """Persistable public projection of one configured Provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    descriptor: ModelProviderDescriptor
    enabled: bool


class ProviderCatalogDefinition(BaseModel):
    """Secret-free startup import payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    providers: tuple[ProviderCatalogDefinitionEntry, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_provider_identity(self) -> Self:
        by_id = {entry.descriptor.provider_id: entry for entry in self.providers}
        if len(by_id) != len(self.providers):
            raise ValueError("Provider catalog definition contains duplicate IDs")
        default = by_id.get(self.default_provider_id)
        if default is None:
            raise ValueError("Default Provider is missing from catalog definition")
        if not default.enabled:
            raise ValueError("Default Provider must be enabled")
        return self


class PersistedProviderCatalog(BaseModel):
    """Versioned catalog projection loaded from persistent storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: int = Field(ge=1)
    imported_at: datetime
    definition: ProviderCatalogDefinition

    @model_validator(mode="after")
    def validate_imported_at(self) -> Self:
        if self.imported_at.utcoffset() is None:
            raise ValueError("Provider catalog import timestamp must be timezone-aware")
        return self


class ProviderCatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: int = Field(ge=1)
    imported_at: datetime
    default_provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    providers: tuple[ProviderCatalogEntry, ...]

    @model_validator(mode="after")
    def validate_imported_at(self) -> Self:
        if self.imported_at.utcoffset() is None:
            raise ValueError("Provider catalog import timestamp must be timezone-aware")
        return self
