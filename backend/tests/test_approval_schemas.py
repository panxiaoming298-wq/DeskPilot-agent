from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from deskpilot.domain.approvals import (
    ApprovalRead,
    ApprovalResourceRead,
    ApprovalStatus,
    DataEgress,
    ResolveCommand,
)

HASH = "a" * 64
DECISION_ID = f"pdec_{'b' * 64}"


def _pending_approval(**overrides: object) -> ApprovalRead:
    requested_at = datetime(2026, 8, 9, 12, tzinfo=UTC)
    values: dict[str, object] = {
        "approval_id": "apr_123",
        "decision_id": DECISION_ID,
        "task_id": "tsk_123",
        "call_id": "call-123",
        "tool_name": "computer.disk_usage",
        "tool_version": "1.0.0",
        "status": "pending",
        "decision": None,
        "risk_level": "R0",
        "title": "Inspect disk capacity",
        "purpose": "Report free and used bytes for the selected disk.",
        "capabilities": ["filesystem.metadata.read"],
        "resource_scope": [
            {
                "kind": "filesystem_path",
                "label": "C:\\",
                "operations": ["metadata.read"],
                "version": None,
            }
        ],
        "consequences": [],
        "reversible": False,
        "data_egress": {"enabled": False, "destination": None},
        "policy_rule_id": "risk-r0-forced-approval",
        "policy_revision": "deskpilot-policy-v1",
        "reason_code": "USER_CONFIRMATION_REQUIRED",
        "preview_hash": HASH,
        "requested_at": requested_at,
        "expires_at": requested_at + timedelta(minutes=5),
        "resolved_at": None,
        "consumed_at": None,
        "resolution_reason": None,
        "updated_at": requested_at,
    }
    values.update(overrides)
    return ApprovalRead.model_validate(values)


def test_public_approval_projection_is_strict_and_normalizes_collections() -> None:
    approval = _pending_approval()

    assert approval.status is ApprovalStatus.PENDING
    assert approval.status.is_terminal is False
    assert approval.capabilities == ("filesystem.metadata.read",)
    assert approval.resource_scope == (
        ApprovalResourceRead(
            kind="filesystem_path",
            label="C:\\",
            operations=("metadata.read",),
        ),
    )
    assert approval.data_egress == DataEgress(enabled=False)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        _pending_approval(secret="must-not-enter-the-public-projection")


def test_approval_lifecycle_rejects_inconsistent_or_naive_timestamps() -> None:
    requested_at = datetime(2026, 8, 9, 12, tzinfo=UTC)

    with pytest.raises(ValidationError, match="pending approval"):
        _pending_approval(resolved_at=requested_at + timedelta(seconds=1))
    with pytest.raises(ValidationError, match="timezone-aware"):
        _pending_approval(requested_at=requested_at.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="only an approved request"):
        _pending_approval(
            status="rejected",
            decision="rejected",
            resolved_at=requested_at + timedelta(seconds=1),
            consumed_at=requested_at + timedelta(seconds=2),
        )


def test_data_egress_requires_an_exact_destination_only_when_enabled() -> None:
    assert DataEgress(enabled=True, destination="https://api.example.test").enabled

    with pytest.raises(ValidationError, match="requires a destination"):
        DataEgress(enabled=True)
    with pytest.raises(ValidationError, match="must not declare"):
        DataEgress(enabled=False, destination="https://api.example.test")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DataEgress.model_validate({"enabled": False, "destination": None, "token": "x"})


def test_resolve_command_binds_preview_and_allows_only_one_shot_scope() -> None:
    command = ResolveCommand(preview_hash=HASH, reason="User reviewed the preview")

    assert command.scope == "once"
    with pytest.raises(ValidationError):
        ResolveCommand(preview_hash="not-a-digest")
    with pytest.raises(ValidationError):
        ResolveCommand(preview_hash=HASH, scope="always")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResolveCommand.model_validate(
            {"preview_hash": HASH, "scope": "once", "approval_id": "apr_wrong"}
        )
