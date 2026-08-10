"""R0 computer metadata tools."""

from pathlib import Path
from threading import Event

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.domain.policy import PolicyResource
from deskpilot.domain.tool_contracts import (
    ToolContract,
    ToolExecutionContract,
    ToolIdempotency,
    ToolRiskLevel,
    ToolSecurityContract,
)
from deskpilot.runner.executor import (
    ToolExecutionCancelledError,
    ToolExecutionContext,
)


class DiskUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=32_767)


class DiskUsageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_path: str
    resolved_path: str
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    used_percent: float = Field(ge=0, le=100)


DISK_USAGE_CONTRACT = ToolContract.from_models(
    name="computer.disk_usage",
    version="1.0.0",
    description="Read capacity metadata for the disk containing an existing local path.",
    input_model=DiskUsageInput,
    output_model=DiskUsageOutput,
    risk_level=ToolRiskLevel.R0,
    execution=ToolExecutionContract(
        timeout_seconds=5,
        idempotency=ToolIdempotency.IDEMPOTENT,
        max_output_bytes=16_384,
    ),
    security=ToolSecurityContract(capabilities=("filesystem.metadata.read",)),
)


def project_disk_usage_resources(
    arguments: BaseModel,
) -> tuple[PolicyResource, ...]:
    if not isinstance(arguments, DiskUsageInput):
        raise TypeError("computer.disk_usage received an unexpected input model")
    resolved = Path(arguments.path).expanduser().resolve(strict=True)
    canonical_path = str(resolved)
    return (
        PolicyResource(
            kind="filesystem_path",
            identifier=canonical_path,
            operations=DISK_USAGE_CONTRACT.security.capabilities,
            display_name=canonical_path,
        ),
    )


def execute_disk_usage(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    if not isinstance(arguments, DiskUsageInput):
        raise TypeError("computer.disk_usage received an unexpected input model")
    if cancellation.is_set():
        raise ToolExecutionCancelledError("Tool call was cancelled before disk inspection")

    requested = arguments.path
    metadata = context.require_filesystem_metadata()

    if cancellation.is_set():
        raise ToolExecutionCancelledError("Tool call was cancelled during disk inspection")
    used_percent = (
        0.0
        if metadata.total_bytes == 0
        else round(metadata.used_bytes / metadata.total_bytes * 100, 2)
    )
    return DiskUsageOutput(
        requested_path=requested,
        resolved_path=metadata.identifier,
        total_bytes=metadata.total_bytes,
        used_bytes=metadata.used_bytes,
        free_bytes=metadata.free_bytes,
        used_percent=used_percent,
    )
