"""Windows Credential Manager adapter using the Unicode Win32 API."""

import ctypes
import sys
from ctypes import wintypes
from typing import Protocol

from pydantic import SecretStr

from deskpilot.application.credential_resolver import (
    CredentialBackendUnavailableError,
    CredentialInvalidError,
    CredentialNotFoundError,
    CredentialOperationError,
)
from deskpilot.domain.provider_config import CredentialReference

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
ERROR_INVALID_DATA = 13
ERROR_NOT_FOUND = 1168
WINDOWS_CREDENTIAL_TARGET_PREFIX = "DeskPilot/ModelProvider/"


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialApiError(OSError):
    def __init__(self, *, operation: str, error_code: int) -> None:
        super().__init__(error_code, f"Windows credential {operation} failed")
        self.operation = operation
        self.error_code = error_code


class WindowsCredentialApi(Protocol):
    def read(self, target_name: str) -> bytearray | None: ...

    def write(self, target_name: str, credential_blob: bytearray) -> None: ...

    def delete(self, target_name: str) -> bool: ...


class Win32CredentialApi:
    """Thin memory-safe wrapper around Advapi32 Cred*W functions."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows Credential Manager is only available on Windows")
        library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_read = library.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_write = library.CredWriteW
        self._cred_write.argtypes = [
            ctypes.POINTER(_CredentialW),
            wintypes.DWORD,
        ]
        self._cred_write.restype = wintypes.BOOL
        self._cred_delete = library.CredDeleteW
        self._cred_delete.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._cred_delete.restype = wintypes.BOOL
        self._cred_free = library.CredFree
        self._cred_free.argtypes = [wintypes.LPVOID]
        self._cred_free.restype = None

    def read(self, target_name: str) -> bytearray | None:
        credential_pointer = ctypes.POINTER(_CredentialW)()
        succeeded = self._cred_read(
            target_name,
            CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential_pointer),
        )
        if not succeeded:
            error_code = ctypes.get_last_error()
            if error_code == ERROR_NOT_FOUND:
                return None
            raise WindowsCredentialApiError(
                operation="read",
                error_code=error_code,
            )
        try:
            credential = credential_pointer.contents
            blob_size = int(credential.CredentialBlobSize)
            if blob_size > CRED_MAX_CREDENTIAL_BLOB_SIZE:
                raise WindowsCredentialApiError(
                    operation="read",
                    error_code=ERROR_INVALID_DATA,
                )
            if blob_size == 0 or not credential.CredentialBlob:
                return bytearray()
            blob_type = wintypes.BYTE * blob_size
            blob_address = ctypes.addressof(credential.CredentialBlob.contents)
            return bytearray(blob_type.from_address(blob_address))
        finally:
            self._cred_free(credential_pointer)

    def write(self, target_name: str, credential_blob: bytearray) -> None:
        if not credential_blob or len(credential_blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise WindowsCredentialApiError(
                operation="write",
                error_code=ERROR_INVALID_DATA,
            )
        blob_type = wintypes.BYTE * len(credential_blob)
        blob_buffer = blob_type.from_buffer(credential_blob)
        credential = _CredentialW(
            Flags=0,
            Type=CRED_TYPE_GENERIC,
            TargetName=target_name,
            Comment="DeskPilot model Provider credential",
            LastWritten=wintypes.FILETIME(),
            CredentialBlobSize=len(credential_blob),
            CredentialBlob=ctypes.cast(
                blob_buffer,
                ctypes.POINTER(wintypes.BYTE),
            ),
            Persist=CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName="DeskPilot",
        )
        if not self._cred_write(ctypes.byref(credential), 0):
            raise WindowsCredentialApiError(
                operation="write",
                error_code=ctypes.get_last_error(),
            )

    def delete(self, target_name: str) -> bool:
        if self._cred_delete(target_name, CRED_TYPE_GENERIC, 0):
            return True
        error_code = ctypes.get_last_error()
        if error_code == ERROR_NOT_FOUND:
            return False
        raise WindowsCredentialApiError(
            operation="delete",
            error_code=error_code,
        )


class WindowsCredentialManager:
    """DeskPilot-namespaced resolver and managed credential store."""

    def __init__(self, api: WindowsCredentialApi | None = None) -> None:
        self._api = api or Win32CredentialApi()

    def resolve(self, reference: CredentialReference) -> SecretStr:
        target_name = self._target_name(reference)
        try:
            credential_blob = self._api.read(target_name)
        except WindowsCredentialApiError as error:
            raise self._operation_error(reference, error) from error
        if credential_blob is None or not credential_blob:
            raise CredentialNotFoundError(
                "Configured Provider credential is unavailable",
                credential_id=reference.identifier,
            )
        try:
            try:
                value = credential_blob.decode("utf-8")
            except UnicodeDecodeError as error:
                raise CredentialInvalidError(
                    "Configured Provider credential has an invalid encoding",
                    credential_id=reference.identifier,
                ) from error
            if not value.strip():
                raise CredentialInvalidError(
                    "Configured Provider credential is blank",
                    credential_id=reference.identifier,
                )
            return SecretStr(value)
        finally:
            self._zeroize(credential_blob)

    def store(self, reference: CredentialReference, secret: SecretStr) -> None:
        target_name = self._target_name(reference)
        value = secret.get_secret_value()
        if not value.strip():
            raise CredentialInvalidError(
                "Provider credential cannot be blank",
                credential_id=reference.identifier,
            )
        credential_blob = bytearray(value.encode("utf-8"))
        try:
            if len(credential_blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
                raise CredentialInvalidError(
                    "Provider credential exceeds the backend size limit",
                    credential_id=reference.identifier,
                )
            try:
                self._api.write(target_name, credential_blob)
            except WindowsCredentialApiError as error:
                raise self._operation_error(reference, error) from error
        finally:
            self._zeroize(credential_blob)

    def delete(self, reference: CredentialReference) -> bool:
        target_name = self._target_name(reference)
        try:
            return self._api.delete(target_name)
        except WindowsCredentialApiError as error:
            raise self._operation_error(reference, error) from error

    @staticmethod
    def _target_name(reference: CredentialReference) -> str:
        if reference.backend != "windows_credential_manager":
            raise CredentialBackendUnavailableError(
                "Credential reference does not target Windows Credential Manager",
                credential_id=reference.identifier,
                backend=reference.backend,
            )
        return f"{WINDOWS_CREDENTIAL_TARGET_PREFIX}{reference.identifier}"

    @staticmethod
    def _operation_error(
        reference: CredentialReference,
        error: WindowsCredentialApiError,
    ) -> CredentialOperationError:
        return CredentialOperationError(
            "Windows Credential Manager operation failed",
            credential_id=reference.identifier,
            operation=error.operation,
            os_error_code=error.error_code,
        )

    @staticmethod
    def _zeroize(buffer: bytearray) -> None:
        buffer[:] = b"\x00" * len(buffer)
