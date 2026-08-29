"""Strict loader and network-free mirror preflight for 116C-A repository tasks."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_repository_evaluations import (
    WorkspaceRepositoryPreflightRead,
    WorkspaceRepositorySource,
    WorkspaceRepositoryTask,
    WorkspaceRepositoryTaskSuite,
)

MAX_REPOSITORY_SUITE_BYTES = 262_144
MAX_GIT_OUTPUT_BYTES = 16_777_216
MAX_MIRROR_FILES = 100_000
GIT_TIMEOUT_SECONDS = 30
_LFS_POINTER_MARKER = "version https://git-lfs.github.com/spec/v1"
_SAFE_PROCESS_ENVIRONMENT = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


class WorkspaceRepositoryEvaluationError(RuntimeError):
    code = "WORKSPACE_REPOSITORY_EVALUATION_REJECTED"


@dataclass(frozen=True, slots=True)
class WorkspaceRepositoryTaskSuiteBundle:
    suite: WorkspaceRepositoryTaskSuite
    suite_digest: str


def _read_strict_yaml(suite_path: Path) -> object:
    try:
        if suite_path.is_symlink() or not suite_path.is_file():
            raise WorkspaceRepositoryEvaluationError(
                "Workspace repository suite must be one regular file"
            )
        payload = suite_path.read_bytes()
        if not payload or len(payload) > MAX_REPOSITORY_SUITE_BYTES:
            raise WorkspaceRepositoryEvaluationError(
                "Workspace repository suite is empty or exceeds its size limit"
            )
        text = payload.decode("utf-8")
        if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
            raise WorkspaceRepositoryEvaluationError(
                "Workspace repository suite YAML aliases are not allowed"
            )
        return yaml.safe_load(text)
    except WorkspaceRepositoryEvaluationError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise WorkspaceRepositoryEvaluationError(
            "Workspace repository suite failed strict loading"
        ) from error


class WorkspaceRepositoryTaskSuiteLoader:
    """Load the frozen suite without fetching repositories or calling a model."""

    def __init__(self, suite_path: Path | None = None) -> None:
        self._suite_path = suite_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "workspace_repository_tasks_v1.yaml"
        )

    def load(self) -> WorkspaceRepositoryTaskSuiteBundle:
        try:
            suite = WorkspaceRepositoryTaskSuite.model_validate(
                _read_strict_yaml(self._suite_path)
            )
        except WorkspaceRepositoryEvaluationError:
            raise
        except ValidationError as error:
            raise WorkspaceRepositoryEvaluationError(
                "Workspace repository task suite failed strict validation"
            ) from error
        return WorkspaceRepositoryTaskSuiteBundle(
            suite=suite,
            suite_digest=sha256_digest(suite.model_dump(mode="json")),
        )


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes


class WorkspaceRepositoryOfflinePreflight:
    """Verify operator-staged bare mirrors using read-only local Git commands."""

    def __init__(
        self,
        bundle: WorkspaceRepositoryTaskSuiteBundle,
        mirror_root: Path,
        *,
        git_executable: str | None = None,
    ) -> None:
        self._bundle = bundle
        self._mirror_root = mirror_root
        self._git_executable = git_executable or shutil.which("git") or ""

    def run(self) -> WorkspaceRepositoryPreflightRead:
        root = self._verified_root()
        if not self._git_executable:
            raise WorkspaceRepositoryEvaluationError(
                "Offline repository preflight requires a configured Git executable"
            )
        tasks_by_repository: dict[str, list[WorkspaceRepositoryTask]] = {
            source.repository_id: [] for source in self._bundle.suite.repositories
        }
        for task in self._bundle.suite.tasks:
            tasks_by_repository[task.repository_id].append(task)

        verified_repositories: list[str] = []
        verified_tasks: list[str] = []
        for source in self._bundle.suite.repositories:
            mirror = self._verified_mirror(root, source)
            self._verify_repository_contract(mirror, source)
            for task in tasks_by_repository[source.repository_id]:
                self._verify_task(mirror, source, task)
                verified_tasks.append(task.task_id)
            verified_repositories.append(source.repository_id)

        return WorkspaceRepositoryPreflightRead(
            schema_version="deskpilot.workspace-repository-preflight.v1",
            suite_id=self._bundle.suite.suite_id,
            suite_digest=self._bundle.suite_digest,
            repository_count=len(verified_repositories),
            task_count=len(verified_tasks),
            trial_count=(
                len(verified_tasks)
                * self._bundle.suite.thresholds.repetitions_per_task
            ),
            verified_repository_ids=tuple(verified_repositories),
            verified_task_ids=tuple(verified_tasks),
        )

    def _verified_root(self) -> Path:
        try:
            if _is_link_or_reparse(self._mirror_root):
                raise WorkspaceRepositoryEvaluationError(
                    "Offline mirror root cannot be a link or reparse point"
                )
            root = self._mirror_root.resolve(strict=True)
        except WorkspaceRepositoryEvaluationError:
            raise
        except OSError as error:
            raise WorkspaceRepositoryEvaluationError(
                "Offline mirror root is unavailable"
            ) from error
        if not root.is_dir():
            raise WorkspaceRepositoryEvaluationError(
                "Offline mirror root must be one directory"
            )
        return root

    def _verified_mirror(
        self,
        root: Path,
        source: WorkspaceRepositorySource,
    ) -> Path:
        candidate = root.joinpath(*source.mirror_path.split("/"))
        try:
            mirror = candidate.resolve(strict=True)
            mirror.relative_to(root)
        except (OSError, ValueError) as error:
            raise WorkspaceRepositoryEvaluationError(
                f"Offline mirror for {source.repository_id} escaped or is unavailable"
            ) from error
        current = root
        for part in Path(source.mirror_path).parts:
            current /= part
            if _is_link_or_reparse(current):
                raise WorkspaceRepositoryEvaluationError(
                    f"Offline mirror for {source.repository_id} contains a link or reparse point"
                )
        if not mirror.is_dir():
            raise WorkspaceRepositoryEvaluationError(
                f"Offline mirror for {source.repository_id} must be a bare Git directory"
            )
        size, file_count = _bounded_directory_size(mirror, source.maximum_mirror_bytes)
        if size > source.maximum_mirror_bytes or file_count > MAX_MIRROR_FILES:
            raise WorkspaceRepositoryEvaluationError(
                f"Offline mirror for {source.repository_id} exceeds its frozen bounds"
            )
        return mirror

    def _verify_repository_contract(
        self,
        mirror: Path,
        source: WorkspaceRepositorySource,
    ) -> None:
        for relative_path in (
            "objects/info/alternates",
            "objects/info/http-alternates",
        ):
            if mirror.joinpath(*relative_path.split("/")).exists():
                raise WorkspaceRepositoryEvaluationError(
                    f"Offline mirror for {source.repository_id} is not self-contained"
                )
        if self._git(mirror, "rev-parse", "--is-bare-repository").stdout.strip() != b"true":
            raise WorkspaceRepositoryEvaluationError(
                f"Offline mirror for {source.repository_id} is not bare"
            )
        self._git(mirror, "cat-file", "-e", f"{source.frozen_head_commit}^{{commit}}")
        head = self._git(mirror, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        if head != source.frozen_head_commit.encode("ascii"):
            raise WorkspaceRepositoryEvaluationError(
                f"Offline mirror for {source.repository_id} frozen head drifted"
            )

    def _verify_task(
        self,
        mirror: Path,
        source: WorkspaceRepositorySource,
        task: WorkspaceRepositoryTask,
    ) -> None:
        for commit in (task.base_commit, task.reference_commit):
            self._git(mirror, "cat-file", "-e", f"{commit}^{{commit}}")
        listing = self._git(
            mirror,
            "ls-tree",
            "-r",
            "-z",
            "--long",
            task.base_commit,
        ).stdout
        if b"160000 commit" in listing:
            raise WorkspaceRepositoryEvaluationError(
                f"Repository task {task.task_id} base contains a submodule"
            )
        if hashlib.sha256(listing).hexdigest() != task.base_tree_listing_sha256:
            raise WorkspaceRepositoryEvaluationError(
                f"Repository task {task.task_id} base tree listing drifted"
            )
        reference_diff = self._git(
            mirror,
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--binary",
            task.base_commit,
            task.reference_commit,
        ).stdout
        if hashlib.sha256(reference_diff).hexdigest() != task.reference_diff_sha256:
            raise WorkspaceRepositoryEvaluationError(
                f"Repository task {task.task_id} reference diff drifted"
            )
        changed = self._git(
            mirror,
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--name-only",
            "-z",
            task.base_commit,
            task.reference_commit,
        ).stdout
        try:
            changed_paths = tuple(
                item.decode("utf-8") for item in changed.split(b"\0") if item
            )
        except UnicodeDecodeError as error:
            raise WorkspaceRepositoryEvaluationError(
                f"Repository task {task.task_id} has a non-UTF-8 diff path"
            ) from error
        if changed_paths != task.reference_changed_paths:
            raise WorkspaceRepositoryEvaluationError(
                f"Repository task {task.task_id} reference paths drifted"
            )
        reference_listing = self._git(
            mirror,
            "ls-tree",
            "-r",
            "-z",
            "--long",
            task.reference_commit,
        ).stdout
        if b"160000 commit" in reference_listing:
            raise WorkspaceRepositoryEvaluationError(
                f"Repository task {task.task_id} reference contains a submodule"
            )
        required_paths = (
            source.license_path,
            *(() if source.package_lock_path is None else (source.package_lock_path,)),
        )
        for path in required_paths:
            self._git(mirror, "cat-file", "-e", f"{task.base_commit}:{path}")
        for path in task.acceptance_test_paths:
            self._git(mirror, "cat-file", "-e", f"{task.reference_commit}:{path}")
        for label, commit in (
            ("base", task.base_commit),
            ("reference", task.reference_commit),
        ):
            lfs = self._git(
                mirror,
                "grep",
                "-I",
                "-l",
                "--fixed-strings",
                _LFS_POINTER_MARKER,
                commit,
                "--",
                allowed_returncodes=(0, 1),
            )
            if lfs.returncode == 0 and lfs.stdout.strip():
                raise WorkspaceRepositoryEvaluationError(
                    f"Repository task {task.task_id} {label} contains Git LFS pointers"
                )

    def _git(
        self,
        mirror: Path,
        *arguments: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> _GitResult:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SAFE_PROCESS_ENVIRONMENT
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "file",
                "GIT_PROTOCOL_FROM_USER": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and strict arguments.
                (
                    self._git_executable,
                    "--no-optional-locks",
                    f"--git-dir={mirror}",
                    *arguments,
                ),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env=environment,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkspaceRepositoryEvaluationError(
                "Offline repository Git preflight failed to execute"
            ) from error
        if completed.returncode not in allowed_returncodes:
            raise WorkspaceRepositoryEvaluationError(
                "Offline repository Git preflight rejected a frozen object"
            )
        if (
            len(completed.stdout) > MAX_GIT_OUTPUT_BYTES
            or len(completed.stderr) > MAX_GIT_OUTPUT_BYTES
        ):
            raise WorkspaceRepositoryEvaluationError(
                "Offline repository Git preflight output exceeded its bound"
            )
        return _GitResult(returncode=completed.returncode, stdout=completed.stdout)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _bounded_directory_size(root: Path, maximum_bytes: int) -> tuple[int, int]:
    total = 0
    file_count = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = tuple(os.scandir(current))
        except OSError as error:
            raise WorkspaceRepositoryEvaluationError(
                "Offline repository mirror could not be inspected"
            ) from error
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or _is_link_or_reparse(path):
                raise WorkspaceRepositoryEvaluationError(
                    "Offline repository mirror contains a link or reparse point"
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise WorkspaceRepositoryEvaluationError(
                    "Offline repository mirror contains a non-regular entry"
                )
            total += entry.stat(follow_symlinks=False).st_size
            file_count += 1
            if total > maximum_bytes or file_count > MAX_MIRROR_FILES:
                return total, file_count
    return total, file_count


__all__ = [
    "WorkspaceRepositoryEvaluationError",
    "WorkspaceRepositoryOfflinePreflight",
    "WorkspaceRepositoryTaskSuiteBundle",
    "WorkspaceRepositoryTaskSuiteLoader",
]
