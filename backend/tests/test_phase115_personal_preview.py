from datetime import timedelta

import pytest

from deskpilot.application.agent_model_admission import (
    AgentModelAdmissionError,
    build_phase115_admission_bundle,
)
from deskpilot.application.phase107_calibration import Phase107CalibrationService
from deskpilot.application.phase115_personal_preview import (
    Phase115PersonalPreviewError,
    build_phase115_personal_preview_bundle,
    load_phase115_personal_preview,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.phase107_calibrations import Phase107HumanReviewBundle
from tests.test_phase107_calibration_gate import (
    FIXED_NOW,
    SUITE_V3,
    _judgment,
)
from tests.test_phase109_model_admission import (
    CloudCandidateProvider,
    CloudJudgeProvider,
)


def _personal_reviews(
    run_digest: str,
    packet_digest: str,
    sample_ids: tuple[str, ...],
    *,
    second_reviewer: bool = False,
) -> Phase107HumanReviewBundle:
    judgments = tuple(
        judgment
        for sample_id in sample_ids
        for judgment in (
            _judgment(sample_id, "reviewer_operator_owner"),
            *(
                (_judgment(sample_id, "reviewer_unnecessary_second"),)
                if second_reviewer
                else ()
            ),
        )
    )
    material = {
        "schema_version": "deskpilot.phase115-personal-preview-review-bundle.v2",
        "run_digest": run_digest,
        "packet_digest": packet_digest,
        "rubric_version": "deskpilot.phase107-human-rubric.v1",
        "review_mode": "personal_preview",
        "valid_until": FIXED_NOW + timedelta(days=14),
        "judgments": judgments,
    }
    return Phase107HumanReviewBundle.model_validate(
        {**material, "bundle_digest": sha256_digest(material)}
    )


async def _preview_evidence() -> tuple[
    Phase107CalibrationService,
    object,
    object,
    object,
]:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE_V3)
    run = await service.capture(
        suite,
        CloudCandidateProvider(),
        build_id="phase115-personal-preview-candidate",
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
        build_id="phase115-personal-preview-judge",
        now=FIXED_NOW,
    )
    return service, suite, run, (packet, judge_run)


@pytest.mark.asyncio
async def test_personal_preview_accepts_one_reviewer_but_grants_no_runtime_authority(
    tmp_path,
) -> None:
    service, suite, run, combined = await _preview_evidence()
    packet, judge_run = combined
    reviews = _personal_reviews(
        run.run_digest,
        packet.packet_digest,
        tuple(item.sample_id for item in packet.samples),
    )

    bundle = build_phase115_personal_preview_bundle(
        suite=suite,
        run=run,
        packet=packet,
        judge_run=judge_run,
        reviews=reviews,
        operator_ref="reviewer_operator_owner",
        issued_at=FIXED_NOW,
        valid_until=FIXED_NOW + timedelta(days=14),
    )

    assert bundle.report.status == "passed"
    assert bundle.report.review_mode == "personal_preview"
    assert all(
        item.resolution == "single_primary"
        for item in bundle.report.resolved_judgments
    )
    assert bundle.activates_runtime is False
    assert not hasattr(bundle, "admissions")

    artifact = tmp_path / "personal-preview.json"
    artifact.write_text(bundle.model_dump_json(), encoding="utf-8")
    assert load_phase115_personal_preview(artifact) == bundle

    production_report = service.grade(
        suite,
        run,
        packet,
        judge_run,
        reviews,
        now=FIXED_NOW,
    )
    with pytest.raises(AgentModelAdmissionError, match="passed, current"):
        build_phase115_admission_bundle(
            suite=suite,
            run=run,
            packet=packet,
            judge_run=judge_run,
            reviews=reviews,
            report=production_report,
            baseline_id="must-not-be-produced.v1",
            approved_by="reviewer_operator_owner",
            approved_at=FIXED_NOW,
            valid_until=FIXED_NOW + timedelta(days=14),
        )


@pytest.mark.asyncio
async def test_personal_preview_rejects_second_reviewer_wrong_actor_and_long_validity() -> None:
    service, suite, run, combined = await _preview_evidence()
    packet, judge_run = combined
    sample_ids = tuple(item.sample_id for item in packet.samples)
    second_reviewer = _personal_reviews(
        run.run_digest,
        packet.packet_digest,
        sample_ids,
        second_reviewer=True,
    )
    with pytest.raises(Phase115PersonalPreviewError, match="full calibration replay"):
        build_phase115_personal_preview_bundle(
            suite=suite,
            run=run,
            packet=packet,
            judge_run=judge_run,
            reviews=second_reviewer,
            operator_ref="reviewer_operator_owner",
            issued_at=FIXED_NOW,
            valid_until=FIXED_NOW + timedelta(days=14),
        )

    reviews = _personal_reviews(
        run.run_digest,
        packet.packet_digest,
        sample_ids,
    )
    with pytest.raises(Phase115PersonalPreviewError, match="not admissible"):
        build_phase115_personal_preview_bundle(
            suite=suite,
            run=run,
            packet=packet,
            judge_run=judge_run,
            reviews=reviews,
            operator_ref="reviewer_someone_else",
            issued_at=FIXED_NOW,
            valid_until=FIXED_NOW + timedelta(days=14),
        )
    with pytest.raises(Phase115PersonalPreviewError, match="not admissible"):
        build_phase115_personal_preview_bundle(
            suite=suite,
            run=run,
            packet=packet,
            judge_run=judge_run,
            reviews=reviews,
            operator_ref="reviewer_operator_owner",
            issued_at=FIXED_NOW,
            valid_until=FIXED_NOW + timedelta(days=15),
        )


def test_legacy_production_review_digest_and_serialization_are_unchanged() -> None:
    sample_id = "cal_" + "a" * 64
    judgment = _judgment(sample_id, "reviewer_alpha")
    material = {
        "schema_version": "deskpilot.phase107-human-review-bundle.v1",
        "run_digest": "1" * 64,
        "packet_digest": "2" * 64,
        "rubric_version": "deskpilot.phase107-human-rubric.v1",
        "valid_until": FIXED_NOW + timedelta(days=30),
        "judgments": (judgment,),
    }
    bundle = Phase107HumanReviewBundle.model_validate(
        {**material, "bundle_digest": sha256_digest(material)}
    )

    assert bundle.review_mode == "production"
    assert "review_mode" not in bundle.model_dump(mode="json")
    assert bundle.bundle_digest == sha256_digest(material)
