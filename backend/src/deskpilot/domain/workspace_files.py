"""Proof-carrying contracts for controlled conversation workspace files."""

import hashlib
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN


def workspace_edit_confirmation_digest(
    *,
    task_id: str,
    relative_path: str,
    expected_version_digest: str,
    proposed_content_digest: str,
    byte_count: int,
) -> str:
    return sha256_digest(
        {
            "task_id": task_id,
            "relative_path": relative_path,
            "expected_version_digest": expected_version_digest,
            "proposed_content_digest": proposed_content_digest,
            "replacement_count": 1,
            "byte_count": byte_count,
        }
    )


def workspace_patch_confirmation_digest(
    *,
    task_id: str,
    changes: tuple["WorkspacePatchChangePreview", ...],
    staging_workspace_ref: str,
    manifest_digest: str,
    total_byte_count: int,
) -> str:
    return sha256_digest(
        {
            "task_id": task_id,
            "changes": [item.model_dump(mode="json") for item in changes],
            "staging_workspace_ref": staging_workspace_ref,
            "manifest_digest": manifest_digest,
            "total_byte_count": total_byte_count,
        }
    )


def workspace_path_operation_confirmation_digest(
    *,
    task_id: str,
    operation: Literal["create", "rename"],
    source_path: str | None,
    target_path: str,
    expected_source_version_digest: str | None,
    expected_target_parent_version_digest: str,
    proposed_content_digest: str,
    byte_count: int,
) -> str:
    return sha256_digest(
        {
            "task_id": task_id,
            "operation": operation,
            "source_path": source_path,
            "target_path": target_path,
            "expected_source_version_digest": expected_source_version_digest,
            "expected_target_parent_version_digest": expected_target_parent_version_digest,
            "proposed_content_digest": proposed_content_digest,
            "byte_count": byte_count,
        }
    )


class WorkspaceFileRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-file-read.v1"] = "deskpilot.workspace-file-read.v1"
    relative_path: str = Field(min_length=1, max_length=32_767)
    byte_count: int = Field(ge=0, le=262_144)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)
    content: str = Field(max_length=262_144)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace file read result digest does not match")
        return self


class WorkspaceDirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=32_767)
    kind: Literal["directory", "file"]
    byte_count: int | None = Field(default=None, ge=0, le=262_144)
    version_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def byte_count_matches_kind(self) -> Self:
        if (self.kind == "file") != (self.byte_count is not None):
            raise ValueError("Workspace directory file entries require a byte count")
        return self


class WorkspaceDirectoryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-directory-read.v1"] = (
        "deskpilot.workspace-directory-read.v1"
    )
    relative_path: str = Field(min_length=1, max_length=32_767)
    entries: tuple[WorkspaceDirectoryEntry, ...] = Field(max_length=200)
    truncated: bool
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        keys = [(item.kind, item.name.casefold()) for item in self.entries]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("Workspace directory entries must be sorted and unique")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace directory result digest does not match")
        return self


class WorkspaceCheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str = Field(min_length=1, max_length=32_767)
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    code: Literal["JSON_INVALID", "PYTHON_SYNTAX_INVALID"]
    message: str = Field(min_length=1, max_length=300)


class WorkspaceCheckRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-check.v1"] = "deskpilot.workspace-check.v1"
    profile: Literal["json-parse", "python-syntax"]
    relative_path: str = Field(min_length=1, max_length=32_767)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["failed", "passed"]
    checked_file_count: int = Field(ge=1, le=64)
    issues: tuple[WorkspaceCheckIssue, ...] = Field(max_length=64)
    isolation_mode: Literal["windows_appcontainer"]
    network_access: Literal[False] = False
    output_truncated: bool = False
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if (self.status == "passed") != (not self.issues):
            raise ValueError("Workspace check status does not match its issues")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace check result digest does not match")
        return self


class WorkspacePythonTestFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str = Field(min_length=1, max_length=32_767)
    content: str = Field(max_length=262_144)
    byte_count: int = Field(ge=0, le=262_144)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def content_matches(self) -> Self:
        encoded = self.content.encode("utf-8")
        if len(encoded) != self.byte_count:
            raise ValueError("Workspace Python test file byte count does not match")
        if hashlib.sha256(encoded).hexdigest() != self.content_digest:
            raise ValueError("Workspace Python test file digest does not match")
        return self


class WorkspacePythonTestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: Literal["pytest-file"] = "pytest-file"
    project_path: str = Field(min_length=1, max_length=32_767)
    test_path: str = Field(min_length=1, max_length=32_767)
    files: tuple[WorkspacePythonTestFile, ...] = Field(min_length=1, max_length=512)
    total_byte_count: int = Field(ge=1, le=8_388_608)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def snapshot_matches(self) -> Self:
        paths = [item.relative_path.casefold() for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Workspace Python test files must be sorted and unique")
        if sum(item.byte_count for item in self.files) != self.total_byte_count:
            raise ValueError("Workspace Python test snapshot byte count does not match")
        if self.test_path.casefold() not in paths:
            raise ValueError("Workspace Python test target is missing from its snapshot")
        material = self.model_dump(mode="json", exclude={"snapshot_digest", "files"})
        material["files"] = [
            item.model_dump(mode="json", exclude={"content"}) for item in self.files
        ]
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Workspace Python test snapshot digest does not match")
        return self


class WorkspacePythonTestRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-python-test.v1"] = (
        "deskpilot.workspace-python-test.v1"
    )
    profile: Literal["pytest-file"] = "pytest-file"
    project_path: str = Field(min_length=1, max_length=32_767)
    test_path: str = Field(min_length=1, max_length=32_767)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    runtime_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["error", "failed", "passed"]
    exit_code: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    output: str = Field(max_length=32_768)
    output_truncated: bool
    isolation_mode: Literal["windows_appcontainer"]
    network_access: Literal[False] = False
    process_limit: Literal[1] = 1
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        expected_status = (
            "passed" if self.exit_code == 0 else ("failed" if self.exit_code == 1 else "error")
        )
        if self.status != expected_status:
            raise ValueError("Workspace Python test status does not match its exit code")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace Python test result digest does not match")
        return self


class WorkspaceNodeTestFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str = Field(min_length=1, max_length=32_767)
    content: str = Field(max_length=524_288)
    byte_count: int = Field(ge=0, le=524_288)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def content_matches(self) -> Self:
        encoded = self.content.encode("utf-8")
        if len(encoded) != self.byte_count:
            raise ValueError("Workspace Node test file byte count does not match")
        if hashlib.sha256(encoded).hexdigest() != self.content_digest:
            raise ValueError("Workspace Node test file digest does not match")
        return self


class WorkspaceNodeTestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: Literal["node-test-file"] = "node-test-file"
    project_path: str = Field(min_length=1, max_length=32_767)
    test_path: str = Field(min_length=1, max_length=32_767)
    files: tuple[WorkspaceNodeTestFile, ...] = Field(min_length=1, max_length=1_024)
    total_byte_count: int = Field(ge=1, le=16_777_216)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def snapshot_matches(self) -> Self:
        paths = [item.relative_path.casefold() for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Workspace Node test files must be sorted and unique")
        if sum(item.byte_count for item in self.files) != self.total_byte_count:
            raise ValueError("Workspace Node test snapshot byte count does not match")
        if self.test_path.casefold() not in paths:
            raise ValueError("Workspace Node test target is missing from its snapshot")
        material = self.model_dump(mode="json", exclude={"snapshot_digest", "files"})
        material["files"] = [
            item.model_dump(mode="json", exclude={"content"}) for item in self.files
        ]
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Workspace Node test snapshot digest does not match")
        return self


class WorkspaceNodeTestRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-node-test.v1"] = "deskpilot.workspace-node-test.v1"
    profile: Literal["node-test-file"] = "node-test-file"
    project_path: str = Field(min_length=1, max_length=32_767)
    test_path: str = Field(min_length=1, max_length=32_767)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    runtime_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["error", "failed", "passed"]
    exit_code: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    output: str = Field(max_length=32_768)
    output_truncated: bool
    isolation_mode: Literal["windows_appcontainer"]
    network_access: Literal[False] = False
    process_limit: Literal[1] = 1
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        expected_status = (
            "passed" if self.exit_code == 0 else ("failed" if self.exit_code == 1 else "error")
        )
        if self.status != expected_status:
            raise ValueError("Workspace Node test status does not match its exit code")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace Node test result digest does not match")
        return self


class WorkspaceEditPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-edit-preview.v1"] = (
        "deskpilot.workspace-edit-preview.v1"
    )
    task_id: str
    relative_path: str = Field(min_length=1, max_length=32_767)
    expected_version_digest: str = Field(pattern=DIGEST_PATTERN)
    proposed_content_digest: str = Field(pattern=DIGEST_PATTERN)
    replacement_count: Literal[1] = 1
    byte_count: int = Field(ge=0, le=262_144)
    old_text: str = Field(min_length=1, max_length=4_096)
    new_text: str = Field(max_length=4_096)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def confirmation_matches(self) -> Self:
        expected = workspace_edit_confirmation_digest(
            task_id=self.task_id,
            relative_path=self.relative_path,
            expected_version_digest=self.expected_version_digest,
            proposed_content_digest=self.proposed_content_digest,
            byte_count=self.byte_count,
        )
        if self.confirmation_digest != expected:
            raise ValueError("Workspace edit confirmation digest does not match")
        return self


class WorkspaceEditReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-edit-receipt.v1"] = (
        "deskpilot.workspace-edit-receipt.v1"
    )
    task_id: str
    relative_path: str = Field(min_length=1, max_length=32_767)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    previous_version_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    backup_relative_path: str = Field(min_length=1, max_length=32_767)
    byte_count: int = Field(ge=0, le=262_144)
    committed_at: datetime
    receipt_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def receipt_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"receipt_digest"})
        if self.receipt_digest != sha256_digest(material):
            raise ValueError("Workspace edit receipt digest does not match")
        return self


class CommitWorkspaceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)


class WorkspacePatchChangePreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    index: int = Field(ge=1, le=8)
    relative_path: str = Field(min_length=1, max_length=32_767)
    expected_version_digest: str = Field(pattern=DIGEST_PATTERN)
    original_content_digest: str = Field(pattern=DIGEST_PATTERN)
    proposed_content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=0, le=262_144)
    old_text: str = Field(min_length=1, max_length=4_096)
    new_text: str = Field(max_length=4_096)
    change_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"change_digest"})
        if self.change_digest != sha256_digest(material):
            raise ValueError("Workspace patch change digest does not match")
        return self


class WorkspacePatchPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-patch-preview.v1"] = (
        "deskpilot.workspace-patch-preview.v1"
    )
    task_id: str
    changes: tuple[WorkspacePatchChangePreview, ...] = Field(min_length=1, max_length=8)
    staging_workspace_ref: str = Field(min_length=1, max_length=32_767)
    manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    total_byte_count: int = Field(ge=0, le=2_097_152)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def confirmation_matches(self) -> Self:
        paths = [item.relative_path.casefold() for item in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("Workspace patch paths must be unique")
        if [item.index for item in self.changes] != list(range(1, len(self.changes) + 1)):
            raise ValueError("Workspace patch indexes must be contiguous")
        expected = workspace_patch_confirmation_digest(
            task_id=self.task_id,
            changes=self.changes,
            staging_workspace_ref=self.staging_workspace_ref,
            manifest_digest=self.manifest_digest,
            total_byte_count=self.total_byte_count,
        )
        if self.confirmation_digest != expected:
            raise ValueError("Workspace patch confirmation digest does not match")
        return self


class WorkspacePatchReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-patch-receipt.v1"] = (
        "deskpilot.workspace-patch-receipt.v1"
    )
    task_id: str
    status: Literal["committed", "partial"]
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    change_receipts: tuple[WorkspaceEditReceipt, ...] = Field(min_length=1, max_length=8)
    failed_path: str | None = Field(default=None, max_length=32_767)
    error_code: str | None = None
    committed_at: datetime
    receipt_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def receipt_matches(self) -> Self:
        if self.status == "committed" and (
            self.failed_path is not None or self.error_code is not None
        ):
            raise ValueError("Committed workspace patch cannot carry a failure")
        if self.status == "partial" and (self.failed_path is None or self.error_code is None):
            raise ValueError("Partial workspace patch requires failure details")
        material = self.model_dump(mode="json", exclude={"receipt_digest"})
        if self.receipt_digest != sha256_digest(material):
            raise ValueError("Workspace patch receipt digest does not match")
        return self


