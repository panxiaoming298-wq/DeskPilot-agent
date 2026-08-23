"""Phase 71 verification, artifact, browser, and delivery projections."""

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.agent_runtime import RESULT_ID_PATTERN, RUN_ID_PATTERN
from deskpilot.domain.research import CLAIM_ID_PATTERN
from deskpilot.domain.task_plans import PLAN_NODE_ID_PATTERN, TASK_ID_PATTERN

VERIFICATION_RUN_ID_PATTERN = r"^vfy_[0-9a-f]{64}$"
EVIDENCE_SNAPSHOT_ID_PATTERN = r"^ves_[0-9a-f]{64}$"
WORKSPACE_ID_PATTERN = r"^wsp_[0-9a-f]{64}$"
ARTIFACT_ID_PATTERN = r"^art_[0-9a-f]{64}$"
REVISION_ID_PATTERN = r"^arv_[0-9a-f]{64}$"
PATCH_RECEIPT_ID_PATTERN = r"^prc_[0-9a-f]{64}$"
BROWSER_RUN_ID_PATTERN = r"^brr_[0-9a-f]{64}$"
DELIVERY_ID_PATTERN = r"^dlv_[0-9a-f]{64}$"


class CitationJudgment(BaseModel):
    """Untrusted semantic grader observation; the reducer owns the verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    supported: bool
    reason_code: Literal["SUPPORTED", "UNSUPPORTED", "CONTRADICTED"]


class CitationVerificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.citation-verification-decision.v1"] = (
        "deskpilot.citation-verification-decision.v1"
    )
    judgments: tuple[CitationJudgment, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_claims(self) -> Self:
        claim_ids = [item.claim_id for item in self.judgments]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Citation judgments must use unique Claim IDs")
        return self


class ClaimVerdictRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    outcome: Literal["verified", "unsupported", "contradicted"]
    reason_code: str
    citation_ids: tuple[str, ...]
    verdict_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.verdict_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"verdict_digest"})
        ):
            raise ValueError("Claim Verdict digest does not match")
        return self


class VerificationRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verification_run_id: str = Field(pattern=VERIFICATION_RUN_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    result_id: str = Field(pattern=RESULT_ID_PATTERN)
    attempt: int = Field(ge=1)
    policy_id: Literal["builtin.research-citation.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["completed", "failed"]
    outcome: Literal["verified", "rejected", "verification_error"]
    evidence_snapshot_id: str | None = Field(default=None, pattern=EVIDENCE_SNAPSHOT_ID_PATTERN)
    input_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    grader_request_digest: str = Field(pattern=DIGEST_PATTERN)
    grader_output_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    grader_provider_id: str
    grader_model: str
    verdicts: tuple[ClaimVerdictRead, ...]
    created_at: datetime
    completed_at: datetime


class PatchReceiptRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    patch_receipt_id: str = Field(pattern=PATCH_RECEIPT_ID_PATTERN)
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    operation: Literal["create", "replace"]
    relative_path: str
    base_revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)
    new_revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    base_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    new_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=1)
    receipt_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.receipt_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"created_at", "receipt_digest"})
        ):
            raise ValueError("PatchReceipt digest does not match")
        return self


class PdfRenderVerificationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: Literal["deskpilot.pdf-render.v1"]
    status: Literal["passed"]
    engine: str
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    page_count: int = Field(ge=1, le=1_000)
    page_width_points: float = Field(gt=0)
    page_height_points: float = Field(gt=0)
    render_dpi: int = Field(ge=72, le=600)
    rendered_page_digests: tuple[str, ...] = Field(min_length=1, max_length=1_000)
    rendered_page_dimensions: tuple[tuple[int, int], ...] = Field(
        min_length=1, max_length=1_000
    )
    issue_codes: tuple[str, ...]
    evidence_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def proof_matches(self) -> Self:
        if len(self.rendered_page_digests) != self.page_count:
            raise ValueError("PDF rendered page digests are incomplete")
        if len(self.rendered_page_dimensions) != self.page_count:
            raise ValueError("PDF rendered page dimensions are incomplete")
        if any(not re.fullmatch(DIGEST_PATTERN, item) for item in self.rendered_page_digests):
            raise ValueError("PDF rendered page digest is invalid")
        if any(width < 2 or height < 2 for width, height in self.rendered_page_dimensions):
            raise ValueError("PDF rendered page dimensions are invalid")
        if self.issue_codes:
            raise ValueError("Passed PDF render verification cannot contain issues")
        if self.evidence_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"evidence_digest"})
        ):
            raise ValueError("PDF render evidence digest does not match")
        return self


class ArtifactRevisionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    revision_no: int = Field(ge=1)
    media_type: Literal["application/pdf", "text/html", "text/css", "text/markdown"]
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=1)
    patch_receipt_id: str = Field(pattern=PATCH_RECEIPT_ID_PATTERN)
    pdf_render_verification: PdfRenderVerificationRead | None = None
    created_at: datetime


class ArtifactRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    relative_path: str
    active_revision: ArtifactRevisionRead


class TaskWorkspaceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    allowed_extensions: tuple[str, ...]
    max_total_bytes: int = Field(ge=1)
    max_files: int = Field(ge=1)
    status: Literal["active", "delivered"]
    artifacts: tuple[ArtifactRead, ...]
    created_at: datetime
    updated_at: datetime


class BrowserRenderRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    browser_run_id: str = Field(pattern=BROWSER_RUN_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    status: Literal["passed", "failed"]
    engine: str
    profile_id: Literal["deskpilot.browser-static-html.v1"]
    viewport_width: int = Field(ge=320, le=3840)
    viewport_height: int = Field(ge=320, le=2160)
    title: str
    heading_count: int = Field(ge=0)
    link_count: int = Field(ge=0)
    external_request_count: int = Field(ge=0)
    console_error_count: int = Field(ge=0)
    page_error_count: int = Field(ge=0)
    issue_codes: tuple[str, ...]
    dom_digest: str = Field(pattern=DIGEST_PATTERN)
    screenshot_digest: str = Field(pattern=DIGEST_PATTERN)
    evidence_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    completed_at: datetime


class DeliveryManifestRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    delivery_id: str = Field(pattern=DELIVERY_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    browser_run_id: str = Field(pattern=BROWSER_RUN_ID_PATTERN)
    verified_claim_ids: tuple[str, ...]
    citation_ids: tuple[str, ...]
    limitation_codes: tuple[str, ...]
    manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.manifest_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"created_at", "manifest_digest"})
        ):
            raise ValueError("Delivery Manifest digest does not match")
        return self


def digested(
    material: dict[str, object], field: str, *, exclude: tuple[str, ...] = ()
) -> dict[str, object]:
    """Small helper for immutable receipt/evidence DTO construction."""

    digest_material = {key: value for key, value in material.items() if key not in exclude}
    return {**material, field: sha256_digest(digest_material)}
