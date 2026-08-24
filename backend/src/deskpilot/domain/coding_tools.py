"""Proof-carrying contracts for project-scoped read-only coding tools."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.workspace_files import WorkspaceFileRead


class ProjectSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=32_767)
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    preview: str = Field(max_length=1_000)
    preview_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def preview_matches(self) -> Self:
        if self.preview_digest != sha256_digest({"preview": self.preview}):
            raise ValueError("Project search preview digest does not match")
        return self


class ProjectSearchRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.project-search-read.v1"] = (
        "deskpilot.project-search-read.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    query_digest: str = Field(pattern=DIGEST_PATTERN)
    matches: tuple[ProjectSearchMatch, ...] = Field(max_length=200)
    scanned_file_count: int = Field(ge=0, le=2_000)
    scanned_byte_count: int = Field(ge=0, le=33_554_432)
    truncated: bool
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        keys = [(item.relative_path.casefold(), item.line, item.column) for item in self.matches]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("Project search matches must be sorted and unique")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Project search result digest does not match")
        return self


class ProjectBatchRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.project-batch-read.v1"] = (
        "deskpilot.project-batch-read.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    files: tuple[WorkspaceFileRead, ...] = Field(min_length=1, max_length=32)
    total_byte_count: int = Field(ge=0, le=2_097_152)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        paths = [item.relative_path.casefold() for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Project batch-read paths must be sorted and unique")
        if sum(item.byte_count for item in self.files) != self.total_byte_count:
            raise ValueError("Project batch-read byte count does not match")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Project batch-read result digest does not match")
        return self


class GitInspectionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.git-inspection-read.v1"] = (
        "deskpilot.git-inspection-read.v1"
    )
    operation: Literal["status", "diff", "log"]
    project_path: str = Field(min_length=1, max_length=32_767)
    repository_digest: str = Field(pattern=DIGEST_PATTERN)
    toolchain_digest: str = Field(pattern=DIGEST_PATTERN)
    head_oid: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    exit_code: Literal[0] = 0
    output: str = Field(max_length=65_536)
    output_digest: str = Field(pattern=DIGEST_PATTERN)
    output_truncated: bool
    hooks_disabled: Literal[True] = True
    external_diff_disabled: Literal[True] = True
    textconv_disabled: Literal[True] = True
    pager_disabled: Literal[True] = True
    optional_locks_disabled: Literal[True] = True
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.output_digest != sha256_digest({"output": self.output}):
            raise ValueError("Git inspection output digest does not match")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Git inspection result digest does not match")
        return self


__all__ = [
    "GitInspectionRead",
    "ProjectBatchRead",
    "ProjectSearchMatch",
    "ProjectSearchRead",
]
