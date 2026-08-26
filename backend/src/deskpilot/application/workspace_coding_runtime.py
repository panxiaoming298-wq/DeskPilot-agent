"""Project-root constrained coding reads and fixed, proof-carrying Git operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path, PurePath
from typing import Literal

from deskpilot.application.workspace_file_runtime import (
    WorkspaceFileConflictError,
    WorkspaceFilePathRejectedError,
    WorkspaceFileRuntime,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.coding_tools import (
    GitCommitPathProof,
    GitCommitPreview,
    GitCommitReceipt,
    GitInspectionRead,
    ProjectBatchRead,
    ProjectSearchMatch,
    ProjectSearchRead,
)
from deskpilot.domain.command_profiles import (
    CommandProfile,
    WorkspaceCommandFile,
    WorkspaceCommandSnapshot,
)

MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_ENTRIES = 10_000
MAX_SEARCH_BYTES = 33_554_432
MAX_SEARCH_DEPTH = 20
MAX_BATCH_FILES = 32
MAX_BATCH_BYTES = 2_097_152
MAX_GIT_OUTPUT_BYTES = 65_536
GIT_TIMEOUT_SECONDS = 15
CODING_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".cjs",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".mts",
    ".py",
    ".pyi",
    ".rs",
    ".scss",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
EXCLUDED_DIRECTORIES = {
    "__pycache__",
    "build",
    "coverage",
    "data",
    "dist",
    "node_modules",
    "target",
}


class WorkspaceCodingError(RuntimeError):
    code = "WORKSPACE_CODING_REJECTED"


class WorkspaceCodingUnavailableError(WorkspaceCodingError):
    code = "WORKSPACE_CODING_UNAVAILABLE"


class WorkspaceGitRejectedError(WorkspaceCodingError):
    code = "WORKSPACE_GIT_REJECTED"


class WorkspaceGitTimeoutError(WorkspaceCodingError):
    code = "WORKSPACE_GIT_TIMEOUT"


class WorkspaceCodingRuntime:
    """Trusted runtime; no caller-provided executable, argv, cwd, or environment."""

    def __init__(
        self,
        workspace: WorkspaceFileRuntime,
        git_executable: str | None = None,
    ) -> None:
        self._workspace = workspace
        candidate = git_executable or shutil.which("git")
        self._git_executable = Path(candidate).resolve(strict=True) if candidate else None

    @property
    def enabled(self) -> bool:
        return self._workspace.enabled

    @property
    def git_enabled(self) -> bool:
        return bool(
            self.enabled
            and self._git_executable is not None
            and self._git_executable.is_file()
            and not self._is_reparse_point(self._git_executable)
        )

    def search(self, project_path: str, query: str) -> ProjectSearchRead:
        project, normalized_project = self._workspace.resolve_project_directory(project_path)
        needle = query.strip()
        if (
            not needle
            or len(needle) > 256
            or any(value in needle for value in ("\x00", "\r", "\n"))
        ):
            raise WorkspaceFilePathRejectedError("Project search query is invalid")
        paths, scanned_entries = self._coding_paths(project)
        del scanned_entries
        matches: list[ProjectSearchMatch] = []
        scanned_bytes = 0
        scanned_files = 0
        truncated = False
        for path in paths:
            material = self._workspace.read_project_material(path)
            scanned_bytes += len(material.encoded)
            if scanned_bytes > MAX_SEARCH_BYTES:
                truncated = True
                break
            scanned_files += 1
            for line_number, line in enumerate(material.content.splitlines(), start=1):
                start = 0
                while True:
                    index = line.find(needle, start)
                    if index < 0:
                        break
                    preview = line[:1_000]
                    matches.append(
                        ProjectSearchMatch(
                            relative_path=material.relative_path,
                            line=line_number,
                            column=index + 1,
                            preview=preview,
                            preview_digest=sha256_digest({"preview": preview}),
                        )
                    )
                    if len(matches) == MAX_SEARCH_MATCHES:
                        truncated = True
                        break
                    start = index + max(1, len(needle))
                if truncated:
                    break
            if truncated:
                break
        matches.sort(key=lambda item: (item.relative_path.casefold(), item.line, item.column))
        values = {
            "schema_version": "deskpilot.project-search-read.v1",
            "project_path": normalized_project,
            "query_digest": sha256_digest({"query": needle}),
            "matches": [item.model_dump(mode="json") for item in matches],
            "scanned_file_count": scanned_files,
            "scanned_byte_count": min(scanned_bytes, MAX_SEARCH_BYTES),
            "truncated": truncated,
        }
        return ProjectSearchRead.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    def read_many(self, project_path: str, relative_paths: tuple[str, ...]) -> ProjectBatchRead:
        project, normalized_project = self._workspace.resolve_project_directory(project_path)
        if not 1 <= len(relative_paths) <= MAX_BATCH_FILES:
            raise WorkspaceFilePathRejectedError("Project batch read file count is invalid")
        files = []
        seen: set[str] = set()
        total_bytes = 0
        for raw in relative_paths:
            pure = self._safe_relative(raw)
            workspace_relative = (
                pure.as_posix()
                if normalized_project == "."
                else f"{normalized_project}/{pure.as_posix()}"
            )
            read = self._workspace.read(workspace_relative)
            target, _ = self._workspace.resolve_project_file(workspace_relative)
            try:
                target.relative_to(project)
            except ValueError as error:
                raise WorkspaceFilePathRejectedError(
                    "Project batch read escaped its project root"
                ) from error
            folded = read.relative_path.casefold()
            if folded in seen:
                raise WorkspaceFilePathRejectedError("Project batch read paths must be unique")
            seen.add(folded)
            total_bytes += read.byte_count
            if total_bytes > MAX_BATCH_BYTES:
                raise WorkspaceFilePathRejectedError("Project batch read exceeds its byte limit")
            files.append(read)
        files.sort(key=lambda item: item.relative_path.casefold())
        values = {
            "schema_version": "deskpilot.project-batch-read.v1",
            "project_path": normalized_project,
            "files": [item.model_dump(mode="json") for item in files],
            "total_byte_count": total_bytes,
        }
        return ProjectBatchRead.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    def prepare_command_snapshot(
        self,
        project_path: str,
        profile: CommandProfile,
    ) -> WorkspaceCommandSnapshot:
        project, normalized_project = self._workspace.resolve_project_directory(project_path)
        paths, _scanned_entries = self._coding_paths(project)
        allowed = (
            {
                ".py",
                ".pyi",
                ".toml",
                ".ini",
                ".cfg",
                ".txt",
                ".json",
                ".yaml",
                ".yml",
            }
            if profile.ecosystem == "python"
            else {
                ".js",
                ".jsx",
                ".mjs",
                ".cjs",
                ".ts",
                ".tsx",
                ".mts",
                ".vue",
                ".json",
                ".yaml",
                ".yml",
                ".css",
                ".scss",
                ".html",
            }
        )
        files: list[WorkspaceCommandFile] = []
        total_bytes = 0
        for path in paths:
            relative_path = path.relative_to(project).as_posix()
            if path.suffix.casefold() not in allowed or (
                profile.ecosystem == "python"
                and relative_path.casefold()
                in {
                    "package.json",
                    "package-lock.json",
                    "pnpm-lock.yaml",
                    "pnpm-workspace.yaml",
                    "yarn.lock",
                }
            ):
                continue
            material = self._workspace.read_project_material(path)
            total_bytes += len(material.encoded)
            if total_bytes > MAX_SEARCH_BYTES:
                raise WorkspaceFilePathRejectedError(
                    "Workspace command snapshot exceeds its byte limit"
                )
            files.append(
                WorkspaceCommandFile(
                    relative_path=relative_path,
                    content=material.content,
                    byte_count=len(material.encoded),
                    content_digest=material.content_digest,
                    version_digest=material.version_digest,
                )
            )
        if not files:
            raise WorkspaceFilePathRejectedError(
                "Workspace command snapshot contains no supported project files"
            )
        files.sort(key=lambda item: item.relative_path.casefold())
        if profile.ecosystem == "node":
            by_path = {item.relative_path.casefold(): item for item in files}
            package_file = by_path.get("package.json")
            if package_file is None or "pnpm-lock.yaml" not in by_path:
                raise WorkspaceFilePathRejectedError(
                    "Node Command Profiles require package.json and pnpm-lock.yaml"
                )
            try:
                package = json.loads(package_file.content)
            except json.JSONDecodeError as error:
                raise WorkspaceFilePathRejectedError(
                    "Node Command Profile package.json is invalid"
                ) from error
            script_name = {
                "node.pnpm_test.v1": "test",
                "node.pnpm_typecheck.v1": "type-check",
                "node.pnpm_build.v1": "build",
            }.get(profile.command_profile_id)
            scripts = package.get("scripts") if isinstance(package, dict) else None
            script = scripts.get(script_name) if isinstance(scripts, dict) else None
            if not isinstance(script, str) or not script.strip() or len(script) > 2_048:
                raise WorkspaceFilePathRejectedError(
                    "Node Command Profile requires its fixed package script"
                )
        return WorkspaceCommandSnapshot.build(
            command_profile=profile,
            project_path=normalized_project,
            files=tuple(files),
            total_byte_count=total_bytes,
        )

    def inspect_git(
        self,
        project_path: str,
        operation: Literal["status", "diff", "log"],
    ) -> GitInspectionRead:
        if not self.git_enabled or self._git_executable is None:
            raise WorkspaceCodingUnavailableError("Git inspection runtime is unavailable")
        project, normalized_project = self._workspace.resolve_project_directory(project_path)
        git_dir = project / ".git"
        self._validate_git_directory(git_dir)
        base = (
            str(self._git_executable),
            "--no-pager",
            "-c",
            "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
            "-c",
            "core.attributesFile=NUL"
            if os.name == "nt"
            else "core.attributesFile=/dev/null",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-c",
            "core.pager=cat",
            "-c",
            "pager.status=false",
            "-c",
            "pager.diff=false",
            "-c",
            "pager.log=false",
            "-C",
            str(project),
        )
        commands: dict[str, tuple[str, ...]] = {
            "status": ("status", "--short", "--branch", "--untracked-files=normal"),
            "diff": (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--",
                ".",
            ),
            "log": (
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "-n",
                "20",
                "--date=iso-strict",
                "--pretty=format:%H%x09%aI%x09%an%x09%s",
                "--",
                ".",
            ),
        }
        if operation not in commands:
            raise WorkspaceGitRejectedError("Git inspection operation is not registered")
        environment = self._git_environment()
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                completed = subprocess.run(  # noqa: S603 - exact server-owned command
                    (*base, *commands[operation]),
                    cwd=project,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=GIT_TIMEOUT_SECONDS,
                    close_fds=True,
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                    ),
                )
            except subprocess.TimeoutExpired as error:
                raise WorkspaceGitTimeoutError("Git inspection exceeded its time limit") from error
            stdout.seek(0)
            stderr.seek(0)
            raw = stdout.read(MAX_GIT_OUTPUT_BYTES + 1)
            error_output = stderr.read(4_097)
        if completed.returncode != 0:
            raise WorkspaceGitRejectedError(
                self._safe_error(error_output, project) or "Git inspection failed"
            )
        output_truncated = len(raw) > MAX_GIT_OUTPUT_BYTES
        output = raw[:MAX_GIT_OUTPUT_BYTES].decode("utf-8", errors="replace")
        head_oid = self._head_oid(base, project, environment)
        repository_digest = sha256_digest(
            {
                "project_path": normalized_project,
                "git_directory_identity": self._stat_identity(
                    git_dir.stat(follow_symlinks=False)
                ),
                "head_oid": head_oid,
            }
        )
        toolchain_digest = self._file_digest(self._git_executable)
        values = {
            "schema_version": "deskpilot.git-inspection-read.v1",
            "operation": operation,
            "project_path": normalized_project,
            "repository_digest": repository_digest,
            "toolchain_digest": toolchain_digest,
            "head_oid": head_oid,
            "exit_code": 0,
            "output": output,
            "output_digest": sha256_digest({"output": output}),
            "output_truncated": output_truncated,
            "hooks_disabled": True,
            "external_diff_disabled": True,
            "textconv_disabled": True,
            "pager_disabled": True,
            "optional_locks_disabled": True,
        }
        return GitInspectionRead.model_validate(
            {**values, "result_digest": sha256_digest(values)}
        )

    def prepare_git_commit(
        self,
        *,
        task_id: str,
        project_path: str,
        paths: tuple[str, ...],
    ) -> GitCommitPreview:
        """Seal one clean, exact worktree delta without changing repository state."""

        project, normalized_project, base, environment = self._git_scope(project_path)
        git_executable = self._git_executable
        if git_executable is None:  # pragma: no cover - narrowed by _git_scope.
            raise WorkspaceCodingUnavailableError("Git runtime is unavailable")
        relative_paths = self._git_relative_paths(normalized_project, paths)
        head_oid = self._required_head_oid(base, project, environment)
        original_branch = self._current_branch(base, project, environment)
        target_branch = f"codex/deskpilot-{task_id.removeprefix('tsk_')[:16]}"
        if original_branch == target_branch:
            raise WorkspaceGitRejectedError("Git commit is already on its target branch")
        target_exists = self._run_git(
            base,
            project,
            environment,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}"),
            allowed_returncodes=(0, 1),
        )
        if target_exists.returncode == 0:
            raise WorkspaceGitRejectedError("Git commit target branch already exists")
        status = self._git_status(base, project, environment)
        backup_paths = self._git_backup_paths(status, relative_paths)
        expected_status = {
            **{path: " M" for path in relative_paths},
            **{path: "??" for path in backup_paths},
        }
        if status != expected_status:
            raise WorkspaceGitRejectedError(
                "Git commit requires only the exact unstaged Task files to be modified"
            )
        proofs = tuple(self._git_path_proof(project, path) for path in relative_paths)
        backup_proofs = tuple(
            self._git_path_proof(project, path) for path in backup_paths
        )
        repository_digest = self._repository_digest(
            project,
            normalized_project,
            head_oid,
        )
        values = {
            "schema_version": "deskpilot.git-commit-preview.v1",
            "task_id": task_id,
            "project_path": normalized_project,
            "expected_repository_digest": repository_digest,
            "toolchain_digest": self._file_digest(git_executable),
            "expected_head_oid": head_oid,
            "original_branch": original_branch,
            "target_branch": target_branch,
            "commit_message": f"完成 DeskPilot 任务 {task_id.removeprefix('tsk_')[:12]}",
            "paths": [item.model_dump(mode="json") for item in proofs],
            "excluded_backups": [
                item.model_dump(mode="json") for item in backup_proofs
            ],
            "hooks_disabled": True,
            "signing_disabled": True,
            "push_disabled": True,
        }
        return GitCommitPreview.model_validate(
            {**values, "confirmation_digest": sha256_digest(values)}
        )

    def commit_git(self, preview: GitCommitPreview) -> GitCommitReceipt:
        """Create or exactly reconcile the server-owned branch and commit."""

        project, normalized_project, base, environment = self._git_scope(
            preview.project_path
        )
        git_executable = self._git_executable
        if git_executable is None:  # pragma: no cover - narrowed by _git_scope.
            raise WorkspaceCodingUnavailableError("Git runtime is unavailable")
        if (
            normalized_project != preview.project_path
            or self._file_digest(git_executable) != preview.toolchain_digest
        ):
            raise WorkspaceGitRejectedError("Git commit toolchain or project scope changed")
        current_head = self._required_head_oid(base, project, environment)
        current_branch = self._current_branch(base, project, environment)
        if current_head != preview.expected_head_oid:
            if current_branch != preview.target_branch:
                raise WorkspaceGitRejectedError("Git HEAD changed outside the approved commit")
            return self._git_commit_receipt(
                preview,
                project,
                base,
                environment,
                current_head,
            )
        expected_repository_digest = self._repository_digest(
            project,
            normalized_project,
            current_head,
        )
        if expected_repository_digest != preview.expected_repository_digest:
            raise WorkspaceGitRejectedError("Git repository identity changed after approval")
        expected_paths = tuple(item.relative_path for item in preview.paths)
        for item in preview.paths:
            if self._git_path_proof(project, item.relative_path) != item:
                raise WorkspaceGitRejectedError("Git commit file content changed after approval")
        for item in preview.excluded_backups:
            if self._git_path_proof(project, item.relative_path) != item:
                raise WorkspaceGitRejectedError("Git rollback backup changed after approval")
        status = self._git_status(base, project, environment)
        backup_status = {
            item.relative_path: "??" for item in preview.excluded_backups
        }
        unstaged = {**{path: " M" for path in expected_paths}, **backup_status}
        staged = {**{path: "M " for path in expected_paths}, **backup_status}
        if current_branch == preview.original_branch:
            if status != unstaged:
                raise WorkspaceGitRejectedError("Git worktree changed after approval")
            target_exists = self._run_git(
                base,
                project,
                environment,
                (
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{preview.target_branch}",
                ),
                allowed_returncodes=(0, 1),
            )
            if target_exists.returncode == 0:
                raise WorkspaceGitRejectedError("Git target branch appeared after approval")
            self._run_git(
                base,
                project,
                environment,
                ("switch", "--no-guess", "--create", preview.target_branch),
            )
            current_branch = preview.target_branch
        if current_branch != preview.target_branch or (
            status != unstaged and status != staged
        ):
            raise WorkspaceGitRejectedError("Git commit partial state is not reconcilable")
        if status == unstaged:
            self._run_git(
                base,
                project,
                environment,
                ("add", "--", *expected_paths),
            )
            if self._git_status(base, project, environment) != staged:
                raise WorkspaceGitRejectedError("Git index did not stage the exact approved files")
        commit_environment = dict(environment)
        commit_environment.update(
            {
                "GIT_AUTHOR_NAME": "DeskPilot",
                "GIT_AUTHOR_EMAIL": "deskpilot@localhost.invalid",
                "GIT_COMMITTER_NAME": "DeskPilot",
                "GIT_COMMITTER_EMAIL": "deskpilot@localhost.invalid",
            }
        )
        self._run_git(
            base,
            project,
            commit_environment,
            (
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "--cleanup=verbatim",
                "-m",
                preview.commit_message,
            ),
        )
        commit_oid = self._required_head_oid(base, project, environment)
        return self._git_commit_receipt(
            preview,
            project,
            base,
            environment,
            commit_oid,
        )

    def _git_scope(
        self,
        project_path: str,
    ) -> tuple[Path, str, tuple[str, ...], dict[str, str]]:
        if not self.git_enabled or self._git_executable is None:
            raise WorkspaceCodingUnavailableError("Git runtime is unavailable")
        project, normalized_project = self._workspace.resolve_project_directory(project_path)
        git_dir = project / ".git"
        self._validate_git_directory(git_dir)
        base = (
            str(self._git_executable),
            "--no-pager",
            "-c",
            "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
            "-c",
            "core.attributesFile=NUL"
            if os.name == "nt"
            else "core.attributesFile=/dev/null",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-c",
            "core.pager=cat",
            "-c",
            "pager.status=false",
            "-c",
            "pager.diff=false",
            "-c",
            "pager.log=false",
            "-C",
            str(project),
        )
        environment = self._git_environment()
        scope = self._run_git(
            base,
            project,
            environment,
            (
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
                "--show-object-format",
            ),
        )
        try:
            lines = scope.stdout.decode("utf-8").splitlines()
            top_level = Path(lines[0]).resolve(strict=True)
            resolved_git_dir = Path(lines[1]).resolve(strict=True)
            common_dir = Path(lines[2]).resolve(strict=True)
            object_format = lines[3].strip()
        except (IndexError, OSError, UnicodeDecodeError) as error:
            raise WorkspaceGitRejectedError("Git repository scope is invalid") from error
        if (
            top_level != project
            or resolved_git_dir != git_dir
            or common_dir != git_dir
            or object_format not in {"sha1", "sha256"}
        ):
            raise WorkspaceGitRejectedError(
                "Git worktree, common directory, or object store escaped the project"
            )
        if (git_dir / "info" / "attributes").exists():
            raise WorkspaceGitRejectedError("Git repository-local attributes are rejected")
        tracked_attributes = self._run_git(
            base,
            project,
            environment,
            (
                "ls-files",
                "-z",
                "--",
                ".gitattributes",
                ":(glob)**/.gitattributes",
            ),
        )
        if tracked_attributes.stdout:
            raise WorkspaceGitRejectedError("Git attributes and content filters are rejected")
        return project, normalized_project, base, environment

    def _git_relative_paths(
        self,
        normalized_project: str,
        paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not 2 <= len(paths) <= 8:
            raise WorkspaceGitRejectedError("Git commit requires 2 to 8 exact paths")
        project_parts = PurePath(normalized_project).parts
        resolved: list[str] = []
        for raw in paths:
            safe = self._safe_relative(raw)
            parts = safe.parts
            if parts[: len(project_parts)] == project_parts:
                parts = parts[len(project_parts) :]
            if not parts:
                raise WorkspaceGitRejectedError("Git commit path resolves to the project root")
            relative = PurePath(*parts).as_posix()
            if (
                any(value in relative for value in ("\x00", "\r", "\n", ":"))
                or Path(relative).suffix.casefold() not in CODING_SUFFIXES
            ):
                raise WorkspaceGitRejectedError("Git commit path type is not allowed")
            resolved.append(relative)
        normalized = tuple(sorted(resolved, key=str.casefold))
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise WorkspaceGitRejectedError("Git commit paths must be unique")
        return normalized

    def _git_status(
        self,
        base: tuple[str, ...],
        project: Path,
        environment: dict[str, str],
    ) -> dict[str, str]:
        result = self._run_git(
            base,
            project,
            environment,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        )
        entries = result.stdout.split(b"\x00")
        status: dict[str, str] = {}
        for raw in entries:
            if not raw:
                continue
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorkspaceGitRejectedError("Git status path is not UTF-8") from error
            if len(decoded) < 4 or decoded[2] != " ":
                raise WorkspaceGitRejectedError("Git status output is not canonical")
            state, path = decoded[:2], decoded[3:].replace("\\", "/")
            if state[0] in {"R", "C"} or state[1] in {"R", "C"} or path in status:
                raise WorkspaceGitRejectedError("Git rename/copy status is not supported")
            status[path] = state
        return status

    @staticmethod
    def _git_backup_paths(
        status: dict[str, str],
        expected_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        backups: list[str] = []
        for expected in expected_paths:
            pure = PurePath(expected)
            prefix = (
                PurePath(*pure.parts[:-1]).as_posix() + "/"
                if len(pure.parts) > 1
                else ""
            )
            pattern = re.compile(
                rf"^{re.escape(prefix)}\.{re.escape(pure.name)}\.deskpilot-"
                r"[0-9a-f]{16}\.backup$"
            )
            matches = tuple(
                path
                for path, state in status.items()
                if state == "??" and pattern.fullmatch(path)
            )
            if len(matches) != 1:
                raise WorkspaceGitRejectedError(
                    "Git commit requires one exact rollback backup per Task file"
                )
            backups.append(matches[0])
        normalized = tuple(sorted(backups, key=str.casefold))
        expected_status_paths = set(expected_paths) | set(normalized)
        if set(status) != expected_status_paths:
            raise WorkspaceGitRejectedError(
                "Git commit contains worktree changes outside its Task and rollback proofs"
            )
        return normalized

    def _git_path_proof(self, project: Path, relative_path: str) -> GitCommitPathProof:
        pure = PurePath(relative_path)
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any(value in relative_path for value in ("\x00", "\r", "\n", ":"))
        ):
            raise WorkspaceGitRejectedError("Git commit proof path escaped its project")
        path = project.joinpath(*pure.parts)
        try:
            cursor = path
            while cursor != project:
                if cursor == cursor.parent:
                    raise WorkspaceGitRejectedError(
                        "Git commit path escaped its project"
                    )
                cursor_stat = cursor.stat(follow_symlinks=False)
                if self._is_reparse_point(cursor, cursor_stat):
                    raise WorkspaceGitRejectedError(
                        "Git commit path contains a link or reparse point"
                    )
                cursor = cursor.parent
            resolved = path.resolve(strict=True)
            resolved.relative_to(project)
            value = resolved.stat(follow_symlinks=False)
            content = resolved.read_bytes()
            after = resolved.stat(follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise WorkspaceGitRejectedError("Git commit file cannot be read") from error
        if (
            self._is_reparse_point(resolved, value)
            or not stat.S_ISREG(value.st_mode)
            or len(content) > 262_144
            or self._stat_identity(value) != self._stat_identity(after)
        ):
            raise WorkspaceGitRejectedError("Git commit file is outside the bounded file policy")
        values = {
            "relative_path": relative_path,
            "content_digest": hashlib.sha256(content).hexdigest(),
            "byte_count": len(content),
        }
        return GitCommitPathProof.model_validate(
            {**values, "proof_digest": sha256_digest(values)}
        )

    def _git_commit_receipt(
        self,
        preview: GitCommitPreview,
        project: Path,
        base: tuple[str, ...],
        environment: dict[str, str],
        commit_oid: str,
    ) -> GitCommitReceipt:
        current_branch = self._current_branch(base, project, environment)
        details = self._run_git(
            base,
            project,
            environment,
            ("show", "-s", "--format=%P%x00%T%x00%cI%x00%B", commit_oid),
        ).stdout
        parts = details.split(b"\x00", 3)
        if len(parts) != 4:
            raise WorkspaceGitRejectedError("Git commit receipt details are invalid")
        parents = parts[0].decode("ascii", errors="ignore").strip().split()
        tree_oid = parts[1].decode("ascii", errors="ignore").strip().casefold()
        try:
            committed_at = datetime.fromisoformat(
                parts[2].decode("ascii").strip().replace("Z", "+00:00")
            )
            message = parts[3].decode("utf-8").rstrip("\r\n")
        except (UnicodeDecodeError, ValueError) as error:
            raise WorkspaceGitRejectedError("Git commit receipt encoding is invalid") from error
        changed = self._run_git(
            base,
            project,
            environment,
            ("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit_oid),
        ).stdout
        try:
            changed_paths = tuple(
                sorted(
                    (item.decode("utf-8") for item in changed.split(b"\x00") if item),
                    key=str.casefold,
                )
            )
        except UnicodeDecodeError as error:
            raise WorkspaceGitRejectedError("Git commit path receipt is not UTF-8") from error
        original_ref = self._run_git(
            base,
            project,
            environment,
            ("rev-parse", "--verify", f"refs/heads/{preview.original_branch}"),
        ).stdout.decode("ascii", errors="ignore").strip().casefold()
        expected_paths = tuple(item.relative_path for item in preview.paths)
        if (
            current_branch != preview.target_branch
            or parents != [preview.expected_head_oid]
            or message != preview.commit_message
            or changed_paths != expected_paths
            or original_ref != preview.expected_head_oid
            or self._git_status(base, project, environment)
            != {
                item.relative_path: "??" for item in preview.excluded_backups
            }
        ):
            raise WorkspaceGitRejectedError("Git commit does not match its approved preview")
        for proof in preview.paths:
            content = self._run_git(
                base,
                project,
                environment,
                ("show", f"{commit_oid}:{proof.relative_path}"),
            ).stdout
            if (
                len(content) != proof.byte_count
                or hashlib.sha256(content).hexdigest() != proof.content_digest
            ):
                raise WorkspaceGitRejectedError("Git committed blob changed from its preview")
        values = {
            "schema_version": "deskpilot.git-commit-receipt.v1",
            "task_id": preview.task_id,
            "project_path": preview.project_path,
            "confirmation_digest": preview.confirmation_digest,
            "expected_head_oid": preview.expected_head_oid,
            "commit_oid": commit_oid,
            "tree_oid": tree_oid,
            "original_branch": preview.original_branch,
            "target_branch": preview.target_branch,
            "commit_message_digest": sha256_digest({"message": preview.commit_message}),
            "paths": [item.model_dump(mode="json") for item in preview.paths],
            "excluded_backups": [
                item.model_dump(mode="json") for item in preview.excluded_backups
            ],
            "committed_at": committed_at,
            "hooks_disabled": True,
            "signing_disabled": True,
            "push_disabled": True,
            "rollback_available": True,
        }
        return GitCommitReceipt.model_validate(
            {**values, "receipt_digest": sha256_digest(values)}
        )

    def _repository_digest(
        self,
        project: Path,
        normalized_project: str,
        head_oid: str,
    ) -> str:
        git_dir = project / ".git"
        objects_dir = git_dir / "objects"
        config_path = git_dir / "config"
        try:
            git_stat = git_dir.stat(follow_symlinks=False)
            objects_stat = objects_dir.stat(follow_symlinks=False)
            config_stat = config_path.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceGitRejectedError(
                "Git repository identity cannot be inspected"
            ) from error
        if (
            self._is_reparse_point(git_dir, git_stat)
            or self._is_reparse_point(objects_dir, objects_stat)
            or self._is_reparse_point(config_path, config_stat)
            or not stat.S_ISDIR(git_stat.st_mode)
            or not stat.S_ISDIR(objects_stat.st_mode)
            or not stat.S_ISREG(config_stat.st_mode)
        ):
            raise WorkspaceGitRejectedError("Git repository identity is outside its boundary")
        try:
            config = config_path.read_bytes()
            config_after = config_path.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceGitRejectedError(
                "Git repository config cannot be read"
            ) from error
        if (
            len(config) > 262_144
            or self._stat_identity(config_stat) != self._stat_identity(config_after)
        ):
            raise WorkspaceGitRejectedError("Git repository config changed during inspection")
        return sha256_digest(
            {
                "project_path": normalized_project,
                "git_directory_identity": self._stable_stat_identity(git_stat),
                "object_directory_identity": self._stable_stat_identity(objects_stat),
                "config_digest": hashlib.sha256(config).hexdigest(),
                "head_oid": head_oid,
            }
        )

    def _required_head_oid(
        self,
        base: tuple[str, ...],
        project: Path,
        environment: dict[str, str],
    ) -> str:
        value = self._head_oid(base, project, environment)
        if value is None:
            raise WorkspaceGitRejectedError("Git commit requires an existing HEAD")
        return value

    def _current_branch(
        self,
        base: tuple[str, ...],
        project: Path,
        environment: dict[str, str],
    ) -> str:
        result = self._run_git(
            base,
            project,
            environment,
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
        )
        try:
            branch = result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise WorkspaceGitRejectedError("Git branch name is not UTF-8") from error
        if (
            not branch
            or len(branch) > 240
            or any(value in branch for value in ("\x00", "\r", "\n"))
        ):
            raise WorkspaceGitRejectedError("Git branch name is invalid")
        return branch

    def _run_git(
        self,
        base: tuple[str, ...],
        project: Path,
        environment: dict[str, str],
        arguments: tuple[str, ...],
        *,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(  # noqa: S603 - exact server-owned command
                (*base, *arguments),
                cwd=project,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
            )
        except subprocess.TimeoutExpired as error:
            raise WorkspaceGitTimeoutError("Git operation exceeded its time limit") from error
        if completed.returncode not in allowed_returncodes:
            raise WorkspaceGitRejectedError(
                self._safe_error(completed.stderr[:4_097], project)
                or "Git operation failed"
            )
        return completed

    def _coding_paths(self, project: Path) -> tuple[list[Path], int]:
        result: list[Path] = []
        scanned = 0

        def visit(directory: Path, depth: int) -> None:
            nonlocal scanned
            if depth > MAX_SEARCH_DEPTH:
                raise WorkspaceFilePathRejectedError("Project search depth exceeds its limit")
            before = directory.stat(follow_symlinks=False)
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name.casefold())
            for child in children:
                scanned += 1
                if scanned > MAX_SEARCH_ENTRIES:
                    raise WorkspaceFilePathRejectedError("Project search scan exceeds its limit")
                folded = child.name.casefold()
                if child.name.startswith(".") or folded in EXCLUDED_DIRECTORIES:
                    continue
                path = Path(child.path)
                value = path.stat(follow_symlinks=False)
                if self._is_reparse_point(path, value):
                    raise WorkspaceFilePathRejectedError(
                        "Project search rejects links and reparse points"
                    )
                if stat.S_ISDIR(value.st_mode):
                    visit(path, depth + 1)
                elif (
                    stat.S_ISREG(value.st_mode)
                    and path.suffix.casefold() in CODING_SUFFIXES
                    and value.st_size <= 262_144
                ):
                    result.append(path)
                    if len(result) > MAX_SEARCH_FILES:
                        raise WorkspaceFilePathRejectedError(
                            "Project search file count exceeds its limit"
                        )
            after = directory.stat(follow_symlinks=False)
            if self._stat_identity(before) != self._stat_identity(after):
                raise WorkspaceFileConflictError("Project changed while it was searched")

        try:
            visit(project, 0)
        except OSError as error:
            raise WorkspaceFileConflictError("Project changed while it was searched") from error
        result.sort(key=lambda item: item.relative_to(project).as_posix().casefold())
        return result, scanned

    def _validate_git_directory(self, git_dir: Path) -> None:
        try:
            value = git_dir.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceGitRejectedError("Project is not a supported Git repository") from error
        if self._is_reparse_point(git_dir, value) or not stat.S_ISDIR(value.st_mode):
            raise WorkspaceGitRejectedError(
                "Git directory links and worktree pointers are rejected"
            )
        forbidden = (
            git_dir / "commondir",
            git_dir / "objects" / "info" / "alternates",
        )
        if any(path.exists() for path in forbidden):
            raise WorkspaceGitRejectedError("Git repository uses an external object directory")

    def _head_oid(
        self,
        base: tuple[str, ...],
        project: Path,
        environment: dict[str, str],
    ) -> str | None:
        try:
            result = subprocess.run(  # noqa: S603 - exact server-owned command
                (*base, "rev-parse", "--verify", "HEAD"),
                cwd=project,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
            )
        except subprocess.TimeoutExpired as error:
            raise WorkspaceGitTimeoutError("Git HEAD inspection exceeded its time limit") from error
        candidate = result.stdout.decode("ascii", errors="ignore").strip().casefold()
        if result.returncode != 0:
            return None
        if len(candidate) not in {40, 64} or any(
            value not in "0123456789abcdef" for value in candidate
        ):
            raise WorkspaceGitRejectedError("Git HEAD object identity is invalid")
        return candidate

    @staticmethod
    def _git_environment() -> dict[str, str]:
        allowed = ("PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        null_path = "NUL" if os.name == "nt" else "/dev/null"
        environment.update(
            {
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": null_path,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_EXTERNAL_DIFF": "",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "LC_ALL": "C.UTF-8",
            }
        )
        environment.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
        return environment

    @staticmethod
    def _safe_relative(value: str) -> PurePath:
        raw = value.strip().replace("\\", "/")
        pure = PurePath(raw)
        if (
            not raw
            or pure.is_absolute()
            or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
        ):
            raise WorkspaceFilePathRejectedError("Project file path must stay beneath its root")
        return pure

    @staticmethod
    def _is_reparse_point(path: Path, value: os.stat_result | None = None) -> bool:
        current = value or path.stat(follow_symlinks=False)
        return stat.S_ISLNK(current.st_mode) or bool(
            getattr(current, "st_file_attributes", 0) & 0x00000400
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
    def _stable_stat_identity(value: os.stat_result) -> dict[str, int]:
        return {
            "device": value.st_dev,
            "inode": value.st_ino,
            "mode": value.st_mode,
        }

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1_048_576), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_error(raw: bytes, project: Path) -> str:
        text = raw[:4_096].decode("utf-8", errors="replace")
        return text.replace(str(project), "<project>").strip()[:500]


__all__ = [
    "WorkspaceCodingError",
    "WorkspaceCodingRuntime",
    "WorkspaceCodingUnavailableError",
    "WorkspaceGitRejectedError",
    "WorkspaceGitTimeoutError",
]
