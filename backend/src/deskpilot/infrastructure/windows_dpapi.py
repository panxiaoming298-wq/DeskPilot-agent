"""Current-user Windows DPAPI protection for Provider runtime configuration."""

import ctypes
import sys
from ctypes import wintypes
from typing import Protocol

from deskpilot.application.provider_runtime_store import (
    ProviderRuntimeConfigProtectionError,
    ProviderRuntimeConfigProtectionUnavailableError,
)

CRYPTPROTECT_UI_FORBIDDEN = 0x1
DPAPI_SCHEME = "windows_dpapi_current_user_v1"
_MAX_DPAPI_OUTPUT_BYTES = 4 * 1024 * 1024
_DESCRIPTION = "DeskPilot Provider runtime configuration"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class DataProtectionApi(Protocol):
    def protect(
        self,
        plaintext: bytearray,
        *,
        entropy: bytearray,
        description: str,
    ) -> bytes: ...

    def unprotect(
        self,
        payload: bytes,
        *,
        entropy: bytearray,
    ) -> bytearray: ...


class Win32DataProtectionApi:
    """Thin ctypes adapter around CryptProtectData/CryptUnprotectData."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ProviderRuntimeConfigProtectionUnavailableError(
                "Windows DPAPI is unavailable on this platform"
            )
        self._crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)

        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(
        self,
        plaintext: bytearray,
        *,
        entropy: bytearray,
        description: str,
    ) -> bytes:
        input_blob, input_array = self._blob_from_bytearray(plaintext)
        entropy_blob, entropy_array = self._blob_from_bytearray(entropy)
        output_blob = _DataBlob()
        _ = input_array, entropy_array
        succeeded = self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            description,
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
        return bytes(self._copy_and_free(output_blob, clear_before_free=False))

    def unprotect(
        self,
        payload: bytes,
        *,
        entropy: bytearray,
    ) -> bytearray:
        payload_buffer = bytearray(payload)
        try:
            input_blob, input_array = self._blob_from_bytearray(payload_buffer)
            entropy_blob, entropy_array = self._blob_from_bytearray(entropy)
            output_blob = _DataBlob()
            _ = input_array, entropy_array
            succeeded = self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
            if not succeeded:
                raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
            return self._copy_and_free(output_blob, clear_before_free=True)
        finally:
            payload_buffer[:] = b"\x00" * len(payload_buffer)

    def _copy_and_free(
        self,
        output: _DataBlob,
        *,
        clear_before_free: bool,
    ) -> bytearray:
        if not output.pbData:
            raise OSError(13, "DPAPI returned a null output buffer")
        size = int(output.cbData)
        try:
            if size <= 0 or size > _MAX_DPAPI_OUTPUT_BYTES:
                raise OSError(13, "DPAPI returned an invalid output size")
            return bytearray(ctypes.string_at(output.pbData, size))
        finally:
            if clear_before_free and size > 0:
                ctypes.memset(output.pbData, 0, size)
            self._kernel32.LocalFree(
                ctypes.cast(output.pbData, wintypes.HLOCAL)
            )

    @staticmethod
    def _blob_from_bytearray(
        value: bytearray,
    ) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
        if not value:
            raise ValueError("DPAPI input cannot be empty")
        array = (ctypes.c_ubyte * len(value)).from_buffer(value)
        blob = _DataBlob(
            cbData=len(value),
            pbData=ctypes.cast(array, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, array


class WindowsDpapiProtector:
    """Bind ciphertext to the current Windows user and Provider record context."""

    def __init__(self, api: DataProtectionApi | None = None) -> None:
        self._api = api or Win32DataProtectionApi()

    @property
    def scheme(self) -> str:
        return DPAPI_SCHEME

    def protect(self, plaintext: bytearray, *, context: str) -> bytes:
        entropy = self._entropy(context)
        try:
            return self._api.protect(
                plaintext,
                entropy=entropy,
                description=_DESCRIPTION,
            )
        except OSError as error:
            raise ProviderRuntimeConfigProtectionError(
                operation="protect",
                os_error_code=error.errno,
            ) from error
        finally:
            entropy[:] = b"\x00" * len(entropy)

    def unprotect(self, payload: bytes, *, context: str) -> bytearray:
        entropy = self._entropy(context)
        try:
            return self._api.unprotect(payload, entropy=entropy)
        except OSError as error:
            raise ProviderRuntimeConfigProtectionError(
                operation="unprotect",
                os_error_code=error.errno,
            ) from error
        finally:
            entropy[:] = b"\x00" * len(entropy)

    @staticmethod
    def _entropy(context: str) -> bytearray:
        encoded = bytearray(context.encode("utf-8"))
        if not encoded or len(encoded) > 1_024:
            raise ValueError("DPAPI context has an invalid size")
        return encoded
