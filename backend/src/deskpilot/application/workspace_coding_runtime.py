"""Project-root constrained search, batch read, and fixed read-only Git inspection."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePath
from typing import Literal

from deskpilot.application.workspace_file_runtime import (
    WorkspaceFileConflictError,
    WorkspaceFilePathRejectedError,
    WorkspaceFileRuntime,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.coding_tools import (
    GitInspectionRead,
    ProjectBatchRead,
    ProjectSearchMatch,
    ProjectSearchRead,
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
