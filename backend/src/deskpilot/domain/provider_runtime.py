"""Protected Provider runtime configuration and secret-free audit contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.model_contracts import PROVIDER_ID_PATTERN, REQUEST_ID_PATTERN
from deskpilot.domain.provider_config import ProviderConfig


class CredentialDeletionPolicy(StrEnum):
    """Provider deletion never implicitly deletes a referenced credential."""

    RETAIN = "retain"


class ProviderRuntimeConfigBundle(BaseModel):
    """Versioned plaintext shape before protection at the persistence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    config: ProviderConfig
    credential_deletion_policy: Literal[CredentialDeletionPolicy.RETAIN] = (
        CredentialDeletionPolicy.RETAIN
    )

    @model_validator(mode="after")
    def validate_provider_identity(self) -> Self:
        if self.provider_id != self.config.provider_id:
            raise ValueError("Runtime bundle Provider IDs must match")
        return self

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "ProviderRuntimeConfigBundle":
        return cls(provider_id=config.provider_id, config=config)


class ProviderRuntimeConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    bundle: ProviderRuntimeConfigBundle

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("Runtime configuration timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Runtime configuration update cannot precede creation")
        return self


class ProviderConfigAuditAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    ENABLED = "enabled"
    DISABLED = "disabled"
    DEFAULT_CHANGED = "default_changed"
    DELETED = "deleted"


class ProviderConfigAuditSource(StrEnum):
    STARTUP_IMPORT = "startup_import"
    LOCAL_API = "local_api"


class ProviderConfigActorType(StrEnum):
    SYSTEM = "system"
    LOCAL_USER = "local_user"


class CredentialAuditDisposition(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    REFERENCE_UNCHANGED = "reference_unchanged"
    REFERENCE_ATTACHED = "reference_attached"
    REFERENCE_CHANGED_OLD_RETAINED = "reference_changed_old_retained"
    REFERENCE_REMOVED_OLD_RETAINED = "reference_removed_old_retained"
    PROVIDER_DELETED_CREDENTIAL_RETAINED = (
        "provider_deleted_credential_retained"
    )


class ProviderConfigAuditContext(BaseModel):
    """Trusted mutation context; it never contains a session token or secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ProviderConfigAuditSource
    actor_type: ProviderConfigActorType
    correlation_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)

    @model_validator(mode="after")
    def validate_actor_source(self) -> Self:
        expected_actor = (
            ProviderConfigActorType.SYSTEM
            if self.source is ProviderConfigAuditSource.STARTUP_IMPORT
            else ProviderConfigActorType.LOCAL_USER
        )
        if self.actor_type is not expected_actor:
            raise ValueError("Provider audit actor does not match its source")
        return self


class ProviderConfigAuditEvent(BaseModel):
    """Append-only audit projection with field names but never field values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    event_id: str = Field(pattern=r"^pca_[0-9a-f]{32}$")
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    action: ProviderConfigAuditAction
    source: ProviderConfigAuditSource
    actor_type: ProviderConfigActorType
    config_revision: int = Field(ge=1)
    changed_fields: tuple[
        str,
        ...,
    ] = Field(max_length=32)
    credential_disposition: CredentialAuditDisposition
    correlation_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.occurred_at.utcoffset() is None:
            raise ValueError("Provider audit timestamp must be timezone-aware")
        if tuple(sorted(set(self.changed_fields))) != self.changed_fields:
            raise ValueError("Provider audit changed fields must be sorted and unique")
        if any(
            not field_name
            or len(field_name) > 64
            or not field_name.replace("_", "a").isalnum()
            for field_name in self.changed_fields
        ):
            raise ValueError("Provider audit changed field name is invalid")
        return self
