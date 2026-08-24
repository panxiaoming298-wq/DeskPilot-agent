"""Immutable contracts for server-owned coding command profiles."""

import hashlib
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN

CommandProfileId = Literal[
    "python.pytest.v1",
    "python.ruff.v1",
    "python.mypy.v1",
    "node.pnpm_test.v1",
    "node.pnpm_typecheck.v1",
    "node.pnpm_build.v1",
]
COMMAND_PROFILE_IDS: tuple[CommandProfileId, ...] = (
    "python.pytest.v1",
    "python.ruff.v1",
    "python.mypy.v1",
    "node.pnpm_test.v1",
    "node.pnpm_typecheck.v1",
    "node.pnpm_build.v1",
)


class CommandProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.command-profile.v1"] = "deskpilot.command-profile.v1"
    command_profile_id: CommandProfileId
    version: Literal["1.0.0"] = "1.0.0"
    ecosystem: Literal["python", "node"]
    operation: Literal["test", "lint", "type_check", "build"]
    timeout_seconds: int = Field(ge=5, le=600)
    max_output_bytes: Literal[65_536] = 65_536
    max_processes: int = Field(ge=1, le=8)
    network_access: Literal[False] = False
    temporary_snapshot: Literal[True] = True
    model_selects_only_profile_id: Literal[True] = True
    caller_supplies_process_fields: Literal[False] = False
    dependency_mode: Literal["bundled", "offline_frozen"]
    profile_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        expected_ecosystem = "python" if self.command_profile_id.startswith("python.") else "node"
        if self.ecosystem != expected_ecosystem:
            raise ValueError("Command Profile ecosystem does not match its id")
        expected_mode = "bundled" if self.ecosystem == "python" else "offline_frozen"
        if self.dependency_mode != expected_mode:
            raise ValueError("Command Profile dependency mode does not match its ecosystem")
        material = self.model_dump(mode="json", exclude={"profile_digest"})
        if self.profile_digest != sha256_digest(material):
            raise ValueError("Command Profile digest does not match")
        return self

    @classmethod
    def build(cls, **values: object) -> Self:
        material = {"schema_version": "deskpilot.command-profile.v1", **values}
        return cls.model_validate({**material, "profile_digest": sha256_digest(material)})


class WorkspaceCommandFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=32_767)
    content: str = Field(max_length=262_144)
    byte_count: int = Field(ge=0, le=262_144)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def content_matches(self) -> Self:
        encoded = self.content.encode("utf-8")
        if self.byte_count != len(encoded):
            raise ValueError("Workspace command file byte count does not match")
        if self.content_digest != hashlib.sha256(encoded).hexdigest():
            raise ValueError("Workspace command file content digest does not match")
        return self


class WorkspaceCommandSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-snapshot.v1"] = (
        "deskpilot.workspace-command-snapshot.v1"
    )
    command_profile: CommandProfile
    project_path: str = Field(min_length=1, max_length=32_767)
    files: tuple[WorkspaceCommandFile, ...] = Field(min_length=1, max_length=2_000)
    total_byte_count: int = Field(ge=1, le=33_554_432)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        paths = [item.relative_path.casefold() for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Workspace command snapshot paths must be sorted and unique")
        if sum(item.byte_count for item in self.files) != self.total_byte_count:
            raise ValueError("Workspace command snapshot byte count does not match")
        material = self.model_dump(mode="json", exclude={"snapshot_digest"})
        material["files"] = [
            item.model_dump(mode="json", exclude={"content"}) for item in self.files
        ]
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Workspace command snapshot digest does not match")
        return self

    @classmethod
    def build(cls, **values: object) -> Self:
        material = {"schema_version": "deskpilot.workspace-command-snapshot.v1", **values}
        digest_material = dict(material)
        files = material.get("files")
        if not isinstance(files, tuple) or not all(
            isinstance(item, WorkspaceCommandFile) for item in files
        ):
            raise ValueError("Workspace command snapshot files must be one typed tuple")
        digest_material["files"] = [
            item.model_dump(mode="json", exclude={"content"}) for item in files
        ]
        return cls.model_validate(
            {**material, "snapshot_digest": sha256_digest(digest_material)}
        )


class WorkspaceCommandRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-read.v1"] = (
        "deskpilot.workspace-command-read.v1"
    )
    command_profile_id: CommandProfileId
    profile_digest: str = Field(pattern=DIGEST_PATTERN)
    project_path: str = Field(min_length=1, max_length=32_767)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    toolchain_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["passed", "failed", "error", "timed_out", "cancelled"]
    exit_code: int | None = Field(default=None, ge=0, le=2_147_483_647)
    duration_ms: int = Field(ge=0)
    output_summary: str = Field(max_length=65_536)
    output_digest: str = Field(pattern=DIGEST_PATTERN)
    output_truncated: bool
    termination_reason: Literal["completed", "timeout", "cancelled"]
    cancellation_receipt_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    isolation_mode: Literal["windows_appcontainer"]
    network_access: Literal[False] = False
    temporary_snapshot: Literal[True] = True
    snapshot_mutations_discarded: Literal[True] = True
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def receipt_matches(self) -> Self:
        if self.output_digest != sha256_digest({"output": self.output_summary}):
            raise ValueError("Workspace command output digest does not match")
        terminal = self.status in {"timed_out", "cancelled"}
        if terminal != (self.exit_code is None):
            raise ValueError("Workspace command exit code does not match termination")
        if (self.status == "passed") != (self.exit_code == 0):
            raise ValueError("Workspace command status does not match exit code")
        if self.status == "timed_out" and self.termination_reason != "timeout":
            raise ValueError("Workspace command timeout reason changed")
        if self.status == "cancelled" and self.termination_reason != "cancelled":
            raise ValueError("Workspace command cancellation reason changed")
        if not terminal and self.termination_reason != "completed":
            raise ValueError("Workspace command completion reason changed")
        if terminal != (self.cancellation_receipt_digest is not None):
            raise ValueError("Workspace command cancellation receipt is incomplete")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace command result digest does not match")
        return self


__all__ = [
    "COMMAND_PROFILE_IDS",
    "CommandProfile",
    "CommandProfileId",
    "WorkspaceCommandFile",
    "WorkspaceCommandRead",
    "WorkspaceCommandSnapshot",
]
