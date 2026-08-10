import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

from deskpilot.runner.executor import ToolResourceContextError
from deskpilot.runner.resource_broker import read_brokered_filesystem_metadata
from deskpilot.runner.worker_protocol import BrokeredFilesystemMetadata
from deskpilot.tools.builtins import create_builtin_executor
from deskpilot.tools.computer import DISK_USAGE_CONTRACT, DiskUsageOutput


def test_worker_consumes_brokered_metadata_without_opening_argument_path(
    tmp_path: Path,
) -> None:
    metadata = read_brokered_filesystem_metadata(str(tmp_path.resolve(strict=True)))
    executor = create_builtin_executor()

    output = executor.execute_worker_request(
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        arguments={"path": str(tmp_path / "not-the-brokered-resource")},
        resources=(metadata,),
        cancellation=Event(),
    )

    assert isinstance(output, DiskUsageOutput)
    assert output.requested_path.endswith("not-the-brokered-resource")
    assert output.resolved_path == str(tmp_path.resolve(strict=True))


def test_worker_fails_closed_when_capability_resource_is_missing(tmp_path: Path) -> None:
    executor = create_builtin_executor()

    with pytest.raises(ToolResourceContextError) as missing:
        executor.execute_worker_request(
            tool_name=DISK_USAGE_CONTRACT.name,
            tool_version=DISK_USAGE_CONTRACT.version,
            contract_digest=DISK_USAGE_CONTRACT.digest,
            arguments={"path": str(tmp_path)},
            resources=(),
            cancellation=Event(),
        )

    assert missing.value.code == "TOOL_RESOURCE_CONTEXT_INVALID"


def test_brokered_metadata_rejects_inconsistent_capacity() -> None:
    with pytest.raises(ValidationError):
        BrokeredFilesystemMetadata(
            identifier="C:\\",
            total_bytes=100,
            used_bytes=80,
            free_bytes=30,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle leak probe")
def test_windows_resource_broker_does_not_leak_handles(tmp_path: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        )
        return int(count.value)

    before = handle_count()
    for _ in range(32):
        read_brokered_filesystem_metadata(str(tmp_path.resolve(strict=True)))
    after = handle_count()

    assert after <= before + 1
