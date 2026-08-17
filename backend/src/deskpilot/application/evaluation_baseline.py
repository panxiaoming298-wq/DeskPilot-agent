"""Explicit versioned baseline record/compare policy for the golden suite."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deskpilot.domain.evaluations import EvaluationReportRead


class EvaluationBaselineError(RuntimeError):
    code = "EVALUATION_BASELINE_REJECTED"


class EvaluationBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.evaluation-baseline.v1"]
    baseline_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    suite_id: str
    suite_version: int = Field(ge=1)
    suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    minimum_success_rate: float = Field(ge=0, le=1)
    minimum_safety_rate: float = Field(ge=0, le=1)
    maximum_run_duration_p95_ms: int = Field(ge=1)
    maximum_case_duration_p95_ms: int = Field(ge=1)
    source_report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationGateViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    expected: str
    actual: str


class EvaluationGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.evaluation-gate.v1"] = "deskpilot.evaluation-gate.v1"
    baseline_id: str
    passed: bool
    report_digest: str
    violations: tuple[EvaluationGateViolation, ...]


class EvaluationBaselineService:
    def load(self, path: Path) -> EvaluationBaseline:
        try:
            payload = path.read_bytes()
            if not payload or len(payload) > 65_536:
                raise EvaluationBaselineError("Evaluation baseline is empty or too large")
            return EvaluationBaseline.model_validate_json(payload)
        except (OSError, ValidationError, ValueError) as error:
            if isinstance(error, EvaluationBaselineError):
                raise
            raise EvaluationBaselineError("Evaluation baseline failed strict validation") from error

    def compare(
        self,
        baseline: EvaluationBaseline,
        report: EvaluationReportRead,
    ) -> EvaluationGateResult:
        violations: list[EvaluationGateViolation] = []
        self._equal(violations, "SUITE_ID_DRIFT", baseline.suite_id, report.suite_id)
        self._equal(
            violations,
            "SUITE_VERSION_DRIFT",
            str(baseline.suite_version),
            str(report.suite_version),
        )
        self._equal(
            violations,
            "SUITE_DIGEST_DRIFT",
            baseline.suite_digest,
            report.suite_digest,
        )
        self._minimum(
            violations,
            "SUCCESS_RATE_REGRESSION",
            baseline.minimum_success_rate,
            report.run_success_rate,
        )
        latest_safety_rate = report.trend[-1].safety_rate if report.trend else 1.0
        self._minimum(
            violations,
            "SAFETY_RATE_REGRESSION",
            baseline.minimum_safety_rate,
            latest_safety_rate,
        )
        self._maximum(
            violations,
            "RUN_P95_LATENCY_REGRESSION",
            baseline.maximum_run_duration_p95_ms,
            report.run_duration_p95_ms,
        )
        self._maximum(
            violations,
            "CASE_P95_LATENCY_REGRESSION",
            baseline.maximum_case_duration_p95_ms,
            report.case_duration_p95_ms,
        )
        return EvaluationGateResult(
            baseline_id=baseline.baseline_id,
            passed=not violations,
            report_digest=report.report_digest,
            violations=tuple(violations),
        )

    def record(
        self,
        path: Path,
        report: EvaluationReportRead,
        *,
        baseline_id: str,
        maximum_run_duration_p95_ms: int,
        maximum_case_duration_p95_ms: int,
    ) -> EvaluationBaseline:
        if path.exists():
            raise EvaluationBaselineError(
                "Baseline is immutable; choose a new versioned output path"
            )
        if (
            report.suite_id is None
            or report.suite_version is None
            or report.suite_digest is None
            or report.run_count < 1
            or not report.trend
            or report.run_duration_p95_ms is None
            or report.case_duration_p95_ms is None
        ):
            raise EvaluationBaselineError("A completed evaluation report is required")
        if report.run_success_rate < 1.0 or report.trend[-1].safety_rate < 1.0:
            raise EvaluationBaselineError("A failing evaluation report cannot become a baseline")
        if maximum_run_duration_p95_ms < 1 or maximum_case_duration_p95_ms < 1:
            raise EvaluationBaselineError("Latency limits must be positive")
        if (
            maximum_run_duration_p95_ms < report.run_duration_p95_ms
            or maximum_case_duration_p95_ms < report.case_duration_p95_ms
        ):
            raise EvaluationBaselineError("Latency limits cannot be below the recorded report")
        baseline = EvaluationBaseline(
            schema_version="deskpilot.evaluation-baseline.v1",
            baseline_id=baseline_id,
            suite_id=report.suite_id,
            suite_version=report.suite_version,
            suite_digest=report.suite_digest,
            minimum_success_rate=1.0,
            minimum_safety_rate=1.0,
            maximum_run_duration_p95_ms=maximum_run_duration_p95_ms,
            maximum_case_duration_p95_ms=maximum_case_duration_p95_ms,
            source_report_digest=report.report_digest,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(baseline.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return baseline

    @staticmethod
    def _equal(
        target: list[EvaluationGateViolation], code: str, expected: object, actual: object
    ) -> None:
        if expected != actual:
            target.append(
                EvaluationGateViolation(code=code, expected=str(expected), actual=str(actual))
            )

    @staticmethod
    def _minimum(
        target: list[EvaluationGateViolation], code: str, expected: float, actual: float
    ) -> None:
        if actual < expected:
            target.append(
                EvaluationGateViolation(code=code, expected=f">={expected}", actual=str(actual))
            )

    @staticmethod
    def _maximum(
        target: list[EvaluationGateViolation], code: str, expected: int, actual: int | None
    ) -> None:
        if actual is None or actual > expected:
            target.append(
                EvaluationGateViolation(code=code, expected=f"<={expected}", actual=str(actual))
            )
