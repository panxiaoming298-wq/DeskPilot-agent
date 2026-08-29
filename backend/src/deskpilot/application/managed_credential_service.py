"""Application service for secret-free Windows credential administration."""

from typing import Literal

from pydantic import SecretStr

from deskpilot.application.credential_resolver import (
    CredentialInvalidError,
    CredentialNotFoundError,
    ManagedCredentialStore,
)
from deskpilot.domain.managed_credentials import ManagedCredentialStatus
from deskpilot.domain.provider_config import CredentialReference


class ManagedCredentialService:
    """Stores credentials while exposing only availability state."""

    def __init__(self, store: ManagedCredentialStore) -> None:
        self._store = store

    def status(self, identifier: str) -> ManagedCredentialStatus:
        reference = self._reference(identifier)
        state: Literal["available", "missing", "invalid"]
        try:
            _ = self._store.resolve(reference)
        except CredentialNotFoundError:
            state = "missing"
        except CredentialInvalidError:
            state = "invalid"
        else:
            state = "available"
        return ManagedCredentialStatus(identifier=identifier, state=state)

    def store(self, identifier: str, secret: SecretStr) -> ManagedCredentialStatus:
        self._store.store(self._reference(identifier), secret)
        return ManagedCredentialStatus(identifier=identifier, state="available")

    def delete(self, identifier: str) -> ManagedCredentialStatus:
        deleted = self._store.delete(self._reference(identifier))
        return ManagedCredentialStatus(
            identifier=identifier,
            state="missing",
            deleted=deleted,
        )

    @staticmethod
    def _reference(identifier: str) -> CredentialReference:
        return CredentialReference(
            backend="windows_credential_manager",
            identifier=identifier,
        )
