import hmac
import json
from pathlib import Path

import pytest

from deskpilot.application.phase75_evaluation import (
    ExternalOracle,
    Phase75EvaluationCompiler,
    Phase75EvaluationError,
    Phase75EvaluationService,
    Phase75GateError,
    Phase75GateService,
    Phase75ScenarioRunner,
)
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.phase75_evaluations import TrialObservation

BASELINE = (
    Path(__file__).parent
    / "baselines"
    / "evaluations"
    / "multi-agent-core-v1.baseline.json"
)


def _observation(material: dict[str, object]) -> TrialObservation:
    candidate = TrialObservation.model_construct(
        **material, observation_digest="0" * 64
    )
    normalized = candidate.model_dump(mode="json", exclude={"observation_digest"})
    return TrialObservation.model_validate(
        {**normalized, "observation_digest": sha256_digest(normalized)}
    )


@pytest.mark.asyncio
async def test_phase75_independent_suite_runs_real_parallel_join_and_passes_gate() -> None:
    service = Phase75EvaluationService()
    plan = service.plan()
    assert plan.suite_id == "deskpilot.multi-agent-core"
    assert len(plan.trials) == 11
    assert plan.total_worst_case_wall_seconds == 135

    parallel = next(
        item for item in plan.trials if item.case.scenario == "runtime.parallel_verified_join"
    )
    observation = await Phase75ScenarioRunner().run(parallel)
    assert observation.agent_contracts_executed == (
        "builtin.computer_observer",
        "builtin.knowledge_researcher",
    )
    assert len(observation.invocation_ids) == 2
    assert len(observation.handoff_ids) == 2
    assert observation.join_unlocked is True
    assert observation.duplicate_invocation_count == 0
    assert observation.artifact_evidence == {
        "same_run_after_restart": True,
        "invocation_count": 2,
        "handoff_count": 2,
        "result_count": 2,
        "validated_output_count": 2,
        "parallel_limit": 3,
    }

    report = await service.run()
    assert report.status == "passed"
    assert report.trial_count == report.passed_count == 11
    assert report.false_success_count == 0
    assert report.unauthorized_effect_count == 0
    assert report.skipped_case_ids == report.quarantined_case_ids == ()
    assert report.confusion_matrix.model_dump() == {
        "true_accept": 1,
        "true_reject": 2,
        "false_accept": 0,
        "false_reject": 0,
        "precision": 1.0,
        "recall": 1.0,
    }
    gate = Phase75GateService()
    assert gate.compare(gate.load_baseline(BASELINE), report) == ()
    regressed = report.model_copy(
        update={"status": "failed", "false_success_count": 1}
    )
    assert set(gate.compare(gate.load_baseline(BASELINE), regressed)) == {
        "REQUIRED_TRIAL_FAILED",
        "FALSE_SUCCESS",
    }


def test_external_oracle_catches_false_success_and_false_accept() -> None:
    plan = Phase75EvaluationService().plan()
    success_case = next(
        item.case for item in plan.trials if item.case.expected_task_outcome == "succeeded"
    )
    material = {
        "trial_id": "evt-mutant",
        "sut_outcome": "succeeded",
        "acceptance_results": {
            item: False for item in success_case.required_acceptance
        },
        "evidence_valid": False,
    }
    false_success = _observation(material)
    grade = ExternalOracle.grade(success_case, false_success)
    assert grade.false_success is True
    assert grade.passed is False
    assert {"FALSE_SUCCESS", "REQUIRED_ACCEPTANCE_UNMET"}.issubset(grade.error_codes)

    bad_case = next(
        item.case
        for item in plan.trials
        if item.case.scenario == "verification.factual_mutant"
    )
    material = {
        "trial_id": "evt-false-accept",
        "sut_outcome": "partial",
        "acceptance_results": {item: False for item in bad_case.required_acceptance},
        "evidence_valid": False,
        "limitation_codes": ("INSUFFICIENT_VALID_EVIDENCE",),
        "verifier_accepted": True,
        "ground_truth_good": False,
    }
    false_accept = _observation(material)
    grade = ExternalOracle.grade(bad_case, false_accept)
    assert grade.confusion == "false_accept"
    assert "VERIFIER_MUTANT_MISCLASSIFIED" in grade.error_codes


def test_suite_loader_baseline_and_attestation_fail_closed(tmp_path: Path) -> None:
    aliased = tmp_path / "aliased.yaml"
    aliased.write_text(
        "schema_version: deskpilot.multi-agent-suite.v1\n"
        "suite_id: deskpilot.multi-agent-core\nversion: 1\n"
        "harness_version: deskpilot.phase75-harness.v1\n"
        "gate_policy_id: deskpilot.phase75-zero-tolerance.v1\n"
        "cases: &cases []\ncopy: *cases\n",
        encoding="utf-8",
    )
    with pytest.raises(Phase75EvaluationError, match="aliases"):
        Phase75EvaluationCompiler().load(aliased)

    gate = Phase75GateService()
    gate.load_baseline(BASELINE)
    tampered = tmp_path / "tampered.json"
    raw = json.loads(BASELINE.read_text(encoding="utf-8"))
    raw["approved_by"] = "attacker"
    tampered.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Phase75GateError):
        gate.load_baseline(tampered)

@pytest.mark.asyncio
async def test_signed_attestation_binds_exact_release_proof() -> None:
    report = await Phase75EvaluationService().run()
    gate = Phase75GateService()
    baseline = gate.load_baseline(BASELINE)
    key = b"phase75-test-signing-key-at-least-32-bytes"
    attestation = gate.attest(
        baseline,
        report,
        build_id="7045a64+stage75",
        key_id="test-key-v1",
        signing_key=key,
    )
    signed = attestation.model_dump(
        mode="json", exclude={"attestation_digest", "signature"}
    )
    assert attestation.attestation_digest == sha256_digest(signed)
    assert hmac.compare_digest(
        attestation.signature,
        hmac.new(key, canonical_json_bytes(signed), "sha256").hexdigest(),
    )
    gate.verify_attestation(attestation, signing_key=key)
    forged = attestation.model_copy(update={"signature": "0" * 64})
    with pytest.raises(Phase75GateError, match="signature"):
        gate.verify_attestation(forged, signing_key=key)
    with pytest.raises(Phase75GateError, match="32 bytes"):
        gate.attest(
            baseline,
            report,
            build_id="build",
            key_id="short",
            signing_key=b"short",
        )
