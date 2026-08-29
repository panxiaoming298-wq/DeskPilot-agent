"""Build non-activating one-reviewer Phase-115 personal preview evidence."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from deskpilot.application.phase107_calibration import (
    Phase107CalibrationError,
    Phase107CalibrationService,
)
from deskpilot.application.phase115_personal_preview import (
    MAX_PERSONAL_PREVIEW_BUNDLE_BYTES,
    Phase115PersonalPreviewError,
    build_phase115_personal_preview_bundle,
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
    parser.add_argument("--operator-ref", required=True)
    parser.add_argument("--issued-at", type=_timestamp, required=True)
    parser.add_argument("--valid-until", type=_timestamp, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    service = Phase107CalibrationService()
    try:
        bundle = build_phase115_personal_preview_bundle(
            suite=service.load_suite(arguments.suite),
            run=service.load_run(arguments.run),
            packet=service.load_packet(arguments.packet),
            judge_run=service.load_judge_run(arguments.judge),
            reviews=service.load_review_bundle(arguments.reviews),
            operator_ref=arguments.operator_ref,
            issued_at=arguments.issued_at,
            valid_until=arguments.valid_until,
        )
        if arguments.output.exists():
            raise Phase115PersonalPreviewError(
                "Personal preview artifact output is immutable"
            )
        payload = bundle.model_dump_json(indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_PERSONAL_PREVIEW_BUNDLE_BYTES:
            raise Phase115PersonalPreviewError(
                "Personal preview artifact output is too large"
            )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(payload, encoding="utf-8", newline="\n")
        print(
            json.dumps(
                {
                    "admission_tier": "personal_preview",
                    "bundle_digest": bundle.bundle_digest,
                    "operator_ref": bundle.operator_ref,
                    "data_class": bundle.data_class,
                    "valid_until": bundle.valid_until.isoformat(),
                    "activates_runtime": bundle.activates_runtime,
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        Phase115PersonalPreviewError,
        Phase107CalibrationError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
    ) as error:
        print(
            json.dumps(
                {
                    "code": "PHASE115_PERSONAL_PREVIEW_INVALID",
                    "detail": str(error),
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
