"""Narrow, proof-carrying read and exact-replace boundary for a configured workspace."""

import ctypes
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Literal, cast

from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.workspace_files import (
    WorkspaceDirectoryEntry,
    WorkspaceDirectoryRead,
    WorkspaceEditPreview,
    WorkspaceEditReceipt,
    WorkspaceFileRead,
    WorkspaceNodeTestFile,
    WorkspaceNodeTestSnapshot,
    WorkspacePatchChangePreview,
    WorkspacePatchPreview,
    WorkspacePatchReceipt,
    WorkspacePathOperationPreview,
    WorkspacePathOperationReceipt,
    WorkspacePythonTestFile,
    WorkspacePythonTestSnapshot,
    workspace_edit_confirmation_digest,
    workspace_patch_confirmation_digest,
    workspace_path_operation_confirmation_digest,
)
from deskpilot.tools.workspace_checks import WorkspaceCheckFile, WorkspaceCheckInput

MAX_FILE_BYTES = 262_144
MAX_CHECK_FILES = 64
MAX_CHECK_TOTAL_BYTES = 524_288
MAX_DIRECTORY_ENTRIES = 200
MAX_DIRECTORY_SCAN = 1_000
MAX_TEST_FILES = 512
MAX_TEST_TOTAL_BYTES = 8_388_608
MAX_TEST_SCAN = 5_000
MAX_NODE_TEST_FILES = 1_024
MAX_NODE_TEST_TOTAL_BYTES = 16_777_216
ALLOWED_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
TEST_SNAPSHOT_SUFFIXES = {
    ".ini",
    ".json",
    ".mako",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".typed",
    ".yaml",
    ".yml",
}
TEST_EXCLUDED_DIRECTORIES = {"__pycache__", "data", "dist", "node_modules"}
NODE_TEST_SNAPSHOT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".mts",
    ".scss",
    ".ts",
    ".tsx",
    ".vue",
}
NODE_TEST_EXCLUDED_DIRECTORIES = {
    "coverage",
    "data",
    "dist",
    "node_modules",
    "src-tauri",
}


class WorkspaceFileError(RuntimeError):
    code = "WORKSPACE_FILE_REJECTED"


class WorkspaceFileDisabledError(WorkspaceFileError):
    code = "WORKSPACE_FILE_DISABLED"


class WorkspaceFilePathRejectedError(WorkspaceFileError):
    code = "WORKSPACE_FILE_PATH_REJECTED"


class WorkspaceFileConflictError(WorkspaceFileError):
    code = "WORKSPACE_FILE_CONFLICT"


class WorkspaceFileCommitUnknownError(WorkspaceFileError):
    code = "WORKSPACE_FILE_COMMIT_UNKNOWN"


class WorkspacePatchPartialError(WorkspaceFileError):
    code = "WORKSPACE_PATCH_PARTIAL"

    def __init__(self, receipt: WorkspacePatchReceipt) -> None:
        super().__init__("Workspace patch completed only partially")
        self.receipt = receipt


@dataclass(frozen=True)
class _Material:
    path: Path
    relative_path: str
    content: str
    encoded: bytes
    content_digest: str
    version_digest: str


