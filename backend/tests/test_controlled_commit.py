from threading import Event

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from deskpilot.domain.policy import PolicyResource
from deskpilot.domain.tool_contracts import (
    ToolCommitProtocol,
    ToolContract,
    ToolExecutionContract,
    ToolIdempotency,
    ToolRiskLevel,
    ToolSecurityContract,
)
from deskpilot.runner.controlled_commit import (
    ControlledCommitBoundary,
    ControlledCommitPhase,
)
from deskpilot.runner.executor import (
    ControlledCommitUnavailableError,
    ToolExecutionContext,
    ToolExecutor,
)


class WriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


class WriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    committed: bool


def _project(arguments: BaseModel) -> tuple[PolicyResource, ...]:
    del arguments
    return (PolicyResource(kind="test_resource", identifier="write:test"),)


def _handler(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    del arguments, cancellation, context
    raise AssertionError("side-effecting handler must not run without a commit broker")


def _write_contract(commit_protocol: ToolCommitProtocol) -> ToolContract:
    return ToolContract.from_models(
        name="test.controlled_write",
        version="1.0.0",
        description="Synthetic write contract for controlled-commit enforcement.",
        input_model=WriteInput,
        output_model=WriteOutput,
        risk_level=ToolRiskLevel.R2,
        side_effects=("test_write",),
        execution=ToolExecutionContract(
            timeout_seconds=5,
            idempotency=ToolIdempotency.KEY_REQUIRED,
            commit_protocol=commit_protocol,
        ),
        security=ToolSecurityContract(),
    )


def test_side_effect_contract_cannot_use_read_only_execution() -> None:
    with pytest.raises(ValidationError, match="brokered commit"):
        _write_contract(ToolCommitProtocol.READ_ONLY)


def test_brokered_commit_fails_before_side_effecting_handler_runs() -> None:
    contract = _write_contract(ToolCommitProtocol.BROKERED)
    executor = ToolExecutor()
    executor.register(contract, WriteInput, WriteOutput, _project, _handler)

    with pytest.raises(ControlledCommitUnavailableError) as unavailable:
        executor.execute_worker_request(
            tool_name=contract.name,
            tool_version=contract.version,
            contract_digest=contract.digest,
            arguments={"value": "must-not-commit"},
            resources=(),
            cancellation=Event(),
        )

    assert unavailable.value.code == "TOOL_CONTROLLED_COMMIT_UNAVAILABLE"


def test_commit_boundary_atomically_refuses_published_cancellation() -> None:
    boundary = ControlledCommitBoundary()
    cancellation = Event()
    cancellation.set()

    assert boundary.try_mark_committing(cancellation) is False
    assert boundary.snapshot().phase is ControlledCommitPhase.NO_EFFECT