class WorkspacePatchTestRead(BaseModel):
    """Proof that one approved patch was applied before a server-fixed test."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-patch-test.v1"] = (
        "deskpilot.workspace-patch-test.v1"
    )
    task_id: str
    status: Literal["verified", "test_failed", "test_error"]
    test_kind: Literal["python", "node"]
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    patch_receipt: WorkspacePatchReceipt
    python_test: WorkspacePythonTestRead | None = None
    node_test: WorkspaceNodeTestRead | None = None
    error_code: str | None = Field(default=None, max_length=100)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def proof_matches(self) -> Self:
        if (
            self.patch_receipt.status != "committed"
            or self.patch_receipt.task_id != self.task_id
            or self.patch_receipt.confirmation_digest != self.confirmation_digest
        ):
            raise ValueError("Workspace patch test receipt binding does not match")
        selected = self.python_test if self.test_kind == "python" else self.node_test
        unselected = self.node_test if self.test_kind == "python" else self.python_test
        if unselected is not None:
            raise ValueError("Workspace patch test kind carries the wrong test result")
        if selected is None:
            if self.status != "test_error" or self.error_code is None:
                raise ValueError("Workspace patch test runtime error proof is incomplete")
        else:
            if self.error_code is not None:
                raise ValueError("Workspace patch test result cannot carry a runtime error")
            expected = {
                "passed": "verified",
                "failed": "test_failed",
                "error": "test_error",
            }[selected.status]
            if self.status != expected:
                raise ValueError("Workspace patch test status does not match its result")
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Workspace patch test result digest does not match")
        return self


class CommitWorkspacePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)


class WorkspacePathOperationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-path-operation-preview.v1"] = (
        "deskpilot.workspace-path-operation-preview.v1"
    )
    task_id: str
    operation: Literal["create", "rename"]
    source_path: str | None = Field(default=None, min_length=1, max_length=32_767)
    target_path: str = Field(min_length=1, max_length=32_767)
    expected_source_version_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    expected_target_parent_version_digest: str = Field(pattern=DIGEST_PATTERN)
    proposed_content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=0, le=262_144)
    content: str | None = Field(default=None, max_length=262_144)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def confirmation_matches(self) -> Self:
        if self.operation == "create":
            if self.source_path is not None or self.expected_source_version_digest is not None:
                raise ValueError("Workspace create preview cannot carry a source")
            if self.content is None:
                raise ValueError("Workspace create preview requires content")
            encoded = self.content.encode("utf-8")
            if (
                len(encoded) != self.byte_count
                or hashlib.sha256(encoded).hexdigest() != self.proposed_content_digest
            ):
                raise ValueError("Workspace create preview content does not match")
        elif (
            self.source_path is None
            or self.expected_source_version_digest is None
            or self.content is not None
        ):
            raise ValueError("Workspace rename preview source does not match")
        expected = workspace_path_operation_confirmation_digest(
            task_id=self.task_id,
            operation=self.operation,
            source_path=self.source_path,
            target_path=self.target_path,
            expected_source_version_digest=self.expected_source_version_digest,
            expected_target_parent_version_digest=(self.expected_target_parent_version_digest),
            proposed_content_digest=self.proposed_content_digest,
            byte_count=self.byte_count,
        )
        if self.confirmation_digest != expected:
            raise ValueError("Workspace path operation confirmation digest does not match")
        return self


class WorkspacePathOperationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.workspace-path-operation-receipt.v1"] = (
        "deskpilot.workspace-path-operation-receipt.v1"
    )
    task_id: str
    operation: Literal["create", "rename"]
    source_path: str | None = Field(default=None, min_length=1, max_length=32_767)
    target_path: str = Field(min_length=1, max_length=32_767)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=0, le=262_144)
    committed_at: datetime
    receipt_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def receipt_matches(self) -> Self:
        if (self.operation == "create") != (self.source_path is None):
            raise ValueError("Workspace path operation receipt source does not match")
        material = self.model_dump(mode="json", exclude={"receipt_digest"})
        if self.receipt_digest != sha256_digest(material):
            raise ValueError("Workspace path operation receipt digest does not match")
        return self


class CommitWorkspacePathOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
