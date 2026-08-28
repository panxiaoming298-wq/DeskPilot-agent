"""Versioned contracts for destructive, disposable Workspace Coding evaluations."""

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deskpilot.domain.command_profiles import CommandProfileId

WorkspaceCodingGoldenEcosystem = Literal["python", "node"]
WorkspaceCodingRestartCheckpoint = Literal["patch_approval", "git_approval"]
WorkspaceCodingProofDriftScope = Literal[
    "catalog",
    "profile",
    "project_path",
    "node",
    "input",
]


def _strict_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("Golden workspace paths must use POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0].startswith(".")
    ):
        raise ValueError("Golden workspace paths must be normalized relative paths")
    return value


class WorkspaceCodingGoldenFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=32_768)

    _path_is_relative = field_validator("path")(_strict_relative_path)


class WorkspaceCodingGoldenExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_paths: tuple[str, ...] = Field(min_length=2, max_length=8)
    changed_paths: tuple[str, ...] = Field(min_length=2, max_length=8)
    final_stage: Literal["delivered"] = "delivered"
    push_disabled: Literal[True] = True
    rollback_backups_retained: Literal[True] = True

    _candidate_paths_are_relative = field_validator("candidate_paths")(
        lambda values: tuple(_strict_relative_path(value) for value in values)
    )
    _changed_paths_are_relative = field_validator("changed_paths")(
        lambda values: tuple(_strict_relative_path(value) for value in values)
    )

    @model_validator(mode="after")
    def exact_candidate_change_set(self) -> Self:
        if len(set(self.candidate_paths)) != len(self.candidate_paths):
            raise ValueError("Golden candidate paths must be unique")
        if self.changed_paths != self.candidate_paths:
            raise ValueError("Golden changed paths must exactly preserve candidate order")
        return self


class WorkspaceCodingGoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    ecosystem: WorkspaceCodingGoldenEcosystem
    goal: str = Field(min_length=1, max_length=4_000)
    test_path: str = Field(min_length=1, max_length=500)
    files: tuple[WorkspaceCodingGoldenFile, ...] = Field(min_length=3, max_length=32)
    restart_checkpoints: tuple[WorkspaceCodingRestartCheckpoint, ...] = Field(
        default=(), max_length=2
    )
    max_advances: int = Field(default=40, ge=1, le=100)
    expect: WorkspaceCodingGoldenExpectation

    _test_path_is_relative = field_validator("test_path")(_strict_relative_path)

    @model_validator(mode="after")
    def complete_bounded_fixture(self) -> Self:
        file_paths = tuple(item.path for item in self.files)
        if len(set(file_paths)) != len(file_paths):
            raise ValueError("Golden project file paths must be unique")
        if self.test_path not in file_paths:
            raise ValueError("Golden test path must be present in the project fixture")
        if any(path not in file_paths for path in self.expect.candidate_paths):
            raise ValueError("Golden candidates must be present in the project fixture")
        if self.test_path in self.expect.candidate_paths:
            raise ValueError("Golden test path cannot be a change candidate")
        candidate_suffixes = {
            PurePosixPath(path).suffix for path in self.expect.candidate_paths
        }
        if self.ecosystem == "python":
            if candidate_suffixes != {".py"} or not self.test_path.endswith(".py"):
                raise ValueError("Python golden tasks require Python candidates and test path")
        elif candidate_suffixes - {".js", ".ts", ".mjs", ".cjs"} or not self.test_path.endswith(
            (".js", ".mjs", ".cjs")
        ):
            raise ValueError("Node golden tasks require JavaScript/TypeScript candidates and test")
        if len(set(self.restart_checkpoints)) != len(self.restart_checkpoints):
            raise ValueError("Golden restart checkpoints must be unique")
        return self


class WorkspaceCodingGoldenSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-golden-suite.v1"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    version: int = Field(ge=1)
    cases: tuple[WorkspaceCodingGoldenCase, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_cases_and_ecosystems(self) -> Self:
        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("Workspace Coding golden case IDs must be unique")
        if {case.ecosystem for case in self.cases} != {"python", "node"}:
            raise ValueError("Workspace Coding golden suite must cover Python and Node")
        return self


class WorkspaceCommandGoldenResilienceScenario(BaseModel):
    """Versioned fault plan; it grants no authority to execute a command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    workspace_case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    command_project_path: str = Field(min_length=1, max_length=500)
    command_profile_ids: tuple[CommandProfileId, ...] = Field(min_length=2, max_length=6)
    stable_restart_cycles: int = Field(ge=2, le=8)
    known_failure_count: Literal[1] = 1
    expected_repaired_attempt: Literal[2] = 2
    proof_drift_scopes: tuple[WorkspaceCodingProofDriftScope, ...] = Field(
        min_length=5,
        max_length=5,
    )
    interrupted_profile_id: CommandProfileId
    expected_unknown_error_code: Literal["CAPABILITY_OUTCOME_UNKNOWN_AFTER_LEASE"] = (
        "CAPABILITY_OUTCOME_UNKNOWN_AFTER_LEASE"
    )
    no_automatic_replay: Literal[True] = True
    max_advances: int = Field(default=24, ge=8, le=60)

    _project_path_is_relative = field_validator("command_project_path")(
        _strict_relative_path
    )

    @model_validator(mode="after")
    def exact_fault_matrix(self) -> Self:
        if len(set(self.command_profile_ids)) != len(self.command_profile_ids):
            raise ValueError("Resilience command profiles must be unique")
        ecosystems = {
            profile_id.split(".", maxsplit=1)[0]
            for profile_id in self.command_profile_ids
        }
        if ecosystems != {"python"}:
            raise ValueError("The v1 resilience scenario requires one Python command chain")
        expected_scopes: tuple[WorkspaceCodingProofDriftScope, ...] = (
            "catalog",
            "profile",
            "project_path",
            "node",
            "input",
        )
        if self.proof_drift_scopes != expected_scopes:
            raise ValueError("Resilience proof drift scopes must preserve the complete v1 matrix")
        if self.interrupted_profile_id != self.command_profile_ids[0]:
            raise ValueError("Interrupted Profile must be the first command step")
        return self


class WorkspaceCodingGoldenResilienceSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-resilience-suite.v1"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    version: Literal[1] = 1
    workspace_suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario: WorkspaceCommandGoldenResilienceScenario


class WorkspaceCodingSidecarSoakScenario(BaseModel):
    """Short real-clock supervisor canary; it grants no execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    resilience_scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    command_project_path: str = Field(min_length=1, max_length=500)
    command_profile_ids: tuple[CommandProfileId, ...] = Field(min_length=2, max_length=6)
    observation_seconds: int = Field(ge=2, le=30)
    poll_interval_ms: int = Field(ge=100, le=1_000)
    expected_restart_count: Literal[1] = 1
    expected_process_generations: Literal[2] = 2
    supervisor_restart_budget: Literal[3] = 3
    automatic_workbench_after_restart: Literal[True] = True
    no_automatic_replay: Literal[True] = True
    max_advances: int = Field(default=24, ge=8, le=60)

    _project_path_is_relative = field_validator("command_project_path")(
        _strict_relative_path
    )

    @model_validator(mode="after")
    def bounded_observation_matrix(self) -> Self:
        if len(set(self.command_profile_ids)) != len(self.command_profile_ids):
            raise ValueError("Sidecar soak command profiles must be unique")
        if self.observation_seconds * 1_000 % self.poll_interval_ms:
            raise ValueError("Sidecar soak observation must contain complete polling intervals")
        if self.observation_seconds * 1_000 // self.poll_interval_ms < 4:
            raise ValueError("Sidecar soak observation requires at least four samples")
        return self


class WorkspaceCodingGoldenSidecarSoakSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-sidecar-soak-suite.v1"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    version: Literal[1] = 1
    resilience_suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario: WorkspaceCodingSidecarSoakScenario


class WorkspaceCodingConcurrencyRepository(BaseModel):
    """One bounded disposable repository in the concurrency canary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    message_marker: str = Field(pattern=r"^repo-[a-z0-9][a-z0-9-]{2,39}$")
    ecosystem: WorkspaceCodingGoldenEcosystem
    project_path: str = Field(min_length=1, max_length=500)
    source_file_count: int = Field(ge=24, le=64)
    command_profile_ids: tuple[CommandProfileId, ...] = Field(min_length=2, max_length=3)
    fail_first_profile_once: bool = False

    _project_path_is_relative = field_validator("project_path")(_strict_relative_path)

    @model_validator(mode="after")
    def one_ecosystem_and_unique_profiles(self) -> Self:
        if len(set(self.command_profile_ids)) != len(self.command_profile_ids):
            raise ValueError("Concurrency repository command profiles must be unique")
        profile_ecosystems = {
            profile_id.split(".", maxsplit=1)[0] for profile_id in self.command_profile_ids
        }
        if profile_ecosystems != {self.ecosystem}:
            raise ValueError("Concurrency repository profiles crossed ecosystems")
        return self


class WorkspaceCodingConcurrencyScenario(BaseModel):
    """Bounded multi-repository scheduler canary; it grants no execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    repositories: tuple[WorkspaceCodingConcurrencyRepository, ...] = Field(
        min_length=3,
        max_length=3,
    )
    workbench_concurrency: Literal[2] = 2
    expected_peak_command_concurrency: Literal[2] = 2
    command_delay_ms: int = Field(ge=100, le=1_000)
    poll_interval_ms: int = Field(ge=25, le=500)
    completion_timeout_seconds: int = Field(ge=30, le=180)
    fairness_first_wave: Literal["all_repositories_before_any_second_profile"] = (
        "all_repositories_before_any_second_profile"
    )
    automatic_repair: Literal[True] = True
    no_automatic_replay: Literal[True] = True
    max_advances: int = Field(default=24, ge=8, le=60)

    @model_validator(mode="after")
    def exact_bounded_matrix(self) -> Self:
        if {item.ecosystem for item in self.repositories} != {"python", "node"}:
            raise ValueError("Concurrency scenario must cover Python and Node")
        for field_name, values in (
            ("repository IDs", tuple(item.repository_id for item in self.repositories)),
            ("message markers", tuple(item.message_marker for item in self.repositories)),
            ("project paths", tuple(item.project_path for item in self.repositories)),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"Concurrency scenario {field_name} must be unique")
        if sum(item.source_file_count for item in self.repositories) < 72:
            raise ValueError("Concurrency scenario fixture is below its medium-repository floor")
        failing = tuple(item for item in self.repositories if item.fail_first_profile_once)
        if len(failing) != 1:
            raise ValueError("Concurrency scenario requires exactly one recoverable failure")
        return self


class WorkspaceCodingGoldenConcurrencySuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-coding-concurrency-suite.v1"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    version: Literal[1] = 1
    sidecar_suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario: WorkspaceCodingConcurrencyScenario
