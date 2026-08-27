"""Versioned contracts for destructive, disposable Workspace Coding evaluations."""

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkspaceCodingGoldenEcosystem = Literal["python", "node"]
WorkspaceCodingRestartCheckpoint = Literal["patch_approval", "git_approval"]


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
