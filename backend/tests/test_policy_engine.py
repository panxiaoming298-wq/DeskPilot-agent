from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.tools.computer import (
    DISK_USAGE_CONTRACT,
    DiskUsageInput,
    project_disk_usage_resources,
)
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FileMoveInput,
    project_file_move_resources,
)

SAFE_PATH = "D:\\DeskPilot\\allowed"
SAFE_SCOPE = ("filesystem_path", SAFE_PATH)


def make_request(
    *,
    risk_level: ToolRiskLevel = ToolRiskLevel.R0,
    capabilities: tuple[str, ...] = ("filesystem.metadata.read",),
    resource_identifier: str = SAFE_PATH,
    operations: tuple[str, ...] | None = None,
    side_effects: tuple[str, ...] = (),
    reversible: bool = False,
    network_access: bool = False,
    data_egress: bool = False,
    origin: str = "builtin",
    interactive: bool = True,
    batch_count: int = 1,
) -> ToolAuthorizationRequest:
    return ToolAuthorizationRequest.model_validate(
        {
            "task_id": "tsk-policy",
            "step_id": "inspect-disk",
            "call_id": "call-policy",
            "actor": "model:fake",
            "origin": origin,
            "tool_name": "computer.disk_usage",
            "tool_version": "1.0.0",
            "contract_digest": "a" * 64,
            "arguments_digest": sha256_digest({"path": resource_identifier}),
            "risk_level": risk_level,
            "side_effects": side_effects,
            "reversible": reversible,
            "capabilities": capabilities,
            "network_access": network_access,
            "data_egress": data_egress,
            "resources": [
                {
                    "kind": "filesystem_path",
                    "identifier": resource_identifier,
                    "operations": capabilities if operations is None else operations,
                    "display_name": "Configured disk inspection path",
                }
            ],
            "expected_resource_versions_digest": sha256_digest({}),
            "interactive": interactive,
            "batch_count": batch_count,
        }
    )


def make_engine(**overrides: Any) -> BuiltinPolicyEngine:
    options: dict[str, Any] = {"allowed_resource_scopes": (SAFE_SCOPE,)}
    options.update(overrides)
    return BuiltinPolicyEngine(**options)


def test_valid_r0_request_is_allowed_deterministically() -> None:
    request = make_request()
    first = make_engine().evaluate(request)
    second = make_engine().evaluate(make_request())

    assert first == second
    assert first.effect is PolicyEffect.ALLOW
    assert first.effective_risk is ToolRiskLevel.R0
    assert first.reason_code == "DEFAULT_R0_ALLOW"
    assert first.rule_id == "default.r0-allow"
    assert first.decision_id.startswith("pdec_")
    assert len(first.decision_id) == 69
    assert first.request_digest == request.request_digest
    assert first.resource_scope_digest == request.resource_scope_digest
    assert first.approval_ttl_seconds is None


def test_request_normalizes_set_like_facts_before_hashing() -> None:
    first_resource = PolicyResource(
        kind="filesystem_path",
        identifier=SAFE_PATH,
        operations=("filesystem.metadata.read", "filesystem.metadata.read"),
    )
    second_resource = PolicyResource(
        kind="filesystem_path",
        identifier="D:\\DeskPilot\\other",
        operations=("filesystem.metadata.read",),
    )
    base = make_request()
    first = base.model_copy(
        update={
            "capabilities": ("filesystem.metadata.read",),
            "resources": (first_resource, second_resource),
        }
    )
    second = ToolAuthorizationRequest.model_validate(
        {
            **base.model_dump(mode="json"),
            "capabilities": [
                "filesystem.metadata.read",
                "filesystem.metadata.read",
            ],
            "resources": [
                second_resource.model_dump(mode="json"),
                first_resource.model_dump(mode="json"),
            ],
        }
    )

    assert first_resource.operations == ("filesystem.metadata.read",)
    assert second.resources == (first_resource, second_resource)
    assert first.resource_scope_digest == second.resource_scope_digest
    assert first.request_digest == second.request_digest


