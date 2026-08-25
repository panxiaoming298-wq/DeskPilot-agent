from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.agent_model_admission import (
    AgentModelAdmissionError,
    AgentModelAdmissionRegistry,
    build_phase115_admission_bundle,
    load_agent_model_admissions,
)
from deskpilot.application.phase107_calibration import Phase107CalibrationService
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.agent_contracts import AgentRegistryStatus
from deskpilot.domain.agent_model_admissions import (
    AgentModelAdmissionBundle,
    ApprovedAgentModelAdmission,
)
from deskpilot.domain.model_contracts import ModelLocation, ModelProtocol
from deskpilot.domain.phase107_calibrations import Phase107CalibrationBaseline
from deskpilot.tools import create_builtin_registry
from tests.test_phase107_calibration_gate import (
    FIXED_NOW,
    SUITE,
    SUITE_V3,
    CalibratedJudgeProvider,
    CalibratedProposalProvider,
    _review_bundle,
)


class CloudCandidateProvider(CalibratedProposalProvider):
    def __init__(self) -> None:
        super().__init__()
        self._descriptor = self.descriptor.model_copy(
            update={
                "provider_id": "candidate-cloud",
                "display_name": "Candidate cloud fixture",
                "model": "candidate-cloud-v1",
                "protocol": ModelProtocol.OPENAI_COMPATIBLE_CHAT,
                "location": ModelLocation.CLOUD,
            }
        )


class CloudJudgeProvider(CalibratedJudgeProvider):
    def __init__(self) -> None:
        super().__init__()
        self._descriptor = self.descriptor.model_copy(
            update={
                "provider_id": "judge-cloud",
                "display_name": "Independent Judge cloud fixture",
                "model": "judge-cloud-v1",
                "protocol": ModelProtocol.OPENAI_COMPATIBLE_CHAT,
                "location": ModelLocation.CLOUD,
            }
        )


async def _approved_bundle(
    *,
    artifact_schema_version: Literal["v1", "v2"] = "v2",
) -> AgentModelAdmissionBundle:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    candidate = CloudCandidateProvider()
    run = await service.capture(
        suite,
        candidate,
        build_id="phase109-candidate-build",
        artifact_schema_version=artifact_schema_version,
        now=FIXED_NOW,
    )
    packet = service.make_blind_packet(suite, run)
    judge_run = await service.judge(
        suite,
        run,
        packet,
        CloudJudgeProvider(),
        build_id="phase109-judge-build",
        now=FIXED_NOW,
    )
    reviews = _review_bundle(
        run.run_digest,
        packet.packet_digest,
        tuple(item.sample_id for item in packet.samples),
    )
    report = service.grade(
        suite,
        run,
        packet,
        judge_run,
        reviews,
        now=FIXED_NOW,
    )
    baseline_material = {
        "schema_version": (
            "deskpilot.phase107-calibration-baseline.v2"
            if run.calibrated_agents
            else "deskpilot.phase107-calibration-baseline.v1"
        ),
        "baseline_id": "phase109-test-approved.v1",
        "suite_digest": suite.suite_digest,
        "cohort_digest": run.cohort_digest,
        "provider_snapshot_digest": run.provider_snapshot_digest,
        "coordinator_prompt_digest": run.coordinator_prompt_digest,
        "patch_prompt_digest": run.patch_prompt_digest,
        "judge_provider_snapshot_digest": judge_run.judge_provider_snapshot_digest,
        "judge_prompt_digest": judge_run.judge_prompt_digest,
        "judge_schema_digest": judge_run.judge_schema_digest,
        "minimum_acceptance_rate": 1.0,
        "maximum_primary_disagreement_rate": 0.0,
        "maximum_safety_reject_count": 0,
        "minimum_judge_human_agreement_rate": 1.0,
        "maximum_judge_false_accept_count": 0,
        "source_report_digest": report.report_digest,
        "previous_baseline_digest": None,
        "approved_by": "phase109-test-review",
    }
    if run.calibrated_agents:
        baseline_material["calibrated_agents"] = run.calibrated_agents
    baseline = Phase107CalibrationBaseline.model_validate(
        {**baseline_material, "approval_digest": sha256_digest(baseline_material)}
    )
    if run.calibrated_agents:
        patch_identity = run.calibrated_agents[1]
    else:
        _, patch_binding = service.calibrated_agent_bindings(run.provider)
        patch_identity = patch_binding.identity
    identity = {
        "agent_id": patch_identity.agent_id,
        "agent_version": patch_identity.agent_version,
        "agent_contract_digest": patch_identity.agent_contract_digest,
        "prompt_package_digest": patch_identity.prompt_package_digest,
        "provider_snapshot_digest": run.provider_snapshot_digest,
        "build_id": run.build_id,
        "report_digest": report.report_digest,
        "baseline_approval_digest": baseline.approval_digest,
    }
    admission_material = {
        "schema_version": "deskpilot.approved-agent-model-admission.v1",
        "admission_id": f"ama_{sha256_digest(identity)}",
        "agent_id": identity["agent_id"],
        "agent_version": identity["agent_version"],
        "agent_contract_digest": identity["agent_contract_digest"],
        "prompt_package_digest": identity["prompt_package_digest"],
        "provider": run.provider,
        "provider_snapshot_digest": run.provider_snapshot_digest,
        "build_id": run.build_id,
        "request_schema_digest": run.request_schema_digest,
        "run_digest": run.run_digest,
        "report_digest": report.report_digest,
        "baseline_approval_digest": baseline.approval_digest,
        "review_bundle_digest": reviews.bundle_digest,
        "approved_by": baseline.approved_by,
        "approved_at": FIXED_NOW,
        "valid_until": FIXED_NOW + timedelta(days=30),
    }
    admission = ApprovedAgentModelAdmission.model_validate(
        {
            **admission_material,
            "admission_digest": sha256_digest(admission_material),
        }
    )
    bundle_material = {
        "schema_version": "deskpilot.agent-model-admission-bundle.v1",
        "suite": suite,
        "run": run,
        "packet": packet,
        "judge_run": judge_run,
        "reviews": reviews,
        "report": report,
        "baseline": baseline,
        "admissions": (admission,),
    }
    return AgentModelAdmissionBundle.model_validate(
        {**bundle_material, "bundle_digest": sha256_digest(bundle_material)}
    )


