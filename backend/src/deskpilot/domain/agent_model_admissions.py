"""Immutable proof bundle for admitting one calibrated cloud Agent route."""

from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import AGENT_ID_PATTERN, DIGEST_PATTERN
from deskpilot.domain.model_contracts import (
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
)
from deskpilot.domain.phase107_calibrations import (
    Phase107BlindReviewPacket,
    Phase107CalibrationBaseline,
    Phase107CalibrationReport,
    Phase107CalibrationRun,
    Phase107CalibrationSuite,
    Phase107HumanReviewBundle,
    Phase107JudgeRun,
)
from deskpilot.domain.tool_contracts import SEMVER_PATTERN

MAX_ADMISSION_VALIDITY = timedelta(days=90)


class ApprovedAgentModelAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.approved-agent-model-admission.v1"]
    admission_id: str = Field(pattern=r"^ama_[0-9a-f]{64}$")
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    agent_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_package_digest: str = Field(pattern=DIGEST_PATTERN)
    provider: ModelProviderDescriptor
    provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    build_id: str = Field(min_length=1, max_length=200)
    request_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    run_digest: str = Field(pattern=DIGEST_PATTERN)
    report_digest: str = Field(pattern=DIGEST_PATTERN)
    baseline_approval_digest: str = Field(pattern=DIGEST_PATTERN)
    review_bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    approved_by: str = Field(min_length=1, max_length=100)
    approved_at: datetime
    valid_until: datetime
    admission_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> Self:
        if self.approved_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Agent model admission timestamps must be timezone-aware")
        if (
            self.valid_until <= self.approved_at
            or self.valid_until - self.approved_at > MAX_ADMISSION_VALIDITY
        ):
            raise ValueError("Agent model admission validity must be within 90 days")
        if (
            self.provider.location is not ModelLocation.CLOUD
            or self.provider.protocol is ModelProtocol.FAKE
        ):
            raise ValueError("Agent model admission requires a non-Fake cloud Provider")
        if self.provider_snapshot_digest != sha256_digest(self.provider):
            raise ValueError("Agent model admission Provider snapshot changed")
        identity = {
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "agent_contract_digest": self.agent_contract_digest,
            "prompt_package_digest": self.prompt_package_digest,
            "provider_snapshot_digest": self.provider_snapshot_digest,
            "build_id": self.build_id,
            "report_digest": self.report_digest,
            "baseline_approval_digest": self.baseline_approval_digest,
        }
        if self.admission_id != f"ama_{sha256_digest(identity)}":
            raise ValueError("Agent model admission id changed")
        if self.admission_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"admission_digest"})
        ):
            raise ValueError("Agent model admission digest changed")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.agent_id, self.agent_version, self.provider_snapshot_digest)


class AgentModelAdmissionBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.agent-model-admission-bundle.v1"]
    suite: Phase107CalibrationSuite
    run: Phase107CalibrationRun
    packet: Phase107BlindReviewPacket
    judge_run: Phase107JudgeRun
    reviews: Phase107HumanReviewBundle
    report: Phase107CalibrationReport
    baseline: Phase107CalibrationBaseline
    admissions: tuple[ApprovedAgentModelAdmission, ...] = Field(
        min_length=1,
        max_length=16,
    )
    bundle_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def bundle_is_unique_and_digested(self) -> Self:
        keys = tuple(item.key for item in self.admissions)
        if len(keys) != len(set(keys)):
            raise ValueError("Agent model admission bundle contains a duplicate route")
        if self.bundle_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"bundle_digest"})
        ):
            raise ValueError("Agent model admission bundle digest changed")
        return self
