"""Versioned golden-suite, trace and replay contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvaluationScenario = Literal[
    "mcp.text_metrics",
    "mcp.invalid_input_rejected",
    "security.mcp_bundle_tamper",
    "knowledge.source_stale",
    "fault.model_rate_limit",
    "fault.runner_crash_recovery",
    "fault.websocket_disconnect",
    "fault.mcp_protocol_anomaly",
]


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    scenario: EvaluationScenario
    safety_case: bool = False
    input: dict[str, Any]
    expect: dict[str, Any]


class GoldenSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.golden-suite.v1"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    version: int = Field(ge=1)
    cases: tuple[GoldenCase, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self) -> "GoldenSuite":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Golden case IDs must be unique")
        return self


class EvaluationTraceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sequence: int = Field(ge=1)
    case_id: str
    scenario: str
    status: Literal["passed", "failed"]
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_code: str | None
    duration_ms: int = Field(ge=0)
    previous_event_digest: str | None
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    suite_id: str
    suite_version: int
    suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["passed", "failed"]
    replay_of_run_id: str | None
    replay_match: bool | None
    case_count: int = Field(ge=1)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    safety_case_count: int = Field(ge=0)
    safety_passed_count: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    safety_rate: float = Field(ge=0, le=1)
    duration_ms: int = Field(ge=0)
    result_manifest: dict[str, Any]
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    traces: tuple[EvaluationTraceRead, ...]
    started_at: datetime
    completed_at: datetime


class EvaluationRunPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runs: tuple[EvaluationRunRead, ...]


class EvaluationTrendPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    status: Literal["passed", "failed"]
    success_rate: float = Field(ge=0, le=1)
    safety_rate: float = Field(ge=0, le=1)
    duration_ms: int = Field(ge=0)
    replay_of_run_id: str | None
    started_at: datetime


class EvaluationReportRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.evaluation-report.v1"]
    suite_id: str | None
    suite_version: int | None = Field(default=None, ge=1)
    suite_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    as_of: datetime | None
    run_count: int = Field(ge=0)
    passed_run_count: int = Field(ge=0)
    failed_run_count: int = Field(ge=0)
    run_success_rate: float = Field(ge=0, le=1)
    run_duration_p50_ms: int | None = Field(default=None, ge=0)
    run_duration_p95_ms: int | None = Field(default=None, ge=0)
    case_duration_p50_ms: int | None = Field(default=None, ge=0)
    case_duration_p95_ms: int | None = Field(default=None, ge=0)
    failure_counts: dict[str, int]
    trend: tuple[EvaluationTrendPoint, ...]
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
