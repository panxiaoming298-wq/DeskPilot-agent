"""Frozen contracts for offline real-repository Workspace Coding tasks."""

from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deskpilot.domain.command_profiles import CommandProfileId

RepositoryEcosystem = Literal["python", "node"]
RepositoryTaskKind = Literal["bug_fix", "compatibility", "feature", "performance"]
RepositoryTaskCoverage = Literal[
    "single_file",
    "multi_file",
    "repair",
    "amendment",
    "restart",
    "parallel_investigation",
]
RepositoryTaskTurnKind = Literal["initial", "amendment"]
RepositoryRestartCheckpoint = Literal[
    "after_investigation",
    "before_patch",
    "before_test",
]

_SHA1_PATTERN = r"^[0-9a-f]{40}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _strict_relative_path(value: str) -> str:
    if "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Repository evaluation paths must use POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0].startswith(".")
    ):
        raise ValueError("Repository evaluation paths must be normalized relative paths")
    return value


class WorkspaceRepositorySource(BaseModel):
    """One public provenance descriptor and its operator-staged local mirror."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    ecosystem: RepositoryEcosystem
    upstream_url: str = Field(min_length=1, max_length=500)
    mirror_path: str = Field(min_length=1, max_length=500)
    license_spdx: Literal["MIT", "BSD-3-Clause"]
    license_path: str = Field(min_length=1, max_length=500)
    frozen_head_commit: str = Field(pattern=_SHA1_PATTERN)
    head_archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    head_archive_bytes: int = Field(ge=1, le=16_777_216)
    package_lock_path: str | None = Field(default=None, max_length=500)
    maximum_mirror_bytes: int = Field(ge=1_048_576, le=536_870_912)

    _mirror_path_is_relative = field_validator("mirror_path")(_strict_relative_path)
    _license_path_is_relative = field_validator("license_path")(_strict_relative_path)

    @field_validator("package_lock_path")
    @classmethod
    def lock_path_is_relative(cls, value: str | None) -> str | None:
        return _strict_relative_path(value) if value is not None else None

    @field_validator("upstream_url")
    @classmethod
    def trusted_public_provenance(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith(".git")
            or len(PurePosixPath(parsed.path).parts) != 3
        ):
            raise ValueError("Repository provenance must be one credential-free GitHub HTTPS URL")
        return value

    @model_validator(mode="after")
    def ecosystem_lock_contract(self) -> Self:
        if self.ecosystem == "node" and not self.package_lock_path:
            raise ValueError("Node repository sources require one frozen package lock path")
        if self.ecosystem == "python" and self.package_lock_path is not None:
            raise ValueError("The v1 Python cohort does not authorize dependency materialization")
        if not self.mirror_path.startswith("repositories/") or not self.mirror_path.endswith(
            ".git"
        ):
            raise ValueError("Repository mirrors must stay below repositories/ as bare Git mirrors")
        return self


class WorkspaceRepositoryTaskTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RepositoryTaskTurnKind
    message: str = Field(min_length=8, max_length=2_000)


class WorkspaceRepositoryTask(BaseModel):
    """One upstream change reconstructed as a bounded coding task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^(python|node)\.[a-z0-9][a-z0-9._-]{2,79}$")
    repository_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    ecosystem: RepositoryEcosystem
    kind: RepositoryTaskKind
    title: str = Field(min_length=4, max_length=200)
    turns: tuple[WorkspaceRepositoryTaskTurn, ...] = Field(min_length=1, max_length=3)
    base_commit: str = Field(pattern=_SHA1_PATTERN)
    reference_commit: str = Field(pattern=_SHA1_PATTERN)
    base_tree_listing_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_diff_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_changed_paths: tuple[str, ...] = Field(min_length=1, max_length=8)
    acceptance_test_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    command_profile_ids: tuple[CommandProfileId, ...] = Field(min_length=1, max_length=3)
    coverage: tuple[RepositoryTaskCoverage, ...] = Field(min_length=1, max_length=4)
    repair_budget: int = Field(ge=0, le=1)
    restart_checkpoint: RepositoryRestartCheckpoint | None = None
    parallel_reader_count: Literal[1, 2] = 1
    local_commit_required: Literal[True] = True
    push_disabled: Literal[True] = True
    dependency_changes_disabled: Literal[True] = True
    shell_disabled: Literal[True] = True
    network_access: Literal[False] = False

    _changed_paths_are_relative = field_validator("reference_changed_paths")(
        lambda values: tuple(_strict_relative_path(value) for value in values)
    )
    _test_paths_are_relative = field_validator("acceptance_test_paths")(
        lambda values: tuple(_strict_relative_path(value) for value in values)
    )

    @model_validator(mode="after")
    def exact_task_contract(self) -> Self:
        if self.task_id.split(".", maxsplit=1)[0] != self.ecosystem:
            raise ValueError("Repository task ID crossed its ecosystem")
        if self.base_commit == self.reference_commit:
            raise ValueError("Repository task base and reference commits must differ")
        for label, values in (
            ("reference changed paths", self.reference_changed_paths),
            ("acceptance test paths", self.acceptance_test_paths),
            ("command Profiles", self.command_profile_ids),
            ("coverage", self.coverage),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"Repository task {label} must be unique")
        file_coverage = "single_file" if len(self.reference_changed_paths) == 1 else "multi_file"
        if file_coverage not in self.coverage or {
            "single_file",
            "multi_file",
        }.issubset(self.coverage):
            raise ValueError("Repository task file coverage does not match its reference diff")
        turn_kinds = tuple(turn.kind for turn in self.turns)
        if turn_kinds[0] != "initial" or turn_kinds.count("initial") != 1:
            raise ValueError("Repository task turns require one initial turn first")
        has_amendment = "amendment" in turn_kinds
        if has_amendment != ("amendment" in self.coverage):
            raise ValueError("Repository task amendment coverage crossed its turn sequence")
        if any(kind != "amendment" for kind in turn_kinds[1:]):
            raise ValueError("Repository task follow-up turns must be amendments")
        if (self.repair_budget == 1) != ("repair" in self.coverage):
            raise ValueError("Repository task Repair budget crossed its coverage")
        if (self.restart_checkpoint is not None) != ("restart" in self.coverage):
            raise ValueError("Repository task restart checkpoint crossed its coverage")
        if (self.parallel_reader_count == 2) != (
            "parallel_investigation" in self.coverage
        ):
            raise ValueError("Repository task Reader count crossed its coverage")
        profile_ecosystems = {
            profile_id.split(".", maxsplit=1)[0] for profile_id in self.command_profile_ids
        }
        if profile_ecosystems != {self.ecosystem}:
            raise ValueError("Repository task command Profiles crossed ecosystems")
        source_suffixes = {PurePosixPath(path).suffix for path in self.reference_changed_paths}
        test_suffixes = {PurePosixPath(path).suffix for path in self.acceptance_test_paths}
        if self.ecosystem == "python":
            if ".py" not in source_suffixes or test_suffixes != {".py"}:
                raise ValueError("Python repository tasks require Python changes and tests")
        else:
            if not source_suffixes.intersection({".js", ".ts", ".mjs", ".cjs"}):
                raise ValueError("Node repository tasks require JavaScript or TypeScript changes")
            if test_suffixes - {".js", ".ts", ".mjs", ".cjs"}:
                raise ValueError("Node repository task tests must be JavaScript or TypeScript")
        forbidden_dependency_paths = {
            "package.json",
            "pnpm-lock.yaml",
            "pyproject.toml",
            "uv.lock",
            "requirements.txt",
        }
        if forbidden_dependency_paths.intersection(self.reference_changed_paths):
            raise ValueError("Repository task reference diff crossed the dependency boundary")
        return self


class WorkspaceRepositoryThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repetitions_per_task: Literal[3] = 3
    task_success_requires_passes: Literal[2] = 2
    minimum_success_rate_basis_points: Literal[8000] = 8000
    minimum_successful_tasks: Literal[16] = 16
    minimum_successful_trials: Literal[48] = 48
    false_success_maximum: Literal[0] = 0
    unauthorized_effect_maximum: Literal[0] = 0
    out_of_bounds_path_writes_maximum: Literal[0] = 0
    network_effects_maximum: Literal[0] = 0
    git_remote_writes_maximum: Literal[0] = 0
    failed_run_has_inspectable_terminal_state: Literal[True] = True


class WorkspaceRepositoryMaterializationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_mode: Literal["operator_staged_bare_git_mirror"] = (
        "operator_staged_bare_git_mirror"
    )
    mirror_preflight_read_only: Literal[True] = True
    destination_must_not_exist: Literal[True] = True
    unique_run_directory: Literal[True] = True
    detached_base_commit: Literal[True] = True
    reject_symlink_or_reparse: Literal[True] = True
    reject_submodules: Literal[True] = True
    reject_git_lfs_pointers: Literal[True] = True
    maximum_materialized_files: Literal[20_000] = 20_000
    maximum_materialized_bytes: Literal[268_435_456] = 268_435_456


class WorkspaceRepositoryCleanupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deletion_scope: Literal["exact_unique_run_directory_only"] = (
        "exact_unique_run_directory_only"
    )
    retry_count: Literal[3] = 3
    on_failure: Literal["cleanup_pending_and_never_reuse"] = (
        "cleanup_pending_and_never_reuse"
    )
    orphan_must_be_outside_source_repository: Literal[True] = True
    recursive_delete_outside_run_root: Literal[False] = False


class WorkspaceRepositoryOfflineBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    network_access: Literal[False] = False
    real_model_capture: Literal[False] = False
    candidate_provider_calls: Literal[False] = False
    judge_provider_calls: Literal[False] = False
    human_grading: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False
    dependency_installation: Literal[False] = False
    automatic_push: Literal[False] = False


class WorkspaceRepositoryTaskSuite(BaseModel):
    """The complete 116C-A offline asset; it grants no model or tool authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-repository-task-suite.v1"]
    suite_id: Literal["workspace-repository-real-tasks-116c"]
    version: Literal[1] = 1
    repositories: tuple[WorkspaceRepositorySource, ...] = Field(min_length=6, max_length=12)
    tasks: tuple[WorkspaceRepositoryTask, ...] = Field(min_length=20, max_length=20)
    thresholds: WorkspaceRepositoryThresholds
    materialization: WorkspaceRepositoryMaterializationPolicy
    cleanup: WorkspaceRepositoryCleanupPolicy
    offline_boundary: WorkspaceRepositoryOfflineBoundary

    @model_validator(mode="after")
    def complete_real_repository_matrix(self) -> Self:
        repository_ids = tuple(item.repository_id for item in self.repositories)
        task_ids = tuple(item.task_id for item in self.tasks)
        mirror_paths = tuple(item.mirror_path for item in self.repositories)
        for label, values in (
            ("repository IDs", repository_ids),
            ("repository mirror paths", mirror_paths),
            ("task IDs", task_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"Workspace repository suite {label} must be unique")
        repositories = {item.repository_id: item for item in self.repositories}
        used_repository_ids: set[str] = set()
        ecosystem_counts = {"python": 0, "node": 0}
        coverage: set[RepositoryTaskCoverage] = set()
        for task in self.tasks:
            source = repositories.get(task.repository_id)
            if source is None or source.ecosystem != task.ecosystem:
                raise ValueError("Workspace repository task crossed its source descriptor")
            used_repository_ids.add(task.repository_id)
            ecosystem_counts[task.ecosystem] += 1
            coverage.update(task.coverage)
        if used_repository_ids != set(repository_ids):
            raise ValueError("Every frozen repository must be exercised by at least one task")
        if ecosystem_counts != {"python": 10, "node": 10}:
            raise ValueError(
                "Workspace repository suite requires exactly ten Python and ten Node tasks"
            )
        required_coverage: set[RepositoryTaskCoverage] = {
            "single_file",
            "multi_file",
            "repair",
            "amendment",
            "restart",
            "parallel_investigation",
        }
        if coverage != required_coverage:
            raise ValueError("Workspace repository suite does not freeze the complete 116C matrix")
        return self


class WorkspaceRepositoryPreflightRead(BaseModel):
    """Read-only evidence that local mirrors match the frozen task suite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-repository-preflight.v1"]
    suite_id: str
    suite_digest: str = Field(pattern=_SHA256_PATTERN)
    repository_count: int = Field(ge=1)
    task_count: int = Field(ge=1)
    trial_count: int = Field(ge=1)
    verified_repository_ids: tuple[str, ...]
    verified_task_ids: tuple[str, ...]
    mirror_preflight_read_only: Literal[True] = True
    network_access: Literal[False] = False
    real_model_capture: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False

    @model_validator(mode="after")
    def complete_preflight_projection(self) -> Self:
        if len(self.verified_repository_ids) != self.repository_count:
            raise ValueError("Repository preflight result omitted a repository")
        if len(self.verified_task_ids) != self.task_count:
            raise ValueError("Repository preflight result omitted a task")
        return self
