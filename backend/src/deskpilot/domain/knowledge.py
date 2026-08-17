"""Public contracts for the local, read-only knowledge base."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=32_767)


class KnowledgeSourceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    canonical_path: str
    artifact_id: str
    source_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_at: datetime
    updated_at: datetime


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


class KnowledgeCitationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    artifact_id: str
    chunk_id: str
    canonical_path: str
    locator: str
    snippet: str
    score: float = Field(gt=0)
    text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeSearchRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    citations: tuple[KnowledgeCitationRead, ...]
    searched_sources: int = Field(ge=0)
    stale_source_ids: tuple[str, ...] = ()
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
