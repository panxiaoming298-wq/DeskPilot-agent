"""Win32 handle-backed resource inspection used only by the trusted Runner."""

import ctypes
import os
from ctypes import wintypes

from deskpilot.runner.worker_protocol import BrokeredFilesystemMetadata

if os.name != "nt":
    raise ImportError("Windows resource brokerage is available only on Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
]
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
kernel32.GetVolumePathNameW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
kernel32.GetVolumePathNameW.restype = wintypes.BOOL
kernel32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]
kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL


def _raise_last_error(operation: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed with Win32 error {error}")


def _dos_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _normalized(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def read_filesystem_metadata(identifier: str) -> BrokeredFilesystemMetadata:
    """Open the exact path, verify its final target, then query its volume facts."""
    handle = kernel32.CreateFileW(
        identifier,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    handle_value = handle if isinstance(handle, int) else handle.value
    if handle_value == INVALID_HANDLE_VALUE:
        _raise_last_error("CreateFileW(resource)")
    try:
        required = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if required == 0:
            _raise_last_error("GetFinalPathNameByHandleW(size)")
        final_buffer = ctypes.create_unicode_buffer(required + 1)
        written = kernel32.GetFinalPathNameByHandleW(
            handle,
            final_buffer,
            len(final_buffer),
            0,
        )
        if written == 0 or written >= len(final_buffer):
            _raise_last_error("GetFinalPathNameByHandleW")
        final_path = _dos_path(final_buffer.value)
        if _normalized(final_path) != _normalized(identifier):
            raise OSError("Opened filesystem resource does not match its authorized final path")

        volume_buffer = ctypes.create_unicode_buffer(32_768)
        if not kernel32.GetVolumePathNameW(
            final_path,
            volume_buffer,
            len(volume_buffer),
        ):
            _raise_last_error("GetVolumePathNameW")
        available = ctypes.c_ulonglong()
        total = ctypes.c_ulonglong()
        free = ctypes.c_ulonglong()
        if not kernel32.GetDiskFreeSpaceExW(
            volume_buffer.value,
            ctypes.byref(available),
            ctypes.byref(total),
            ctypes.byref(free),
        ):
            _raise_last_error("GetDiskFreeSpaceExW")
        del available
        return BrokeredFilesystemMetadata(
            identifier=final_path,
            total_bytes=int(total.value),
            used_bytes=int(total.value - free.value),
            free_bytes=int(free.value),
        )
    finally:
        kernel32.CloseHandle(handle)
