"""Validated model Provider configuration without secret material."""

import re
from ipaddress import ip_address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.model_contracts import (
    PROVIDER_ID_PATTERN,
    ModelLocation,
)

CREDENTIAL_ID_PATTERN = r"^DESKPILOT_CREDENTIAL_[A-Z0-9_]{1,96}$"
WINDOWS_CREDENTIAL_ID_PATTERN = r"^[A-Z][A-Z0-9_]{0,95}$"


class CredentialReference(BaseModel):
    """Reference to secret material; the secret itself is never configuration data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["environment", "windows_credential_manager"] = "environment"
    identifier: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_backend_identifier(self) -> Self:
        pattern = (
            CREDENTIAL_ID_PATTERN
            if self.backend == "environment"
            else WINDOWS_CREDENTIAL_ID_PATTERN
        )
        if re.fullmatch(pattern, self.identifier) is None:
            raise ValueError(
                "Credential identifier does not match its backend namespace"
            )
        return self


class FakeProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fake"] = "fake"
    enabled: bool = True
    provider_id: str = Field(default="fake-local", pattern=PROVIDER_ID_PATTERN)
    display_name: str = Field(
        default="DeskPilot Fake Model", min_length=1, max_length=100
    )
    model: str = Field(default="deskpilot-fake-v1", min_length=1, max_length=200)
    delay_seconds: float = Field(default=0, ge=0, le=60)


class OpenAICompatibleChatProviderConfig(BaseModel):
    """Configuration for cloud or local Chat Completions-compatible endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["openai_compatible_chat"] = "openai_compatible_chat"
    enabled: bool = True
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2_048)
    location: ModelLocation
    credential_ref: CredentialReference | None = None
    allow_private_network: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = True
    supports_strict_json_schema: bool = False
    max_context_tokens: int = Field(default=32_768, ge=1, le=10_000_000)
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    max_response_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1_024,
        le=64 * 1024 * 1024,
    )
    health_timeout_seconds: float = Field(default=5, gt=0, le=30)

    @model_validator(mode="after")
    def validate_endpoint_and_credential_policy(self) -> Self:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Provider base_url must use HTTP(S) and include a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Provider base_url cannot contain credentials, query, or fragment"
            )
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("Provider base_url contains an invalid port") from error

        if self.location is ModelLocation.CLOUD:
            if parsed.scheme != "https":
                raise ValueError("Cloud Provider base_url must use HTTPS")
            if self.credential_ref is None:
                raise ValueError("Cloud Provider requires a credential_ref")
            if self.allow_private_network:
                raise ValueError(
                    "Cloud Provider cannot enable the private-network exception"
                )
            return self

        host = parsed.hostname.lower()
        if host == "localhost":
            return self
        try:
            address = ip_address(host)
        except ValueError as error:
            raise ValueError(
                "Local Provider host must be localhost or an IP literal"
            ) from error
        if address.is_loopback:
            return self
        if not (address.is_private or address.is_link_local):
            raise ValueError("Local Provider cannot target a public IP address")
        if not self.allow_private_network:
            raise ValueError(
                "Private-network Provider requires allow_private_network=true"
            )
        return self


ProviderConfig = Annotated[
    FakeProviderConfig | OpenAICompatibleChatProviderConfig,
    Field(discriminator="kind"),
]
