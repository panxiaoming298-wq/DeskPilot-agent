"""CI compare and explicitly signed release attestation for phase 75."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from deskpilot.application.phase75_evaluation import (
    Phase75EvaluationError,
    Phase75EvaluationService,
    Phase75GateError,
    Phase75GateService,
    dump_json,
)

DEFAULT_BASELINE = Path(
    "tests/baselines/evaluations/multi-agent-core-v16.baseline.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compare", "attest"))
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-id", default="source-tree")
    parser.add_argument("--key-id", default="local-release-key")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        report = asyncio.run(Phase75EvaluationService(build_id="source-tree").run())
        gate = Phase75GateService()
        baseline = gate.load_baseline(arguments.baseline)
        violations = gate.compare(baseline, report)
        if arguments.command == "compare":
            print(
                json.dumps(
                    {
                        "passed": not violations,
                        "baseline_id": baseline.baseline_id,
                        "report_digest": report.report_digest,
                        "violations": violations,
                    },
                    sort_keys=True,
                )
            )
            return 0 if not violations else 1
        if os.getenv("CI", "").lower() in {"1", "true", "yes"}:
            raise Phase75GateError("CI is not allowed to sign release attestations")
        if arguments.output is None:
            raise Phase75GateError("attest requires --output")
        if arguments.output.exists():
            raise Phase75GateError("Attestation output is immutable")
        key = os.getenv("DESKPILOT_EVALUATION_ATTESTATION_KEY", "").encode("utf-8")
        attestation = gate.attest(
            baseline,
            report,
            build_id=arguments.build_id,
            key_id=arguments.key_id,
            signing_key=key,
        )
        dump_json(arguments.output, attestation)
        print(json.dumps(attestation.model_dump(mode="json"), sort_keys=True))
        return 0
    except Phase75EvaluationError as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