@pytest.mark.asyncio
async def test_full_evidence_replay_admits_only_the_exact_unexpired_route() -> None:
    bundle = await _approved_bundle()
    admissions = AgentModelAdmissionRegistry.from_bundle(bundle, now=FIXED_NOW)
    assert admissions.admission_count == 1

    local_descriptor = bundle.run.provider.model_copy(
        update={"location": ModelLocation.LOCAL}
    )
    registry = create_builtin_agent_registry(
        create_builtin_registry(),
        (local_descriptor,),
    )
    patch = registry.resolve_exact("builtin.workspace_patch_planner", "1.0.0")
    assert admissions.allows(
        patch.contract,
        patch.prompt_package.digest,
        bundle.run.provider,
        now=FIXED_NOW,
    )
    assert not admissions.allows(
        patch.contract,
        patch.prompt_package.digest,
        bundle.run.provider.model_copy(update={"model": "substituted-model"}),
        now=FIXED_NOW,
    )
    assert not admissions.allows(
        patch.contract,
        "0" * 64,
        bundle.run.provider,
        now=FIXED_NOW,
    )
    assert not admissions.allows(
        patch.contract,
        patch.prompt_package.digest,
        bundle.run.provider,
        now=FIXED_NOW + timedelta(days=31),
    )


@pytest.mark.asyncio
async def test_phase115_builder_requires_and_admits_exact_three_role_cohort() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE_V3)
    run = await service.capture(
        suite,
        CloudCandidateProvider(),
        build_id="phase115-admission-build",
        turn_planner_version="2.0.0",
        coordinator_version="2.0.0",
        patch_version="2.0.0",
        artifact_schema_version="v3",
        now=FIXED_NOW,
    )
    packet = service.make_blind_packet(suite, run)
    judge_run = await service.judge(
        suite,
        run,
        packet,
        CloudJudgeProvider(),
        build_id="phase115-independent-judge",
        now=FIXED_NOW,
    )
    reviews = _review_bundle(
        run.run_digest,
        packet.packet_digest,
        tuple(item.sample_id for item in packet.samples),
    )
    report = service.grade(
        suite,
        run,
        packet,
        judge_run,
        reviews,
        now=FIXED_NOW,
    )
    bundle = build_phase115_admission_bundle(
        suite=suite,
        run=run,
        packet=packet,
        judge_run=judge_run,
        reviews=reviews,
        report=report,
        baseline_id="phase115.release-cohort.v3",
        approved_by="phase115-human-review-board",
        approved_at=FIXED_NOW,
        valid_until=FIXED_NOW + timedelta(days=30),
    )
    assert len(bundle.admissions) == 3
    assert {
        (item.agent_id, item.agent_version) for item in bundle.admissions
    } == {(item.agent_id, item.agent_version) for item in run.calibrated_agents}
    registry = AgentModelAdmissionRegistry.from_bundle(bundle, now=FIXED_NOW)
    assert registry.admission_count == 3

    partial_material = bundle.model_dump(mode="python", exclude={"bundle_digest"})
    partial_material["admissions"] = bundle.admissions[:2]
    with pytest.raises(ValueError, match="exact three-role cohort"):
        AgentModelAdmissionBundle.model_validate(
            {
                **partial_material,
                "bundle_digest": sha256_digest(partial_material),
            }
        )


@pytest.mark.asyncio
async def test_legacy_v1_bundle_remains_admissible() -> None:
    bundle = await _approved_bundle(artifact_schema_version="v1")
    assert bundle.run.calibrated_agents == ()
    assert bundle.report.calibrated_agents == ()
    admissions = AgentModelAdmissionRegistry.from_bundle(bundle, now=FIXED_NOW)
    assert admissions.admission_count == 1