class WorkspaceFileRuntime:
    """Allow only UTF-8 regular files beneath one explicit workspace root."""

    def __init__(self, root: str | None, staging_root: str | None = None) -> None:
        self._root: Path | None = None
        self._staging_root: Path | None = None
        if root is None:
            return
        candidate = Path(root)
        try:
            candidate_stat = candidate.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceFilePathRejectedError(
                "Conversation workspace root does not exist"
            ) from error
        if self._is_reparse_point(candidate, candidate_stat):
            raise WorkspaceFilePathRejectedError(
                "Conversation workspace root cannot be a link or reparse point"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise WorkspaceFilePathRejectedError(
                "Conversation workspace root does not exist"
            ) from error
        if not resolved.is_dir():
            raise WorkspaceFilePathRejectedError("Conversation workspace root must be a directory")
        self._root = resolved
        if staging_root is not None:
            staging = Path(staging_root).resolve(strict=False)
            if staging.exists() and (staging.is_symlink() or not staging.is_dir()):
                raise WorkspaceFilePathRejectedError(
                    "Workspace patch staging root must be a regular directory"
                )
            self._staging_root = staging

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def patch_enabled(self) -> bool:
        return self.enabled and self._staging_root is not None

    @property
    def path_operation_enabled(self) -> bool:
        if (
            not self.patch_enabled
            or os.name != "nt"
            or self._root is None
            or self._staging_root is None
        ):
            return False
        try:
            return self._root.stat().st_dev == self._staging_root.stat().st_dev
        except OSError:
            return bool(self._root.drive) and (
                self._root.drive.casefold() == self._staging_root.drive.casefold()
            )

    def read(self, relative_path: str) -> WorkspaceFileRead:
        material = self._read_material(relative_path)
        result = {
            "schema_version": "deskpilot.workspace-file-read.v1",
            "relative_path": material.relative_path,
            "byte_count": len(material.encoded),
            "content_digest": material.content_digest,
            "version_digest": material.version_digest,
            "content": material.content,
        }
        return WorkspaceFileRead.model_validate({**result, "result_digest": sha256_digest(result)})

    def resolve_project_directory(self, relative_path: str) -> tuple[Path, str]:
        """Resolve one trusted project root without exposing a user-selected cwd to models."""

        return self._resolve_directory(relative_path)

    def resolve_project_file(self, relative_path: str) -> tuple[Path, str]:
        """Resolve one project file through the same root and reparse-point guard."""

        return self._resolve_file(relative_path)

    def read_project_material(self, path: Path) -> _Material:
        """Read a path that a trusted recursive scanner already resolved beneath the root."""

        if self._root is None:
            raise WorkspaceFileDisabledError("Conversation workspace is not configured")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise WorkspaceFilePathRejectedError(
                "Project file must stay beneath the configured workspace"
            ) from error
        value = resolved.stat(follow_symlinks=False)
        if self._is_reparse_point(resolved, value):
            raise WorkspaceFilePathRejectedError(
                "Workspace links and reparse points are not allowed"
            )
        return self._read_absolute_material(resolved, self._relative(resolved))

    def list_directory(self, relative_path: str) -> WorkspaceDirectoryRead:
        path, normalized = self._resolve_directory(relative_path)
        try:
            before = path.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceFileConflictError(
                "Workspace directory changed before it was listed"
            ) from error
        entries: list[WorkspaceDirectoryEntry] = []
        scanned = 0
        truncated = False
        try:
            iterator = os.scandir(path)
        except OSError as error:
            raise WorkspaceFilePathRejectedError("Workspace directory cannot be opened") from error
        with iterator:
            for child in iterator:
                scanned += 1
                if scanned > MAX_DIRECTORY_SCAN:
                    truncated = True
                    break
                if child.name.startswith("."):
                    continue
                child_path = Path(child.path)
                try:
                    child_stat = child_path.stat(follow_symlinks=False)
                except OSError as error:
                    raise WorkspaceFileConflictError(
                        "Workspace directory changed while it was listed"
                    ) from error
                if self._is_reparse_point(child_path, child_stat):
                    continue
                if stat.S_ISDIR(child_stat.st_mode):
                    kind: Literal["directory", "file"] = "directory"
                    byte_count = None
                elif (
                    stat.S_ISREG(child_stat.st_mode)
                    and child_path.suffix.casefold() in ALLOWED_SUFFIXES
                    and child_stat.st_size <= MAX_FILE_BYTES
                ):
                    kind = "file"
                    byte_count = child_stat.st_size
                else:
                    continue
                if len(entries) == MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    continue
                identity = self._stat_identity(child_stat)
                entries.append(
                    WorkspaceDirectoryEntry(
                        name=child.name,
                        relative_path=self._relative(child_path),
                        kind=kind,
                        byte_count=byte_count,
                        version_digest=sha256_digest(identity),
                    )
                )
        try:
            after = path.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceFileConflictError(
                "Workspace directory changed while it was listed"
            ) from error
        if self._stat_identity(before) != self._stat_identity(after):
            raise WorkspaceFileConflictError("Workspace directory changed while it was listed")
        entries.sort(key=lambda item: (item.kind, item.name.casefold()))
        material = {
            "schema_version": "deskpilot.workspace-directory-read.v1",
            "relative_path": normalized,
            "entries": [item.model_dump(mode="json") for item in entries],
            "truncated": truncated,
        }
        return WorkspaceDirectoryRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )

    def prepare_check(
        self,
        profile: str,
        relative_path: str,
    ) -> WorkspaceCheckInput:
        if profile not in {"json-parse", "python-syntax"}:
            raise WorkspaceFilePathRejectedError("Workspace check profile is not allowed")
        suffix = ".json" if profile == "json-parse" else ".py"
        path, normalized = self._resolve_existing(relative_path)
        if path.is_file():
            if path.suffix.casefold() != suffix:
                raise WorkspaceFilePathRejectedError(
                    "Workspace check target does not match its fixed profile"
                )
            paths = [path]
        else:
            try:
                paths = self._check_paths(path, suffix)
            except OSError as error:
                raise WorkspaceFileConflictError(
                    "Workspace check directory changed during snapshotting"
                ) from error
        files: list[WorkspaceCheckFile] = []
        total_bytes = 0
        for item in paths:
            material = self._read_material(self._relative(item))
            total_bytes += len(material.encoded)
            if total_bytes > MAX_CHECK_TOTAL_BYTES:
                raise WorkspaceFilePathRejectedError(
                    "Workspace check snapshot exceeds the total byte limit"
                )
            files.append(
                WorkspaceCheckFile(
                    relative_path=material.relative_path,
                    content=material.content,
                    content_digest=material.content_digest,
                    version_digest=material.version_digest,
                )
            )
        files.sort(key=lambda item: item.relative_path.casefold())
        facts = {
            "profile": profile,
            "relative_path": normalized,
            "files": [item.model_dump(mode="json", exclude={"content"}) for item in files],
        }
        return WorkspaceCheckInput(
            profile=cast(Literal["json-parse", "python-syntax"], profile),
            relative_path=normalized,
            files=tuple(files),
            snapshot_digest=sha256_digest(facts),
        )

    def prepare_python_test(
        self,
        project_path: str,
        test_path: str,
    ) -> WorkspacePythonTestSnapshot:
        project, normalized_project = self._resolve_directory(project_path)
        raw_test = test_path.strip().replace("\\", "/")
        pure_test = PurePath(raw_test)
        if (
            not raw_test
            or pure_test.is_absolute()
            or any(part in {"", ".", ".."} or part.startswith(".") for part in pure_test.parts)
            or not pure_test.parts
            or pure_test.parts[0].casefold() != "tests"
            or pure_test.suffix.casefold() != ".py"
            or not (pure_test.name.startswith("test_") or pure_test.name.endswith("_test.py"))
        ):
            raise WorkspaceFilePathRejectedError(
                "Pytest target must be one explicit Python test file under tests/"
            )
        workspace_test_path = (
            raw_test if normalized_project == "." else f"{normalized_project}/{raw_test}"
        )
        target, _ = self._resolve_file(workspace_test_path)
        try:
            target.relative_to(project)
        except ValueError as error:
            raise WorkspaceFilePathRejectedError(
                "Pytest target must stay beneath its project snapshot"
            ) from error
        try:
            paths = self._python_test_paths(project)
        except OSError as error:
            raise WorkspaceFileConflictError(
                "Python test directory changed during snapshotting"
            ) from error
        relative_paths = {item.relative_to(project).as_posix().casefold() for item in paths}
        if raw_test.casefold() not in relative_paths:
            raise WorkspaceFilePathRejectedError(
                "Pytest target is not present in the bounded project snapshot"
            )
        files: list[WorkspacePythonTestFile] = []
        total_bytes = 0
        for item in paths:
            material = self._read_material(self._relative(item))
            total_bytes += len(material.encoded)
            if total_bytes > MAX_TEST_TOTAL_BYTES:
                raise WorkspaceFilePathRejectedError(
                    "Python test snapshot exceeds the total byte limit"
                )
            files.append(
                WorkspacePythonTestFile(
                    relative_path=item.relative_to(project).as_posix(),
                    content=material.content,
                    byte_count=len(material.encoded),
                    content_digest=material.content_digest,
                    version_digest=material.version_digest,
                )
            )
        files.sort(key=lambda item: item.relative_path.casefold())
        facts = {
            "profile": "pytest-file",
            "project_path": normalized_project,
            "test_path": raw_test,
            "total_byte_count": total_bytes,
            "files": [item.model_dump(mode="json", exclude={"content"}) for item in files],
        }
        return WorkspacePythonTestSnapshot(
            profile="pytest-file",
            project_path=normalized_project,
            test_path=raw_test,
            files=tuple(files),
            total_byte_count=total_bytes,
            snapshot_digest=sha256_digest(facts),
        )

    def prepare_node_test(
        self,
        project_path: str,
        test_path: str,
    ) -> WorkspaceNodeTestSnapshot:
        project, normalized_project = self._resolve_directory(project_path)
        raw_test = test_path.strip().replace("\\", "/")
        pure_test = PurePath(raw_test)
        suffixes = tuple(part.casefold() for part in pure_test.suffixes)
        is_test = (
            len(suffixes) >= 2 and suffixes[-2] in {".spec", ".test"} and (suffixes[-1] == ".js")
        )
        if (
            not raw_test
            or pure_test.is_absolute()
            or any(part in {"", ".", ".."} or part.startswith(".") for part in pure_test.parts)
            or not is_test
        ):
            raise WorkspaceFilePathRejectedError(
                "Node test target must be one explicit JavaScript *.spec.js or *.test.js file"
            )
        workspace_test_path = (
            raw_test if normalized_project == "." else f"{normalized_project}/{raw_test}"
        )
        target, _ = self._resolve_file(workspace_test_path)
        try:
            target.relative_to(project)
        except ValueError as error:
            raise WorkspaceFilePathRejectedError(
                "Node test target must stay beneath its project snapshot"
            ) from error
        paths = self._node_test_paths(project)
        relative_paths = {item.relative_to(project).as_posix().casefold() for item in paths}
        if raw_test.casefold() not in relative_paths:
            raise WorkspaceFilePathRejectedError(
                "Node test target is not present in the bounded project snapshot"
            )
        files: list[WorkspaceNodeTestFile] = []
        total_bytes = 0
        for item in paths:
            material = self._read_material(self._relative(item))
            total_bytes += len(material.encoded)
            if total_bytes > MAX_NODE_TEST_TOTAL_BYTES:
                raise WorkspaceFilePathRejectedError(
                    "Node test snapshot exceeds the total byte limit"
                )
            files.append(
                WorkspaceNodeTestFile(
                    relative_path=item.relative_to(project).as_posix(),
                    content=material.content,
                    byte_count=len(material.encoded),
                    content_digest=material.content_digest,
                    version_digest=material.version_digest,
                )
            )
        files.sort(key=lambda item: item.relative_path.casefold())
        facts = {
            "profile": "node-test-file",
            "project_path": normalized_project,
            "test_path": raw_test,
            "total_byte_count": total_bytes,
            "files": [item.model_dump(mode="json", exclude={"content"}) for item in files],
        }
        return WorkspaceNodeTestSnapshot(
            profile="node-test-file",
            project_path=normalized_project,
            test_path=raw_test,
            files=tuple(files),
            total_byte_count=total_bytes,
            snapshot_digest=sha256_digest(facts),
        )

    def prepare_replace(
        self,
        *,
        task_id: str,
        relative_path: str,
        old_text: str,
        new_text: str,
    ) -> WorkspaceEditPreview:
        if not old_text:
            raise WorkspaceFileConflictError("Replacement source text cannot be empty")
        material = self._read_material(relative_path)
        count = material.content.count(old_text)
        if count != 1:
            raise WorkspaceFileConflictError(
                "Exact replacement requires the source text to occur once"
            )
        proposed = material.content.replace(old_text, new_text, 1)
        encoded = proposed.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise WorkspaceFileConflictError("Proposed file exceeds the workspace size limit")
        proposed_digest = hashlib.sha256(encoded).hexdigest()
        confirmation_digest = workspace_edit_confirmation_digest(
            task_id=task_id,
            relative_path=material.relative_path,
            expected_version_digest=material.version_digest,
            proposed_content_digest=proposed_digest,
            byte_count=len(encoded),
        )
        return WorkspaceEditPreview(
            task_id=task_id,
            relative_path=material.relative_path,
            expected_version_digest=material.version_digest,
            proposed_content_digest=proposed_digest,
            byte_count=len(encoded),
            old_text=old_text,
            new_text=new_text,
            confirmation_digest=confirmation_digest,
        )

    def commit_replace(self, preview: WorkspaceEditPreview) -> WorkspaceEditReceipt:
        if os.name != "nt":
            raise WorkspaceFileDisabledError(
                "Atomic recoverable workspace replacement is currently available only on Windows"
            )
        current = self._read_material(preview.relative_path)
        target = current.path
        replacement, backup = self._replacement_paths(target, preview.task_id)

        if backup.exists():
            return self._reconcile(preview, current, backup)
        encoded = self._validate_proposed_replacement(preview, current)

        self._stage_replacement(replacement, encoded)
        try:
            self._replace_file_windows(target, replacement, backup)
        except OSError:
            if replacement.exists():
                replacement.unlink()
            raise
        return self._reconcile(preview, self._read_material(preview.relative_path), backup)

    def prepare_patch(
        self,
        *,
        task_id: str,
        changes: tuple[dict[str, str], ...],
        minimum_changes: int = 2,
        maximum_changes: int = 8,
    ) -> WorkspacePatchPreview:
        if self._staging_root is None:
            raise WorkspaceFileDisabledError("Workspace patch staging is not configured")
        if not 1 <= minimum_changes <= maximum_changes <= 8:
            raise ValueError("Workspace patch change bounds are invalid")
        if not minimum_changes <= len(changes) <= maximum_changes:
            raise WorkspaceFileConflictError(
                f"Workspace patch requires {minimum_changes} to {maximum_changes} changes"
            )
        previews: list[WorkspacePatchChangePreview] = []
        staged: list[tuple[str, bytes, bytes]] = []
        seen_paths: set[str] = set()
        for index, change in enumerate(changes, start=1):
            old_text = change["old_text"]
            new_text = change["new_text"]
            if not old_text:
                raise WorkspaceFileConflictError("Replacement source text cannot be empty")
            material = self._read_material(change["path"])
            path_key = os.path.normcase(material.relative_path)
            if path_key in seen_paths:
                raise WorkspaceFileConflictError("Workspace patch file paths must be unique")
            seen_paths.add(path_key)
            if material.content.count(old_text) != 1:
                raise WorkspaceFileConflictError("Every patch source text must occur exactly once")
            proposed = material.content.replace(old_text, new_text, 1).encode("utf-8")
            if len(proposed) > MAX_FILE_BYTES:
                raise WorkspaceFileConflictError(
                    "A proposed patch file exceeds the workspace size limit"
                )
            item = {
                "index": index,
                "relative_path": material.relative_path,
                "expected_version_digest": material.version_digest,
                "original_content_digest": material.content_digest,
                "proposed_content_digest": hashlib.sha256(proposed).hexdigest(),
                "byte_count": len(proposed),
                "old_text": old_text,
                "new_text": new_text,
            }
            preview = WorkspacePatchChangePreview.model_validate(
                {**item, "change_digest": sha256_digest(item)}
            )
            previews.append(preview)
            staged.append((material.relative_path, material.encoded, proposed))
        preview_changes = tuple(previews)
        total_byte_count = sum(item.byte_count for item in preview_changes)
        manifest = {
            "schema_version": "deskpilot.workspace-patch-staging.v1",
            "task_id": task_id,
            "changes": [item.model_dump(mode="json") for item in preview_changes],
            "total_byte_count": total_byte_count,
        }
        manifest_digest = sha256_digest(manifest)
        directory_name = sha256_digest(
            {
                "task_id": task_id,
                "manifest_digest": manifest_digest,
            }
        )
        staging_workspace_ref = f".deskpilot-workspace-patches/{directory_name}"
        self._stage_patch(staging_workspace_ref, manifest, manifest_digest, staged)
        confirmation_digest = workspace_patch_confirmation_digest(
            task_id=task_id,
            changes=preview_changes,
            staging_workspace_ref=staging_workspace_ref,
            manifest_digest=manifest_digest,
            total_byte_count=total_byte_count,
        )
        return WorkspacePatchPreview(
            task_id=task_id,
            changes=preview_changes,
            staging_workspace_ref=staging_workspace_ref,
            manifest_digest=manifest_digest,
            total_byte_count=total_byte_count,
            confirmation_digest=confirmation_digest,
        )

    def commit_patch(self, preview: WorkspacePatchPreview) -> WorkspacePatchReceipt:
        self._verify_patch_staging(preview)
        legacy_staging_ref = (
            ".deskpilot-workspace-patches/"
            + hashlib.sha256(preview.task_id.encode("utf-8")).hexdigest()
        )
        patch_identity = (
            None
            if preview.staging_workspace_ref == legacy_staging_ref
            else preview.manifest_digest
        )
        edit_previews = tuple(
            self._edit_preview_from_patch(
                preview.task_id,
                item,
                patch_identity=patch_identity,
            )
            for item in preview.changes
        )
        existing: dict[str, WorkspaceEditReceipt] = {}
        for item in edit_previews:
            receipt = self._preflight_replace(item)
            if receipt is not None:
                existing[item.relative_path] = receipt

        receipts: list[WorkspaceEditReceipt] = []
        for item in edit_previews:
            try:
                receipt = existing.get(item.relative_path) or self.commit_replace(item)
            except (WorkspaceFileError, OSError) as error:
                if not receipts:
                    raise
                partial = self._patch_receipt(
                    preview,
                    "partial",
                    receipts,
                    failed_path=item.relative_path,
                    error_code=getattr(error, "code", "WORKSPACE_FILE_OS_ERROR"),
                )
                raise WorkspacePatchPartialError(partial) from error
            receipts.append(receipt)
        return self._patch_receipt(preview, "committed", receipts)

    def prepare_create(
        self,
        *,
        task_id: str,
        target_path: str,
        content: str,
    ) -> WorkspacePathOperationPreview:
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise WorkspaceFileConflictError("Proposed file exceeds the workspace size limit")
        _, normalized, parent_digest, target_stat = self._resolve_target(target_path)
        if target_stat is not None:
            raise WorkspaceFileConflictError("Workspace create target already exists")
        content_digest = hashlib.sha256(encoded).hexdigest()
        confirmation = workspace_path_operation_confirmation_digest(
            task_id=task_id,
            operation="create",
            source_path=None,
            target_path=normalized,
            expected_source_version_digest=None,
            expected_target_parent_version_digest=parent_digest,
            proposed_content_digest=content_digest,
            byte_count=len(encoded),
        )
        return WorkspacePathOperationPreview(
            task_id=task_id,
            operation="create",
            target_path=normalized,
            expected_target_parent_version_digest=parent_digest,
            proposed_content_digest=content_digest,
            byte_count=len(encoded),
            content=content,
            confirmation_digest=confirmation,
        )

    def prepare_rename(
        self,
        *,
        task_id: str,
        source_path: str,
        target_path: str,
    ) -> WorkspacePathOperationPreview:
        source = self._read_material(source_path)
        _, normalized, parent_digest, target_stat = self._resolve_target(target_path)
        if source.relative_path == normalized:
            raise WorkspaceFileConflictError("Workspace rename target must be different")
        if target_stat is not None:
            raise WorkspaceFileConflictError("Workspace rename target already exists")
        confirmation = workspace_path_operation_confirmation_digest(
            task_id=task_id,
            operation="rename",
            source_path=source.relative_path,
            target_path=normalized,
            expected_source_version_digest=source.version_digest,
            expected_target_parent_version_digest=parent_digest,
            proposed_content_digest=source.content_digest,
            byte_count=len(source.encoded),
        )
        return WorkspacePathOperationPreview(
            task_id=task_id,
            operation="rename",
            source_path=source.relative_path,
            target_path=normalized,
            expected_source_version_digest=source.version_digest,
            expected_target_parent_version_digest=parent_digest,
            proposed_content_digest=source.content_digest,
            byte_count=len(source.encoded),
            confirmation_digest=confirmation,
        )

    def commit_path_operation(
        self, preview: WorkspacePathOperationPreview
    ) -> WorkspacePathOperationReceipt:
        if not self.path_operation_enabled:
            raise WorkspaceFileDisabledError(
                "Recoverable workspace path operations require Windows and staging"
            )
        if preview.operation == "create":
            return self._commit_create(preview)
        return self._commit_rename(preview)

    def _commit_create(
        self, preview: WorkspacePathOperationPreview
    ) -> WorkspacePathOperationReceipt:
        if preview.content is None:
            raise WorkspaceFileCommitUnknownError("Workspace create content is missing")
        encoded = preview.content.encode("utf-8")
        target, normalized, parent_digest, target_stat = self._resolve_target(preview.target_path)
        if normalized != preview.target_path:
            raise WorkspaceFileCommitUnknownError("Workspace create target changed")
        if target_stat is not None:
            self._verify_creation_manifest(preview)
            return self._reconcile_path_operation(preview, self._read_material(normalized))
        if parent_digest != preview.expected_target_parent_version_digest:
            raise WorkspaceFileConflictError("Workspace create parent changed after preview")
        if (
            len(encoded) != preview.byte_count
            or hashlib.sha256(encoded).hexdigest() != preview.proposed_content_digest
        ):
            raise WorkspaceFileCommitUnknownError("Workspace create preview proof changed")
        staging = self._creation_staging_path(preview.task_id)
        if staging.parent.stat().st_dev != target.parent.stat().st_dev:
            raise WorkspaceFileDisabledError(
                "Workspace create staging must be on the workspace volume"
            )
        self._stage_replacement(staging, encoded)
        self._ensure_creation_manifest(preview)
        _, _, current_parent_digest, current_target_stat = self._resolve_target(preview.target_path)
        if current_target_stat is not None:
            return self._reconcile_path_operation(preview, self._read_material(normalized))
        if current_parent_digest != preview.expected_target_parent_version_digest:
            raise WorkspaceFileConflictError("Workspace create parent changed during commit")
        try:
            os.rename(staging, target)
        except OSError as error:
            try:
                material = self._read_material(normalized)
            except WorkspaceFileError:
                raise error from None
            return self._reconcile_path_operation(preview, material)
        return self._reconcile_path_operation(preview, self._read_material(normalized))

    def _commit_rename(
        self, preview: WorkspacePathOperationPreview
    ) -> WorkspacePathOperationReceipt:
        if preview.source_path is None or preview.expected_source_version_digest is None:
            raise WorkspaceFileCommitUnknownError("Workspace rename source is missing")
        source_path, source_normalized, _, source_stat = self._resolve_target(preview.source_path)
        target_path, target_normalized, parent_digest, target_stat = self._resolve_target(
            preview.target_path
        )
        if source_normalized != preview.source_path or target_normalized != preview.target_path:
            raise WorkspaceFileCommitUnknownError("Workspace rename path changed")
        if source_stat is None:
            if target_stat is None:
                raise WorkspaceFileCommitUnknownError(
                    "Workspace rename source and target are both missing"
                )
            return self._reconcile_path_operation(preview, self._read_material(target_normalized))
        if target_stat is not None:
            raise WorkspaceFileConflictError("Workspace rename target already exists")
        source = self._read_absolute_material(source_path, source_normalized)
        if (
            source.version_digest != preview.expected_source_version_digest
            or source.content_digest != preview.proposed_content_digest
            or len(source.encoded) != preview.byte_count
        ):
            raise WorkspaceFileConflictError("Workspace rename source changed after preview")
        if parent_digest != preview.expected_target_parent_version_digest:
            raise WorkspaceFileConflictError("Workspace rename target parent changed after preview")
        try:
            os.rename(source_path, target_path)
        except OSError as error:
            try:
                material = self._read_material(target_normalized)
            except WorkspaceFileError:
                raise error from None
            return self._reconcile_path_operation(preview, material)
        return self._reconcile_path_operation(preview, self._read_material(target_normalized))

    def _reconcile_path_operation(
        self,
        preview: WorkspacePathOperationPreview,
        material: _Material,
    ) -> WorkspacePathOperationReceipt:
        if (
            material.relative_path != preview.target_path
            or material.content_digest != preview.proposed_content_digest
            or len(material.encoded) != preview.byte_count
            or (
                preview.operation == "rename"
                and material.version_digest != preview.expected_source_version_digest
            )
        ):
            raise WorkspaceFileCommitUnknownError("Workspace path operation outcome is ambiguous")
        if preview.operation == "create":
            self._verify_creation_manifest(preview)
            staging = self._creation_staging_path(preview.task_id)
            if staging.exists():
                if staging.is_symlink() or not staging.is_file():
                    raise WorkspaceFileCommitUnknownError("Workspace create staging path is unsafe")
                if preview.content is None or staging.read_bytes() != preview.content.encode(
                    "utf-8"
                ):
                    raise WorkspaceFileCommitUnknownError(
                        "Workspace create staging content changed"
                    )
                staging.unlink()
        result = {
            "schema_version": "deskpilot.workspace-path-operation-receipt.v1",
            "task_id": preview.task_id,
            "operation": preview.operation,
            "source_path": preview.source_path,
            "target_path": preview.target_path,
            "confirmation_digest": preview.confirmation_digest,
            "version_digest": material.version_digest,
            "content_digest": material.content_digest,
            "byte_count": len(material.encoded),
            "committed_at": datetime.fromtimestamp(material.path.stat().st_mtime, tz=UTC),
        }
        return WorkspacePathOperationReceipt.model_validate(
            {**result, "receipt_digest": sha256_digest(result)}
        )

    def _creation_staging_path(self, task_id: str) -> Path:
        if self._staging_root is None:
            raise WorkspaceFileDisabledError("Workspace path operation staging is unavailable")
        root = self._staging_root / ".deskpilot-workspace-creations"
        if root.is_symlink():
            raise WorkspaceFileCommitUnknownError("Workspace create staging root is unsafe")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceFileCommitUnknownError("Workspace create staging root is unsafe")
        return root / f"{hashlib.sha256(task_id.encode('utf-8')).hexdigest()}.staged"

    def _creation_manifest_path(self, task_id: str) -> Path:
        return self._creation_staging_path(task_id).with_suffix(".manifest.json")

    @staticmethod
    def _creation_manifest(preview: WorkspacePathOperationPreview) -> bytes:
        material = preview.model_dump(mode="json", exclude={"content"})
        return canonical_json_bytes(
            {
                "schema_version": "deskpilot.workspace-create-staging.v1",
                "preview": material,
                "manifest_digest": sha256_digest(material),
            }
        )

    def _ensure_creation_manifest(self, preview: WorkspacePathOperationPreview) -> None:
        path = self._creation_manifest_path(preview.task_id)
        expected = self._creation_manifest(preview)
        try:
            self._write_exclusive(path, expected)
        except FileExistsError:
            self._verify_creation_manifest(preview)

    def _verify_creation_manifest(self, preview: WorkspacePathOperationPreview) -> None:
        path = self._creation_manifest_path(preview.task_id)
        if path.is_symlink() or not path.is_file():
            raise WorkspaceFileCommitUnknownError(
                "Workspace create staging manifest is missing or unsafe"
            )
        try:
            actual = path.read_bytes()
        except OSError as error:
            raise WorkspaceFileCommitUnknownError(
                "Workspace create staging manifest cannot be read"
            ) from error
        if actual != self._creation_manifest(preview):
            raise WorkspaceFileCommitUnknownError("Workspace create staging manifest changed")

    def _preflight_replace(self, preview: WorkspaceEditPreview) -> WorkspaceEditReceipt | None:
        current = self._read_material(preview.relative_path)
        _, backup = self._replacement_paths(current.path, preview.task_id)
        if backup.exists():
            return self._reconcile(preview, current, backup)
        self._validate_proposed_replacement(preview, current)
        return None

    @staticmethod
    def _edit_preview_from_patch(
        task_id: str,
        item: WorkspacePatchChangePreview,
        *,
        patch_identity: str | None,
    ) -> WorkspaceEditPreview:
        child_identity: dict[str, str | int] = {
            "patch_task_id": task_id,
            "change_index": item.index,
        }
        if patch_identity is not None:
            child_identity["patch_manifest_digest"] = patch_identity
        child_task_id = "tsk_" + sha256_digest(child_identity)
        confirmation = workspace_edit_confirmation_digest(
            task_id=child_task_id,
            relative_path=item.relative_path,
            expected_version_digest=item.expected_version_digest,
            proposed_content_digest=item.proposed_content_digest,
            byte_count=item.byte_count,
        )
        return WorkspaceEditPreview(
            task_id=child_task_id,
            relative_path=item.relative_path,
            expected_version_digest=item.expected_version_digest,
            proposed_content_digest=item.proposed_content_digest,
            byte_count=item.byte_count,
            old_text=item.old_text,
            new_text=item.new_text,
            confirmation_digest=confirmation,
        )

    @staticmethod
    def _patch_receipt(
        preview: WorkspacePatchPreview,
        status: str,
        receipts: list[WorkspaceEditReceipt],
        *,
        failed_path: str | None = None,
        error_code: str | None = None,
    ) -> WorkspacePatchReceipt:
        committed_at = max(item.committed_at for item in receipts)
        result = {
            "schema_version": "deskpilot.workspace-patch-receipt.v1",
            "task_id": preview.task_id,
            "status": status,
            "confirmation_digest": preview.confirmation_digest,
            "change_receipts": [item.model_dump(mode="json") for item in receipts],
            "failed_path": failed_path,
            "error_code": error_code,
            "committed_at": committed_at,
        }
        return WorkspacePatchReceipt.model_validate(
            {**result, "receipt_digest": sha256_digest(result)}
        )

    def _stage_patch(
        self,
        staging_workspace_ref: str,
        manifest: dict[str, object],
        manifest_digest: str,
        staged: list[tuple[str, bytes, bytes]],
    ) -> None:
        if self._staging_root is None:
            raise WorkspaceFileDisabledError("Workspace patch staging is not configured")
        patch_root = self._staging_root / ".deskpilot-workspace-patches"
        if patch_root.is_symlink():
            raise WorkspaceFileCommitUnknownError("Workspace patch staging root is unsafe")
        patch_root.mkdir(parents=True, exist_ok=True)
        if patch_root.is_symlink() or not patch_root.is_dir():
            raise WorkspaceFileCommitUnknownError("Workspace patch staging root is unsafe")
        final = self._staging_root.joinpath(*PurePath(staging_workspace_ref).parts)
        if final.exists():
            self._verify_staged_files(final, manifest, manifest_digest, staged)
            return
        temporary = Path(tempfile.mkdtemp(prefix=".workspace-patch-", dir=patch_root))
        try:
            for relative_path, before, after in staged:
                parts = PurePath(relative_path).parts
                before_path = temporary.joinpath("before", *parts)
                after_path = temporary.joinpath("after", *parts)
                before_path.parent.mkdir(parents=True, exist_ok=True)
                after_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_exclusive(before_path, before)
                self._write_exclusive(after_path, after)
            self._write_exclusive(
                temporary / "manifest.json",
                canonical_json_bytes({**manifest, "manifest_digest": manifest_digest}),
            )
            os.replace(temporary, final)
        except Exception:
            resolved = temporary.resolve(strict=False)
            if resolved.is_relative_to(patch_root.resolve(strict=True)):
                shutil.rmtree(resolved, ignore_errors=True)
            raise
        self._verify_staged_files(final, manifest, manifest_digest, staged)

    def _verify_patch_staging(self, preview: WorkspacePatchPreview) -> None:
        if self._staging_root is None:
            raise WorkspaceFileDisabledError("Workspace patch staging is not configured")
        legacy_ref = (
            ".deskpilot-workspace-patches/"
            + hashlib.sha256(preview.task_id.encode("utf-8")).hexdigest()
        )
        expected_ref = (
            ".deskpilot-workspace-patches/"
            + sha256_digest(
                {
                    "task_id": preview.task_id,
                    "manifest_digest": preview.manifest_digest,
                }
            )
        )
        if preview.staging_workspace_ref not in {legacy_ref, expected_ref}:
            raise WorkspaceFileCommitUnknownError("Workspace patch staging reference changed")
        staged: list[tuple[str, bytes, bytes]] = []
        for item in preview.changes:
            _, normalized = self._resolve_file(item.relative_path)
            if normalized != item.relative_path:
                raise WorkspaceFileCommitUnknownError("Workspace patch file path changed")
            before = self._read_staged_bytes(
                preview.staging_workspace_ref, "before", item.relative_path
            )
            try:
                before_text = before.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorkspaceFileCommitUnknownError(
                    "Workspace patch staging file is not UTF-8"
                ) from error
            after = before_text.replace(item.old_text, item.new_text, 1).encode("utf-8")
            if (
                before_text.count(item.old_text) != 1
                or hashlib.sha256(before).hexdigest() != item.original_content_digest
                or hashlib.sha256(after).hexdigest() != item.proposed_content_digest
                or len(after) != item.byte_count
            ):
                raise WorkspaceFileCommitUnknownError("Workspace patch staging proof changed")
            staged.append((item.relative_path, before, after))
        manifest = {
            "schema_version": "deskpilot.workspace-patch-staging.v1",
            "task_id": preview.task_id,
            "changes": [item.model_dump(mode="json") for item in preview.changes],
            "total_byte_count": preview.total_byte_count,
        }
        final = self._staging_root.joinpath(*PurePath(preview.staging_workspace_ref).parts)
        self._verify_staged_files(final, manifest, preview.manifest_digest, staged)

    def _read_staged_bytes(self, workspace_ref: str, side: str, relative_path: str) -> bytes:
        if self._staging_root is None:
            raise WorkspaceFileDisabledError("Workspace patch staging is not configured")
        boundary = self._staging_root.joinpath(*PurePath(workspace_ref).parts)
        path = boundary.joinpath(side, *PurePath(relative_path).parts)
        return self._read_staging_path(path, boundary)

    def _verify_staged_files(
        self,
        directory: Path,
        manifest: dict[str, object],
        manifest_digest: str,
        staged: list[tuple[str, bytes, bytes]],
    ) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise WorkspaceFileCommitUnknownError("Workspace patch staging is missing or unsafe")
        expected_manifest = canonical_json_bytes({**manifest, "manifest_digest": manifest_digest})
        manifest_path = directory / "manifest.json"
        if (
            manifest_digest != sha256_digest(manifest)
            or self._read_staging_path(manifest_path, directory) != expected_manifest
        ):
            raise WorkspaceFileCommitUnknownError("Workspace patch manifest changed")
        for relative_path, before, after in staged:
            parts = PurePath(relative_path).parts
            before_path = directory.joinpath("before", *parts)
            after_path = directory.joinpath("after", *parts)
            try:
                matches = (
                    self._read_staging_path(before_path, directory) == before
                    and self._read_staging_path(after_path, directory) == after
                )
            except OSError as error:
                raise WorkspaceFileCommitUnknownError(
                    "Workspace patch staging file is missing"
                ) from error
            if not matches:
                raise WorkspaceFileCommitUnknownError("Workspace patch staging content changed")

    def _read_staging_path(self, path: Path, boundary: Path) -> bytes:
        self._reject_staging_symlinks(path, boundary)
        if not path.is_file():
            raise WorkspaceFileCommitUnknownError("Workspace patch staging file is unsafe")
        try:
            return self._read_absolute_material(path, path.name).encoded
        except WorkspaceFileError as error:
            raise WorkspaceFileCommitUnknownError(
                "Workspace patch staging file cannot be verified"
            ) from error

    @staticmethod
    def _reject_staging_symlinks(path: Path, boundary: Path) -> None:
        cursor = path
        while cursor != boundary:
            if cursor.is_symlink():
                raise WorkspaceFileCommitUnknownError("Workspace patch staging link is unsafe")
            cursor = cursor.parent

    @staticmethod
    def _write_exclusive(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _reconcile(
        self,
        preview: WorkspaceEditPreview,
        current: _Material,
        backup: Path,
    ) -> WorkspaceEditReceipt:
        if not backup.is_file() or backup.is_symlink():
            raise WorkspaceFileCommitUnknownError(
                "Workspace replacement backup is missing or unsafe"
            )
        backup_material = self._read_absolute_material(backup, backup.name)
        if (
            current.content_digest != preview.proposed_content_digest
            or backup_material.version_digest != preview.expected_version_digest
        ):
            raise WorkspaceFileCommitUnknownError(
                "Workspace replacement outcome is ambiguous; the safety backup was retained"
            )
        committed_at = datetime.fromtimestamp(current.path.stat().st_mtime, tz=UTC)
        backup_relative = self._relative(backup)
        result = {
            "schema_version": "deskpilot.workspace-edit-receipt.v1",
            "task_id": preview.task_id,
            "relative_path": preview.relative_path,
            "confirmation_digest": preview.confirmation_digest,
            "previous_version_digest": preview.expected_version_digest,
            "version_digest": current.version_digest,
            "content_digest": current.content_digest,
            "backup_relative_path": backup_relative,
            "byte_count": len(current.encoded),
            "committed_at": committed_at,
        }
        return WorkspaceEditReceipt.model_validate(
            {**result, "receipt_digest": sha256_digest(result)}
        )

    @staticmethod
    def _replacement_paths(target: Path, task_id: str) -> tuple[Path, Path]:
        suffix = task_id[-16:].replace("_", "-")
        return (
            target.with_name(f".{target.name}.deskpilot-{suffix}.replacement"),
            target.with_name(f".{target.name}.deskpilot-{suffix}.backup"),
        )

    @staticmethod
    def _validate_proposed_replacement(preview: WorkspaceEditPreview, current: _Material) -> bytes:
        if current.version_digest != preview.expected_version_digest:
            raise WorkspaceFileConflictError(
                "Workspace file changed after preview; prepare a new replacement"
            )
        if current.content.count(preview.old_text) != 1:
            raise WorkspaceFileConflictError(
                "Workspace file no longer has exactly one approved source occurrence"
            )
        encoded = current.content.replace(preview.old_text, preview.new_text, 1).encode("utf-8")
        if (
            len(encoded) != preview.byte_count
            or hashlib.sha256(encoded).hexdigest() != preview.proposed_content_digest
        ):
            raise WorkspaceFileConflictError("Workspace replacement preview proof changed")
        return encoded

    def _read_material(self, relative_path: str) -> _Material:
        path, normalized = self._resolve_file(relative_path)
        return self._read_absolute_material(path, normalized)

    def _read_absolute_material(self, path: Path, relative_path: str) -> _Material:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise WorkspaceFilePathRejectedError("Workspace file cannot be opened") from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise WorkspaceFilePathRejectedError("Workspace path is not a regular file")
            if before.st_size > MAX_FILE_BYTES:
                raise WorkspaceFilePathRejectedError("Workspace file exceeds the size limit")
            chunks: list[bytes] = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        identity_before = self._stat_identity(before)
        if identity_before != self._stat_identity(after) or len(encoded) != after.st_size:
            raise WorkspaceFileConflictError("Workspace file changed while it was read")
        if len(encoded) > MAX_FILE_BYTES:
            raise WorkspaceFilePathRejectedError("Workspace file exceeds the size limit")
        try:
            content = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceFilePathRejectedError("Workspace file must be UTF-8 text") from error
        content_digest = hashlib.sha256(encoded).hexdigest()
        version_digest = sha256_digest(
            {"identity": identity_before, "content_sha256": content_digest}
        )
        return _Material(
            path=path,
            relative_path=relative_path,
            content=content,
            encoded=encoded,
            content_digest=content_digest,
            version_digest=version_digest,
        )

    def _check_paths(self, root: Path, suffix: str) -> list[Path]:
        result: list[Path] = []
        scanned = 0

        def visit(directory: Path, depth: int) -> None:
            nonlocal scanned
            if depth > 8:
                raise WorkspaceFilePathRejectedError(
                    "Workspace check directory depth exceeds the limit"
                )
            before = directory.stat(follow_symlinks=False)
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError as error:
                raise WorkspaceFilePathRejectedError(
                    "Workspace check directory cannot be opened"
                ) from error
            for child in children:
                scanned += 1
                if scanned > MAX_DIRECTORY_SCAN:
                    raise WorkspaceFilePathRejectedError(
                        "Workspace check directory scan exceeds the limit"
                    )
                if child.name.startswith(".") or child.name == "__pycache__":
                    continue
                child_path = Path(child.path)
                child_stat = child_path.stat(follow_symlinks=False)
                if self._is_reparse_point(child_path, child_stat):
                    raise WorkspaceFilePathRejectedError(
                        "Workspace checks reject links and reparse points"
                    )
                if stat.S_ISDIR(child_stat.st_mode):
                    visit(child_path, depth + 1)
                elif stat.S_ISREG(child_stat.st_mode) and child_path.suffix.casefold() == suffix:
                    result.append(child_path)
                    if len(result) > MAX_CHECK_FILES:
                        raise WorkspaceFilePathRejectedError(
                            "Workspace check exceeds the file limit"
                        )
            after = directory.stat(follow_symlinks=False)
            if self._stat_identity(before) != self._stat_identity(after):
                raise WorkspaceFileConflictError(
                    "Workspace check directory changed during snapshotting"
                )

        visit(root, 0)
        if not result:
            raise WorkspaceFilePathRejectedError(
                "Workspace check target contains no matching files"
            )
        return result

    def _python_test_paths(self, root: Path) -> list[Path]:
        result: list[Path] = []
        scanned = 0

        def visit(directory: Path, depth: int) -> None:
            nonlocal scanned
            if depth > 12:
                raise WorkspaceFilePathRejectedError(
                    "Python test snapshot directory depth exceeds the limit"
                )
            before = directory.stat(follow_symlinks=False)
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name.casefold())
            except OSError as error:
                raise WorkspaceFilePathRejectedError(
                    "Python test snapshot directory cannot be opened"
                ) from error
            for child in children:
                scanned += 1
                if scanned > MAX_TEST_SCAN:
                    raise WorkspaceFilePathRejectedError(
                        "Python test snapshot scan exceeds the limit"
                    )
                folded_name = child.name.casefold()
                if child.name.startswith(".") or folded_name in TEST_EXCLUDED_DIRECTORIES:
                    continue
                child_path = Path(child.path)
                child_stat = child_path.stat(follow_symlinks=False)
                if self._is_reparse_point(child_path, child_stat):
                    raise WorkspaceFilePathRejectedError(
                        "Python test snapshots reject links and reparse points"
                    )
                if stat.S_ISDIR(child_stat.st_mode):
                    visit(child_path, depth + 1)
                elif (
                    stat.S_ISREG(child_stat.st_mode)
                    and child_path.suffix.casefold() in TEST_SNAPSHOT_SUFFIXES
                ):
                    result.append(child_path)
                    if len(result) > MAX_TEST_FILES:
                        raise WorkspaceFilePathRejectedError(
                            "Python test snapshot exceeds the file limit"
                        )
            after = directory.stat(follow_symlinks=False)
            if self._stat_identity(before) != self._stat_identity(after):
                raise WorkspaceFileConflictError(
                    "Python test directory changed during snapshotting"
                )

        visit(root, 0)
        if not result:
            raise WorkspaceFilePathRejectedError("Python test snapshot is empty")
        return result

    def _node_test_paths(self, root: Path) -> list[Path]:
        result: list[Path] = []
        scanned = 0

        def visit(directory: Path, depth: int) -> None:
            nonlocal scanned
            if depth > 12:
                raise WorkspaceFilePathRejectedError(
                    "Node test snapshot directory depth exceeds the limit"
                )
            before = directory.stat(follow_symlinks=False)
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name.casefold())
            for child in children:
                scanned += 1
                if scanned > MAX_TEST_SCAN:
                    raise WorkspaceFilePathRejectedError(
                        "Node test snapshot scan exceeds the limit"
                    )
                folded_name = child.name.casefold()
                if child.name.startswith(".") or folded_name in NODE_TEST_EXCLUDED_DIRECTORIES:
                    continue
                child_path = Path(child.path)
                child_stat = child_path.stat(follow_symlinks=False)
                if self._is_reparse_point(child_path, child_stat):
                    raise WorkspaceFilePathRejectedError(
                        "Node test snapshots reject links and reparse points"
                    )
                if stat.S_ISDIR(child_stat.st_mode):
                    visit(child_path, depth + 1)
                elif (
                    stat.S_ISREG(child_stat.st_mode)
                    and child_path.suffix.casefold() in NODE_TEST_SNAPSHOT_SUFFIXES
                ):
                    result.append(child_path)
                    if len(result) > MAX_NODE_TEST_FILES:
                        raise WorkspaceFilePathRejectedError(
                            "Node test snapshot exceeds the file limit"
                        )
            after = directory.stat(follow_symlinks=False)
            if self._stat_identity(before) != self._stat_identity(after):
                raise WorkspaceFileConflictError("Node test directory changed during snapshotting")

        try:
            visit(root, 0)
        except OSError as error:
            raise WorkspaceFileConflictError(
                "Node test directory changed during snapshotting"
            ) from error
        if not result:
            raise WorkspaceFilePathRejectedError("Node test snapshot is empty")
        return result

    def _resolve_existing(self, relative_path: str) -> tuple[Path, str]:
        if self._root is None:
            raise WorkspaceFileDisabledError("Conversation workspace is not configured")
        raw = relative_path.strip().replace("\\", "/")
        if raw == ".":
            return self._root, "."
        pure = PurePath(raw)
        if (
            not raw
            or pure.is_absolute()
            or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
        ):
            raise WorkspaceFilePathRejectedError("Workspace path must stay beneath its root")
        candidate = self._root.joinpath(*pure.parts)
        cursor = candidate
        while cursor != self._root:
            try:
                cursor_stat = cursor.stat(follow_symlinks=False)
            except OSError as error:
                raise WorkspaceFilePathRejectedError(
                    "Workspace path must exist beneath the configured root"
                ) from error
            if self._is_reparse_point(cursor, cursor_stat):
                raise WorkspaceFilePathRejectedError(
                    "Workspace links and reparse points are not allowed"
                )
            cursor = cursor.parent
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise WorkspaceFilePathRejectedError(
                "Workspace path must exist beneath the configured root"
            ) from error
        return resolved, self._relative(resolved)

    def _resolve_target(self, relative_path: str) -> tuple[Path, str, str, os.stat_result | None]:
        if self._root is None:
            raise WorkspaceFileDisabledError("Conversation workspace is not configured")
        raw = relative_path.strip().replace("\\", "/")
        pure = PurePath(raw)
        if (
            not raw
            or pure.is_absolute()
            or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
            or Path(pure.name).suffix.casefold() not in ALLOWED_SUFFIXES
        ):
            raise WorkspaceFilePathRejectedError(
                "Workspace target must be an allowed file beneath its root"
            )
        parent_raw = PurePath(*pure.parts[:-1]).as_posix() if len(pure.parts) > 1 else "."
        parent, normalized_parent = self._resolve_directory(parent_raw)
        target = parent / pure.name
        try:
            target_stat = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            target_stat = None
        except OSError as error:
            raise WorkspaceFilePathRejectedError("Workspace target cannot be inspected") from error
        if target_stat is not None and self._is_reparse_point(target, target_stat):
            raise WorkspaceFilePathRejectedError(
                "Workspace links and reparse points are not allowed"
            )
        normalized = pure.name if normalized_parent == "." else f"{normalized_parent}/{pure.name}"
        return target, normalized, self._directory_version_digest(parent), target_stat

    def _resolve_directory(self, relative_path: str) -> tuple[Path, str]:
        resolved, normalized = self._resolve_existing(relative_path)
        if not resolved.is_dir():
            raise WorkspaceFilePathRejectedError("Workspace path is not a directory")
        return resolved, normalized

    def _resolve_file(self, relative_path: str) -> tuple[Path, str]:
        resolved, normalized = self._resolve_existing(relative_path)
        if resolved.suffix.casefold() not in ALLOWED_SUFFIXES:
            raise WorkspaceFilePathRejectedError("Workspace file type is not allowed")
        if not resolved.is_file():
            raise WorkspaceFilePathRejectedError("Workspace path is not a file")
        return resolved, normalized

    def _relative(self, path: Path) -> str:
        if self._root is None:
            raise WorkspaceFileDisabledError("Conversation workspace is not configured")
        return path.relative_to(self._root).as_posix()

    def _directory_version_digest(self, path: Path) -> str:
        value = path.stat(follow_symlinks=False)
        if self._is_reparse_point(path, value) or not stat.S_ISDIR(value.st_mode):
            raise WorkspaceFilePathRejectedError("Workspace parent is not a safe directory")
        return sha256_digest(
            {"relative_path": self._relative(path), "identity": self._stat_identity(value)}
        )

    @staticmethod
    def _stat_identity(value: os.stat_result) -> dict[str, int]:
        return {
            "device": value.st_dev,
            "inode": value.st_ino,
            "mode": value.st_mode,
            "size": value.st_size,
            "modified_ns": value.st_mtime_ns,
        }

    @staticmethod
    def _is_reparse_point(path: Path, value: os.stat_result | None = None) -> bool:
        current = value or path.stat(follow_symlinks=False)
        return stat.S_ISLNK(current.st_mode) or bool(
            getattr(current, "st_file_attributes", 0) & 0x00000400
        )

    @staticmethod
    def _stage_replacement(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as error:
            try:
                if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
                    return
            except OSError:
                pass
            raise WorkspaceFileCommitUnknownError(
                "Workspace replacement staging file already exists with different content"
            ) from error
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _replace_file_windows(target: Path, replacement: Path, backup: Path) -> None:
        # ReplaceFileW atomically captures the exact overwritten file as backup; a plain
        # os.replace plus pre-copy leaves a race where an external editor's change is lost.
        replace_file = ctypes.windll.kernel32.ReplaceFileW
        replace_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        replace_file.restype = ctypes.c_int
        if not replace_file(str(target), str(replacement), str(backup), 0x1, None, None):
            raise ctypes.WinError()
