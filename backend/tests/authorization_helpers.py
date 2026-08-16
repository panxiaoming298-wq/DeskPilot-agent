"""Test-only builders for exact Tool Runner policy authorization proofs."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.policy import (
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.tool_contracts import ToolContract, ToolRiskLevel


def authorization_resources(
    contract: ToolContract,
    arguments: dict[str, object],
    *,
    require_existing: bool = False,
) -> tuple[PolicyResource, ...]:
    """Project the same canonical test resources used by Runner registrations."""
    if contract.name == "computer.disk_usage":
        path = arguments.get("path")
        if not isinstance(path, str):
            raise TypeError("computer.disk_usage test arguments require a path")
        canonical = str(Path(path).expanduser().resolve(strict=require_existing))
        return (
            PolicyResource(
                kind="filesystem_path",
                identifier=canonical,
                operations=contract.security.capabilities,
                display_name=canonical,
            ),
        )
    if contract.name == "file.move":
        source = arguments.get("source")
        destination = arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise TypeError("file.move test arguments require source and destination")
        from deskpilot.tools.files import FileMoveInput, project_file_move_resources

        return project_file_move_resources(
            FileMoveInput(source=source, destination=destination)
        )
    return (
        PolicyResource(
            kind="test_resource",
            identifier=f"tool:{contract.name}",
            operations=contract.security.capabilities,
        ),
    )


def make_test_resource_projector(
    contract: ToolContract,
) -> Callable[[BaseModel], tuple[PolicyResource, ...]]:
    def project(arguments: BaseModel) -> tuple[PolicyResource, ...]:
        return authorization_resources(
            contract,
            arguments.model_dump(mode="python"),
            require_existing=True,
        )

    return project


def make_tool_authorization(
    contract: ToolContract,
    *,
    task_id: str,
    step_id: str,
    call_id: str,
    actor_id: str,
    arguments: dict[str, object],
    expected_resource_versions: dict[str, str] | None = None,
    effective_risk: ToolRiskLevel | None = None,
    require_approval: bool | None = None,
    grant_expires_at: datetime | None = None,
    approved_at: datetime | None = None,
    origin: str = "builtin",
    data_egress: bool = False,
    interactive: bool = True,
    batch_count: int = 1,
    now: datetime | None = None,
) -> ToolAuthorizationGrant:
    versions = expected_resource_versions or {}
    resolved_risk = effective_risk or contract.risk_level
    needs_approval = (
        contract.risk_level is not ToolRiskLevel.R0 or resolved_risk is not ToolRiskLevel.R0
        if require_approval is None
        else require_approval
    )
    issued_at = now or datetime.now(UTC)
    resources = authorization_resources(contract, arguments)
    request = ToolAuthorizationRequest.model_validate(
        {
            "task_id": task_id,
            "step_id": step_id,
            "call_id": call_id,
            "actor": actor_id,
            "origin": origin,
            "tool_name": contract.name,
            "tool_version": contract.version,
            "contract_digest": contract.digest,
            "arguments_digest": sha256_digest(arguments),
            "risk_level": contract.risk_level,
            "side_effects": contract.side_effects,
            "reversible": contract.reversible,
            "capabilities": contract.security.capabilities,
            "network_access": contract.security.network_access,
            "data_egress": data_egress,
            "resources": [resource.model_dump(mode="json") for resource in resources],
            "expected_resource_versions_digest": sha256_digest(versions),
            "interactive": interactive,
            "batch_count": batch_count,
        }
    )
    binding = {
        "task_id": task_id,
        "step_id": step_id,
        "call_id": call_id,
        "tool_name": contract.name,
        "tool_version": contract.version,
        "contract_digest": contract.digest,
        "arguments_digest": request.arguments_digest,
        "resource_scope_digest": request.resource_scope_digest,
        "expected_resource_versions_digest": sha256_digest(versions),
        "effective_risk": resolved_risk.value,
        "policy_revision": "test-runner-policy-v1",
        "rule_id": "test.exact-call-authorization",
        "reason_code": ("TEST_APPROVAL_GRANTED" if needs_approval else "TEST_R0_ALLOWED"),
    }
    decision_id = f"pdec_{sha256_digest({'binding': binding, 'kind': 'decision'})}"
    approval_id = f"apr_{sha256_digest(binding)[:32]}" if needs_approval else None
    preview_hash = (
        sha256_digest({"binding": binding, "kind": "preview"}) if needs_approval else None
    )
    expiry = grant_expires_at or issued_at + timedelta(minutes=5) if needs_approval else None
    resolved_approved_at = (approved_at or issued_at) if needs_approval else None
    return ToolAuthorizationGrant.issue(
        decision_id=decision_id,
        request_digest=request.request_digest,
        task_id=task_id,
        step_id=step_id,
        call_id=call_id,
        actor_id=actor_id,
        origin=origin,
        tool_name=contract.name,
        tool_version=contract.version,
        contract_digest=contract.digest,
        policy_revision=binding["policy_revision"],
        rule_id=binding["rule_id"],
        reason_code=binding["reason_code"],
        effect=PolicyEffect.ALLOW,
        effective_risk=resolved_risk,
        arguments_digest=binding["arguments_digest"],
        resource_scope_digest=binding["resource_scope_digest"],
        expected_resource_versions_digest=binding["expected_resource_versions_digest"],
        capabilities=request.capabilities,
        network_access=request.network_access,
        data_egress=request.data_egress,
        side_effects=request.side_effects,
        reversible=request.reversible,
        resources=request.resources,
        interactive=request.interactive,
        batch_count=request.batch_count,
        approval_id=approval_id,
        preview_hash=preview_hash,
        approved_at=resolved_approved_at,
        grant_expires_at=expiry,
    )
