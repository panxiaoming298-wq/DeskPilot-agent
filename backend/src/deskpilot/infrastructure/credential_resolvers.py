"""Credential resolver composition without fallback across trust backends."""

import sys
from collections.abc import Mapping

from pydantic import SecretStr

from deskpilot.application.credential_resolver import (
    CredentialBackendUnavailableError,
    CredentialResolver,
    ManagedCredentialStore,
)
from deskpilot.domain.provider_config import CredentialReference
from deskpilot.infrastructure.environment_credentials import (
    EnvironmentCredentialResolver,
)
from deskpilot.infrastructure.windows_credentials import WindowsCredentialManager


class CompositeCredentialResolver:
    """Dispatch by explicit backend; never silently fall back to another store."""

    def __init__(self, resolvers: Mapping[str, CredentialResolver]) -> None:
        self._resolvers = dict(resolvers)

    def resolve(self, reference: CredentialReference) -> SecretStr:
        resolver = self._resolvers.get(reference.backend)
        if resolver is None:
            raise CredentialBackendUnavailableError(
                "Configured credential backend is unavailable",
                credential_id=reference.identifier,
                backend=reference.backend,
            )
        return resolver.resolve(reference)


def create_default_credential_services() -> tuple[
    CredentialResolver,
    ManagedCredentialStore | None,
]:
    resolvers: dict[str, CredentialResolver] = {
        "environment": EnvironmentCredentialResolver()
    }
    managed_store: ManagedCredentialStore | None = None
    if sys.platform == "win32":
        manager = WindowsCredentialManager()
        resolvers["windows_credential_manager"] = manager
        managed_store = manager
    return CompositeCredentialResolver(resolvers), managed_store


def create_default_credential_resolver() -> CredentialResolver:
    resolver, _ = create_default_credential_services()
    return resolver