@pytest.mark.asyncio
async def test_v2_admission_rejects_calibrated_agent_identity_drift() -> None:
    bundle = await _approved_bundle()
    drifted_patch = bundle.run.calibrated_agents[1].model_copy(
        update={"agent_contract_digest": "0" * 64}
    )
    drifted_run = bundle.run.model_copy(
        update={
            "calibrated_agents": (
                bundle.run.calibrated_agents[0],
                drifted_patch,
            )
        }
    )
    drifted_bundle = bundle.model_copy(update={"run": drifted_run})
    with pytest.raises(AgentModelAdmissionError, match="full calibration replay"):
        AgentModelAdmissionRegistry.from_bundle(drifted_bundle, now=FIXED_NOW)


@pytest.mark.asyncio
async def test_admission_rejects_expiry_tampering_and_never_expands_contract() -> None:
    bundle = await _approved_bundle()
    with pytest.raises(AgentModelAdmissionError, match="human evidence expired"):
        AgentModelAdmissionRegistry.from_bundle(
            bundle,
            now=FIXED_NOW + timedelta(days=31),
        )
    tampered = bundle.model_copy(
        update={
            "admissions": (
                bundle.admissions[0].model_copy(update={"build_id": "substituted-build"}),
            )
        }
    )
    with pytest.raises(AgentModelAdmissionError, match="exact approved"):
        AgentModelAdmissionRegistry.from_bundle(tampered, now=FIXED_NOW)
    unapproved = bundle.model_copy(
        update={
            "baseline": bundle.baseline.model_copy(
                update={"source_report_digest": "0" * 64}
            )
        }
    )
    with pytest.raises(AgentModelAdmissionError, match="does not approve"):
        AgentModelAdmissionRegistry.from_bundle(unapproved, now=FIXED_NOW)

    admissions = AgentModelAdmissionRegistry.from_bundle(bundle, now=FIXED_NOW)
    registry = create_builtin_agent_registry(
        create_builtin_registry(),
        (bundle.run.provider,),
        admissions,
    )
    descriptor = registry.descriptor_exact("builtin.workspace_patch_planner", "1.0.0")
    assert descriptor.status is AgentRegistryStatus.DISABLED
    assert descriptor.status_reason == "model_requirements_unsatisfied"

    original = bundle.admissions[0]
    fake_provider = original.provider.model_copy(update={"protocol": ModelProtocol.FAKE})
    fake_provider_digest = sha256_digest(fake_provider)
    fake_identity = {
        "agent_id": original.agent_id,
        "agent_version": original.agent_version,
        "agent_contract_digest": original.agent_contract_digest,
        "prompt_package_digest": original.prompt_package_digest,
        "provider_snapshot_digest": fake_provider_digest,
        "build_id": original.build_id,
        "report_digest": original.report_digest,
        "baseline_approval_digest": original.baseline_approval_digest,
    }
    fake_material = original.model_dump(
        mode="python",
        exclude={"admission_id", "admission_digest"},
    )
    fake_material.update(
        {
            "admission_id": f"ama_{sha256_digest(fake_identity)}",
            "provider": fake_provider,
            "provider_snapshot_digest": fake_provider_digest,
        }
    )
    with pytest.raises(ValueError, match="non-Fake cloud Provider"):
        ApprovedAgentModelAdmission.model_validate(
            {
                **fake_material,
                "admission_digest": sha256_digest(fake_material),
            }
        )


@pytest.mark.asyncio
async def test_production_loader_is_strict_and_default_closed(tmp_path: Path) -> None:
    assert load_agent_model_admissions(
        None,
        explicitly_allowed=False,
    ).admission_count == 0
    with pytest.raises(AgentModelAdmissionError, match="requires an evidence bundle"):
        load_agent_model_admissions(None, explicitly_allowed=True)
    missing = tmp_path / "missing.json"
    with pytest.raises(AgentModelAdmissionError, match="explicit allow"):
        load_agent_model_admissions(missing, explicitly_allowed=False)
    with pytest.raises(AgentModelAdmissionError, match="CI cannot"):
        load_agent_model_admissions(
            missing,
            explicitly_allowed=True,
            environ={"CI": "true"},
        )
    bundle = await _approved_bundle()
    approved = tmp_path / "approved.json"
    approved.write_text(bundle.model_dump_json(), encoding="utf-8")
    loaded = load_agent_model_admissions(
        approved,
        explicitly_allowed=True,
        environ={},
        now=FIXED_NOW,
    )
    assert loaded.admission_count == 1
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
    with pytest.raises(AgentModelAdmissionError, match="duplicate JSON key"):
        load_agent_model_admissions(
            duplicate,
            explicitly_allowed=True,
            environ={},
            now=FIXED_NOW,
        )
    with pytest.raises(ValueError, match="must be set together"):
        Settings(model_admission_allow=True)
    with pytest.raises(ValueError, match="must be set together"):
        Settings(model_admission_bundle_path="approved.json")
