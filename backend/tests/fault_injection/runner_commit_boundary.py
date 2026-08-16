"""Test-only Runner that pauses at a durable file.move commit checkpoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event

from deskpilot.runner.commit_receipts import PreparedCommitRecord
from deskpilot.runner.executor import ToolExecutor
from deskpilot.runner.server import run_server_from_stdio
from deskpilot.tools.computer import (
    DISK_USAGE_CONTRACT,
    DiskUsageInput,
    DiskUsageOutput,
    execute_disk_usage,
    project_disk_usage_resources,
)
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FileMoveCommitCheckpoint,
    FileMoveCommitProvider,
    FileMoveInput,
    FileMoveOutput,
    FileMovePrepare,
    prepare_file_move,
    project_file_move_resources,
)

_PHASE_VARIABLE = "DESKPILOT_TEST_RUNNER_FAULT_PHASE"
_CHECKPOINT_VARIABLE = "DESKPILOT_TEST_RUNNER_FAULT_CHECKPOINT"
_BLOCK_FOREVER = Event()


def _fault_observer(
    checkpoint: FileMoveCommitCheckpoint,
    record: PreparedCommitRecord,
) -> None:
    if checkpoint.value != os.environ.get(_PHASE_VARIABLE):
        return
    checkpoint_path = Path(os.environ[_CHECKPOINT_VARIABLE])
    payload = {
        "checkpoint": "runner_file_move_commit",
        "phase": checkpoint.value,
        "process_id": os.getpid(),
        "parent_process_id": os.getppid(),
        "call_id": record.call_id,
        "receipt_id": record.receipt_id,
        "journal_state": record.state,
    }
    temporary = checkpoint_path.with_suffix(f"{checkpoint_path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(checkpoint_path)
    _BLOCK_FOREVER.wait()


def _create_fault_executor() -> ToolExecutor:
    executor = ToolExecutor()
    executor.register(
        DISK_USAGE_CONTRACT,
        DiskUsageInput,
        DiskUsageOutput,
        project_disk_usage_resources,
        execute_disk_usage,
    )
    executor.register(
        FILE_MOVE_CONTRACT,
        FileMoveInput,
        FileMoveOutput,
        project_file_move_resources,
        prepare_file_move,
        prepare_model=FileMovePrepare,
        commit_provider=FileMoveCommitProvider(observer=_fault_observer),
    )
    return executor


def main() -> int:
    return run_server_from_stdio(
        _create_fault_executor(),
        worker_factory="deskpilot.tools:create_builtin_executor",
    )


if __name__ == "__main__":
    raise SystemExit(main())