@pytest.mark.parametrize(
    ("authorization_request", "reason_code"),
    [
        (
            make_request(capabilities=("filesystem.contents.read",)),
            "CAPABILITY_NOT_ALLOWED",
        ),
        (
            make_request(resource_identifier="D:\\DeskPilot\\outside"),
            "RESOURCE_SCOPE_DENIED",
        ),
        (
            make_request(operations=()),
            "RESOURCE_CAPABILITY_MISMATCH",
        ),
        (
            make_request(origin="mcp"),
            "TOOL_ORIGIN_NOT_ALLOWED",
        ),
    ],
)
def test_capability_origin_and_resource_boundaries_fail_closed(
    authorization_request: ToolAuthorizationRequest,
    reason_code: str,
) -> None:
    decision = make_engine().evaluate(authorization_request)

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    "changes",
    [
        {"side_effects": ("filesystem_write",)},
        {"reversible": True},
        {"network_access": True},
        {"data_egress": True},
    ],
)
def test_r0_contract_contradictions_are_denied(changes: dict[str, Any]) -> None:
    decision = make_engine().evaluate(make_request(**changes))

    assert decision.effect is PolicyEffect.DENY
    assert decision.reason_code == "R0_CONTRACT_INCONSISTENT"


@pytest.mark.parametrize("risk", [ToolRiskLevel.R1, ToolRiskLevel.R2])
def test_r1_and_r2_require_a_one_time_approval(risk: ToolRiskLevel) -> None:
    decision = make_engine(approval_ttl_seconds=90).evaluate(make_request(risk_level=risk))

    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.effective_risk is risk
    assert decision.reason_code == "APPROVAL_REQUIRED"
    assert decision.approval_ttl_seconds == 90


def test_user_selected_file_move_scope_requires_exact_local_user_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "policy-source.txt"
    destination = tmp_path / "policy-destination.txt"
    source.write_text("policy preview", encoding="utf-8")
    arguments = FileMoveInput(source=str(source), destination=str(destination))
    resources = project_file_move_resources(arguments)
    versions = {
        "destination": "absent",
        "source": next(
            resource.version_digest
            for resource in resources
            if resource.version_digest is not None
        ),
    }
    request = ToolAuthorizationRequest(
        task_id="tsk-file-policy",
        step_id="move-file",
        call_id="call-file-policy",
        actor="local_user",
        origin="builtin",
        tool_name=FILE_MOVE_CONTRACT.name,
        tool_version=FILE_MOVE_CONTRACT.version,
        contract_digest=FILE_MOVE_CONTRACT.digest,
        arguments_digest=sha256_digest(arguments),
        risk_level=FILE_MOVE_CONTRACT.risk_level,
        side_effects=FILE_MOVE_CONTRACT.side_effects,
        reversible=FILE_MOVE_CONTRACT.reversible,
        capabilities=FILE_MOVE_CONTRACT.security.capabilities,
        network_access=False,
        data_egress=False,
        resources=resources,
        expected_resource_versions_digest=sha256_digest(versions),
        interactive=True,
        batch_count=1,
    )
    engine = BuiltinPolicyEngine(
        allowed_capabilities=(
            "filesystem.file.move_destination",
            "filesystem.file.move_source",
        ),
        allow_user_selected_file_move=True,
        approval_ttl_seconds=90,
    )

    allowed = engine.evaluate(request)
    model_attributed = engine.evaluate(request.model_copy(update={"actor": "model:fake"}))

    assert allowed.effect is PolicyEffect.REQUIRE_APPROVAL
    assert allowed.effective_risk is ToolRiskLevel.R1
    assert allowed.reason_code == "APPROVAL_REQUIRED"
    assert model_attributed.effect is PolicyEffect.DENY
    assert model_attributed.reason_code == "RESOURCE_SCOPE_DENIED"


