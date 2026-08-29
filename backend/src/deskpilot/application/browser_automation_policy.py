"""Strict, network-free validation for the phase 117A Browser Agent policy."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.browser_automation import (
    BrowserActionApprovalBinding,
    BrowserActionKind,
    BrowserActionPolicy,
    BrowserActionProposal,
    BrowserActionReadinessReport,
    BrowserAutomationPolicy,
    BrowserOriginAllowlistSnapshot,
    BrowserSensitiveDataKind,
    browser_origin_is_loopback,
)

MAX_BROWSER_POLICY_BYTES = 65_536


class BrowserAutomationPolicyError(RuntimeError):
    code = "BROWSER_AUTOMATION_POLICY_REJECTED"


@dataclass(frozen=True, slots=True)
class BrowserAutomationPolicyBundle:
    policy: BrowserAutomationPolicy
    policy_digest: str


def _read_strict_yaml(policy_path: Path) -> object:
    try:
        if policy_path.is_symlink() or not policy_path.is_file():
            raise BrowserAutomationPolicyError(
                "Browser automation policy must be one regular file"
            )
        payload = policy_path.read_bytes()
        if not payload or len(payload) > MAX_BROWSER_POLICY_BYTES:
            raise BrowserAutomationPolicyError(
                "Browser automation policy is empty or exceeds its size limit"
            )
        text = payload.decode("utf-8")
        if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
            raise BrowserAutomationPolicyError(
                "Browser automation policy YAML aliases are not allowed"
            )
        return yaml.safe_load(text)
    except BrowserAutomationPolicyError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise BrowserAutomationPolicyError(
            "Browser automation policy failed strict loading"
        ) from error


class BrowserAutomationPolicyLoader:
    def __init__(self, policy_path: Path | None = None) -> None:
        self._policy_path = policy_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "phase117_browser_automation_policy_v1.yaml"
        )

    def load(self) -> BrowserAutomationPolicyBundle:
        try:
            policy = BrowserAutomationPolicy.model_validate(
                _read_strict_yaml(self._policy_path)
            )
        except BrowserAutomationPolicyError:
            raise
        except ValidationError as error:
            raise BrowserAutomationPolicyError(
                "Browser automation policy failed strict validation"
            ) from error
        return BrowserAutomationPolicyBundle(
            policy=policy,
            policy_digest=sha256_digest(policy.model_dump(mode="json")),
        )


def issue_browser_origin_allowlist_snapshot(
    bundle: BrowserAutomationPolicyBundle,
    *,
    origins: tuple[str, ...] = (),
    revision: int = 1,
    updated_at: datetime | None = None,
) -> BrowserOriginAllowlistSnapshot:
    material: dict[str, Any] = {
        "schema_version": "deskpilot.browser-origin-allowlist.v1",
        "policy_digest": bundle.policy_digest,
        "revision": revision,
        "origins": tuple(sorted(origins)),
        "updated_by": "local_user",
        "updated_at": updated_at or datetime.now(UTC),
    }
    return BrowserOriginAllowlistSnapshot.model_validate(
        {**material, "snapshot_digest": sha256_digest(material)}
    )


def issue_browser_action_proposal(
    bundle: BrowserAutomationPolicyBundle,
    *,
    task_id: str,
    step_id: str,
    action: BrowserActionKind,
    origin: str,
    target_digest: str,
    content_digest: str,
    acceptance_mode: bool = False,
    sensitive_data_kinds: tuple[BrowserSensitiveDataKind, ...] = (),
) -> BrowserActionProposal:
    material: dict[str, Any] = {
        "schema_version": "deskpilot.browser-action-proposal.v1",
        "policy_digest": bundle.policy_digest,
        "task_id": task_id,
        "step_id": step_id,
        "action": action,
        "origin": origin,
        "target_digest": target_digest,
        "content_digest": content_digest,
        "acceptance_mode": acceptance_mode,
        "user_visible_window": True,
        "semantic_dom_targeting": True,
        "automated_login_requested": False,
        "coordinate_targeting_requested": False,
        "sensitive_data_kinds": sensitive_data_kinds,
    }
    return BrowserActionProposal.model_validate(
        {**material, "proposal_digest": sha256_digest(material)}
    )


class BrowserActionOfflinePreflight:
    """Check exact action scope without launching Edge or granting execution."""

    def __init__(self, bundle: BrowserAutomationPolicyBundle) -> None:
        self._bundle = bundle

    def run(
        self,
        proposal: BrowserActionProposal,
        allowlist: BrowserOriginAllowlistSnapshot,
        *,
        approval: BrowserActionApprovalBinding | None = None,
        now: datetime | None = None,
    ) -> BrowserActionReadinessReport:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise BrowserAutomationPolicyError(
                "Browser action readiness time must be timezone-aware"
            )
        action_policy = self._action_policy(proposal.action)
        violations: list[str] = []
        if proposal.policy_digest != self._bundle.policy_digest:
            violations.append("PROPOSAL_POLICY_DIGEST_MISMATCH")
        if allowlist.policy_digest != self._bundle.policy_digest:
            violations.append("ALLOWLIST_POLICY_DIGEST_MISMATCH")
        if proposal.acceptance_mode:
            if not browser_origin_is_loopback(proposal.origin):
                violations.append("ACCEPTANCE_ORIGIN_NOT_LOOPBACK")
        elif proposal.origin not in allowlist.origins:
            violations.append("ORIGIN_NOT_ALLOWLISTED")
        if proposal.sensitive_data_kinds:
            violations.append("SENSITIVE_DATA_REQUESTED")
        if action_policy.requires_fresh_approval:
            self._check_approval(proposal, approval, checked_at, violations)
        elif approval is not None:
            violations.append("APPROVAL_UNEXPECTED")

        return BrowserActionReadinessReport(
            schema_version="deskpilot.browser-action-readiness.v1",
            policy_digest=self._bundle.policy_digest,
            proposal_digest=proposal.proposal_digest,
            allowlist_snapshot_digest=allowlist.snapshot_digest,
            approval_binding_digest=(approval.binding_digest if approval else None),
            action=proposal.action,
            origin_digest=sha256_digest({"origin": proposal.origin}),
            ready=not violations,
            violations=tuple(violations),
            checked_at=checked_at,
        )

    def _action_policy(self, action: BrowserActionKind) -> BrowserActionPolicy:
        for policy in self._bundle.policy.actions:
            if policy.action is action:
                return policy
        raise BrowserAutomationPolicyError("Browser action is not frozen in the policy")

    def _check_approval(
        self,
        proposal: BrowserActionProposal,
        approval: BrowserActionApprovalBinding | None,
        checked_at: datetime,
        violations: list[str],
    ) -> None:
        if approval is None:
            violations.append("APPROVAL_REQUIRED")
            return
        if (
            approval.policy_digest != self._bundle.policy_digest
            or approval.proposal_digest != proposal.proposal_digest
            or approval.action is not proposal.action
            or approval.origin != proposal.origin
            or approval.target_digest != proposal.target_digest
            or approval.content_digest != proposal.content_digest
        ):
            violations.append("APPROVAL_BINDING_MISMATCH")
        if checked_at < approval.approved_at or checked_at > approval.valid_until:
            violations.append("APPROVAL_NOT_CURRENT")
