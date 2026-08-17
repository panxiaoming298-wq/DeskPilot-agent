import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deskpilot.application.evaluation_baseline import (
    EvaluationBaselineError,
    EvaluationBaselineService,
)
from deskpilot.domain.evaluations import EvaluationReportRead

BASELINE = (
    Path(__file__).parent / "baselines" / "evaluations" / "golden-resilience-v2.baseline.json"
)


def test_versioned_baseline_compare_passes_and_detects_each_regression(
    client: TestClient,
) -> None:
    run = client.post("/api/v1/evaluations/golden:run")
    assert run.status_code == 200
    report = EvaluationReportRead.model_validate(
        client.get("/api/v1/evaluations/reports/latest", params={"limit": 1}).json()
    )
    service = EvaluationBaselineService()
    baseline = service.load(BASELINE)
    assert service.compare(baseline, report).passed is True

    regressed = report.model_copy(
        update={
            "run_success_rate": 0.5,
            "run_duration_p95_ms": baseline.maximum_run_duration_p95_ms + 1,
            "case_duration_p95_ms": baseline.maximum_case_duration_p95_ms + 1,
            "trend": (report.trend[0].model_copy(update={"safety_rate": 0.5}),),
        }
    )
    result = service.compare(baseline, regressed)
    assert result.passed is False
    assert {violation.code for violation in result.violations} == {
        "SUCCESS_RATE_REGRESSION",
        "SAFETY_RATE_REGRESSION",
        "RUN_P95_LATENCY_REGRESSION",
        "CASE_P95_LATENCY_REGRESSION",
    }


def test_baseline_is_strict_and_immutable(tmp_path: Path) -> None:
    service = EvaluationBaselineService()
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps(
            {
                **json.loads(BASELINE.read_text(encoding="utf-8")),
                "unreviewed_threshold": 0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationBaselineError):
        service.load(invalid)

    report = EvaluationReportRead.model_validate(
        {
            "schema_version": "deskpilot.evaluation-report.v1",
            "suite_id": "deskpilot.resilience-safety",
            "suite_version": 2,
            "suite_digest": "0" * 64,
            "as_of": None,
            "run_count": 0,
            "passed_run_count": 0,
            "failed_run_count": 0,
            "run_success_rate": 1,
            "run_duration_p50_ms": None,
            "run_duration_p95_ms": None,
            "case_duration_p50_ms": None,
            "case_duration_p95_ms": None,
            "failure_counts": {},
            "trend": [],
            "report_digest": "1" * 64,
        }
    )
    existing = tmp_path / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    with pytest.raises(EvaluationBaselineError, match="immutable"):
        service.record(
            existing,
            report,
            baseline_id="golden-resilience-v3.windows-v1",
            maximum_run_duration_p95_ms=5_000,
            maximum_case_duration_p95_ms=1_000,
        )
