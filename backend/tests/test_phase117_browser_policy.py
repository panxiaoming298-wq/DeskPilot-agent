from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from deskpilot.application.browser_automation_policy import (
    BrowserActionOfflinePreflight,
    BrowserAutomationPolicyError,
    BrowserAutomationPolicyLoader,
    issue_browser_action_proposal,
    issue_browser_origin_allowlist_snapshot,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.browser_automation import (
    BrowserActionApprovalBinding,
    BrowserActionKind,
    BrowserOriginAllowlistSnapshot,
    browser_action_approval_preview_hash,
)

BACKEND_ROOT = Path(__file__).parents[1]
POLICY_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "phase117_browser_automation_policy_v1.yaml"
)
FIXED_NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
TARGET_DIGEST = sha256_digest({"selector": "form[name=profile]"})
CONTENT_DIGEST = sha256_digest({"fields": {"display_name": "DeskPilot test"}})


def _proposal(
    action: BrowserActionKind,
    *,
    origin: str = "https://example.com",
    acceptance_mode: bool = False,
    sensitive_data_kinds: tuple[str, ...] = (),
):
    bundle = BrowserAutomationPolicyLoader().load()
    return issue_browser_action_proposal(
        bundle,
        task_id="task-browser-117a",
        step_id=f"step-{action.value}",
        action=action,
        origin=origin,
        target_digest=TARGET_DIGEST,
        content_digest=CONTENT_DIGEST,
        acceptance_mode=acceptance_mode,
        sensitive_data_kinds=sensitive_data_kinds,
    )


def _approval(
    proposal,
    *,
    approved_at: datetime = FIXED_NOW,
    validity: timedelta = timedelta(minutes=5),
) -> BrowserActionApprovalBinding:
    bundle = BrowserAutomationPolicyLoader().load()
    material = {
        "schema_version": "deskpilot.browser-action-approval-binding.v1",
        "policy_digest": bundle.policy_digest,
        "proposal_digest": proposal.proposal_digest,
        "approval_id": "apr_0123456789abcdef0123456789abcdef",
        "preview_hash": browser_action_approval_preview_hash(proposal),
        "action": proposal.action,
        "origin": proposal.origin,
        "target_digest": proposal.target_digest,
        "content_digest": proposal.content_digest,
        "approved_by": "local_user",
        "approved_at": approved_at,
        "valid_until": approved_at + validity,
    }
    return BrowserActionApprovalBinding.model_validate(
        {**material, "binding_digest": sha256_digest(material)}
    )


def test_policy_freezes_edge_profile_action_matrix_and_non_execution_boundary() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    policy = bundle.policy

    assert (
        bundle.policy_digest
        == "2aeb30b31161f41ba48841c86d6f80f7847327f58d31cfd500b2ed936633177f"
    )
    assert policy.profile.browser_product == "microsoft_edge"
    assert policy.profile.profile_name == "DeskPilot"
    assert policy.profile.default_allowed_origins == ()
    assert policy.profile.visible_window_required is True
    assert policy.profile.manual_login_only is True
    assert policy.profile.automated_login is False
    assert policy.profile.existing_personal_profile_reuse is False
    assert policy.profile.semantic_dom_targeting_only is True
    assert policy.profile.arbitrary_coordinate_input is False
    assert [item.action for item in policy.actions] == list(BrowserActionKind)
    assert [item.requires_fresh_approval for item in policy.actions] == [
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
    ]
    assert policy.sensitive_data.cookie_values_readable is False
    assert policy.sensitive_data.passwords_readable is False
    assert policy.sensitive_data.one_time_codes_readable is False
    assert policy.sensitive_data.captcha_bypass_allowed is False
    assert not any(policy.offline_execution_boundary.model_dump().values())


