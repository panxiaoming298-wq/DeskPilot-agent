"""Public, secret-free contracts for locally managed Provider credentials."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from deskpilot.domain.provider_config import WINDOWS_CREDENTIAL_ID_PATTERN


class ManagedCredentialWrite(BaseModel):
    """Write-only request body; the secret is never part of a response model."""

    model_config = ConfigDict(extra="forbid")

    secret: SecretStr = Field(min_length=1, max_length=2_560)

    @field_validator("secret")
    @classmethod
    def reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Provider credential cannot be blank")
        return value


class ManagedCredentialStatus(BaseModel):
    """Secret-free state returned to the settings UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.managed-credential-status.v1"] = (
        "deskpilot.managed-credential-status.v1"
    )
    backend: Literal["windows_credential_manager"] = "windows_credential_manager"
    identifier: str = Field(pattern=WINDOWS_CREDENTIAL_ID_PATTERN)
    state: Literal["available", "missing", "invalid"]
    writable: Literal[True] = True
    deleted: bool = False
