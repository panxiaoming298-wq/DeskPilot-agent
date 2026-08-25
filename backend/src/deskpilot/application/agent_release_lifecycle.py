"""Strict Agent release lifecycle and startup activation policy."""

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from deskpilot.application.agent_registry import AgentRegistry
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import AgentContract
from deskpilot.domain.agent_releases import (
    AgentActivationChannel,
    AgentReleaseBundle,
    AgentReleaseEvent,
    AgentReleaseEventKind,
    AgentReleaseIdentity,
    AgentReleaseManifest,
)

MAX_AGENT_RELEASE_BUNDLE_BYTES = 4 * 1024 * 1024
RELEASE_COHORT_IDENTITIES = (
    ("turn_planner", "builtin.turn_planner", "2.0.0"),
    ("dynamic_coordinator", "builtin.workspace_coordinator", "2.0.0"),
    ("patch_planner", "builtin.workspace_patch_planner", "2.0.0"),
)
RELEASE_COMPANION_IDENTITIES = (
    ("workspace_reader", "builtin.workspace_reader", "2.0.0"),
    ("workspace_tester", "builtin.workspace_tester", "2.0.0"),
)


class AgentReleaseError(RuntimeError):
    code = "AGENT_RELEASE_REJECTED"


class AgentReleaseActivationPolicy:
    """Replay one immutable channel and authorize only its active exact release."""

    def __init__(
        self,
        bundle: AgentReleaseBundle | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self._bundle = bundle or empty_agent_release_bundle()
        self._now = now or datetime.now(UTC)
        self._releases = {item.release_id: item for item in self._bundle.releases}
        self._active = self._replay()

    @property
    def release_count(self) -> int:
        return len(self._releases)

    @property
    def active_release_id(self) -> str | None:
        return self._active.release_id if self._active is not None else None

    @property
    def bundle(self) -> AgentReleaseBundle:
        return self._bundle

    def allows(
        self,
        contract: AgentContract,
        prompt_package_digest: str,
    ) -> bool:
        if self._active is None or self._active.valid_until <= self._now:
            return False
        return any(
            identity.agent_id == contract.agent_id
            and identity.agent_version == contract.version
            and identity.agent_contract_digest == contract.digest
            and identity.prompt_package_digest == prompt_package_digest
            for identity in (*self._active.cohort, *self._active.companions)
        )

    def _replay(self) -> AgentReleaseManifest | None:
        releases = self._releases
        registered: set[str] = set()
        active_release_id: str | None = None
        previous: str | None = None
        previous_created_at: datetime | None = None
        for expected_sequence, event in enumerate(self._bundle.events, start=1):
            if (
                event.sequence != expected_sequence
                or event.channel != self._bundle.activation.channel
                or event.previous_event_digest != previous
                or event.release_id not in releases
                or event.created_at > self._now
                or (
                    previous_created_at is not None
                    and event.created_at < previous_created_at
                )
            ):
                raise AgentReleaseError("Agent release event chain is invalid")
            if event.related_release_id is not None and event.related_release_id not in releases:
                raise AgentReleaseError("Agent release event references an unknown release")
            release = releases[event.release_id]
            if event.kind is AgentReleaseEventKind.REGISTERED:
                if event.release_id in registered or event.created_at < release.created_at:
                    raise AgentReleaseError("Agent release registration is invalid")
                registered.add(event.release_id)
            elif event.kind is AgentReleaseEventKind.ACTIVATED:
                if (
                    event.release_id not in registered
                    or active_release_id is not None
                    or not (release.created_at <= event.created_at < release.valid_until)
                ):
                    raise AgentReleaseError("Agent release activation is invalid")
                active_release_id = event.release_id
            elif event.kind in {
                AgentReleaseEventKind.DISABLED,
                AgentReleaseEventKind.EXPIRED,
            }:
                if event.release_id not in registered:
                    raise AgentReleaseError("Unregistered Agent release was terminated")
                if event.kind is AgentReleaseEventKind.DISABLED:
                    if active_release_id != event.release_id:
                        raise AgentReleaseError("Disabled Agent release is not active")
                elif event.created_at < release.valid_until:
                    raise AgentReleaseError("Agent release expired before valid_until")
                if active_release_id == event.release_id:
                    active_release_id = None
            elif event.kind is AgentReleaseEventKind.REPLACED:
                replacement = releases[event.related_release_id or ""]
                if (
                    active_release_id != event.release_id
                    or replacement.release_id not in registered
                    or replacement.supersedes_release_id != event.release_id
                    or not (
                        replacement.created_at
                        <= event.created_at
                        < replacement.valid_until
                    )
                ):
                    raise AgentReleaseError("Agent release replacement is invalid")
                active_release_id = replacement.release_id
            elif event.kind is AgentReleaseEventKind.ROLLED_BACK:
                if active_release_id != event.release_id:
                    raise AgentReleaseError("Agent release rollback source is not active")
                target = releases[event.related_release_id or ""]
                if (
                    target.release_id not in registered
                    or not (target.created_at <= event.created_at < target.valid_until)
                ):
                    raise AgentReleaseError("Agent release rollback target is invalid")
                active_release_id = target.release_id
            previous = event.event_digest
            previous_created_at = event.created_at
        if registered != set(releases):
            raise AgentReleaseError("Agent release bundle contains an unregistered manifest")
        activation = self._bundle.activation
        if (
            activation.revision != len(self._bundle.events)
            or activation.last_event_digest != previous
            or activation.active_release_id != active_release_id
        ):
            raise AgentReleaseError("Agent activation channel does not match its event replay")
        active = releases.get(active_release_id) if active_release_id is not None else None
        if active is not None and active.created_at > self._now:
            raise AgentReleaseError("Agent release cannot be active before its creation")
        return active


class AgentReleaseLifecycle:
    """Pure lifecycle reducer that appends content-addressed audit events."""

    def __init__(self, bundle: AgentReleaseBundle | None = None) -> None:
        self._bundle = bundle or empty_agent_release_bundle()
        AgentReleaseActivationPolicy(self._bundle)

    @property
    def bundle(self) -> AgentReleaseBundle:
        return self._bundle

    def register(
        self,
        release: AgentReleaseManifest,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> AgentReleaseBundle:
        if any(item.release_id == release.release_id for item in self._bundle.releases):
            raise AgentReleaseError("Agent release is already registered")
        self._bundle = self._append(
            AgentReleaseEventKind.REGISTERED,
            release.release_id,
            actor=actor,
            now=now,
            releases=(*self._bundle.releases, release),
        )
        return self._bundle

    def activate(
        self,
        release_id: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> AgentReleaseBundle:
        release = self._release(release_id)
        timestamp = now or datetime.now(UTC)
        if release.valid_until <= timestamp:
            raise AgentReleaseError("Expired Agent release cannot be activated")
        self._bundle = self._append(
            AgentReleaseEventKind.ACTIVATED,
            release_id,
            actor=actor,
            now=timestamp,
            active_release_id=release_id,
        )
        return self._bundle

    def disable(
        self,
        release_id: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> AgentReleaseBundle:
        self._require_active(release_id)
        self._bundle = self._append(
            AgentReleaseEventKind.DISABLED,
            release_id,
            actor=actor,
            now=now,
            active_release_id=None,
        )
        return self._bundle

    def expire(
        self,
        release_id: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> AgentReleaseBundle:
        release = self._release(release_id)
        timestamp = now or datetime.now(UTC)
        if timestamp < release.valid_until:
            raise AgentReleaseError("Agent release cannot expire before valid_until")
        active = self._bundle.activation.active_release_id
        self._bundle = self._append(
            AgentReleaseEventKind.EXPIRED,
            release_id,
            actor=actor,
            now=timestamp,
            active_release_id=None if active == release_id else active,
        )
        return self._bundle

    def replace(
        self,
        current_release_id: str,
        replacement: AgentReleaseManifest,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> AgentReleaseBundle:
        self._require_active(current_release_id)
        if replacement.supersedes_release_id != current_release_id:
            raise AgentReleaseError("Replacement does not bind the active release")
        self.register(replacement, actor=actor, now=now)
        self._bundle = self._append(
            AgentReleaseEventKind.REPLACED,
            current_release_id,
            related_release_id=replacement.release_id,
            actor=actor,
            now=now,
            active_release_id=replacement.release_id,
        )
        return self._bundle

    def rollback(
        self,
        current_release_id: str,
        target_release_id: str,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> AgentReleaseBundle:
        self._require_active(current_release_id)
        target = self._release(target_release_id)
        timestamp = now or datetime.now(UTC)
        if target.valid_until <= timestamp:
            raise AgentReleaseError("Rollback target is expired")
        self._bundle = self._append(
            AgentReleaseEventKind.ROLLED_BACK,
            current_release_id,
            related_release_id=target_release_id,
            actor=actor,
            now=timestamp,
            active_release_id=target_release_id,
        )
        return self._bundle

    def _release(self, release_id: str) -> AgentReleaseManifest:
        try:
            return next(item for item in self._bundle.releases if item.release_id == release_id)
        except StopIteration as error:
            raise AgentReleaseError("Agent release is not registered") from error

    def _require_active(self, release_id: str) -> None:
        self._release(release_id)
        if self._bundle.activation.active_release_id != release_id:
            raise AgentReleaseError("Agent release is not active")

    def _append(
        self,
        kind: AgentReleaseEventKind,
        release_id: str,
        *,
        actor: str,
        now: datetime | None,
        releases: tuple[AgentReleaseManifest, ...] | None = None,
        related_release_id: str | None = None,
        active_release_id: str | None | object = ...,
    ) -> AgentReleaseBundle:
        timestamp = now or datetime.now(UTC)
        sequence = len(self._bundle.events) + 1
        previous = self._bundle.activation.last_event_digest
        identity = {
            "sequence": sequence,
            "channel": self._bundle.activation.channel,
            "kind": kind,
            "release_id": release_id,
            "related_release_id": related_release_id,
            "actor": actor,
            "created_at": timestamp,
            "previous_event_digest": previous,
        }
        event_material = {
            "schema_version": "deskpilot.agent-release-event.v1",
            "event_id": f"are_{sha256_digest(identity)}",
            **identity,
        }
        event = AgentReleaseEvent.model_validate(
            {**event_material, "event_digest": sha256_digest(event_material)}
        )
        resolved_active = (
            self._bundle.activation.active_release_id
            if active_release_id is ...
            else active_release_id
        )
        channel_material = {
            "schema_version": "deskpilot.agent-activation-channel.v1",
            "channel": self._bundle.activation.channel,
            "revision": sequence,
            "active_release_id": resolved_active,
            "last_event_digest": event.event_digest,
        }
        activation = AgentActivationChannel.model_validate(
            {**channel_material, "channel_digest": sha256_digest(channel_material)}
        )
        bundle_material = {
            "schema_version": "deskpilot.agent-release-bundle.v1",
            "releases": releases if releases is not None else self._bundle.releases,
            "events": (*self._bundle.events, event),
            "activation": activation,
        }
        candidate = AgentReleaseBundle.model_validate(
            {**bundle_material, "bundle_digest": sha256_digest(bundle_material)}
        )
        AgentReleaseActivationPolicy(candidate, now=timestamp)
        return candidate


def build_agent_release_manifest(
    registry: AgentRegistry,
    *,
    build_id: str,
    created_at: datetime,
    valid_until: datetime,
    supersedes_release_id: str | None = None,
) -> AgentReleaseManifest:
    """Bind one release to exact frozen Registry Contract and Prompt digests."""

    def identity(role: str, agent_id: str, version: str) -> AgentReleaseIdentity:
        descriptor = registry.descriptor_exact(agent_id, version)
        return AgentReleaseIdentity(
            role=role,
            agent_id=descriptor.agent_id,
            agent_version=descriptor.version,
            agent_contract_digest=descriptor.contract_digest,
            prompt_package_digest=descriptor.prompt_package.digest,
        )

    cohort = tuple(identity(*item) for item in RELEASE_COHORT_IDENTITIES)
    companions = tuple(identity(*item) for item in RELEASE_COMPANION_IDENTITIES)
    identity_material = {
        "build_id": build_id,
        "cohort": cohort,
        "companions": companions,
        "created_at": created_at,
        "valid_until": valid_until,
        "supersedes_release_id": supersedes_release_id,
    }
    manifest_material = {
        "schema_version": "deskpilot.agent-release-manifest.v1",
        "release_id": f"arl_{sha256_digest(identity_material)}",
        **identity_material,
    }
    return AgentReleaseManifest.model_validate(
        {
            **manifest_material,
            "release_digest": sha256_digest(manifest_material),
        }
    )


def empty_agent_release_bundle(channel: str = "production") -> AgentReleaseBundle:
    channel_material = {
        "schema_version": "deskpilot.agent-activation-channel.v1",
        "channel": channel,
        "revision": 0,
        "active_release_id": None,
        "last_event_digest": None,
    }
    activation = AgentActivationChannel.model_validate(
        {**channel_material, "channel_digest": sha256_digest(channel_material)}
    )
    bundle_material = {
        "schema_version": "deskpilot.agent-release-bundle.v1",
        "releases": (),
        "events": (),
        "activation": activation,
    }
    return AgentReleaseBundle.model_validate(
        {**bundle_material, "bundle_digest": sha256_digest(bundle_material)}
    )


def load_agent_release_activations(
    path: Path | None,
    *,
    explicitly_allowed: bool,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> AgentReleaseActivationPolicy:
    if path is None:
        if explicitly_allowed:
            raise AgentReleaseError("Agent release allow switch requires a bundle path")
        return AgentReleaseActivationPolicy(now=now)
    if not explicitly_allowed:
        raise AgentReleaseError("Agent release bundle requires an explicit allow switch")
    environment = environ if environ is not None else os.environ
    if environment.get("CI", "").lower() in {"1", "true", "yes"}:
        raise AgentReleaseError("CI cannot activate production Agent releases")
    bundle = load_agent_release_bundle(path)
    return AgentReleaseActivationPolicy(bundle, now=now)


def load_agent_release_bundle(path: Path) -> AgentReleaseBundle:
    """Strictly load an immutable bundle without granting runtime authority."""

    try:
        if path.is_symlink():
            raise AgentReleaseError("Agent release bundle cannot be a symbolic link")
        payload = path.resolve(strict=True).read_bytes()
        if not payload or len(payload) > MAX_AGENT_RELEASE_BUNDLE_BYTES:
            raise AgentReleaseError("Agent release bundle size is invalid")
        json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        return AgentReleaseBundle.model_validate_json(payload, strict=True)
    except AgentReleaseError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise AgentReleaseError("Agent release bundle failed strict loading") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentReleaseError("Agent release bundle contains a duplicate JSON key")
        result[key] = value
    return result
