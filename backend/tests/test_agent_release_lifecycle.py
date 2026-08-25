from datetime import timedelta
from pathlib import Path

import pytest

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.agent_registry import AgentRegistry
from deskpilot.application.agent_release_lifecycle import (
    AgentReleaseActivationPolicy,
    AgentReleaseError,
    AgentReleaseLifecycle,
    load_agent_release_activations,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.agent_contracts import AgentContract, AgentRegistryStatus
from deskpilot.domain.agent_releases import AgentReleaseIdentity, AgentReleaseManifest
from deskpilot.domain.model_contracts import ModelLocation, ModelProtocol
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry
from tests.test_phase107_calibration_gate import FIXED_NOW


class AllowAllAdmissions:
    def allows(self, *_args: object, **_kwargs: object) -> bool:
        return True


def _candidate_registry() -> AgentRegistry:
    provider = FakeModelProvider().descriptor
    return create_builtin_agent_registry(
        create_builtin_registry(),
        (provider,),
    )


def _identity(
    registry: AgentRegistry, role: str, agent_id: str, version: str
) -> AgentReleaseIdentity:
    descriptor = registry.descriptor_exact(agent_id, version)
    return AgentReleaseIdentity(
        role=role,
        agent_id=agent_id,
        agent_version=version,
        agent_contract_digest=descriptor.contract_digest,
        prompt_package_digest=descriptor.prompt_package.digest,
    )


def _manifest(
    *,
    build_id: str = "stage115-build-a",
    supersedes_release_id: str | None = None,
    valid_days: int = 30,
) -> AgentReleaseManifest:
    registry = _candidate_registry()
    cohort = (
        _identity(registry, "turn_planner", "builtin.turn_planner", "2.0.0"),
        _identity(
            registry,
            "dynamic_coordinator",
            "builtin.workspace_coordinator",
            "2.0.0",
        ),
        _identity(
            registry,
            "patch_planner",
            "builtin.workspace_patch_planner",
            "2.0.0",
        ),
    )
    companions = (
        _identity(
            registry,
            "workspace_reader_companion",
            "builtin.workspace_reader",
            "2.0.0",
        ),
        _identity(
            registry,
            "workspace_tester_companion",
            "builtin.workspace_tester",
            "2.0.0",
        ),
    )
    identity = {
        "build_id": build_id,
        "cohort": cohort,
        "companions": companions,
        "created_at": FIXED_NOW,
        "valid_until": FIXED_NOW + timedelta(days=valid_days),
        "supersedes_release_id": supersedes_release_id,
    }
    release_material = {
        "schema_version": "deskpilot.agent-release-manifest.v1",
        "release_id": f"arl_{sha256_digest(identity)}",
        **identity,
    }
    return AgentReleaseManifest.model_validate(
        {**release_material, "release_digest": sha256_digest(release_material)}
    )


def test_release_lifecycle_replays_activation_disable_replace_and_rollback() -> None:
    lifecycle = AgentReleaseLifecycle()
    first = _manifest()
    lifecycle.register(first, actor="release-admin", now=FIXED_NOW)
    assert lifecycle.bundle.activation.active_release_id is None
    lifecycle.activate(first.release_id, actor="release-admin", now=FIXED_NOW)
    assert AgentReleaseActivationPolicy(
        lifecycle.bundle,
        now=FIXED_NOW,
    ).active_release_id == first.release_id

    lifecycle.disable(first.release_id, actor="release-admin", now=FIXED_NOW)
    assert lifecycle.bundle.activation.active_release_id is None
    lifecycle.activate(first.release_id, actor="release-admin", now=FIXED_NOW)

    replacement = _manifest(
        build_id="stage115-build-b",
        supersedes_release_id=first.release_id,
    )
    lifecycle.replace(
        first.release_id,
        replacement,
        actor="release-admin",
        now=FIXED_NOW,
    )
    assert lifecycle.bundle.activation.active_release_id == replacement.release_id
    lifecycle.rollback(
        replacement.release_id,
        first.release_id,
        actor="release-admin",
        now=FIXED_NOW,
    )
    policy = AgentReleaseActivationPolicy(lifecycle.bundle, now=FIXED_NOW)
    assert policy.active_release_id == first.release_id
    assert [item.sequence for item in lifecycle.bundle.events] == list(
        range(1, len(lifecycle.bundle.events) + 1)
    )


def test_release_expiry_and_event_tampering_fail_closed() -> None:
    lifecycle = AgentReleaseLifecycle()
    release = _manifest(valid_days=1)
    lifecycle.register(release, actor="release-admin", now=FIXED_NOW)
    lifecycle.activate(release.release_id, actor="release-admin", now=FIXED_NOW)
    expired_at = FIXED_NOW + timedelta(days=1)
    assert not AgentReleaseActivationPolicy(
        lifecycle.bundle,
        now=expired_at,
    ).allows(AgentContract.model_construct(), "0" * 64)
    lifecycle.expire(release.release_id, actor="release-admin", now=expired_at)
    assert lifecycle.bundle.activation.active_release_id is None

    tampered_event = lifecycle.bundle.events[-1].model_copy(
        update={"previous_event_digest": "0" * 64}
    )
    tampered = lifecycle.bundle.model_copy(
        update={"events": (*lifecycle.bundle.events[:-1], tampered_event)}
    )
    with pytest.raises(AgentReleaseError, match="event chain"):
        AgentReleaseActivationPolicy(tampered, now=expired_at)


def test_release_loader_is_strict_explicit_and_default_closed(tmp_path: Path) -> None:
    assert load_agent_release_activations(
        None,
        explicitly_allowed=False,
        now=FIXED_NOW,
    ).release_count == 0
    with pytest.raises(AgentReleaseError, match="requires a bundle path"):
        load_agent_release_activations(None, explicitly_allowed=True, now=FIXED_NOW)
    missing = tmp_path / "missing.json"
    with pytest.raises(AgentReleaseError, match="explicit allow"):
        load_agent_release_activations(
            missing,
            explicitly_allowed=False,
            now=FIXED_NOW,
        )
    with pytest.raises(AgentReleaseError, match="CI cannot"):
        load_agent_release_activations(
            missing,
            explicitly_allowed=True,
            environ={"CI": "true"},
            now=FIXED_NOW,
        )

    lifecycle = AgentReleaseLifecycle()
    release = _manifest()
    lifecycle.register(release, actor="release-admin", now=FIXED_NOW)
    lifecycle.activate(release.release_id, actor="release-admin", now=FIXED_NOW)
    approved = tmp_path / "release.json"
    approved.write_text(lifecycle.bundle.model_dump_json(), encoding="utf-8")
    loaded = load_agent_release_activations(
        approved,
        explicitly_allowed=True,
        environ={},
        now=FIXED_NOW,
    )
    assert loaded.active_release_id == release.release_id

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
    with pytest.raises(AgentReleaseError, match="duplicate JSON key"):
        load_agent_release_activations(
            duplicate,
            explicitly_allowed=True,
            environ={},
            now=FIXED_NOW,
        )
    with pytest.raises(ValueError, match="must be set together"):
        Settings(agent_release_allow=True)
    with pytest.raises(ValueError, match="must be set together"):
        Settings(agent_release_bundle_path="release.json")


def test_cloud_release_never_becomes_preferred_without_both_release_and_admission() -> None:
    local = FakeModelProvider().descriptor
    cloud = local.model_copy(
        update={
            "provider_id": "stage115-cloud",
            "display_name": "Stage 115 cloud fixture",
            "model": "stage115-cloud-v1",
            "protocol": ModelProtocol.OPENAI_COMPATIBLE_CHAT,
            "location": ModelLocation.CLOUD,
        }
    )
    default_registry = create_builtin_agent_registry(
        create_builtin_registry(),
        (local, cloud),
        AllowAllAdmissions(),
    )
    cloud_descriptor = default_registry.descriptor_exact("builtin.turn_planner", "2.0.0")
    assert cloud_descriptor.status is AgentRegistryStatus.DISABLED
    assert cloud_descriptor.status_reason == "release_not_activated"
    assert default_registry.resolve_preferred("builtin.turn_planner").contract.version == "1.0.0"

    lifecycle = AgentReleaseLifecycle()
    release = _manifest()
    lifecycle.register(release, actor="release-admin", now=FIXED_NOW)
    lifecycle.activate(release.release_id, actor="release-admin", now=FIXED_NOW)
    policy = AgentReleaseActivationPolicy(lifecycle.bundle, now=FIXED_NOW)

    missing_admission = create_builtin_agent_registry(
        create_builtin_registry(),
        (local, cloud),
        release_activations=policy,
    )
    for agent_id in (
        "builtin.turn_planner",
        "builtin.workspace_coordinator",
        "builtin.workspace_patch_planner",
        "builtin.workspace_reader",
        "builtin.workspace_tester",
    ):
        descriptor = missing_admission.descriptor_exact(agent_id, "2.0.0")
        assert descriptor.status is AgentRegistryStatus.DISABLED
        assert descriptor.status_reason == "release_cohort_unsatisfied"

    active_registry = create_builtin_agent_registry(
        create_builtin_registry(),
        (local, cloud),
        AllowAllAdmissions(),
        policy,
    )
    assert active_registry.resolve_preferred("builtin.turn_planner").contract.version == "2.0.0"
    assert (
        active_registry.resolve_preferred_compatible(
            "builtin.turn_planner",
            allowed_locations=(ModelLocation.LOCAL,),
            allowed_privacy_modes=("local_preferred",),
        ).contract.version
        == "1.0.0"
    )
    assert (
        active_registry.resolve_preferred_compatible(
            "builtin.turn_planner",
            allowed_locations=(ModelLocation.CLOUD,),
            allowed_privacy_modes=("balanced",),
        ).contract.version
        == "2.0.0"
    )
    assert (
        active_registry.resolve_preferred("builtin.workspace_coordinator").contract.version
        == "2.0.0"
    )
