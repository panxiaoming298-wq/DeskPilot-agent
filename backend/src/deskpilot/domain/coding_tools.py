"""Proof-carrying contracts for project-scoped coding tools."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.task_plans import TASK_ID_PATTERN
from deskpilot.domain.workspace_files import WorkspaceFileRead


def _validated_git_manifest_path(value: str) -> PurePosixPath:
    pure = PurePosixPath(value)
    if (
        value != value.strip()
        or not value
        or "\\" in value
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(character in value for character in ("\x00", "\r", "\n", ":"))
    ):
        raise ValueError("Git commit proof path must stay beneath its project")
    return pure


def _git_backup_scope_matches(
    paths: tuple[GitCommitPathProof, ...],
    backups: tuple[GitCommitPathProof, ...],
) -> bool:
    if len(paths) != len(backups):
        return False
    remaining = {item.relative_path for item in backups}
    for proof in paths:
        pure = _validated_git_manifest_path(proof.relative_path)
        parent = (
            f"{PurePosixPath(*pure.parts[:-1]).as_posix()}/"
            if len(pure.parts) > 1
            else ""
        )
        pattern = re.compile(
            rf"^{re.escape(parent)}\.{re.escape(pure.name)}\.deskpilot-"
            r"[0-9a-f]{16}\.backup$"
        )
        matches = {candidate for candidate in remaining if pattern.fullmatch(candidate)}
        if len(matches) != 1:
            return False
        remaining -= matches
    return not remaining


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


class GitCommitPathProof(BaseModel):
    """One exact worktree file authorized for the controlled commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=32_767)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=0, le=262_144)
    proof_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        _validated_git_manifest_path(self.relative_path)
        material = self.model_dump(mode="json", exclude={"proof_digest"})
        if self.proof_digest != sha256_digest(material):
            raise ValueError("Git commit path proof digest does not match")
        return self


class GitCommitPreview(BaseModel):
    """Immutable authority preview for one server-owned branch and commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.git-commit-preview.v1"] = (
        "deskpilot.git-commit-preview.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    project_path: str = Field(min_length=1, max_length=32_767)
    expected_repository_digest: str = Field(pattern=DIGEST_PATTERN)
    toolchain_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_head_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    original_branch: str = Field(min_length=1, max_length=240)
    target_branch: str = Field(pattern=r"^codex/deskpilot-[0-9a-f]{16}$")
    commit_message: str = Field(min_length=1, max_length=200)
    paths: tuple[GitCommitPathProof, ...] = Field(min_length=2, max_length=8)
    excluded_backups: tuple[GitCommitPathProof, ...] = Field(min_length=2, max_length=8)
    hooks_disabled: Literal[True] = True
    signing_disabled: Literal[True] = True
    push_disabled: Literal[True] = True
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def authority_matches(self) -> Self:
        normalized = tuple(item.relative_path.casefold() for item in self.paths)
        if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
            raise ValueError("Git commit preview paths must be sorted and unique")
        if self.original_branch == self.target_branch:
            raise ValueError("Git commit target branch must be new")
        task_suffix = self.task_id.removeprefix("tsk_")
        if (
            self.target_branch != f"codex/deskpilot-{task_suffix[:16]}"
            or self.commit_message != f"完成 DeskPilot 任务 {task_suffix[:12]}"
        ):
            raise ValueError("Git commit branch or message escaped its Task authority")
        backup_paths = tuple(item.relative_path.casefold() for item in self.excluded_backups)
        if (
            backup_paths != tuple(sorted(backup_paths))
            or len(backup_paths) != len(set(backup_paths))
            or set(normalized) & set(backup_paths)
            or not _git_backup_scope_matches(self.paths, self.excluded_backups)
        ):
            raise ValueError("Git commit excluded backup proofs are inconsistent")
        material = self.model_dump(mode="json", exclude={"confirmation_digest"})
        if self.confirmation_digest != sha256_digest(material):
            raise ValueError("Git commit confirmation digest does not match")
        return self


class GitCommitReceipt(BaseModel):
    """Verified receipt for the exact commit created from a preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.git-commit-receipt.v1"] = (
        "deskpilot.git-commit-receipt.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    project_path: str = Field(min_length=1, max_length=32_767)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_head_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    commit_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tree_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    original_branch: str = Field(min_length=1, max_length=240)
    target_branch: str = Field(pattern=r"^codex/deskpilot-[0-9a-f]{16}$")
    commit_message_digest: str = Field(pattern=DIGEST_PATTERN)
    paths: tuple[GitCommitPathProof, ...] = Field(min_length=2, max_length=8)
    excluded_backups: tuple[GitCommitPathProof, ...] = Field(min_length=2, max_length=8)
    committed_at: datetime
    hooks_disabled: Literal[True] = True
    signing_disabled: Literal[True] = True
    push_disabled: Literal[True] = True
    rollback_available: Literal[True] = True
    receipt_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def receipt_matches(self) -> Self:
        normalized = tuple(item.relative_path.casefold() for item in self.paths)
        backup_paths = tuple(item.relative_path.casefold() for item in self.excluded_backups)
        task_suffix = self.task_id.removeprefix("tsk_")
        if (
            normalized != tuple(sorted(normalized))
            or len(normalized) != len(set(normalized))
            or backup_paths != tuple(sorted(backup_paths))
            or len(backup_paths) != len(set(backup_paths))
            or not _git_backup_scope_matches(self.paths, self.excluded_backups)
            or self.target_branch != f"codex/deskpilot-{task_suffix[:16]}"
            or self.commit_message_digest
            != sha256_digest({"message": f"完成 DeskPilot 任务 {task_suffix[:12]}"})
            or self.commit_oid == self.expected_head_oid
            or self.committed_at.tzinfo is None
            or {item.relative_path.casefold() for item in self.paths}
            & {item.relative_path.casefold() for item in self.excluded_backups}
        ):
            raise ValueError("Git commit receipt scope is inconsistent")
        material = self.model_dump(mode="json", exclude={"receipt_digest"})
        if self.receipt_digest != sha256_digest(material):
            raise ValueError("Git commit receipt digest does not match")
        return self


__all__ = [
    "GitCommitPathProof",
    "GitCommitPreview",
    "GitCommitReceipt",
    "GitInspectionRead",
    "ProjectBatchRead",
    "ProjectSearchMatch",
    "ProjectSearchRead",
]
