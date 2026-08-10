"""Port and stable errors for resolving secret credential references."""

from typing import Protocol

from pydantic import SecretStr

from deskpilot.domain.provider_config import CredentialReference


class CredentialResolutionError(RuntimeError):
    code = "CREDENTIAL_RESOLUTION_FAILED"

    def __init__(self, message: str, *, credential_id: str) -> None:
        super().__init__(message)
        self.credential_id = credential_id


class CredentialNotFoundError(CredentialResolutionError):
    code = "CREDENTIAL_NOT_FOUND"


class CredentialBackendUnavailableError(CredentialResolutionError):
    code = "CREDENTIAL_BACKEND_UNAVAILABLE"

    def __init__(
        self,
        message: str,
        *,
        credential_id: str,
        backend: str,
    ) -> None:
        super().__init__(message, credential_id=credential_id)
        self.backend = backend


class CredentialInvalidError(CredentialResolutionError):
    code = "CREDENTIAL_INVALID"


class CredentialOperationError(CredentialResolutionError):
    code = "CREDENTIAL_BACKEND_OPERATION_FAILED"

    def __init__(
        self,
        message: str,
        *,
        credential_id: str,
        operation: str,
        os_error_code: int,
    ) -> None:
        super().__init__(message, credential_id=credential_id)
        self.operation = operation
        self.os_error_code = os_error_code


class CredentialResolver(Protocol):
    def resolve(self, reference: CredentialReference) -> SecretStr: ...


class ManagedCredentialStore(CredentialResolver, Protocol):
    def store(self, reference: CredentialReference, secret: SecretStr) -> None: ...

    def delete(self, reference: CredentialReference) -> bool: ...
