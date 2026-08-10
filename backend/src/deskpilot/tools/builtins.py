"""Composition helpers shared by the control plane and isolated Runner."""

from deskpilot.application.tool_registry import ToolRegistry
from deskpilot.runner.executor import ToolExecutor
from deskpilot.tools.computer import (
    DISK_USAGE_CONTRACT,
    DiskUsageInput,
    DiskUsageOutput,
    execute_disk_usage,
    project_disk_usage_resources,
)
from deskpilot.tools.files import (
    FILE_MOVE_COMMIT_PROVIDER,
    FILE_MOVE_CONTRACT,
    FileMoveInput,
    FileMoveOutput,
    FileMovePrepare,
    prepare_file_move,
    project_file_move_resources,
)


def create_builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        DISK_USAGE_CONTRACT,
        DiskUsageInput,
        DiskUsageOutput,
        project_disk_usage_resources,
    )
    registry.register(
        FILE_MOVE_CONTRACT,
        FileMoveInput,
        FileMoveOutput,
        project_file_move_resources,
    )
    return registry


def create_builtin_executor() -> ToolExecutor:
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
        commit_provider=FILE_MOVE_COMMIT_PROVIDER,
    )
    return executor
