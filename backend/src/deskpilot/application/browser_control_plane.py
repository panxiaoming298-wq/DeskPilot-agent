"""Durable, local-only Browser configuration bootstrap and read service."""

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.browser_automation_policy import (
    BrowserAutomationPolicyBundle,
    BrowserAutomationPolicyLoader,
    issue_browser_origin_allowlist_snapshot,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.browser_automation import BrowserOriginAllowlistSnapshot
from deskpilot.domain.browser_control_plane import (
    BrowserActionContractRead,
    BrowserControlPlaneSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    BrowserControlPlaneStateRecord,
    BrowserOriginAllowlistSnapshotRecord,
)

BROWSER_CONTROL_PLANE_CONFIGURATION_ID = "edge-deskpilot-v1"


class BrowserControlPlaneIntegrityError(RuntimeError):
    code = "BROWSER_CONTROL_PLANE_INTEGRITY_REJECTED"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _issue_control_plane_snapshot(
    bundle: BrowserAutomationPolicyBundle,
    *,
    allowlist: BrowserOriginAllowlistSnapshot,
    revision: int,
    updated_at: datetime,
    expected_digest: str | None = None,
) -> BrowserControlPlaneSnapshot:
    profile = bundle.policy.profile
    actions = tuple(
        BrowserActionContractRead(
            action=item.action,
            capability=item.capability,
            risk_level=item.risk_level,
            requires_origin_allowlist=item.requires_origin_allowlist,
            requires_fresh_approval=item.requires_fresh_approval,
            automatic_retries=item.automatic_retries,
            postcondition_verification=item.postcondition_verification,
        )
        for item in bundle.policy.actions
    )
    material = {
        "schema_version": "deskpilot.browser-control-plane.v1",
        "policy_digest": bundle.policy_digest,
        "configuration_id": BROWSER_CONTROL_PLANE_CONFIGURATION_ID,
        "revision": revision,
        "browser_product": profile.browser_product,
        "profile_name": profile.profile_name,
        "profile_mode": profile.profile_mode,
        "visible_window_required": profile.visible_window_required,
        "manual_login_only": profile.manual_login_only,
        "acceptance_loopback_only": profile.acceptance_loopback_only,
        "semantic_dom_targeting_only": profile.semantic_dom_targeting_only,
        "profile_created": False,
        "browser_launched": False,
        "operator_enabled": False,
        "origin_allowlist": allowlist.model_dump(mode="json"),
        "actions": tuple(item.model_dump(mode="json") for item in actions),
        "browser_operator_available": False,
        "network_execution_available": False,
        "action_execution_available": False,
        "updated_at": updated_at,
    }
    return BrowserControlPlaneSnapshot.model_validate(
        {
            **material,
            "snapshot_digest": expected_digest or sha256_digest(material),
        }
    )


class BrowserControlPlaneService:
    """Initializes and validates configuration without touching Edge or the network."""

    def __init__(
        self,
        database: Database,
        policy_loader: BrowserAutomationPolicyLoader | None = None,
    ) -> None:
        self._database = database
        self._policy_loader = policy_loader or BrowserAutomationPolicyLoader()

    async def initialize(self) -> BrowserControlPlaneSnapshot:
        bundle = self._policy_loader.load()
        async with self._database.session() as session:
            async with session.begin():
                state = await session.get(
                    BrowserControlPlaneStateRecord,
                    BROWSER_CONTROL_PLANE_CONFIGURATION_ID,
                )
                if state is not None:
                    return await self._validated_snapshot(session, state, bundle)

                now = datetime.now(UTC)
                allowlist = issue_browser_origin_allowlist_snapshot(
                    bundle,
                    origins=(),
                    revision=1,
                    updated_at=now,
                )
                snapshot = _issue_control_plane_snapshot(
                    bundle,
                    allowlist=allowlist,
                    revision=1,
                    updated_at=now,
                )
                profile = bundle.policy.profile
                session.add(
                    BrowserControlPlaneStateRecord(
                        configuration_id=BROWSER_CONTROL_PLANE_CONFIGURATION_ID,
                        policy_digest=bundle.policy_digest,
                        revision=1,
                        browser_product=profile.browser_product,
                        profile_name=profile.profile_name,
                        profile_mode=profile.profile_mode,
                        profile_created=False,
                        operator_enabled=False,
                        active_allowlist_revision=1,
                        active_allowlist_digest=allowlist.snapshot_digest,
                        control_plane_digest=snapshot.snapshot_digest,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add(
                    BrowserOriginAllowlistSnapshotRecord(
                        configuration_id=BROWSER_CONTROL_PLANE_CONFIGURATION_ID,
                        revision=1,
                        policy_digest=bundle.policy_digest,
                        origins=list(allowlist.origins),
                        updated_by=allowlist.updated_by,
                        updated_at=now,
                        snapshot_digest=allowlist.snapshot_digest,
                    )
                )
                return snapshot

    async def snapshot(self) -> BrowserControlPlaneSnapshot:
        bundle = self._policy_loader.load()
        async with self._database.session() as session:
            state = await session.get(
                BrowserControlPlaneStateRecord,
                BROWSER_CONTROL_PLANE_CONFIGURATION_ID,
            )
            if state is None:
                raise BrowserControlPlaneIntegrityError(
                    "Browser control plane is not initialized"
                )
            return await self._validated_snapshot(session, state, bundle)

    async def _validated_snapshot(
        self,
        session: AsyncSession,
        state: BrowserControlPlaneStateRecord,
        bundle: BrowserAutomationPolicyBundle,
    ) -> BrowserControlPlaneSnapshot:
        profile = bundle.policy.profile
        expected_state = (
            state.policy_digest == bundle.policy_digest
            and state.browser_product == profile.browser_product
            and state.profile_name == profile.profile_name
            and state.profile_mode == profile.profile_mode
            and state.revision == state.active_allowlist_revision
            and not state.profile_created
            and not state.operator_enabled
        )
        if not expected_state:
            raise BrowserControlPlaneIntegrityError(
                "Browser control-plane state does not match the frozen policy"
            )
        allowlist_record = await session.get(
            BrowserOriginAllowlistSnapshotRecord,
            (BROWSER_CONTROL_PLANE_CONFIGURATION_ID, state.active_allowlist_revision),
        )
        if allowlist_record is None:
            raise BrowserControlPlaneIntegrityError(
                "Active Browser allowlist revision is missing"
            )
        if not isinstance(allowlist_record.origins, list) or any(
            not isinstance(origin, str) for origin in allowlist_record.origins
        ):
            raise BrowserControlPlaneIntegrityError(
                "Browser allowlist payload is not a string list"
            )
        try:
            allowlist = BrowserOriginAllowlistSnapshot.model_validate(
                {
                    "schema_version": "deskpilot.browser-origin-allowlist.v1",
                    "policy_digest": allowlist_record.policy_digest,
                    "revision": allowlist_record.revision,
                    "origins": tuple(allowlist_record.origins),
                    "updated_by": allowlist_record.updated_by,
                    "updated_at": _as_utc(allowlist_record.updated_at),
                    "snapshot_digest": allowlist_record.snapshot_digest,
                }
            )
            if (
                allowlist.snapshot_digest != state.active_allowlist_digest
                or allowlist.policy_digest != state.policy_digest
            ):
                raise BrowserControlPlaneIntegrityError(
                    "Active Browser allowlist binding changed"
                )
            return _issue_control_plane_snapshot(
                bundle,
                allowlist=allowlist,
                revision=state.revision,
                updated_at=_as_utc(state.updated_at),
                expected_digest=state.control_plane_digest,
            )
        except ValidationError as error:
            raise BrowserControlPlaneIntegrityError(
                "Browser control-plane persisted digest validation failed"
            ) from error
