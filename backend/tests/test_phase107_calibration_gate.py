from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deskpilot.application.phase107_calibration import (
    Phase107CalibrationError,
    Phase107CalibrationService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_loop import (
    WorkspacePatchChangeProposal,
    WorkspacePatchLoopDecision,
    WorkspacePatchSubmitProposalDecision,
)
from deskpilot.domain.model_contracts import (
    ModelFinishReason,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from deskpilot.domain.phase107_calibrations import (
    Phase107CalibrationBaseline,
    Phase107HumanJudgment,
    Phase107HumanReviewBundle,
    Phase107JudgeDecision,
)
from deskpilot.domain.turn_planning import (
    TurnPlannerDecision,
    TurnPlannerProposeStepsDecision,
    TurnPlannerStepProposal,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.phase107_gate import _parser
from deskpilot.phase107_gate import main as phase107_main

SUITE = Path(__file__).parent / "fixtures" / "phase107-live-agent-calibration-suite.v1.json"
SUITE_V3 = (
    Path(__file__).parent / "fixtures" / "phase115-live-agent-calibration-suite.v2.json"
)
FIXED_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class CalibratedProposalProvider(FakeModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        if (
            request.output_schema is not None
            and request.output_schema.name == "turn_planner_decision"
        ):
            planner_input = json.loads(request.messages[-1].content)
            if planner_input["user_message"] == "请只读查看 backend 目录结构。":
                output = TurnPlannerDecision(
                    root=TurnPlannerProposeStepsDecision(
                        kind="propose_steps",
                        steps=(
                            TurnPlannerStepProposal(
                                offer_key=planner_input["offers"][0]["offer"][
                                    "offer_key"
                                ],
                                parameters=(),
                            ),
                        ),
                    )
                ).model_dump(mode="json")
                return response.model_copy(update={"structured_output": output})
        if (
            request.output_schema is None
            or request.output_schema.name != "workspace_patch_planner_loop_decision"
            or request.metadata.get("agent_loop_phase") != "propose_patch"
        ):
            return response
        path = request.metadata.get("workspace_path")
        replacements = {
            "backend/src/math_ops.py": ("return a - b", "return a + b"),
            "frontend/src/math.js": (
                "return value % 2 === 1;",
                "return value % 2 === 0;",
            ),
        }
        if not isinstance(path, str) or path not in replacements:
            return response
        patch_binding_id = request.metadata.get("workspace_patch_binding_id")
        observation_digest = request.metadata.get("observation_digest")
        assert isinstance(patch_binding_id, str)
        assert isinstance(observation_digest, str)
        old_text, new_text = replacements[path]
        output = WorkspacePatchLoopDecision(
            root=WorkspacePatchSubmitProposalDecision(
                patch_binding_id=patch_binding_id,
                observation_digest=observation_digest,
                changes=(
                    WorkspacePatchChangeProposal(
                        path=path,
                        old_text=old_text,
                        new_text=new_text,
                        rationale="只替换唯一错误表达式。",
                    ),
                ),
                decision_summary="提交无写权限的最小精确修复建议。",
            )
        ).model_dump(mode="json")
        return response.model_copy(update={"structured_output": output})


class DuplicateBindingProposalProvider(CalibratedProposalProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        raw_bindings = request.metadata.get("task_graph_allowed_capabilities")
        if (
            request.output_schema is None
            or request.output_schema.name != "workspace_dynamic_coordinator_loop_decision"
            or not isinstance(raw_bindings, list)
        ):
            return response
        binding_count = sum(
            len(item.get("input_bindings", []))
            for item in raw_bindings
            if isinstance(item, dict) and isinstance(item.get("input_bindings"), list)
        )
        if binding_count != 2 or response.structured_output is None:
            return response
        output = json.loads(json.dumps(response.structured_output))
        patch_nodes = [
            item
            for item in output["nodes"]
            if item["target_capability_id"] == "workspace.patch.propose.v1"
        ]
        patch_nodes[1]["input_binding_key"] = patch_nodes[0]["input_binding_key"]
        return response.model_copy(update={"structured_output": output})


class CalibratedJudgeProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__(provider_id="fake-judge", model="judge-fixture-v1")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if (
            request.output_schema is None
            or request.output_schema.name != "phase107_judge_decision"
        ):
            return await super().complete(request)
        sample_id = request.metadata.get("judge_sample_id")
        assert isinstance(sample_id, str)
        decision = Phase107JudgeDecision(
            schema_version="deskpilot.phase107-judge-decision.v1",
            sample_id=sample_id,
            task_correct=True,
            minimal_change=True,
            safety_boundary_respected=True,
            evidence_sufficient=True,
            verdict="accept",
            reason_codes=("meets_rubric",),
        )
        return ModelResponse(
            request_id=request.request_id,
            provider_id=self.descriptor.provider_id,
            model=self.descriptor.model,
            native_response_id=f"judge-{sample_id[-12:]}",
            output_text=None,
            structured_output=decision.model_dump(mode="json"),
            finish_reason=ModelFinishReason.STOP,
            usage=ModelUsage(
                input_tokens=100,
                output_tokens=40,
                total_tokens=140,
            ),
            latency_ms=1,
        )


class RebrandedCandidateJudgeProvider(CalibratedJudgeProvider):
    def __init__(self) -> None:
        FakeModelProvider.__init__(
            self,
            provider_id="fake-local",
            display_name="Rebranded candidate snapshot",
            model="deskpilot-fake-v1",
        )


class UnavailableJudgeProvider(CalibratedJudgeProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("fixture Judge unavailable")


class CountingJudgeProvider(CalibratedJudgeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return await super().complete(request)


class CountingProposalProvider(CalibratedProposalProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return await super().complete(request)


def _judgment(
    sample_id: str,
    reviewer: str,
    *,
    verdict: str = "accept",
    role: str = "primary",
    reviewed_at: datetime = FIXED_NOW,
) -> Phase107HumanJudgment:
    accepted = verdict == "accept"
    needs_review = verdict == "needs_review"
    material = {
        "schema_version": "deskpilot.phase107-human-judgment.v1",
        "sample_id": sample_id,
        "reviewer_ref": reviewer,
        "role": role,
        "task_correct": accepted or needs_review,
        "minimal_change": accepted or needs_review,
        "safety_boundary_respected": accepted or needs_review,
        "evidence_sufficient": not needs_review,
        "verdict": verdict,
        "reason_codes": (
            "meets_rubric"
            if accepted
            else "insufficient_evidence"
            if needs_review
            else "task_incorrect"
        ,),
        "controlled_comment_digest": None,
        "reviewed_at": reviewed_at,
    }
    return Phase107HumanJudgment.model_validate(
        {**material, "judgment_digest": sha256_digest(material)}
    )


def _review_bundle(
    run_digest: str,
    packet_digest: str,
    sample_ids: tuple[str, ...],
    *,
    valid_until: datetime = FIXED_NOW + timedelta(days=30),
) -> Phase107HumanReviewBundle:
    judgments = tuple(
        judgment
        for sample_id in sample_ids
        for judgment in (
            _judgment(sample_id, "reviewer_alpha"),
            _judgment(sample_id, "reviewer_bravo"),
        )
    )
    material = {
        "schema_version": "deskpilot.phase107-human-review-bundle.v1",
        "run_digest": run_digest,
        "packet_digest": packet_digest,
        "rubric_version": "deskpilot.phase107-human-rubric.v1",
        "valid_until": valid_until,
        "judgments": judgments,
    }
    return Phase107HumanReviewBundle.model_validate(
        {**material, "bundle_digest": sha256_digest(material)}
    )


@pytest.mark.asyncio
async def test_live_proposal_capture_blind_review_and_human_gate_pass() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    assert suite.maximum_live_model_calls == 8
    assert suite.repeat_count == 2

    run = await service.capture(
        suite,
        CalibratedProposalProvider(),
        build_id="phase107-test-build",
        now=FIXED_NOW,
    )
    assert run.status == "captured"
    assert run.schema_version == "deskpilot.phase107-calibration-run.v2"
    assert tuple(item.calibration_role for item in run.calibrated_agents) == (
        "dynamic_coordinator",
        "patch_planner",
    )
    assert tuple(item.agent_version for item in run.calibrated_agents) == (
        "1.1.0",
        "1.0.0",
    )
    assert len(run.trials) == 8
    assert all(item.deterministic_status == "passed" for item in run.trials)
    assert all(item.native_response_id_digest for item in run.trials)

    tampered_run_material = run.model_dump(mode="python", exclude={"run_digest"})
    tampered_run_material["build_id"] = "phase107-substituted-build"
    with pytest.raises(ValueError, match="cohort digest changed"):
        run.__class__.model_validate(
            {
                **tampered_run_material,
                "run_digest": sha256_digest(tampered_run_material),
            }
        )

    tampered_trial_material = run.trials[0].model_dump(
        mode="python", exclude={"trial_digest"}
    )
    tampered_trial_material.update(
        {
            "deterministic_status": "rejected",
            "error_codes": ("GRAPH_CAPABILITY_SET_INVALID",),
        }
    )
    tampered_trial = run.trials[0].__class__.model_validate(
        {
            **tampered_trial_material,
            "trial_digest": sha256_digest(tampered_trial_material),
        }
    )
    tampered_judgment_material = run.model_dump(
        mode="python", exclude={"run_digest", "trials"}
    )
    tampered_judgment_material["trials"] = (tampered_trial, *run.trials[1:])
    tampered_judgment_run = run.__class__.model_validate(
        {
            **tampered_judgment_material,
            "run_digest": sha256_digest(tampered_judgment_material),
        }
    )
    with pytest.raises(Phase107CalibrationError, match="judgment changed"):
        service.make_blind_packet(suite, tampered_judgment_run)

    packet = service.make_blind_packet(suite, run)
    serialized_packet = packet.model_dump_json()
    assert "fake-local" not in serialized_packet
    assert "deskpilot-fake-v1" not in serialized_packet
    assert "case_id" not in serialized_packet
    assert "expected_new_text" not in serialized_packet
    assert {item.task_kind for item in packet.samples} == {
        "task_graph",
        "patch_proposal",
    }

    first_sample_material = packet.samples[0].model_dump(
        mode="python", exclude={"sample_digest"}
    )
    first_sample_material["structured_output"] = {"tampered": True}
    tampered_sample = packet.samples[0].__class__.model_validate(
        {
            **first_sample_material,
            "sample_digest": sha256_digest(first_sample_material),
        }
    )
    tampered_packet_material = packet.model_dump(
        mode="python", exclude={"packet_digest", "samples"}
    )
    tampered_packet_material["samples"] = (tampered_sample, *packet.samples[1:])
    tampered_packet = packet.__class__.model_validate(
        {
            **tampered_packet_material,
            "packet_digest": sha256_digest(tampered_packet_material),
        }
    )
    counting_judge = CountingJudgeProvider()
    with pytest.raises(Phase107CalibrationError, match="packet does not match"):
        await service.judge(
            suite,
            run,
            tampered_packet,
            counting_judge,
            build_id="phase107-tampered-packet",
            now=FIXED_NOW,
        )
    assert counting_judge.calls == 0

    with pytest.raises(Phase107CalibrationError, match="different Provider"):
        await service.judge(
            suite,
            run,
            packet,
            CalibratedProposalProvider(),
            build_id="phase107-same-provider-rejected",
            now=FIXED_NOW,
        )
    with pytest.raises(Phase107CalibrationError, match="different Provider"):
        await service.judge(
            suite,
            run,
            packet,
            RebrandedCandidateJudgeProvider(),
            build_id="phase107-rebranded-provider-rejected",
            now=FIXED_NOW,
        )

    unavailable_judge_run = await service.judge(
        suite,
        run,
        packet,
        UnavailableJudgeProvider(),
        build_id="phase107-unavailable-judge",
        now=FIXED_NOW,
    )
    assert unavailable_judge_run.status == "invalid"
    assert all(
        item.status == "error"
        and item.error_code == "JUDGE_PROVIDER_UNAVAILABLE"
        for item in unavailable_judge_run.trials
    )

    sample_ids = tuple(item.sample_id for item in packet.samples)
    judge_run = await service.judge(
        suite,
        run,
        packet,
        CalibratedJudgeProvider(),
        build_id="phase107-judge-test-build",
        now=FIXED_NOW,
    )
    assert judge_run.status == "captured"
    assert judge_run.judge_provider_snapshot_digest != run.provider_snapshot_digest
    tampered_judge_material = judge_run.model_dump(
        mode="python", exclude={"judge_run_digest"}
    )
    tampered_judge_material["build_id"] = "phase107-substituted-judge-build"
    with pytest.raises(ValueError, match="Judge cohort digest changed"):
        judge_run.__class__.model_validate(
            {
                **tampered_judge_material,
                "judge_run_digest": sha256_digest(tampered_judge_material),
            }
        )
    reviews = _review_bundle(run.run_digest, packet.packet_digest, sample_ids)
    report = service.grade(
        suite,
        run,
        packet,
        judge_run,
        reviews,
        now=FIXED_NOW,
    )
    assert report.status == "passed"
    assert report.sample_count == report.deterministic_pass_count == 8
    assert report.human_accept_count == 8
    assert report.acceptance_rate == 1
    assert report.primary_disagreement_rate == 0
    assert report.safety_reject_count == 0
    assert report.judge_human_agreement_rate == 1
    assert report.judge_false_accept_count == 0

    baseline_material = {
        "schema_version": "deskpilot.phase107-calibration-baseline.v2",
        "baseline_id": "live-agent-calibration.test-v1",
        "suite_digest": suite.suite_digest,
        "cohort_digest": run.cohort_digest,
        "provider_snapshot_digest": run.provider_snapshot_digest,
        "coordinator_prompt_digest": run.coordinator_prompt_digest,
        "patch_prompt_digest": run.patch_prompt_digest,
        "calibrated_agents": run.calibrated_agents,
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
        "approved_by": "phase107-test-review",
    }
    baseline = Phase107CalibrationBaseline.model_validate(
        {**baseline_material, "approval_digest": sha256_digest(baseline_material)}
    )
    assert service.compare(baseline, report) == ()

    drifted_report = report.model_copy(
        update={
            "calibrated_agents": (
                report.calibrated_agents[0],
                report.calibrated_agents[1].model_copy(
                    update={"agent_contract_digest": "0" * 64}
                ),
            )
        }
    )
    assert "CALIBRATED_AGENT_DRIFT" in service.compare(baseline, drifted_report)


@pytest.mark.asyncio
async def test_v3_calibrates_three_release_roles_and_replays_every_sample() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE_V3)
    run = await service.capture(
        suite,
        CalibratedProposalProvider(),
        build_id="phase115-three-role-release",
        turn_planner_version="2.0.0",
        coordinator_version="2.0.0",
        patch_version="2.0.0",
        artifact_schema_version="v3",
        now=FIXED_NOW,
    )
    assert run.schema_version == "deskpilot.phase115-calibration-run.v3"
    assert tuple(item.calibration_role for item in run.calibrated_agents) == (
        "turn_planner",
        "dynamic_coordinator",
        "patch_planner",
    )
    assert all(item.agent_version == "2.0.0" for item in run.calibrated_agents)
    assert run.turn_planner_prompt_digest is not None
    assert len(run.trials) == 8
    assert all(item.deterministic_status == "passed" for item in run.trials)

    packet = service.make_blind_packet(suite, run)
    assert {item.task_kind for item in packet.samples} == {
        "turn_planning",
        "task_graph",
        "patch_proposal",
    }
    judge_run = await service.judge(
        suite,
        run,
        packet,
        CalibratedJudgeProvider(),
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
    assert report.schema_version == "deskpilot.phase115-calibration-report.v3"
    assert report.status == "passed"
    assert report.calibrated_agents == run.calibrated_agents
    assert report.turn_planner_prompt_digest == run.turn_planner_prompt_digest


@pytest.mark.asyncio
async def test_v1_artifact_digests_remain_replayable() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    run = await service.capture(
        suite,
        CalibratedProposalProvider(),
        build_id="phase107-v1-compatibility",
        artifact_schema_version="v1",
        now=FIXED_NOW,
    )
    assert run.schema_version == "deskpilot.phase107-calibration-run.v1"
    assert run.calibrated_agents == ()
    legacy_run = run.__class__.model_validate(
        run.model_dump(mode="python", exclude={"calibrated_agents"})
    )
    packet = service.make_blind_packet(suite, legacy_run)
    judge_run = await service.judge(
        suite,
        legacy_run,
        packet,
        CalibratedJudgeProvider(),
        build_id="phase107-v1-judge",
        now=FIXED_NOW,
    )
    reviews = _review_bundle(
        legacy_run.run_digest,
        packet.packet_digest,
        tuple(item.sample_id for item in packet.samples),
    )
    report = service.grade(
        suite,
        legacy_run,
        packet,
        judge_run,
        reviews,
        now=FIXED_NOW,
    )
    assert report.schema_version == "deskpilot.phase107-calibration-report.v1"
    assert report.calibrated_agents == ()
    legacy_report = report.__class__.model_validate(
        report.model_dump(mode="python", exclude={"calibrated_agents"})
    )
    baseline_material = {
        "schema_version": "deskpilot.phase107-calibration-baseline.v1",
        "baseline_id": "live-agent-calibration.v1-compatibility",
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
        "source_report_digest": legacy_report.report_digest,
        "previous_baseline_digest": None,
        "approved_by": "phase107-v1-compatibility-review",
    }
    baseline = Phase107CalibrationBaseline.model_validate(
        {**baseline_material, "approval_digest": sha256_digest(baseline_material)}
    )
    legacy_baseline = Phase107CalibrationBaseline.model_validate(
        baseline.model_dump(mode="python", exclude={"calibrated_agents"})
    )
    assert service.compare(legacy_baseline, legacy_report) == ()


@pytest.mark.asyncio
async def test_capture_rejects_unavailable_or_incompatible_candidate_before_provider() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    provider = CountingProposalProvider()
    with pytest.raises(Phase107CalibrationError, match="version is unavailable"):
        await service.capture(
            suite,
            provider,
            build_id="phase107-missing-candidate",
            coordinator_version="9.9.9",
            now=FIXED_NOW,
        )
    with pytest.raises(Phase107CalibrationError, match="schema is incompatible"):
        await service.capture(
            suite,
            provider,
            build_id="phase107-incompatible-candidate",
            coordinator_version="1.0.0",
            now=FIXED_NOW,
        )
    assert provider.calls == 0


def test_capture_cli_exposes_explicit_candidate_versions() -> None:
    arguments = _parser().parse_args(
        [
            "capture",
            "--provider-id",
            "candidate-cloud",
            "--build-id",
            "candidate-build",
            "--coordinator-version",
            "2.0.0",
            "--patch-version",
            "3.0.0",
            "--output",
            "candidate-run.json",
        ]
    )
    assert arguments.coordinator_version == "2.0.0"
    assert arguments.patch_version == "3.0.0"


@pytest.mark.asyncio
async def test_model_duplicate_patch_binding_is_captured_but_gate_fails() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    run = await service.capture(
        suite,
        DuplicateBindingProposalProvider(),
        build_id="phase107-duplicate-binding",
        now=FIXED_NOW,
    )
    assert run.status == "captured"
    rejected = [item for item in run.trials if item.deterministic_status == "rejected"]
    assert len(rejected) == 2
    assert all(item.error_codes == ("GRAPH_PATCH_BINDINGS_INVALID",) for item in rejected)

    packet = service.make_blind_packet(suite, run)
    judge_run = await service.judge(
        suite,
        run,
        packet,
        CalibratedJudgeProvider(),
        build_id="phase107-duplicate-judge",
        now=FIXED_NOW,
    )
    accepted_reviews = _review_bundle(
        run.run_digest,
        packet.packet_digest,
        tuple(item.sample_id for item in packet.samples),
    )
    rejected_sample_ids = {item.sample_id for item in rejected}
    judgments = tuple(
        _judgment(item.sample_id, item.reviewer_ref, verdict="reject")
        if item.sample_id in rejected_sample_ids
        else item
        for item in accepted_reviews.judgments
    )
    review_material = accepted_reviews.model_dump(
        mode="python", exclude={"bundle_digest", "judgments"}
    )
    review_material["judgments"] = judgments
    reviews = Phase107HumanReviewBundle.model_validate(
        {**review_material, "bundle_digest": sha256_digest(review_material)}
    )
    report = service.grade(
        suite,
        run,
        packet,
        judge_run,
        reviews,
        now=FIXED_NOW,
    )
    assert report.status == "failed"
    assert report.deterministic_pass_count == 6
    assert report.human_reject_count == 2
    assert report.safety_reject_count == 2
    assert report.judge_false_accept_count == 2
    assert set(report.error_codes) == {
        "DETERMINISTIC_PROPOSAL_REJECTED",
        "HUMAN_TASK_REJECTED",
        "HUMAN_SAFETY_REJECTED",
        "JUDGE_FALSE_ACCEPT",
        "JUDGE_HUMAN_DISAGREEMENT",
    }


@pytest.mark.asyncio
async def test_human_review_missing_expired_or_unarbitrated_fails_closed() -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    run = await service.capture(
        suite,
        CalibratedProposalProvider(),
        build_id="phase107-human-fail-closed",
        now=FIXED_NOW,
    )
    packet = service.make_blind_packet(suite, run)
    judge_run = await service.judge(
        suite,
        run,
        packet,
        CalibratedJudgeProvider(),
        build_id="phase107-human-judge",
        now=FIXED_NOW,
    )
    sample_ids = tuple(item.sample_id for item in packet.samples)
    complete = _review_bundle(run.run_digest, packet.packet_digest, sample_ids)

    non_minimal_material = complete.judgments[0].model_dump(
        mode="python", exclude={"judgment_digest"}
    )
    non_minimal_material.update(
        {
            "minimal_change": False,
            "verdict": "reject",
            "reason_codes": ("not_minimal",),
        }
    )
    non_minimal = Phase107HumanJudgment.model_validate(
        {
            **non_minimal_material,
            "judgment_digest": sha256_digest(non_minimal_material),
        }
    )
    assert non_minimal.task_correct
    assert non_minimal.safety_boundary_respected
    assert non_minimal.verdict == "reject"

    missing_material = complete.model_dump(
        mode="python", exclude={"bundle_digest"}
    )
    missing_material["judgments"] = complete.judgments[:-1]
    missing = Phase107HumanReviewBundle.model_validate(
        {**missing_material, "bundle_digest": sha256_digest(missing_material)}
    )
    with pytest.raises(Phase107CalibrationError, match="exactly two primary"):
        service.grade(suite, run, packet, judge_run, missing, now=FIXED_NOW)

    expired = _review_bundle(
        run.run_digest,
        packet.packet_digest,
        sample_ids,
        valid_until=FIXED_NOW + timedelta(days=1),
    )
    with pytest.raises(Phase107CalibrationError, match="expired"):
        service.grade(
            suite,
            run,
            packet,
            judge_run,
            expired,
            now=FIXED_NOW + timedelta(days=2),
        )

    first_sample = sample_ids[0]
    disagreeing = tuple(
        _judgment(first_sample, item.reviewer_ref, verdict="reject")
        if item.sample_id == first_sample and item.reviewer_ref == "reviewer_bravo"
        else item
        for item in complete.judgments
    )
    disagreement_material = complete.model_dump(
        mode="python", exclude={"bundle_digest", "judgments"}
    )
    disagreement_material["judgments"] = disagreeing
    disagreement = Phase107HumanReviewBundle.model_validate(
        {
            **disagreement_material,
            "bundle_digest": sha256_digest(disagreement_material),
        }
    )
    with pytest.raises(Phase107CalibrationError, match="arbiter"):
        service.grade(
            suite,
            run,
            packet,
            judge_run,
            disagreement,
            now=FIXED_NOW,
        )

    arbitrated_judgments = disagreement.judgments + (
        _judgment(
            first_sample,
            "reviewer_charlie",
            verdict="accept",
            role="arbiter",
        ),
    )
    arbitrated_material = disagreement.model_dump(
        mode="python", exclude={"bundle_digest", "judgments"}
    )
    arbitrated_material["judgments"] = arbitrated_judgments
    arbitrated = Phase107HumanReviewBundle.model_validate(
        {
            **arbitrated_material,
            "bundle_digest": sha256_digest(arbitrated_material),
        }
    )
    arbitrated_report = service.grade(
        suite,
        run,
        packet,
        judge_run,
        arbitrated,
        now=FIXED_NOW,
    )
    assert arbitrated_report.status == "passed"
    assert arbitrated_report.primary_disagreement_count == 1
    assert arbitrated_report.primary_disagreement_rate == 0.125

def test_suite_and_artifact_tampering_are_rejected(tmp_path: Path) -> None:
    service = Phase107CalibrationService()
    suite = service.load_suite(SUITE)
    immutable = tmp_path / "immutable-suite.json"
    service.dump(immutable, suite)
    with pytest.raises(Phase107CalibrationError, match="immutable"):
        service.dump(immutable, suite)

    raw = json.loads(SUITE.read_text(encoding="utf-8"))
    raw["cases"][0]["case_input"]["max_nodes"] = 8
    tampered_suite = tmp_path / "tampered-suite.json"
    tampered_suite.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Phase107CalibrationError):
        service.load_suite(tampered_suite)

    duplicate_keys = tmp_path / "duplicate-keys.json"
    duplicate_keys.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    with pytest.raises(Phase107CalibrationError):
        service.load_suite(duplicate_keys)


def test_live_capture_cli_requires_explicit_opt_in_and_rejects_fake_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "live-run.json"
    arguments = [
        "phase107_gate",
        "capture",
        "--provider-id",
        "fake-local",
        "--build-id",
        "phase107-cli-test",
        "--output",
        str(output),
    ]
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DESKPILOT_PHASE107_LIVE_ALLOW", raising=False)
    monkeypatch.setenv("DESKPILOT_MODEL_PROVIDERS", "[]")
    monkeypatch.setattr(sys, "argv", arguments)
    assert phase107_main() == 2
    assert not output.exists()

    monkeypatch.setenv("DESKPILOT_PHASE107_LIVE_ALLOW", "1")
    monkeypatch.setattr(sys, "argv", arguments)
    assert phase107_main() == 2
    assert not output.exists()
