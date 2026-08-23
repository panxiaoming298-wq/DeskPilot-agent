"""ctypes Win32 launcher for low-integrity, restricted, per-call workers."""

import ctypes
import msvcrt
import os
import shutil
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from deskpilot.runner.process_isolation import (
    IsolatedProcessCancelledError,
    IsolatedProcessResult,
    IsolationMode,
    IsolationPolicy,
    NetworkIsolationMode,
    ProcessIsolationError,
    ProcessIsolationUnavailableError,
    ProcessLauncher,
    sanitized_worker_environment,
)
from deskpilot.runner.profile_journal import (
    AppContainerProfileJournal,
    ProfileJournalError,
)
from deskpilot.runner.worker_protocol import MAX_WORKER_FRAME_BYTES
from deskpilot.runner.worker_runtime import WORKER_RUNTIME_CAPABILITY

if os.name != "nt":
    raise ImportError("Windows sandbox support is available only on Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernelbase = ctypes.WinDLL("kernelbase", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)
ole32 = ctypes.OleDLL("ole32")

LPVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
DISABLE_MAX_PRIVILEGE = 0x0001
LUA_TOKEN = 0x0004
TOKEN_INTEGRITY_LEVEL = 25
TOKEN_PRIVILEGES = 3
SE_GROUP_INTEGRITY = 0x00000020
LOW_INTEGRITY_SID = "S-1-16-4096"

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
HANDLE_FLAG_INHERIT = 0x00000001

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", LPVOID), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", LPVOID),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


def _configure_functions() -> None:
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SIZE_T),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        LPVOID,
        wintypes.DWORD,
        SIZE_T,
        LPVOID,
        SIZE_T,
        LPVOID,
        LPVOID,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [LPVOID]
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        LPVOID,
        LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernelbase.DeriveCapabilitySidsFromName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(LPVOID)),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(LPVOID)),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernelbase.DeriveCapabilitySidsFromName.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        LPVOID,
        wintypes.DWORD,
        LPVOID,
        wintypes.DWORD,
        LPVOID,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(LPVOID),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.FreeSid.argtypes = [LPVOID]
    advapi32.FreeSid.restype = LPVOID
    advapi32.GetLengthSid.argtypes = [LPVOID]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.SetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        LPVOID,
        wintypes.DWORD,
    ]
    advapi32.SetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [LPVOID]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [LPVOID, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        LPVOID,
        LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(SID_AND_ATTRIBUTES),
        wintypes.DWORD,
        ctypes.POINTER(LPVOID),
    ]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.GetAppContainerFolderPath.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    userenv.GetAppContainerFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [LPVOID]


_configure_functions()


def _raise_last_error(operation: str) -> None:
    error = ctypes.get_last_error()
    raise ProcessIsolationError(f"{operation} failed with Win32 error {error}")


def _close_handle(handle: wintypes.HANDLE | None) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def _handle_integer(handle: wintypes.HANDLE) -> int:
    value = handle.value
    if not isinstance(value, int):
        raise ProcessIsolationError("Win32 returned an invalid handle value")
    return value


@dataclass(frozen=True, slots=True)
class WindowsSecuritySnapshot:
    integrity_level_rid: int
    privilege_count: int
    is_in_job: bool


@dataclass(frozen=True, slots=True)
class _AppContainerProfile:
    name: str
    sid: LPVOID
    sid_text: str
    local_app_data: str


def _hresult_message(operation: str, result: int) -> ProcessIsolationError:
    return ProcessIsolationError(f"{operation} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")


def _delete_appcontainer_profile_name(profile_name: str) -> None:
    result = int(userenv.DeleteAppContainerProfile(profile_name))
    if result < 0:
        raise _hresult_message("DeleteAppContainerProfile", result)


