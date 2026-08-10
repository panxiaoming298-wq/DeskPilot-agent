import pytest
from pydantic import BaseModel, ConfigDict

from deskpilot.application.tool_registry import (
    DuplicateToolError,
    ToolRegistry,
    ToolSchemaValidationError,
    UnknownToolError,
)
from deskpilot.domain.tool_contracts import (
    ToolContract,
    ToolExecutionContract,
    ToolIdempotency,
    ToolRiskLevel,
    ToolSecurityContract,
)
from tests.authorization_helpers import make_test_resource_projector


class DiskUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class DiskUsageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_bytes: int
    free_bytes: int


def make_contract(*, idempotency: ToolIdempotency = ToolIdempotency.IDEMPOTENT) -> ToolContract:
    return ToolContract.from_models(
        name="computer.disk_usage",
        version="1.0.0",
        description="Read disk capacity without modifying the system.",
        input_model=DiskUsageInput,
        output_model=DiskUsageOutput,
        risk_level=ToolRiskLevel.R0,
        execution=ToolExecutionContract(
            timeout_seconds=5,
            idempotency=idempotency,
            resource_locks=("disk:{path}",),
        ),
        security=ToolSecurityContract(capabilities=("filesystem.metadata.read",)),
    )


def test_contract_digest_is_deterministic_and_covers_metadata() -> None:
    contract = make_contract()
    changed = contract.model_copy(
        update={"execution": contract.execution.model_copy(update={"timeout_seconds": 6})}
    )

    assert contract.digest == make_contract().digest
    assert contract.digest != changed.digest
    assert len(contract.digest) == 64


def test_registry_requires_exact_allowlisted_version() -> None:
    registry = ToolRegistry()
    contract = make_contract()

    registration = registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )

    assert registry.resolve("computer.disk_usage", "1.0.0") is registration
    assert registry.contracts() == (contract,)
    with pytest.raises(UnknownToolError) as unknown:
        registry.resolve("computer.disk_usage", "1.0.1")
    assert unknown.value.code == "TOOL_NOT_REGISTERED"


def test_registry_rejects_duplicate_and_model_contract_mismatch() -> None:
    registry = ToolRegistry()
    contract = make_contract()
    projector = make_test_resource_projector(contract)
    registry.register(contract, DiskUsageInput, DiskUsageOutput, projector)

    with pytest.raises(DuplicateToolError) as duplicate:
        registry.register(contract, DiskUsageInput, DiskUsageOutput, projector)
    assert duplicate.value.code == "TOOL_ALREADY_REGISTERED"

    mismatched = ToolRegistry()
    with pytest.raises(ValueError, match="input model"):
        mismatched.register(contract, DiskUsageOutput, DiskUsageOutput, projector)


def test_registry_validates_input_and_output_with_pydantic_models() -> None:
    registry = ToolRegistry()
    contract = make_contract()
    registry.register(
        contract,
        DiskUsageInput,
        DiskUsageOutput,
        make_test_resource_projector(contract),
    )

    parsed_input = registry.validate_input("computer.disk_usage", "1.0.0", {"path": "C:\\"})
    parsed_output = registry.validate_output(
        "computer.disk_usage",
        "1.0.0",
        {"total_bytes": 100, "free_bytes": 25},
    )

    assert parsed_input == DiskUsageInput(path="C:\\")
    assert parsed_output == DiskUsageOutput(total_bytes=100, free_bytes=25)

    with pytest.raises(ToolSchemaValidationError) as invalid:
        registry.validate_input(
            "computer.disk_usage",
            "1.0.0",
            {"path": "C:\\", "unexpected": True},
        )
    assert invalid.value.code == "TOOL_SCHEMA_VALIDATION_FAILED"
