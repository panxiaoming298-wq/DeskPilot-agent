"""Build a production-disabled three-role Admission from reviewed live evidence."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from deskpilot.application.agent_model_admission import (
    MAX_ADMISSION_BUNDLE_BYTES,
    AgentModelAdmissionError,
    build_phase115_admission_bundle,
)
from deskpilot.application.phase107_calibration import (
    Phase107CalibrationError,
    Phase107CalibrationService,
)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approved-at", type=_timestamp, required=True)
    parser.add_argument("--valid-until", type=_timestamp, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    service = Phase107CalibrationService()
    try:
        bundle = build_phase115_admission_bundle(
            suite=service.load_suite(arguments.suite),
            run=service.load_run(arguments.run),
            packet=service.load_packet(arguments.packet),
            judge_run=service.load_judge_run(arguments.judge),
            reviews=service.load_review_bundle(arguments.reviews),
            report=service.load_report(arguments.report),
            baseline_id=arguments.baseline_id,
            approved_by=arguments.approved_by,
            approved_at=arguments.approved_at,
            valid_until=arguments.valid_until,
        )
        if arguments.output.exists():
            raise AgentModelAdmissionError("Admission artifact output is immutable")
        payload = bundle.model_dump_json(indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_ADMISSION_BUNDLE_BYTES:
            raise AgentModelAdmissionError("Admission artifact output is too large")
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            payload,
            encoding="utf-8",
            newline="\n",
        )
        result = {
            "bundle_digest": bundle.bundle_digest,
            "baseline_approval_digest": bundle.baseline.approval_digest,
            "provider_id": bundle.run.provider.provider_id,
            "model": bundle.run.provider.model,
            "admission_count": len(bundle.admissions),
            "admitted_agents": [
                [item.agent_id, item.agent_version] for item in bundle.admissions
            ],
            "activates_runtime": False,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        AgentModelAdmissionError,
        Phase107CalibrationError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
    ) as error:
        print(
            json.dumps(
                {"code": "AGENT_MODEL_ADMISSION_INVALID", "detail": str(error)},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