def _create_appcontainer_profile(
    journal: AppContainerProfileJournal | None = None,
) -> _AppContainerProfile:
    name = f"DeskPilot.Worker.{uuid4().hex}"
    sid = LPVOID()
    if journal is not None:
        journal.register(name)
    result = int(
        userenv.CreateAppContainerProfile(
            name,
            "DeskPilot isolated Tool worker",
            "One invocation with no network capabilities",
            None,
            0,
            ctypes.byref(sid),
        )
    )
    if result < 0:
        if journal is not None:
            try:
                _delete_appcontainer_profile_name(name)
            finally:
                journal.unregister(name)
        raise _hresult_message("CreateAppContainerProfile", result)

    sid_text = wintypes.LPWSTR()
    folder = wintypes.LPWSTR()
    try:
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            _raise_last_error("ConvertSidToStringSidW")
        result = int(
            userenv.GetAppContainerFolderPath(
                sid_text,
                ctypes.byref(folder),
            )
        )
        if result < 0:
            raise _hresult_message("GetAppContainerFolderPath", result)
        sid_value = sid_text.value
        if not sid_value:
            raise ProcessIsolationError("AppContainer profile SID is unavailable")
        if not folder.value:
            raise ProcessIsolationError("AppContainer profile folder is unavailable")
        return _AppContainerProfile(
            name=name,
            sid=sid,
            sid_text=sid_value,
            local_app_data=folder.value,
        )
    except Exception:
        advapi32.FreeSid(sid)
        try:
            _delete_appcontainer_profile_name(name)
        finally:
            if journal is not None:
                journal.unregister(name)
        raise
    finally:
        if sid_text:
            kernel32.LocalFree(sid_text)
        if folder:
            ole32.CoTaskMemFree(folder)


def _delete_appcontainer_profile(
    profile: _AppContainerProfile,
    journal: AppContainerProfileJournal | None = None,
) -> None:
    try:
        _delete_appcontainer_profile_name(profile.name)
        if journal is not None:
            journal.unregister(profile.name)
    finally:
        advapi32.FreeSid(profile.sid)


def _free_local(pointer: object) -> None:
    if pointer:
        kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))  # type: ignore[arg-type]


def _derive_worker_runtime_capability() -> LPVOID:
    groups = ctypes.POINTER(LPVOID)()
    capabilities = ctypes.POINTER(LPVOID)()
    group_count = wintypes.DWORD()
    capability_count = wintypes.DWORD()
    if not kernelbase.DeriveCapabilitySidsFromName(
        WORKER_RUNTIME_CAPABILITY,
        ctypes.byref(groups),
        ctypes.byref(group_count),
        ctypes.byref(capabilities),
        ctypes.byref(capability_count),
    ):
        _raise_last_error("DeriveCapabilitySidsFromName")
    capability_sid = LPVOID()
    try:
        if group_count.value != 1 or capability_count.value != 1:
            raise ProcessIsolationError("Windows returned an unexpected worker capability SID set")
        capability_sid = LPVOID(capabilities[0])
        capabilities[0] = LPVOID()
        return capability_sid
    finally:
        for index in range(group_count.value):
            _free_local(groups[index])
        for index in range(capability_count.value):
            _free_local(capabilities[index])
        _free_local(groups)
        _free_local(capabilities)


def _token_information(
    token: wintypes.HANDLE,
    information_class: int,
) -> ctypes.Array[ctypes.c_char]:
    required = wintypes.DWORD()
    advapi32.GetTokenInformation(
        token,
        information_class,
        None,
        0,
        ctypes.byref(required),
    )
    if required.value == 0:
        _raise_last_error("GetTokenInformation(size)")
    buffer = ctypes.create_string_buffer(required.value)
    if not advapi32.GetTokenInformation(
        token,
        information_class,
        buffer,
        required.value,
        ctypes.byref(required),
    ):
        _raise_last_error("GetTokenInformation")
    return buffer


