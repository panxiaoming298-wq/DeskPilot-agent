"""Windows DACL projection for the dedicated AppContainer worker runtime."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

if os.name != "nt":
    raise ImportError("Windows ACL support is available only on Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernelbase = ctypes.WinDLL("kernelbase", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

LPVOID = ctypes.c_void_p
PACL = LPVOID
PSID = LPVOID

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
GRANT_ACCESS = 1
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
TRUSTEE_IS_SID = 0
TRUSTEE_IS_USER = 1
TRUSTEE_IS_WELL_KNOWN_GROUP = 5
FILE_ALL_ACCESS = 0x001F01FF
FILE_GENERIC_READ_EXECUTE = 0x001200A9

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"


class TRUSTEE_W(ctypes.Structure):
    pass


TRUSTEE_W._fields_ = [
    ("pMultipleTrustee", ctypes.POINTER(TRUSTEE_W)),
    ("MultipleTrusteeOperation", ctypes.c_int),
    ("TrusteeForm", ctypes.c_int),
    ("TrusteeType", ctypes.c_int),
    ("ptstrName", wintypes.LPWSTR),
]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [
        ("Sid", PSID),
        ("Attributes", wintypes.DWORD),
    ]


def _configure_functions() -> None:
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernelbase.DeriveCapabilitySidsFromName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(PSID)),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(PSID)),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernelbase.DeriveCapabilitySidsFromName.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(PSID),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.BuildTrusteeWithSidW.argtypes = [ctypes.POINTER(TRUSTEE_W), PSID]
    advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        PACL,
        ctypes.POINTER(PACL),
    ]
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        PSID,
        PSID,
        PACL,
        PACL,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD


_configure_functions()


class WindowsAclError(RuntimeError):
    """The dedicated worker runtime ACL could not be established."""


def _raise_last_error(operation: str) -> None:
    raise WindowsAclError(f"{operation} failed with Win32 error {ctypes.get_last_error()}")


def _free_local(pointer: object) -> None:
    if pointer:
        kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))  # type: ignore[arg-type]


def _derive_capability_sid(capability_name: str) -> PSID:
    groups = ctypes.POINTER(PSID)()
    capabilities = ctypes.POINTER(PSID)()
    group_count = wintypes.DWORD()
    capability_count = wintypes.DWORD()
    if not kernelbase.DeriveCapabilitySidsFromName(
        capability_name,
        ctypes.byref(groups),
        ctypes.byref(group_count),
        ctypes.byref(capabilities),
        ctypes.byref(capability_count),
    ):
        _raise_last_error("DeriveCapabilitySidsFromName")
    capability_sid = PSID()
    try:
        if group_count.value != 1 or capability_count.value != 1:
            raise WindowsAclError("Windows returned an unexpected capability SID set")
        capability_sid = PSID(capabilities[0])
        capabilities[0] = PSID()
        return capability_sid
    finally:
        for index in range(group_count.value):
            _free_local(groups[index])
        for index in range(capability_count.value):
            _free_local(capabilities[index])
        _free_local(groups)
        _free_local(capabilities)


def capability_sid_string(capability_name: str) -> str:
    sid = _derive_capability_sid(capability_name)
    value = wintypes.LPWSTR()
    try:
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            _raise_last_error("ConvertSidToStringSidW")
        if not value.value:
            raise WindowsAclError("Windows returned an empty capability SID")
        return value.value
    finally:
        _free_local(value)
        _free_local(sid)


def _current_user_sid() -> tuple[wintypes.HANDLE, ctypes.Array[ctypes.c_char], PSID]:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        _raise_last_error("OpenProcessToken")
    required = wintypes.DWORD()
    advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
    if not required.value:
        kernel32.CloseHandle(token)
        _raise_last_error("GetTokenInformation(size)")
    buffer = ctypes.create_string_buffer(required.value)
    if not advapi32.GetTokenInformation(
        token,
        1,
        ctypes.cast(buffer, LPVOID),
        required,
        ctypes.byref(required),
    ):
        kernel32.CloseHandle(token)
        _raise_last_error("GetTokenInformation(TokenUser)")
    user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
    return token, buffer, user.Sid


def _string_sid(value: str) -> PSID:
    sid = PSID()
    if not advapi32.ConvertStringSidToSidW(value, ctypes.byref(sid)):
        _raise_last_error("ConvertStringSidToSidW")
    return sid


def _entry(sid: PSID, access: int, trustee_type: int) -> EXPLICIT_ACCESS_W:
    entry = EXPLICIT_ACCESS_W()
    entry.grfAccessPermissions = access
    entry.grfAccessMode = GRANT_ACCESS
    entry.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
    advapi32.BuildTrusteeWithSidW(ctypes.byref(entry.Trustee), sid)
    entry.Trustee.TrusteeType = trustee_type
    return entry


def _protect_tree(
    resolved: Path,
    reader_sid: PSID,
    reader_type: int,
    reader_access: int,
) -> None:
    token = wintypes.HANDLE()
    user_buffer: ctypes.Array[ctypes.c_char] | None = None
    user_sid = system_sid = administrators_sid = PSID()
    new_acl = PACL()
    try:
        token, user_buffer, user_sid = _current_user_sid()
        system_sid = _string_sid(SYSTEM_SID)
        administrators_sid = _string_sid(ADMINISTRATORS_SID)
        entries = (EXPLICIT_ACCESS_W * 4)(
            _entry(user_sid, FILE_ALL_ACCESS, TRUSTEE_IS_USER),
            _entry(system_sid, FILE_ALL_ACCESS, TRUSTEE_IS_WELL_KNOWN_GROUP),
            _entry(
                administrators_sid,
                FILE_ALL_ACCESS,
                TRUSTEE_IS_WELL_KNOWN_GROUP,
            ),
            _entry(
                reader_sid,
                reader_access,
                reader_type,
            ),
        )
        result = int(advapi32.SetEntriesInAclW(4, entries, None, ctypes.byref(new_acl)))
        if result != 0:
            raise WindowsAclError(f"SetEntriesInAclW failed with Win32 error {result}")
        mutable_path = ctypes.create_unicode_buffer(str(resolved))
        result = int(
            advapi32.SetNamedSecurityInfoW(
                mutable_path,
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                new_acl,
                None,
            )
        )
        if result != 0:
            raise WindowsAclError(f"SetNamedSecurityInfoW failed with Win32 error {result}")
    finally:
        del user_buffer
        if token:
            kernel32.CloseHandle(token)
        _free_local(system_sid)
        _free_local(administrators_sid)
        _free_local(new_acl)


def protect_worker_runtime(path: Path, capability_name: str) -> str:
    """Protect a new runtime tree and grant one capability read/execute access."""
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise WindowsAclError("Worker runtime ACL target must be a directory")
    capability_sid = _derive_capability_sid(capability_name)
    try:
        _protect_tree(
            resolved,
            capability_sid,
            TRUSTEE_IS_WELL_KNOWN_GROUP,
            FILE_GENERIC_READ_EXECUTE,
        )
        return capability_sid_string(capability_name)
    finally:
        _free_local(capability_sid)


def protect_appcontainer_read_path(path: Path, appcontainer_sid: str) -> None:
    """Grant one per-invocation AppContainer SID read-only tree access."""
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise WindowsAclError("AppContainer read target must be a directory")
    sid = _string_sid(appcontainer_sid)
    try:
        _protect_tree(resolved, sid, TRUSTEE_IS_USER, FILE_GENERIC_READ_EXECUTE)
    finally:
        _free_local(sid)


def protect_appcontainer_write_path(path: Path, appcontainer_sid: str) -> None:
    """Grant one per-invocation AppContainer SID access to its scratch tree."""
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise WindowsAclError("AppContainer scratch target must be a directory")
    sid = _string_sid(appcontainer_sid)
    try:
        _protect_tree(resolved, sid, TRUSTEE_IS_USER, FILE_ALL_ACCESS)
    finally:
        _free_local(sid)
