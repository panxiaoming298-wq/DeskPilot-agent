"""Runner fixture with one cancellable slow tool; never registered in production."""

import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

from pydantic import BaseModel, ConfigDict

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
    ToolExecutor,
)
from deskpilot.runner.server import run_server_from_stdio


class SlowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delay_seconds: float


class SlowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed: bool


class IsolationProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    write_path: str
    secret_name: str


class IsolationProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_id: int
    parent_process_id: int
    integrity_level_rid: int
    privilege_count: int
    in_job: bool
    write_succeeded: bool
    child_process_succeeded: bool
    secret_present: bool


SLOW_CONTRACT = ToolContract.from_models(
    name="test.slow",
    version="1.0.0",
    description="Wait cooperatively for Runner timeout and cancellation tests.",
    input_model=SlowInput,
    output_model=SlowOutput,
    risk_level=ToolRiskLevel.R0,
    execution=ToolExecutionContract(
        timeout_seconds=1,
        idempotency=ToolIdempotency.IDEMPOTENT,
        max_output_bytes=4_096,
    ),
    security=ToolSecurityContract(),
)

NON_IDEMPOTENT_SLOW_CONTRACT = ToolContract.from_models(
    name="test.non_idempotent_slow",
    version="1.0.0",
    description="Ignore cancellation to exercise an uncertain timeout result.",
    input_model=SlowInput,
    output_model=SlowOutput,
    risk_level=ToolRiskLevel.R1,
    execution=ToolExecutionContract(
        timeout_seconds=1,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        max_output_bytes=4_096,
    ),
    security=ToolSecurityContract(),
)

ISOLATION_PROBE_CONTRACT = ToolContract.from_models(
    name="test.isolation_probe",
    version="1.0.0",
    description="Report process-boundary facts for Runner integration tests.",
    input_model=IsolationProbeInput,
    output_model=IsolationProbeOutput,
    risk_level=ToolRiskLevel.R0,
    execution=ToolExecutionContract(
        timeout_seconds=3,
        idempotency=ToolIdempotency.IDEMPOTENT,
        max_output_bytes=4_096,
    ),
    security=ToolSecurityContract(),
)


def execute_slow(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    del context
    if not isinstance(arguments, SlowInput):
        raise TypeError("test.slow received an unexpected input model")
    if cancellation.wait(arguments.delay_seconds):
        raise ToolExecutionCancelledError("Slow test tool was cancelled")
    return SlowOutput(completed=True)


def execute_non_idempotent_slow(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    del cancellation, context
    if not isinstance(arguments, SlowInput):
        raise TypeError("test.non_idempotent_slow received an unexpected input model")
    time.sleep(arguments.delay_seconds)
    return SlowOutput(completed=True)


def execute_isolation_probe(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    del cancellation, context
    if not isinstance(arguments, IsolationProbeInput):
        raise TypeError("test.isolation_probe received an unexpected input model")
    from deskpilot.runner.windows_sandbox import current_process_security_snapshot

    security = current_process_security_snapshot()
    try:
        Path(arguments.write_path).write_text("sandbox escape", encoding="utf-8")
    except OSError:
        write_succeeded = False
    else:
        write_succeeded = True
    try:
        child = subprocess.run(  # noqa: S603 - fixed trusted interpreter probe
            (sys.executable, "-c", "raise SystemExit(0)"),
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        child_process_succeeded = False
    else:
        child_process_succeeded = child.returncode == 0
    return IsolationProbeOutput(
        process_id=os.getpid(),
        parent_process_id=os.getppid(),
        integrity_level_rid=security.integrity_level_rid,
        privilege_count=security.privilege_count,
        in_job=security.is_in_job,
        write_succeeded=write_succeeded,
        child_process_succeeded=child_process_succeeded,
        secret_present=arguments.secret_name in os.environ,
    )


def project_slow_resources(arguments: BaseModel) -> tuple[PolicyResource, ...]:
    if not isinstance(arguments, SlowInput):
        raise TypeError("slow test tool received an unexpected input model")
    return (
        PolicyResource(
            kind="test_resource",
            identifier="tool:test.slow",
        ),
    )


def project_non_idempotent_slow_resources(
    arguments: BaseModel,
) -> tuple[PolicyResource, ...]:
    if not isinstance(arguments, SlowInput):
        raise TypeError("non-idempotent slow test tool received an unexpected input model")
    return (
        PolicyResource(
            kind="test_resource",
            identifier="tool:test.non_idempotent_slow",
        ),
    )


def project_isolation_probe_resources(
    arguments: BaseModel,
) -> tuple[PolicyResource, ...]:
    if not isinstance(arguments, IsolationProbeInput):
        raise TypeError("isolation probe received an unexpected input model")
    return (PolicyResource(kind="test_resource", identifier="tool:test.isolation_probe"),)


def create_slow_executor() -> ToolExecutor:
    executor = ToolExecutor()
    executor.register(
        SLOW_CONTRACT,
        SlowInput,
        SlowOutput,
        project_slow_resources,
        execute_slow,
    )
    executor.register(
        NON_IDEMPOTENT_SLOW_CONTRACT,
        SlowInput,
        SlowOutput,
        project_non_idempotent_slow_resources,
        execute_non_idempotent_slow,
    )
    executor.register(
        ISOLATION_PROBE_CONTRACT,
        IsolationProbeInput,
        IsolationProbeOutput,
        project_isolation_probe_resources,
        execute_isolation_probe,
    )
    return executor


if __name__ == "__main__":
    raise SystemExit(
        run_server_from_stdio(
            create_slow_executor(),
            worker_factory="tests.fixtures.slow_runner_service:create_slow_executor",
        )
    )
