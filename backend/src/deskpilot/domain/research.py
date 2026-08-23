"""Provider-neutral read-only web research and citation contracts."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.task_plans import TASK_ID_PATTERN

RESEARCH_SESSION_ID_PATTERN = r"^rsr_[0-9a-f]{64}$"
SEARCH_CALL_ID_PATTERN = r"^src_[0-9a-f]{64}$"
SEARCH_HIT_ID_PATTERN = r"^sht_[0-9a-f]{64}$"
PAGE_SNAPSHOT_ID_PATTERN = r"^snp_[0-9a-f]{64}$"
CLAIM_ID_PATTERN = r"^clm_[0-9a-f]{64}$"
CITATION_ID_PATTERN = r"^cit_[0-9a-f]{64}$"


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(ge=1, le=20)
    allowed_domains: tuple[str, ...] = Field(default=(), max_length=20)


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hit_id: str = Field(pattern=SEARCH_HIT_ID_PATTERN)
    rank: int = Field(ge=1, le=20)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=4_096)
    snippet: str = Field(default="", max_length=2_000)
    origin: Literal["external_untrusted"] = "external_untrusted"
    hit_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"hit_digest"})
        if self.hit_digest != sha256_digest(material):
            raise ValueError("Search Hit digest does not match")
        return self


class SearchProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    hits: tuple[SearchHit, ...] = Field(max_length=20)


class SearchCallRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    search_call_id: str = Field(pattern=SEARCH_CALL_ID_PATTERN)
    research_session_id: str = Field(pattern=RESEARCH_SESSION_ID_PATTERN)
    attempt: int = Field(ge=1)
    provider_id: str
    query_digest: str = Field(pattern=DIGEST_PATTERN)
    hits: tuple[SearchHit, ...]
    created_at: datetime


class PageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.page-snapshot.v1"] = "deskpilot.page-snapshot.v1"
    page_snapshot_id: str = Field(pattern=PAGE_SNAPSHOT_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    research_session_id: str = Field(pattern=RESEARCH_SESSION_ID_PATTERN)
    search_hit_id: str = Field(pattern=SEARCH_HIT_ID_PATTERN)
    requested_url: str = Field(min_length=1, max_length=4_096)
    final_url: str = Field(min_length=1, max_length=4_096)
    status_code: int = Field(ge=200, le=299)
    media_type: Literal["text/html", "text/plain"]
    title: str | None = Field(default=None, max_length=500)
    extracted_text: str = Field(min_length=1, max_length=200_000)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    extractor_version: Literal["deskpilot.html-text.v1"] = "deskpilot.html-text.v1"
    origin: Literal["external_untrusted"] = "external_untrusted"
    fetched_at: datetime
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.content_digest != sha256_digest({"text": self.extracted_text}):
            raise ValueError("Page content digest does not match")
        material = self.model_dump(mode="json", exclude={"snapshot_digest"})
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Page Snapshot digest does not match")
        return self


class ResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    research_session_id: str = Field(pattern=RESEARCH_SESSION_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=2_000)
    citation_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    status: Literal["awaiting_verification"] = "awaiting_verification"
    claim_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"claim_digest"})
        if self.claim_digest != sha256_digest(material):
            raise ValueError("Research Claim digest does not match")
        return self


class CitationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    citation_id: str = Field(pattern=CITATION_ID_PATTERN)
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    page_snapshot_id: str = Field(pattern=PAGE_SNAPSHOT_ID_PATTERN)
    locator_text: str = Field(min_length=1, max_length=1_000)
    locator_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["awaiting_verification"] = "awaiting_verification"
    citation_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.locator_digest != sha256_digest({"text": self.locator_text}):
            raise ValueError("Citation locator digest does not match")
        material = self.model_dump(mode="json", exclude={"citation_digest"})
        if self.citation_digest != sha256_digest(material):
            raise ValueError("Citation Evidence digest does not match")
        return self


class ResearchClaimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    statement: str = Field(min_length=1, max_length=2_000)
    page_snapshot_ids: tuple[str, ...] = Field(min_length=1, max_length=5)


class ResearchAgentDecision(BaseModel):
    """Strict candidate output; it deliberately has no success/verified field."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.research-decision.v1"] = "deskpilot.research-decision.v1"
    kind: Literal["submit_result"] = "submit_result"
    claims: tuple[ResearchClaimProposal, ...] = Field(min_length=1, max_length=20)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)


class ResearchRouteRequestDecision(BaseModel):
    """A proposal to invoke one server-bound read-only research Route."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["request_route"] = "request_route"
    route_binding_id: str = Field(pattern=r"^rbn_[0-9a-f]{64}$")
    query: str = Field(min_length=1, max_length=500)
    decision_summary: str = Field(min_length=1, max_length=300)


class ResearchSubmitResultDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["submit_result"] = "submit_result"
    claims: tuple[ResearchClaimProposal, ...] = Field(min_length=1, max_length=20)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)
    decision_summary: str = Field(min_length=1, max_length=300)


ResearchLoopDecisionValue = Annotated[
    ResearchRouteRequestDecision | ResearchSubmitResultDecision,
    Field(discriminator="kind"),
]


class ResearchLoopDecision(RootModel[ResearchLoopDecisionValue]):
    """Exactly one normalized decision per persisted research Model Turn."""

    model_config = ConfigDict(frozen=True)


class ResearchSessionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    research_session_id: str = Field(pattern=RESEARCH_SESSION_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    invocation_id: str
    status: Literal[
        "created",
        "running",
        "awaiting_verification",
        "verified",
        "rejected",
        "failed",
    ]
    search_calls: tuple[SearchCallRead, ...]
    page_snapshots: tuple[PageSnapshot, ...]
    claims: tuple[ResearchClaim, ...]
    citations: tuple[CitationEvidence, ...]
    created_at: datetime
    updated_at: datetime
