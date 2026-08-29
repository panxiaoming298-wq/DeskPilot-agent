"""Non-activating evidence for one-reviewer personal cloud evaluation."""

from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.model_contracts import ModelLocation, ModelProtocol
from deskpilot.domain.phase107_calibrations import (
    REVIEWER_PATTERN,
    Phase107BlindReviewPacket,
    Phase107CalibrationReport,
    Phase107CalibrationRun,
    Phase107CalibrationSuite,
    Phase107HumanReviewBundle,
    Phase107JudgeRun,
)

MAX_PERSONAL_PREVIEW_VALIDITY = timedelta(days=14)


class Phase115PersonalPreviewBundle(BaseModel):
    """Exact reviewed evidence that deliberately grants no runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.phase115-personal-preview-bundle.v1"]
    suite: Phase107CalibrationSuite
    run: Phase107CalibrationRun
    packet: Phase107BlindReviewPacket
    judge_run: Phase107JudgeRun
    reviews: Phase107HumanReviewBundle
    report: Phase107CalibrationReport
    operator_ref: str = Field(pattern=REVIEWER_PATTERN)
    data_class: Literal["public_synthetic"] = "public_synthetic"
    issued_at: datetime
    valid_until: datetime
    activates_runtime: Literal[False] = False
    bundle_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def evidence_is_bound_non_activating_and_digested(self) -> Self:
        if self.issued_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Personal preview timestamps must be timezone-aware")
        if (
            self.valid_until <= self.issued_at
            or self.valid_until - self.issued_at > MAX_PERSONAL_PREVIEW_VALIDITY
            or self.valid_until > self.reviews.valid_until
        ):
            raise ValueError("Personal preview validity must be within 14 days")
        if (
            self.run.schema_version != "deskpilot.phase115-calibration-run.v3"
            or self.report.schema_version
            != "deskpilot.phase115-personal-preview-report.v1"
            or self.reviews.schema_version
            != "deskpilot.phase115-personal-preview-review-bundle.v2"
            or self.reviews.review_mode != "personal_preview"
            or self.report.review_mode != "personal_preview"
            or self.report.status != "passed"
        ):
            raise ValueError("Personal preview requires passed three-role preview evidence")
        if (
            self.run.provider.location is not ModelLocation.CLOUD
            or self.run.provider.protocol is ModelProtocol.FAKE
        ):
            raise ValueError("Personal preview requires a non-Fake cloud Provider")
        reviewers = {item.reviewer_ref for item in self.reviews.judgments}
        if reviewers != {self.operator_ref}:
            raise ValueError("Personal preview operator must be its only human reviewer")
        if (
            self.issued_at < self.report.evaluated_at
            or self.report.run_digest != self.run.run_digest
            or self.report.packet_digest != self.packet.packet_digest
            or self.report.judge_run_digest != self.judge_run.judge_run_digest
            or self.report.review_bundle_digest != self.reviews.bundle_digest
        ):
            raise ValueError("Personal preview evidence binding changed")
        if self.activates_runtime:
            raise ValueError("Personal preview evidence cannot activate runtime")
        if self.bundle_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"bundle_digest"})
        ):
            raise ValueError("Personal preview bundle digest changed")
        return self
