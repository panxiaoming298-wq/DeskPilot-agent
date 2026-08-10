from pathlib import Path
from threading import Event

import pytest

from deskpilot.runner.executor import ToolExecutionContext
from deskpilot.runner.resource_broker import read_brokered_filesystem_metadata
from deskpilot.tools.builtins import create_builtin_executor, create_builtin_registry
from deskpilot.tools.computer import (
    DISK_USAGE_CONTRACT,
    DiskUsageInput,
    DiskUsageOutput,
    execute_disk_usage,
)


def test_disk_usage_reads_metadata_without_side_effects(tmp_path: Path) -> None:
    metadata = read_brokered_filesystem_metadata(str(tmp_path.resolve(strict=True)))
    output = execute_disk_usage(
        DiskUsageInput(path=str(tmp_path)),
        Event(),
        ToolExecutionContext((metadata,)),
    )

    assert isinstance(output, DiskUsageOutput)
    assert output.resolved_path == str(tmp_path.resolve())
    assert output.total_bytes >= output.used_bytes
    assert output.total_bytes >= output.free_bytes
    assert 0 <= output.used_percent <= 100
    assert DISK_USAGE_CONTRACT.risk_level == "R0"
    assert DISK_USAGE_CONTRACT.side_effects == ()
    assert DISK_USAGE_CONTRACT.security.network_access is False


def test_disk_usage_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_brokered_filesystem_metadata(str(tmp_path / "missing"))


def test_builtin_registry_and_executor_share_exact_contract() -> None:
    registry = create_builtin_registry()
    executor = create_builtin_executor()

    assert registry.resolve("computer.disk_usage", "1.0.0").contract.digest == (
        DISK_USAGE_CONTRACT.digest
    )
    assert executor.registry.resolve(
        "computer.disk_usage", "1.0.0"
    ).contract.digest == DISK_USAGE_CONTRACT.digest
