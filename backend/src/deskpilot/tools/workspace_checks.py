"""Fixed, snapshot-only workspace verification profiles."""

import ast
import hashlib
import json
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.policy import PolicyResource
from deskpilot.domain.tool_contracts import (
    ToolContract,
    ToolExecutionContract,
    ToolIdempotency,
    ToolRiskLevel,
    ToolSecurityContract,
)
from deskpilot.runner.executor import (
    ToolExecutionCancelledError,
    ToolExecutionContext,
    ToolExecutor,
)


class WorkspaceCheckFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str = Field(min_length=1, max_length=32_767)
    content: str = Field(max_length=262_144)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    version_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def content_matches(self) -> "WorkspaceCheckFile":
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_digest:
            raise ValueError("Workspace check file content digest does not match")
        return self


class WorkspaceCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: Literal["json-parse", "python-syntax"]
    relative_path: str = Field(min_length=1, max_length=32_767)
    files: tuple[WorkspaceCheckFile, ...] = Field(min_length=1, max_length=64)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def snapshot_matches(self) -> "WorkspaceCheckInput":
        paths = [item.relative_path.casefold() for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Workspace check files must be sorted and unique")
        expected = sha256_digest(
            {
                "profile": self.profile,
                "relative_path": self.relative_path,
                "files": [
                    item.model_dump(mode="json", exclude={"content"}) for item in self.files
                ],
            }
        )
        if self.snapshot_digest != expected:
            raise ValueError("Workspace check snapshot digest does not match")
        return self


class WorkspaceCheckIssueOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str = Field(min_length=1, max_length=32_767)
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    code: Literal["JSON_INVALID", "PYTHON_SYNTAX_INVALID"]
    message: str = Field(min_length=1, max_length=300)


class WorkspaceCheckOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: Literal["json-parse", "python-syntax"]
    relative_path: str = Field(min_length=1, max_length=32_767)
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["failed", "passed"]
    checked_file_count: int = Field(ge=1, le=64)
    issues: tuple[WorkspaceCheckIssueOutput, ...] = Field(max_length=64)
    output_truncated: bool = False

    @model_validator(mode="after")
    def status_matches(self) -> "WorkspaceCheckOutput":
        if (self.status == "passed") != (not self.issues):
            raise ValueError("Workspace check status does not match issues")
        return self


WORKSPACE_CHECK_CONTRACT = ToolContract.from_models(
    name="workspace.snapshot_check",
    version="1.0.0",
    description="Parse a bounded text snapshot with one fixed verification profile.",
    input_model=WorkspaceCheckInput,
    output_model=WorkspaceCheckOutput,
    risk_level=ToolRiskLevel.R0,
    execution=ToolExecutionContract(
        timeout_seconds=10,
        idempotency=ToolIdempotency.IDEMPOTENT,
        max_output_bytes=65_536,
    ),
    security=ToolSecurityContract(),
)


def project_workspace_check_resources(_: BaseModel) -> tuple[PolicyResource, ...]:
    return ()


def execute_workspace_check(
    arguments: BaseModel,
    cancellation: Event,
    context: ToolExecutionContext,
) -> BaseModel:
    if not isinstance(arguments, WorkspaceCheckInput):
        raise TypeError("workspace.snapshot_check received an unexpected input model")
    if context.resources:
        raise TypeError("workspace.snapshot_check does not accept brokered resources")
    issues: list[WorkspaceCheckIssueOutput] = []
    for item in arguments.files:
        if cancellation.is_set():
            raise ToolExecutionCancelledError("Workspace check was cancelled")
        try:
            if arguments.profile == "python-syntax":
                ast.parse(item.content, filename=item.relative_path)
            else:
                json.loads(item.content)
        except SyntaxError as error:
            issues.append(
                WorkspaceCheckIssueOutput(
                    relative_path=item.relative_path,
                    line=max(1, error.lineno or 1),
                    column=max(1, error.offset or 1),
                    code="PYTHON_SYNTAX_INVALID",
                    message=(error.msg or "invalid syntax")[:300],
                )
            )
        except json.JSONDecodeError as error:
            issues.append(
                WorkspaceCheckIssueOutput(
                    relative_path=item.relative_path,
                    line=max(1, error.lineno),
                    column=max(1, error.colno),
                    code="JSON_INVALID",
                    message=error.msg[:300],
                )
            )
    return WorkspaceCheckOutput(
        profile=arguments.profile,
        relative_path=arguments.relative_path,
        snapshot_digest=arguments.snapshot_digest,
        status="failed" if issues else "passed",
        checked_file_count=len(arguments.files),
        issues=tuple(issues),
    )


def create_workspace_check_executor() -> ToolExecutor:
    executor = ToolExecutor()
    executor.register(
        WORKSPACE_CHECK_CONTRACT,
        WorkspaceCheckInput,
        WorkspaceCheckOutput,
        project_workspace_check_resources,
        execute_workspace_check,
    )
    return executor


__all__ = [
    "WORKSPACE_CHECK_CONTRACT",
    "WorkspaceCheckFile",
    "WorkspaceCheckInput",
    "WorkspaceCheckOutput",
    "create_workspace_check_executor",
    "execute_workspace_check",
]