def test_user_selected_disk_usage_scope_requires_exact_local_user_contract(
    tmp_path: Path,
) -> None:
    arguments = DiskUsageInput(path=str(tmp_path))
    resources = project_disk_usage_resources(arguments)
    request = ToolAuthorizationRequest(
        task_id="tsk-disk-policy",
        step_id="inspect-capacity",
        call_id="call-disk-policy",
        actor="local_user",
        origin="builtin",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        arguments_digest=sha256_digest(arguments),
        risk_level=DISK_USAGE_CONTRACT.risk_level,
        side_effects=DISK_USAGE_CONTRACT.side_effects,
        reversible=DISK_USAGE_CONTRACT.reversible,
        capabilities=DISK_USAGE_CONTRACT.security.capabilities,
        network_access=False,
        data_egress=False,
        resources=resources,
        expected_resource_versions_digest=sha256_digest({}),
        interactive=True,
        batch_count=1,
    )
    engine = BuiltinPolicyEngine(
        allowed_capabilities=("filesystem.metadata.read",),
        allow_user_selected_disk_usage=True,
    )

    allowed = engine.evaluate(request)
    model_attributed = engine.evaluate(request.model_copy(update={"actor": "model:fake"}))

    assert allowed.effect is PolicyEffect.ALLOW
    assert allowed.reason_code == "DEFAULT_R0_ALLOW"
    assert model_attributed.effect is PolicyEffect.DENY
    assert model_attributed.reason_code == "RESOURCE_SCOPE_DENIED"


@pytest.mark.parametrize(
    ("authorization_request", "rule_id", "reason_code"),
    [
        (
            make_request(risk_level=ToolRiskLevel.R1, interactive=False),
            "interaction.interactive-only",
            "NON_INTERACTIVE_NOT_SUPPORTED",
        ),
        (
            make_request(risk_level=ToolRiskLevel.R1, batch_count=2),
            "batch.single-call-only",
            "BATCH_NOT_SUPPORTED",
        ),
    ],
)
def test_non_interactive_and_batch_requests_fail_closed_before_approval(
    authorization_request: ToolAuthorizationRequest,
    rule_id: str,
    reason_code: str,
) -> None:
    decision = make_engine().evaluate(authorization_request)

    assert decision.effect is PolicyEffect.DENY
    assert decision.rule_id == rule_id
    assert decision.reason_code == reason_code
    assert decision.approval_ttl_seconds is None


def test_r0_approval_override_only_tightens_an_otherwise_valid_request() -> None:
    decision = make_engine(
        require_approval_for_r0=True,
        approval_ttl_seconds=45,
    ).evaluate(make_request())
    out_of_scope = make_engine(require_approval_for_r0=True).evaluate(
        make_request(resource_identifier="D:\\DeskPilot\\outside")
    )

    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.effective_risk is ToolRiskLevel.R0
    assert decision.rule_id == "default.r0-explicit-approval"
    assert decision.reason_code == "R0_APPROVAL_REQUIRED"
    assert decision.approval_ttl_seconds == 45
    assert out_of_scope.effect is PolicyEffect.DENY
    assert out_of_scope.reason_code == "RESOURCE_SCOPE_DENIED"


def test_r3_is_denied_by_default_and_requires_approval_only_when_enabled() -> None:
    request = make_request(risk_level=ToolRiskLevel.R3)

    disabled = make_engine().evaluate(request)
    enabled = make_engine(enable_r3=True).evaluate(request)

    assert disabled.effect is PolicyEffect.DENY
    assert disabled.reason_code == "R3_DISABLED"
    assert enabled.effect is PolicyEffect.REQUIRE_APPROVAL
    assert enabled.reason_code == "R3_APPROVAL_REQUIRED"


