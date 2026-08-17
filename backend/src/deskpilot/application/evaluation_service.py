"""Deterministic built-in golden-suite execution, trace recording and replay."""

import asyncio
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.application.evaluation_scenarios import EvaluationScenarioRunner
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.evaluations import (
    EvaluationReportRead,
    EvaluationRunPage,
    EvaluationRunRead,
    EvaluationTraceRead,
    EvaluationTrendPoint,
    GoldenCase,
    GoldenSuite,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import EvaluationRunRecord, EvaluationTraceRecord, utc_now
from deskpilot.observability import TelemetryFacade

MAX_SUITE_BYTES = 65_536


class EvaluationError(RuntimeError):
    code = "EVALUATION_REJECTED"


class EvaluationRunNotFoundError(EvaluationError):
    code = "EVALUATION_RUN_NOT_FOUND"


class EvaluationProofRejectedError(EvaluationError):
    code = "EVALUATION_PROOF_REJECTED"


@dataclass(frozen=True, slots=True)
class _CaseExecution:
    case: GoldenCase
    status: str
    input_digest: str
    output_digest: str
    error_code: str | None
    duration_ms: int


class EvaluationService:
    def __init__(
        self,
        database: Database,
        suite_path: Path | None = None,
        *,
        telemetry: TelemetryFacade | None = None,
    ) -> None:
        self._database = database
        self._suite_path = suite_path or (
            Path(__file__).parents[1] / "evaluations" / "golden_resilience_v2.yaml"
        )
        self._scenarios = EvaluationScenarioRunner()
        self._lock = asyncio.Lock()
        self._telemetry = telemetry

    async def run_builtin(self) -> EvaluationRunRead:
        async with self._lock:
            suite, suite_digest = await asyncio.to_thread(self._load_suite)
            return await self._execute_and_persist(suite, suite_digest, None, None)

    async def replay(self, run_id: str) -> EvaluationRunRead:
        async with self._lock:
            original = await self.get_run(run_id)
            suite, suite_digest = await asyncio.to_thread(self._load_suite)
            if original.suite_digest != suite_digest:
                raise EvaluationProofRejectedError(
                    "Golden suite changed; the recorded run cannot be replayed as the same version"
                )
            semantic = self._semantic_cases(original.result_manifest)
            return await self._execute_and_persist(suite, suite_digest, run_id, semantic)

    async def get_run(self, run_id: str) -> EvaluationRunRead:
        async with self._database.session() as session:
            record = await session.get(EvaluationRunRecord, run_id)
            if record is None:
                raise EvaluationRunNotFoundError("Evaluation run does not exist")
            traces = (
                (
                    await session.execute(
                        select(EvaluationTraceRecord)
                        .where(EvaluationTraceRecord.run_id == run_id)
                        .order_by(EvaluationTraceRecord.sequence)
                    )
                )
                .scalars()
                .all()
            )
        return self._run_read(record, traces)

    async def list_runs(self, limit: int = 20) -> EvaluationRunPage:
        async with self._database.session() as session:
            records = (
                (
                    await session.execute(
                        select(EvaluationRunRecord)
                        .order_by(EvaluationRunRecord.started_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            result: list[EvaluationRunRead] = []
            for record in records:
                traces = (
                    (
                        await session.execute(
                            select(EvaluationTraceRecord)
                            .where(EvaluationTraceRecord.run_id == record.run_id)
                            .order_by(EvaluationTraceRecord.sequence)
                        )
                    )
                    .scalars()
                    .all()
                )
                result.append(self._run_read(record, traces))
        return EvaluationRunPage(runs=tuple(result))

    async def report(self, limit: int = 50) -> EvaluationReportRead:
        page = await self.list_runs(limit)
        runs = list(page.runs)
        latest = runs[0] if runs else None
        passed_run_count = sum(run.status == "passed" for run in runs)
        run_durations = [run.duration_ms for run in runs]
        case_durations = [trace.duration_ms for run in runs for trace in run.traces]
        failure_counts = Counter(
            trace.error_code or "UNCLASSIFIED_FAILURE"
            for run in runs
            for trace in run.traces
            if trace.status == "failed"
        )
        trend = tuple(
            EvaluationTrendPoint(
                run_id=run.run_id,
                status=run.status,
                success_rate=run.success_rate,
                safety_rate=run.safety_rate,
                duration_ms=run.duration_ms,
                replay_of_run_id=run.replay_of_run_id,
                started_at=run.started_at,
            )
            for run in reversed(runs)
        )
        material = {
            "schema_version": "deskpilot.evaluation-report.v1",
            "suite_id": latest.suite_id if latest else None,
            "suite_version": latest.suite_version if latest else None,
            "suite_digest": latest.suite_digest if latest else None,
            "as_of": latest.completed_at.isoformat() if latest else None,
            "run_count": len(runs),
            "passed_run_count": passed_run_count,
            "failed_run_count": len(runs) - passed_run_count,
            "run_success_rate": passed_run_count / len(runs) if runs else 1.0,
            "run_duration_p50_ms": self._percentile(run_durations, 0.50),
            "run_duration_p95_ms": self._percentile(run_durations, 0.95),
            "case_duration_p50_ms": self._percentile(case_durations, 0.50),
            "case_duration_p95_ms": self._percentile(case_durations, 0.95),
            "failure_counts": dict(sorted(failure_counts.items())),
            "trend": [point.model_dump(mode="json") for point in trend],
        }
        return EvaluationReportRead.model_validate(
            {**material, "report_digest": sha256_digest(material)}
        )

    def _load_suite(self) -> tuple[GoldenSuite, str]:
        payload = self._suite_path.read_bytes()
        if not payload or len(payload) > MAX_SUITE_BYTES:
            raise EvaluationError("Golden suite is empty or exceeds its size limit")
        try:
            text = payload.decode("utf-8")
            if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
                raise EvaluationError("Golden suite YAML aliases are not allowed")
            raw = yaml.safe_load(text)
            suite = GoldenSuite.model_validate(raw)
        except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
            raise EvaluationError("Golden suite failed strict validation") from error
        return suite, sha256_digest(suite.model_dump(mode="json"))

    async def _execute_and_persist(
        self,
        suite: GoldenSuite,
        suite_digest: str,
        replay_of: str | None,
        expected_semantic: list[dict[str, Any]] | None,
    ) -> EvaluationRunRead:
        if self._telemetry is None:
            return await self._execute_and_persist_inner(
                suite, suite_digest, replay_of, expected_semantic
            )
        with self._telemetry.operation(
            "deskpilot.evaluation.run",
            "evaluation",
            {
                "deskpilot.subject.type": "evaluation_run",
                "deskpilot.evaluation.suite_version": suite.version,
            },
        ) as operation:
            result = await self._execute_and_persist_inner(
                suite, suite_digest, replay_of, expected_semantic
            )
            operation.set_attribute("deskpilot.subject.id", result.run_id)
            operation.set_attribute("deskpilot.evaluation.run_id", result.run_id)
            operation.set_outcome("succeeded" if result.status == "passed" else "failed")
            return result

    async def _execute_and_persist_inner(
        self,
        suite: GoldenSuite,
        suite_digest: str,
        replay_of: str | None,
        expected_semantic: list[dict[str, Any]] | None,
    ) -> EvaluationRunRead:
        started_at = utc_now()
        started_clock = time.perf_counter_ns()
        executions = [await self._execute_case(case) for case in suite.cases]
        duration_ms = max(0, (time.perf_counter_ns() - started_clock) // 1_000_000)
        semantic_cases = [
            {
                "case_id": item.case.case_id,
                "scenario": item.case.scenario,
                "status": item.status,
                "output_digest": item.output_digest,
                "error_code": item.error_code,
                "safety_case": item.case.safety_case,
            }
            for item in executions
        ]
        replay_match = None if expected_semantic is None else semantic_cases == expected_semantic
        passed_count = sum(item.status == "passed" for item in executions)
        safety_cases = [item for item in executions if item.case.safety_case]
        safety_passed = sum(item.status == "passed" for item in safety_cases)
        status = (
            "passed" if passed_count == len(executions) and replay_match is not False else "failed"
        )
        result_manifest = {
            "schema_version": "deskpilot.evaluation-result.v1",
            "suite_id": suite.suite_id,
            "suite_version": suite.version,
            "suite_digest": suite_digest,
            "status": status,
            "replay_of_run_id": replay_of,
            "replay_match": replay_match,
            "cases": semantic_cases,
        }
        manifest_digest = sha256_digest(result_manifest)
        run_id = f"evr_{uuid4().hex}"
        completed_at = utc_now()
        run_record = EvaluationRunRecord(
            run_id=run_id,
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_digest=suite_digest,
            status=status,
            replay_of_run_id=replay_of,
            replay_match=replay_match,
            case_count=len(executions),
            passed_count=passed_count,
            failed_count=len(executions) - passed_count,
            safety_case_count=len(safety_cases),
            safety_passed_count=safety_passed,
            duration_ms=duration_ms,
            result_manifest=result_manifest,
            manifest_digest=manifest_digest,
            started_at=started_at,
            completed_at=completed_at,
        )
        trace_records = self._trace_records(run_id, executions)
        async with self._database.session() as session:
            async with session.begin():
                session.add(run_record)
                session.add_all(trace_records)
        return self._run_read(run_record, trace_records)

    async def _execute_case(self, case: GoldenCase) -> _CaseExecution:
        if self._telemetry is None:
            return await self._execute_case_inner(case)
        with self._telemetry.operation(
            "deskpilot.evaluation.case",
            "evaluation",
            {
                "deskpilot.subject.type": "evaluation_case",
                "deskpilot.evaluation.case_id": case.case_id,
                "deskpilot.evaluation.scenario": case.scenario,
            },
        ) as operation:
            result = await self._execute_case_inner(case)
            operation.set_outcome("succeeded" if result.status == "passed" else "failed")
            return result

    async def _execute_case_inner(self, case: GoldenCase) -> _CaseExecution:
        started = time.perf_counter_ns()
        input_digest = sha256_digest({"scenario": case.scenario, "input": case.input})
        error_code: str | None = None
        try:
            output = await self._scenarios.execute(case)
            passed = all(output.get(key) == value for key, value in case.expect.items())
            if not passed:
                error_code = "EXPECTATION_MISMATCH"
        except Exception as error:
            output = {"failure": True}
            error_code = str(getattr(error, "code", "EVALUATION_SCENARIO_FAILED"))
            passed = False
        return _CaseExecution(
            case=case,
            status="passed" if passed else "failed",
            input_digest=input_digest,
            output_digest=sha256_digest({"output": output}),
            error_code=error_code,
            duration_ms=max(0, (time.perf_counter_ns() - started) // 1_000_000),
        )

    @staticmethod
    def _trace_records(
        run_id: str, executions: list[_CaseExecution]
    ) -> list[EvaluationTraceRecord]:
        records: list[EvaluationTraceRecord] = []
        previous: str | None = None
        for sequence, item in enumerate(executions, start=1):
            material = {
                "run_id": run_id,
                "sequence": sequence,
                "case_id": item.case.case_id,
                "scenario": item.case.scenario,
                "status": item.status,
                "input_digest": item.input_digest,
                "output_digest": item.output_digest,
                "error_code": item.error_code,
                "duration_ms": item.duration_ms,
                "previous_event_digest": previous,
            }
            event_digest = sha256_digest(material)
            records.append(
                EvaluationTraceRecord(
                    **material,
                    event_digest=event_digest,
                )
            )
            previous = event_digest
        return records

    def _run_read(
        self, record: EvaluationRunRecord, traces: Sequence[EvaluationTraceRecord]
    ) -> EvaluationRunRead:
        if sha256_digest(record.result_manifest) != record.manifest_digest:
            raise EvaluationProofRejectedError("Evaluation result manifest is invalid")
        manifest_cases = self._semantic_cases(record.result_manifest)
        trace_cases = [
            {
                "case_id": trace.case_id,
                "scenario": trace.scenario,
                "status": trace.status,
                "output_digest": trace.output_digest,
                "error_code": trace.error_code,
            }
            for trace in traces
        ]
        if (
            len(manifest_cases) != len(trace_cases)
            or [
                {key: item.get(key) for key in trace_case}
                for item, trace_case in zip(manifest_cases, trace_cases, strict=False)
            ]
            != trace_cases
        ):
            raise EvaluationProofRejectedError("Evaluation trace does not match its manifest")
        passed_count = sum(item.get("status") == "passed" for item in manifest_cases)
        safety_cases = [item for item in manifest_cases if item.get("safety_case") is True]
        manifest_identity = (
            record.result_manifest.get("suite_id"),
            record.result_manifest.get("suite_version"),
            record.result_manifest.get("suite_digest"),
            record.result_manifest.get("status"),
            record.result_manifest.get("replay_of_run_id"),
            record.result_manifest.get("replay_match"),
        )
        record_identity = (
            record.suite_id,
            record.suite_version,
            record.suite_digest,
            record.status,
            record.replay_of_run_id,
            record.replay_match,
        )
        if (
            manifest_identity != record_identity
            or record.case_count != len(manifest_cases)
            or record.passed_count != passed_count
            or record.failed_count != len(manifest_cases) - passed_count
            or record.safety_case_count != len(safety_cases)
            or record.safety_passed_count
            != sum(item.get("status") == "passed" for item in safety_cases)
        ):
            raise EvaluationProofRejectedError("Evaluation run projection is invalid")
        previous: str | None = None
        trace_reads: list[EvaluationTraceRead] = []
        for expected_sequence, trace in enumerate(traces, start=1):
            material = {
                "run_id": record.run_id,
                "sequence": trace.sequence,
                "case_id": trace.case_id,
                "scenario": trace.scenario,
                "status": trace.status,
                "input_digest": trace.input_digest,
                "output_digest": trace.output_digest,
                "error_code": trace.error_code,
                "duration_ms": trace.duration_ms,
                "previous_event_digest": trace.previous_event_digest,
            }
            if (
                trace.sequence != expected_sequence
                or trace.previous_event_digest != previous
                or trace.event_digest != sha256_digest(material)
            ):
                raise EvaluationProofRejectedError("Evaluation trace chain is invalid")
            previous = trace.event_digest
            trace_reads.append(
                EvaluationTraceRead(
                    sequence=trace.sequence,
                    case_id=trace.case_id,
                    scenario=trace.scenario,
                    status=cast(Any, trace.status),
                    input_digest=trace.input_digest,
                    output_digest=trace.output_digest,
                    error_code=trace.error_code,
                    duration_ms=trace.duration_ms,
                    previous_event_digest=trace.previous_event_digest,
                    event_digest=trace.event_digest,
                )
            )
        if len(traces) != record.case_count:
            raise EvaluationProofRejectedError("Evaluation trace count is invalid")
        started = self._utc(record.started_at)
        completed = self._utc(record.completed_at)
        return EvaluationRunRead(
            run_id=record.run_id,
            suite_id=record.suite_id,
            suite_version=record.suite_version,
            suite_digest=record.suite_digest,
            status=cast(Any, record.status),
            replay_of_run_id=record.replay_of_run_id,
            replay_match=record.replay_match,
            case_count=record.case_count,
            passed_count=record.passed_count,
            failed_count=record.failed_count,
            safety_case_count=record.safety_case_count,
            safety_passed_count=record.safety_passed_count,
            success_rate=record.passed_count / record.case_count,
            safety_rate=(
                record.safety_passed_count / record.safety_case_count
                if record.safety_case_count
                else 1.0
            ),
            duration_ms=record.duration_ms,
            result_manifest=record.result_manifest,
            manifest_digest=record.manifest_digest,
            traces=tuple(trace_reads),
            started_at=started,
            completed_at=completed,
        )

    @staticmethod
    def _semantic_cases(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        cases = manifest.get("cases")
        if not isinstance(cases, list) or any(not isinstance(item, dict) for item in cases):
            raise EvaluationProofRejectedError("Evaluation manifest cases are invalid")
        return cast(list[dict[str, Any]], cases)

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[max(0, ceil(percentile * len(ordered)) - 1)]

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
