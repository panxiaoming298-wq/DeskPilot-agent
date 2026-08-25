"""CLI for immutable baseline recording and CI-safe comparison."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from deskpilot.application.evaluation_baseline import (
    EvaluationBaselineError,
    EvaluationBaselineService,
)
from deskpilot.application.evaluation_service import EvaluationService
from deskpilot.domain.evaluations import EvaluationReportRead
from deskpilot.infrastructure.database import Database

DEFAULT_BASELINE = Path(
    "tests/baselines/evaluations/golden-resilience-v2-windows-v2.baseline.json"
)


async def _run_report() -> EvaluationReportRead:
    with tempfile.TemporaryDirectory(prefix="deskpilot-evaluation-gate-") as temporary:
        database = Database(f"sqlite+aiosqlite:///{(Path(temporary) / 'gate.db').as_posix()}")
        try:
            await database.migrate()
            service = EvaluationService(database)
            await service.run_builtin()
            return await service.report(1)
        finally:
            await database.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    record = commands.add_parser("record")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--baseline-id", required=True)
    record.add_argument("--max-run-p95-ms", type=int, required=True)
    record.add_argument("--max-case-p95-ms", type=int, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    service = EvaluationBaselineService()
    try:
        report = asyncio.run(_run_report())
        if arguments.command == "record":
            if os.getenv("CI", "").lower() in {"1", "true", "yes"}:
                raise EvaluationBaselineError("CI is never allowed to record baselines")
            if os.getenv("DESKPILOT_EVALUATION_BASELINE_MODE") != "record":
                raise EvaluationBaselineError(
                    "Set DESKPILOT_EVALUATION_BASELINE_MODE=record for an explicit record"
                )
            baseline = service.record(
                arguments.output,
                report,
                baseline_id=arguments.baseline_id,
                maximum_run_duration_p95_ms=arguments.max_run_p95_ms,
                maximum_case_duration_p95_ms=arguments.max_case_p95_ms,
            )
            print(json.dumps(baseline.model_dump(mode="json"), sort_keys=True))
            return 0
        baseline = service.load(arguments.baseline)
        result = service.compare(baseline, report)
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
        return 0 if result.passed else 1
    except EvaluationBaselineError as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
