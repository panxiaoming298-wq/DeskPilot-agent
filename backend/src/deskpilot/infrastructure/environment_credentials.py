"""Environment-backed credential resolver with a narrow variable namespace."""

import os
from collections.abc import Mapping

from pydantic import SecretStr

from deskpilot.application.credential_resolver import (
    CredentialBackendUnavailableError,
    CredentialNotFoundError,
)
from deskpilot.domain.provider_config import CredentialReference


class EnvironmentCredentialResolver:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = environment if environment is not None else os.environ

    def resolve(self, reference: CredentialReference) -> SecretStr:
        if reference.backend != "environment":
            raise CredentialBackendUnavailableError(
                "Credential backend is not available through this resolver",
                credential_id=reference.identifier,
                backend=reference.backend,
            )
        value = self._environment.get(reference.identifier)
        if value is None or not value.strip():
            raise CredentialNotFoundError(
                "Configured Provider credential is unavailable",
                credential_id=reference.identifier,
            )
        return SecretStr(value)
