import asyncio
import hmac
import json
import os
import tempfile
from pathlib import Path

import pytest

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
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
from deskpilot.domain.agent_runtime import AgentOutputResult, AgentResult
from deskpilot.domain.phase75_evaluations import TrialObservation
from deskpilot.infrastructure.database import Database

BASELINE = (
    Path(__file__).parent
    / "baselines"
    / "evaluations"
    / "multi-agent-core-v16.baseline.json"
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


@pytest.mark.asyncio
async def test_claimed_worker_can_start_after_a_sibling_enters_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = AgentExecutionRuntime.start_invocation
    original_submit = AgentExecutionRuntime.submit_result
    first_submitted = asyncio.Event()
    deferred_start: tuple[str, str, int] | None = None
    start_count = 0

    async def defer_second_start(
        runtime: AgentExecutionRuntime,
        invocation_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> object:
        nonlocal deferred_start, start_count
        start_count += 1
        if start_count == 1:
            return await original_start(runtime, invocation_id, owner_id, fencing_token)
        deferred_start = (invocation_id, owner_id, fencing_token)
        return object()

    async def ordered_submit(
        runtime: AgentExecutionRuntime,
        result: AgentResult | AgentOutputResult,
        *,
        owner_id: str,
        fencing_token: int,
    ) -> object:
        invocation_id = result.invocation_id
        if deferred_start is not None and invocation_id == deferred_start[0]:
            await asyncio.wait_for(first_submitted.wait(), timeout=5)
            await original_start(runtime, *deferred_start)
        submitted = await original_submit(
            runtime,
            result,
            owner_id=owner_id,
            fencing_token=fencing_token,
        )
        if deferred_start is None or invocation_id != deferred_start[0]:
            first_submitted.set()
        return submitted

    monkeypatch.setattr(AgentExecutionRuntime, "start_invocation", defer_second_start)
    monkeypatch.setattr(AgentExecutionRuntime, "submit_result", ordered_submit)
    parallel = next(
        item
        for item in Phase75EvaluationService().plan().trials
        if item.case.scenario == "runtime.parallel_verified_join"
    )

    observation = await Phase75ScenarioRunner().run(parallel)

    assert observation.sut_outcome == "succeeded"
    assert start_count == 2
    assert first_submitted.is_set()
    assert observation.artifact_evidence["result_count"] == 2
    assert observation.join_unlocked is True


@pytest.mark.asyncio
async def test_phase75_trial_database_is_disposed_when_runtime_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispose_count = 0
    submit_count = 0
    delayed_submit_settled = asyncio.Event()
    original_dispose = Database.dispose

    async def tracked_dispose(database: Database) -> None:
        nonlocal dispose_count
        dispose_count += 1
        await original_dispose(database)

    async def injected_failure(*args: object, **kwargs: object) -> object:
        nonlocal submit_count
        submit_count += 1
        if submit_count == 1:
            raise RuntimeError("injected Phase75 runtime failure")
        await asyncio.sleep(0.05)
        delayed_submit_settled.set()
        return object()

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(Database, "dispose", tracked_dispose)
    monkeypatch.setattr(AgentExecutionRuntime, "submit_result", injected_failure)
    parallel = next(
        item
        for item in Phase75EvaluationService().plan().trials
        if item.case.scenario == "runtime.parallel_verified_join"
    )

    with pytest.raises(RuntimeError, match="injected Phase75 runtime failure"):
        await Phase75ScenarioRunner().run(parallel)

    assert delayed_submit_settled.is_set()
    assert dispose_count == 1
    assert await asyncio.to_thread(os.listdir, tmp_path) == []


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