def current_process_security_snapshot() -> WindowsSecuritySnapshot:
    """Return narrow facts used by startup diagnostics and isolation tests."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        _raise_last_error("OpenProcessToken")
    try:
        integrity_buffer = _token_information(token, TOKEN_INTEGRITY_LEVEL)
        label = ctypes.cast(
            integrity_buffer,
            ctypes.POINTER(TOKEN_MANDATORY_LABEL),
        ).contents
        count_pointer = advapi32.GetSidSubAuthorityCount(label.Label.Sid)
        if not count_pointer or count_pointer.contents.value == 0:
            raise ProcessIsolationError("Token integrity SID is invalid")
        rid_pointer = advapi32.GetSidSubAuthority(
            label.Label.Sid,
            count_pointer.contents.value - 1,
        )
        if not rid_pointer:
            raise ProcessIsolationError("Token integrity RID is unavailable")

        privilege_buffer = _token_information(token, TOKEN_PRIVILEGES)
        privilege_count = int(
            ctypes.cast(privilege_buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
        )
        in_job = wintypes.BOOL()
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
            _raise_last_error("IsProcessInJob")
        return WindowsSecuritySnapshot(
            integrity_level_rid=int(rid_pointer.contents.value),
            privilege_count=privilege_count,
            is_in_job=bool(in_job.value),
        )
    finally:
        _close_handle(token)


def _create_restricted_low_token() -> wintypes.HANDLE:
    source = wintypes.HANDLE()
    restricted = wintypes.HANDLE()
    access = TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), access, ctypes.byref(source)):
        _raise_last_error("OpenProcessToken")
    try:
        if not advapi32.CreateRestrictedToken(
            source,
            DISABLE_MAX_PRIVILEGE | LUA_TOKEN,
            0,
            None,
            0,
            None,
            0,
            None,
            ctypes.byref(restricted),
        ):
            _raise_last_error("CreateRestrictedToken")
        sid = LPVOID()
        if not advapi32.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
            _raise_last_error("ConvertStringSidToSidW")
        try:
            label = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(sid, SE_GROUP_INTEGRITY))
            label_size = ctypes.sizeof(label) + int(advapi32.GetLengthSid(sid))
            if not advapi32.SetTokenInformation(
                restricted,
                TOKEN_INTEGRITY_LEVEL,
                ctypes.byref(label),
                label_size,
            ):
                _raise_last_error("SetTokenInformation(TokenIntegrityLevel)")
        finally:
            kernel32.LocalFree(sid)
        return restricted
    except Exception:
        _close_handle(restricted)
        raise
    finally:
        _close_handle(source)


def _create_job(policy: IsolationPolicy) -> wintypes.HANDLE:
    job = wintypes.HANDLE(kernel32.CreateJobObjectW(None, None))
    if not job:
        _raise_last_error("CreateJobObjectW")
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    limits.BasicLimitInformation.ActiveProcessLimit = policy.active_process_limit
    limits.ProcessMemoryLimit = policy.memory_limit_bytes
    if not kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        _close_handle(job)
        _raise_last_error("SetInformationJobObject")
    return job


def _create_pipe() -> tuple[wintypes.HANDLE, wintypes.HANDLE]:
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    attributes = SECURITY_ATTRIBUTES(
        ctypes.sizeof(SECURITY_ATTRIBUTES),
        None,
        True,
    )
    if not kernel32.CreatePipe(
        ctypes.byref(read_handle),
        ctypes.byref(write_handle),
        ctypes.byref(attributes),
        0,
    ):
        _raise_last_error("CreatePipe")
    return read_handle, write_handle


def _make_parent_only(handle: wintypes.HANDLE) -> None:
    if not kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0):
        _raise_last_error("SetHandleInformation")


def _environment_block(environment: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    content = "\0".join(
        f"{key}={value}"
        for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
    )
    return ctypes.create_unicode_buffer(content + "\0\0")


def _read_handle(
    handle: wintypes.HANDLE,
    *,
    limit: int,
    destination: list[bytes],
) -> None:
    try:
        descriptor = msvcrt.open_osfhandle(_handle_integer(handle), os.O_RDONLY | os.O_BINARY)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = stream.read(min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            destination.append(b"".join(chunks))
    except OSError:
        destination.append(b"")


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
    )


def _mirror_tree(source: Path, target: Path, *, hardlink: bool) -> None:
    resolved = source.resolve(strict=True)
    if not resolved.is_dir() or target.exists():
        raise ProcessIsolationUnavailableError(
            "AppContainer mirror source or destination is invalid"
        )
    if hardlink and resolved.drive.casefold() != target.drive.casefold():
        raise ProcessIsolationUnavailableError(
            "AppContainer runtime mirror must stay on one volume"
        )
    target.mkdir(parents=True, exist_ok=False)
    try:
        for item in sorted(resolved.rglob("*"), key=lambda path: path.as_posix()):
            if _is_reparse_point(item):
                raise ProcessIsolationUnavailableError(
                    "AppContainer mirrors reject links and reparse points"
                )
            destination = target.joinpath(*item.relative_to(resolved).parts)
            if item.is_dir():
                destination.mkdir(exist_ok=False)
            elif item.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if hardlink:
                    os.link(item, destination)
                else:
                    shutil.copyfile(item, destination)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _map_mirrored_path(
    value: str,
    mappings: tuple[tuple[Path, Path], ...],
) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        return value
    for source, target in mappings:
        try:
            relative = candidate.relative_to(source)
        except ValueError:
            continue
        return str(target.joinpath(*relative.parts))
    return value


class WindowsRestrictedProcessLauncher(ProcessLauncher):
    def __init__(self, policy: IsolationPolicy) -> None:
        self._policy = policy
        self._profile_journal = (
            AppContainerProfileJournal(Path(policy.appcontainer_profile_journal_path))
            if policy.appcontainer_profile_journal_path is not None
            else None
        )
        if policy.require_network_isolation:
            self.mode = IsolationMode.WINDOWS_APPCONTAINER
            self.network_isolation_mode = NetworkIsolationMode.APPCONTAINER
        else:
            self.mode = IsolationMode.WINDOWS_RESTRICTED
            self.network_isolation_mode = NetworkIsolationMode.NONE

    def validate(self) -> None:
        token = None
        profile = None
        try:
            if self._policy.require_network_isolation:
                if self._profile_journal is not None:
                    try:
                        self._profile_journal.reap(_delete_appcontainer_profile_name)
                    except ProfileJournalError as error:
                        raise ProcessIsolationUnavailableError(str(error)) from error
                profile = _create_appcontainer_profile(self._profile_journal)
            else:
                token = _create_restricted_low_token()
            job = _create_job(self._policy)
            try:
                return
            finally:
                _close_handle(job)
        finally:
            _close_handle(token)
            if profile is not None:
                _delete_appcontainer_profile(profile, self._profile_journal)

    def validate_command(self, command: tuple[str, ...]) -> None:
        if not self._policy.require_network_isolation:
            return
        result = self.run(command=command, input_frame=b"", cancellation=Event())
        if result.return_code != 0 or not result.stdout:
            raise ProcessIsolationUnavailableError(
                "AppContainer is available, but the configured Tool worker runtime "
                "is not accessible inside it"
            )

    def run(
        self,
        *,
        command: tuple[str, ...],
        input_frame: bytes,
        cancellation: Event,
    ) -> IsolatedProcessResult:
        if not command or not Path(command[0]).is_absolute():
            raise ProcessIsolationError("Tool worker executable path must be absolute")

        effective_command = command
        profile = None
        token = None
        job = None
        process_info = PROCESS_INFORMATION()
        stdin_read = stdin_write = None
        stdout_read = stdout_write = None
        stderr_read = stderr_write = None
        attribute_list: LPVOID | None = None
        attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
        capability_sid = LPVOID()
        capability_entries: ctypes.Array[SID_AND_ATTRIBUTES] | None = None
        process_created = False
        appcontainer_temp_environment: str | None = None
        mirrored_runtime: Path | None = None
        mirrored_workspace: Path | None = None
        try:
            if self._policy.require_network_isolation:
                profile = _create_appcontainer_profile(self._profile_journal)
                if self._policy.appcontainer_mirror_workspace:
                    if (
                        self._policy.worker_runtime_bundle is None
                        or self._policy.working_directory is None
                    ):
                        raise ProcessIsolationUnavailableError(
                            "AppContainer mirror policy is incomplete"
                        )
                    mirror_root = Path(profile.local_app_data) / "DeskPilotInvocation"
                    mirrored_runtime = mirror_root / "runtime"
                    mirrored_workspace = mirror_root / "workspace"
                    runtime_source = Path(self._policy.worker_runtime_bundle).resolve(strict=True)
                    workspace_source = Path(self._policy.working_directory).resolve(strict=True)
                    _mirror_tree(runtime_source, mirrored_runtime, hardlink=True)
                    _mirror_tree(workspace_source, mirrored_workspace, hardlink=False)
                    mappings = (
                        (runtime_source, mirrored_runtime),
                        (workspace_source, mirrored_workspace),
                    )
                    effective_command = tuple(
                        _map_mirrored_path(value, mappings) for value in command
                    )
                if (
                    self._policy.appcontainer_read_paths
                    or self._policy.appcontainer_temp_path is not None
                ):
                    from deskpilot.runner.windows_acl import (
                        WindowsAclError,
                        protect_appcontainer_read_path,
                        protect_appcontainer_write_path,
                    )

                    try:
                        for read_path in self._policy.appcontainer_read_paths:
                            protect_appcontainer_read_path(Path(read_path), profile.sid_text)
                        if self._policy.appcontainer_temp_path is not None:
                            requested_temp = Path(self._policy.appcontainer_temp_path)
                            source_local_app_data = Path(os.environ["LOCALAPPDATA"])
                            try:
                                relative_temp = requested_temp.relative_to(source_local_app_data)
                            except ValueError:
                                physical_temp = requested_temp
                            else:
                                physical_temp = Path(profile.local_app_data) / relative_temp
                                physical_temp.mkdir(parents=True, exist_ok=True)
                            protect_appcontainer_write_path(
                                physical_temp,
                                profile.sid_text,
                            )
                            appcontainer_temp_environment = str(requested_temp)
                    except WindowsAclError as error:
                        raise ProcessIsolationUnavailableError(str(error)) from error
            else:
                token = _create_restricted_low_token()
            job = _create_job(self._policy)
            stdin_read, stdin_write = _create_pipe()
            stdout_read, stdout_write = _create_pipe()
            stderr_read, stderr_write = _create_pipe()
            _make_parent_only(stdin_write)
            _make_parent_only(stdout_read)
            _make_parent_only(stderr_read)

            attribute_count = 2 if profile is not None else 1
            attribute_size = SIZE_T()
            kernel32.InitializeProcThreadAttributeList(
                None, attribute_count, 0, ctypes.byref(attribute_size)
            )
            attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
            attribute_list = ctypes.cast(attribute_buffer, LPVOID)
            if not kernel32.InitializeProcThreadAttributeList(
                attribute_list, attribute_count, 0, ctypes.byref(attribute_size)
            ):
                _raise_last_error("InitializeProcThreadAttributeList")
            inherited_handles = (wintypes.HANDLE * 3)(
                stdin_read,
                stdout_write,
                stderr_write,
            )
            if not kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(inherited_handles, LPVOID),
                ctypes.sizeof(inherited_handles),
                None,
                None,
            ):
                _raise_last_error("UpdateProcThreadAttribute(handle list)")
            security_capabilities = None
            if profile is not None:
                if self._policy.worker_runtime_bundle is not None:
                    capability_sid = _derive_worker_runtime_capability()
                    capability_entries = (SID_AND_ATTRIBUTES * 1)(
                        SID_AND_ATTRIBUTES(capability_sid, 0x00000004)
                    )
                security_capabilities = SECURITY_CAPABILITIES(
                    profile.sid,
                    capability_entries,
                    len(capability_entries) if capability_entries is not None else 0,
                    0,
                )
                if not kernel32.UpdateProcThreadAttribute(
                    attribute_list,
                    0,
                    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                    ctypes.cast(ctypes.byref(security_capabilities), LPVOID),
                    ctypes.sizeof(security_capabilities),
                    None,
                    None,
                ):
                    _raise_last_error("UpdateProcThreadAttribute(security capabilities)")

            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = stdin_read
            startup.StartupInfo.hStdOutput = stdout_write
            startup.StartupInfo.hStdError = stderr_write
            startup.lpAttributeList = attribute_list
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(effective_command))
            runtime_root = (
                mirrored_runtime or Path(self._policy.worker_runtime_bundle)
                if self._policy.worker_runtime_bundle is not None
                else None
            )
            environment_values = sanitized_worker_environment(runtime_root=runtime_root)
            current_directory = (
                str(mirrored_workspace)
                if mirrored_workspace is not None
                else self._policy.working_directory or os.getcwd()
            )
            if profile is not None:
                if mirrored_workspace is not None:
                    profile_temp_path = Path(profile.local_app_data) / "Temp"
                    profile_temp_path.mkdir(parents=True, exist_ok=True)
                    profile_temp = str(profile_temp_path)
                    local_app_data = profile.local_app_data
                else:
                    profile_temp = appcontainer_temp_environment or os.environ.get(
                        "TEMP", str(Path(os.environ["LOCALAPPDATA"]) / "Temp")
                    )
                    local_app_data = os.environ["LOCALAPPDATA"]
                environment_values.update(
                    {
                        "LOCALAPPDATA": local_app_data,
                        "TEMP": profile_temp,
                        "TMP": profile_temp,
                    }
                )
                if mirrored_workspace is None:
                    current_directory = self._policy.working_directory or str(
                        Path(os.environ["SYSTEMROOT"]) / "System32"
                    )
            environment = _environment_block(environment_values)
            creation_flags = (
                CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
            )
            if profile is not None:
                created = kernel32.CreateProcessW(
                    effective_command[0],
                    command_line,
                    None,
                    None,
                    True,
                    creation_flags,
                    ctypes.cast(environment, LPVOID),
                    current_directory,
                    ctypes.byref(startup.StartupInfo),
                    ctypes.byref(process_info),
                )
                operation = "CreateProcessW(AppContainer)"
            else:
                created = advapi32.CreateProcessAsUserW(
                    token,
                    command[0],
                    command_line,
                    None,
                    None,
                    True,
                    creation_flags,
                    ctypes.cast(environment, LPVOID),
                    current_directory,
                    ctypes.byref(startup.StartupInfo),
                    ctypes.byref(process_info),
                )
                operation = "CreateProcessAsUserW"
            if not created:
                _raise_last_error(operation)
            process_created = True
            if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
                _raise_last_error("AssignProcessToJobObject")
            if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
                _raise_last_error("ResumeThread")
            _close_handle(process_info.hThread)
            process_info.hThread = None

            _close_handle(stdin_read)
            stdin_read = None
            _close_handle(stdout_write)
            stdout_write = None
            _close_handle(stderr_write)
            stderr_write = None

            stdout_parts: list[bytes] = []
            stderr_parts: list[bytes] = []
            stdout_thread = Thread(
                target=_read_handle,
                kwargs={
                    "handle": stdout_read,
                    "limit": MAX_WORKER_FRAME_BYTES,
                    "destination": stdout_parts,
                },
                daemon=True,
            )
            stderr_thread = Thread(
                target=_read_handle,
                kwargs={
                    "handle": stderr_read,
                    "limit": 4_096,
                    "destination": stderr_parts,
                },
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            stdout_read = None
            stderr_read = None

            descriptor = msvcrt.open_osfhandle(
                _handle_integer(stdin_write), os.O_WRONLY | os.O_BINARY
            )
            stdin_write = None
            with os.fdopen(descriptor, "wb", buffering=0) as stream:
                stream.write(input_frame)

            cancelled = False
            while True:
                wait_result = kernel32.WaitForSingleObject(process_info.hProcess, 25)
                if wait_result == WAIT_OBJECT_0:
                    break
                if wait_result != WAIT_TIMEOUT:
                    _raise_last_error("WaitForSingleObject")
                if cancellation.is_set():
                    cancelled = True
                    kernel32.TerminateJobObject(job, 1)
                    kernel32.WaitForSingleObject(process_info.hProcess, 5_000)
                    break

            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            if cancelled:
                raise IsolatedProcessCancelledError(
                    "Tool worker was cancelled",
                    stdout=stdout_parts[0] if stdout_parts else b"",
                    stderr=stderr_parts[0] if stderr_parts else b"",
                )
            exit_code = wintypes.DWORD(STILL_ACTIVE)
            if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
                _raise_last_error("GetExitCodeProcess")
            return IsolatedProcessResult(
                return_code=int(exit_code.value),
                stdout=stdout_parts[0] if stdout_parts else b"",
                stderr=stderr_parts[0] if stderr_parts else b"",
            )
        except Exception:
            if process_created:
                kernel32.TerminateJobObject(job, 1)
                kernel32.WaitForSingleObject(process_info.hProcess, 5_000)
            raise
        finally:
            if attribute_list is not None:
                kernel32.DeleteProcThreadAttributeList(attribute_list)
            del attribute_buffer
            del capability_entries
            _free_local(capability_sid)
            for handle in (
                stdin_read,
                stdin_write,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                process_info.hThread,
                process_info.hProcess,
                token,
                job,
            ):
                _close_handle(handle)
            if profile is not None:
                _delete_appcontainer_profile(profile, self._profile_journal)
