"""Build and strictly load non-activating Phase-115 personal preview evidence."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from deskpilot.application.phase107_calibration import (
    Phase107CalibrationError,
    Phase107CalibrationService,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.phase107_calibrations import (
    Phase107BlindReviewPacket,
    Phase107CalibrationRun,
    Phase107CalibrationSuite,
    Phase107HumanReviewBundle,
    Phase107JudgeRun,
)
from deskpilot.domain.phase115_personal_preview import (
    Phase115PersonalPreviewBundle,
)

MAX_PERSONAL_PREVIEW_BUNDLE_BYTES = 32 * 1024 * 1024


class Phase115PersonalPreviewError(RuntimeError):
    code = "PHASE115_PERSONAL_PREVIEW_INVALID"


def build_phase115_personal_preview_bundle(
    *,
    suite: Phase107CalibrationSuite,
    run: Phase107CalibrationRun,
    packet: Phase107BlindReviewPacket,
    judge_run: Phase107JudgeRun,
    reviews: Phase107HumanReviewBundle,
    operator_ref: str,
    issued_at: datetime,
    valid_until: datetime,
) -> Phase115PersonalPreviewBundle:
    """Create short-lived one-reviewer evidence without admitting or activating a route."""

    if (
        run.schema_version != "deskpilot.phase115-calibration-run.v3"
        or reviews.schema_version
        != "deskpilot.phase115-personal-preview-review-bundle.v2"
        or reviews.review_mode != "personal_preview"
        or issued_at.tzinfo is None
        or valid_until.tzinfo is None
    ):
        raise Phase115PersonalPreviewError(
            "Personal preview requires timezone-aware three-role preview evidence"
        )
    service = Phase107CalibrationService()
    try:
        report = service.grade(
            suite,
            run,
            packet,
            judge_run,
            reviews,
            now=issued_at,
        )
    except (Phase107CalibrationError, ValidationError, ValueError, TypeError) as error:
        raise Phase115PersonalPreviewError(
            "Personal preview evidence failed full calibration replay"
        ) from error
    if report.status != "passed":
        raise Phase115PersonalPreviewError("Personal preview calibration did not pass")
    material = {
        "schema_version": "deskpilot.phase115-personal-preview-bundle.v1",
        "suite": suite,
        "run": run,
        "packet": packet,
        "judge_run": judge_run,
        "reviews": reviews,
        "report": report,
        "operator_ref": operator_ref,
        "data_class": "public_synthetic",
        "issued_at": issued_at,
        "valid_until": valid_until,
        "activates_runtime": False,
    }
    try:
        return Phase115PersonalPreviewBundle.model_validate(
            {**material, "bundle_digest": sha256_digest(material)}
        )
    except (ValidationError, ValueError, TypeError) as error:
        raise Phase115PersonalPreviewError(
            "Personal preview evidence is not admissible"
        ) from error


def load_phase115_personal_preview(
    path: Path,
) -> Phase115PersonalPreviewBundle:
    """Strictly inspect a personal preview artifact; loading never activates runtime."""

    try:
        if path.is_symlink():
            raise Phase115PersonalPreviewError(
                "Personal preview bundle cannot be a symbolic link"
            )
        payload = path.resolve(strict=True).read_bytes()
        if not payload or len(payload) > MAX_PERSONAL_PREVIEW_BUNDLE_BYTES:
            raise Phase115PersonalPreviewError(
                "Personal preview bundle size is invalid"
            )
        json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        bundle = Phase115PersonalPreviewBundle.model_validate_json(payload, strict=True)
        service = Phase107CalibrationService()
        regraded = service.grade(
            bundle.suite,
            bundle.run,
            bundle.packet,
            bundle.judge_run,
            bundle.reviews,
            now=bundle.report.evaluated_at,
        )
        if regraded.report_digest != bundle.report.report_digest:
            raise Phase115PersonalPreviewError(
                "Personal preview report replay changed"
            )
        return bundle
    except Phase115PersonalPreviewError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        Phase107CalibrationError,
        ValueError,
        TypeError,
    ) as error:
        raise Phase115PersonalPreviewError(
            "Personal preview bundle failed strict loading"
        ) from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Phase115PersonalPreviewError(
                "Personal preview bundle contains a duplicate JSON key"
            )
        result[key] = value
    return result