def test_r4_project_prohibition_wins_over_other_rules() -> None:
    decision = make_engine(
        enable_r3=True,
        allowed_capabilities=(),
        allowed_resource_scopes=(),
    ).evaluate(
        make_request(
            risk_level=ToolRiskLevel.R4,
            capabilities=("forbidden.capability",),
            resource_identifier="D:\\outside",
        )
    )

    assert decision.effect is PolicyEffect.DENY
    assert decision.effective_risk is ToolRiskLevel.R4
    assert decision.reason_code == "RISK_LEVEL_PROHIBITED"


def test_free_form_model_safety_text_is_not_a_policy_input() -> None:
    payload = make_request().model_dump(mode="json")
    payload["model_safety_assessment"] = "The model says this should bypass approval"

    with pytest.raises(ValidationError) as error:
        ToolAuthorizationRequest.model_validate(payload)

    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_policy_models_are_frozen_and_reject_extra_fields() -> None:
    request = make_request()
    decision = make_engine().evaluate(request)

    with pytest.raises(ValidationError, match="frozen"):
        request.actor = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        decision.effect = PolicyEffect.DENY  # type: ignore[misc]

    with pytest.raises(ValidationError):
        PolicyResource.model_validate(
            {
                "kind": "filesystem_path",
                "identifier": SAFE_PATH,
                "unexpected": True,
            }
        )


def test_decision_requires_ttl_exactly_for_require_approval() -> None:
    allowed = make_engine().evaluate(make_request())
    required = make_engine().evaluate(make_request(risk_level=ToolRiskLevel.R2))

    with pytest.raises(ValidationError, match="approval_ttl_seconds"):
        PolicyDecision.model_validate(
            {**allowed.model_dump(mode="json"), "approval_ttl_seconds": 30}
        )
    with pytest.raises(ValidationError, match="approval_ttl_seconds"):
        PolicyDecision.model_validate(
            {**required.model_dump(mode="json"), "approval_ttl_seconds": None}
        )


def test_runner_grant_is_allow_only_and_approval_fields_are_all_or_none() -> None:
    request = make_request()
    decision = make_engine().evaluate(request)
    base = {
        "decision_id": decision.decision_id,
        "request_digest": decision.request_digest,
        "task_id": "tsk-policy",
        "step_id": "inspect-disk",
        "call_id": "call-policy",
        "actor_id": "model:fake",
        "origin": "builtin",
        "tool_name": "computer.disk_usage",
        "tool_version": "1.0.0",
        "contract_digest": "a" * 64,
        "policy_revision": decision.policy_revision,
        "rule_id": decision.rule_id,
        "reason_code": decision.reason_code,
        "effective_risk": decision.effective_risk,
        "arguments_digest": sha256_digest({"path": SAFE_PATH}),
        "resource_scope_digest": decision.resource_scope_digest,
        "expected_resource_versions_digest": sha256_digest({}),
        "capabilities": request.capabilities,
        "network_access": request.network_access,
        "data_egress": request.data_egress,
        "side_effects": request.side_effects,
        "reversible": request.reversible,
        "resources": request.resources,
        "interactive": request.interactive,
        "batch_count": request.batch_count,
    }
    automatic = ToolAuthorizationGrant.issue(**base)
    approved_at = datetime.now(UTC)
    approved = ToolAuthorizationGrant.issue(
        **base,
        approval_id="apr-policy",
        preview_hash="d" * 64,
        approved_at=approved_at,
        grant_expires_at=approved_at + timedelta(minutes=5),
    )

    assert automatic.effect is PolicyEffect.ALLOW
    assert automatic.approval_id is None
    assert approved.approval_id == "apr-policy"
    with pytest.raises(ValidationError, match="provided together"):
        ToolAuthorizationGrant.issue(**base, approval_id="apr-policy")
    with pytest.raises(ValidationError):
        ToolAuthorizationGrant.issue(**base, effect="deny")

    with pytest.raises(ValidationError, match="complete grant binding"):
        ToolAuthorizationGrant.model_validate(
            {**automatic.model_dump(mode="json"), "actor_id": "changed"}
        )