def test_policy_loader_rejects_yaml_aliases_and_policy_drift(tmp_path: Path) -> None:
    alias_policy = tmp_path / "alias.yaml"
    alias_policy.write_text("base: &base {value: 1}\ncopy: *base\n", encoding="utf-8")
    with pytest.raises(BrowserAutomationPolicyError):
        BrowserAutomationPolicyLoader(alias_policy).load()

    drifted_policy = tmp_path / "drifted.yaml"
    drifted_policy.write_text(
        POLICY_PATH.read_text(encoding="utf-8").replace(
            "default_allowed_origins: []",
            "default_allowed_origins: [https://example.com]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(BrowserAutomationPolicyError):
        BrowserAutomationPolicyLoader(drifted_policy).load()


@pytest.mark.parametrize(
    "origin",
    (
        "http://example.com",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
        "https://EXAMPLE.com",
        "https://example.com:443",
    ),
)
def test_proposal_rejects_unsafe_or_non_normalized_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        _proposal(BrowserActionKind.NAVIGATE, origin=origin)


def test_default_empty_allowlist_blocks_public_navigation_without_side_effects() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(bundle, updated_at=FIXED_NOW)

    report = BrowserActionOfflinePreflight(bundle).run(
        _proposal(BrowserActionKind.NAVIGATE),
        allowlist,
        now=FIXED_NOW,
    )

    assert report.ready is False
    assert report.violations == ("ORIGIN_NOT_ALLOWLISTED",)
    assert report.browser_profile_created is False
    assert report.browser_launched is False
    assert report.network_access is False
    assert report.action_executed is False
    assert report.execution_authorized is False


def test_local_user_allowlist_snapshot_allows_public_observation_but_grants_no_execution() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(
        bundle,
        origins=("https://example.com",),
        updated_at=FIXED_NOW,
    )

    report = BrowserActionOfflinePreflight(bundle).run(
        _proposal(BrowserActionKind.DOM_READ),
        allowlist,
        now=FIXED_NOW,
    )

    assert report.ready is True
    assert report.violations == ()
    assert report.execution_authorized is False


@pytest.mark.parametrize(
    "action",
    (
        BrowserActionKind.NAVIGATE,
        BrowserActionKind.DOM_READ,
        BrowserActionKind.SCREENSHOT,
        BrowserActionKind.FORM_PREFILL,
    ),
)
def test_automated_acceptance_only_allows_loopback_observation_and_prefill(
    action: BrowserActionKind,
) -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(bundle, updated_at=FIXED_NOW)
    report = BrowserActionOfflinePreflight(bundle).run(
        _proposal(
            action,
            origin="http://127.0.0.1:8765",
            acceptance_mode=True,
        ),
        allowlist,
        now=FIXED_NOW,
    )

    assert report.ready is True
    assert report.violations == ()
    assert report.network_access is False
    assert report.action_executed is False


def test_acceptance_mode_rejects_public_origin_even_when_allowlisted() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(
        bundle,
        origins=("https://example.com",),
        updated_at=FIXED_NOW,
    )
    report = BrowserActionOfflinePreflight(bundle).run(
        _proposal(BrowserActionKind.DOM_READ, acceptance_mode=True),
        allowlist,
        now=FIXED_NOW,
    )

    assert report.ready is False
    assert report.violations == ("ACCEPTANCE_ORIGIN_NOT_LOOPBACK",)


def test_sensitive_data_request_is_blocked_even_on_loopback() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(bundle, updated_at=FIXED_NOW)
    report = BrowserActionOfflinePreflight(bundle).run(
        _proposal(
            BrowserActionKind.DOM_READ,
            origin="http://localhost:8765",
            acceptance_mode=True,
            sensitive_data_kinds=("password",),
        ),
        allowlist,
        now=FIXED_NOW,
    )

    assert report.ready is False
    assert report.violations == ("SENSITIVE_DATA_REQUESTED",)


@pytest.mark.parametrize(
    "action",
    (
        BrowserActionKind.SUBMIT,
        BrowserActionKind.UPLOAD,
        BrowserActionKind.DOWNLOAD,
        BrowserActionKind.PUBLISH,
    ),
)
def test_consequential_actions_require_a_new_exact_approval(
    action: BrowserActionKind,
) -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(
        bundle,
        origins=("https://example.com",),
        updated_at=FIXED_NOW,
    )
    proposal = _proposal(action)

    blocked = BrowserActionOfflinePreflight(bundle).run(
        proposal,
        allowlist,
        now=FIXED_NOW,
    )
    approval = _approval(proposal)
    ready = BrowserActionOfflinePreflight(bundle).run(
        proposal,
        allowlist,
        approval=approval,
        now=FIXED_NOW,
    )

    assert blocked.ready is False
    assert blocked.violations == ("APPROVAL_REQUIRED",)
    assert ready.ready is True
    assert ready.violations == ()
    assert ready.approval_binding_digest == approval.binding_digest
    assert ready.execution_authorized is False


def test_approval_cannot_be_reused_for_changed_target_or_after_expiry() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    allowlist = issue_browser_origin_allowlist_snapshot(
        bundle,
        origins=("https://example.com",),
        updated_at=FIXED_NOW,
    )
    original = _proposal(BrowserActionKind.SUBMIT)
    changed = issue_browser_action_proposal(
        bundle,
        task_id="task-browser-117a",
        step_id="step-submit-changed",
        action=BrowserActionKind.SUBMIT,
        origin="https://example.com",
        target_digest=sha256_digest({"selector": "form[name=other]"}),
        content_digest=CONTENT_DIGEST,
    )
    approval = _approval(original)

    mismatch = BrowserActionOfflinePreflight(bundle).run(
        changed,
        allowlist,
        approval=approval,
        now=FIXED_NOW,
    )
    expired = BrowserActionOfflinePreflight(bundle).run(
        original,
        allowlist,
        approval=approval,
        now=FIXED_NOW + timedelta(minutes=5, seconds=1),
    )

    assert mismatch.ready is False
    assert mismatch.violations == ("APPROVAL_BINDING_MISMATCH",)
    assert expired.ready is False
    assert expired.violations == ("APPROVAL_NOT_CURRENT",)


def test_approval_validity_cannot_exceed_five_minutes() -> None:
    with pytest.raises(ValidationError):
        _approval(
            _proposal(BrowserActionKind.PUBLISH),
            validity=timedelta(minutes=5, seconds=1),
        )


def test_allowlist_snapshot_digest_rejects_tampering() -> None:
    bundle = BrowserAutomationPolicyLoader().load()
    snapshot = issue_browser_origin_allowlist_snapshot(
        bundle,
        origins=("https://example.com",),
        updated_at=FIXED_NOW,
    )
    tampered = snapshot.model_dump(mode="python")
    tampered["origins"] = ("https://openai.com",)

    with pytest.raises(ValidationError):
        BrowserOriginAllowlistSnapshot.model_validate(tampered)
