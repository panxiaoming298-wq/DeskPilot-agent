"""Structured intent and plan outputs consumed by task orchestration."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.tool_contracts import (
    SEMVER_PATTERN,
    TOOL_NAME_PATTERN,
    ToolRiskLevel,
)


class TaskIntent(StrEnum):
    COMPUTER_INFO = "computer_info"
    FILE = "file"
    APPLICATION = "application"
    BROWSER = "browser"
    SEARCH = "search"
    GENERAL = "general"


class TaskComplexity(StrEnum):
    SIMPLE = "simple"
    COMPOUND = "compound"


class TaskClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: TaskIntent
    complexity: TaskComplexity
    risk_level: ToolRiskLevel
    requires_planning: bool
    confidence: float = Field(ge=0, le=1)
    recommended_agent: str = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=500)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    agent: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    tool_name: str | None = Field(default=None, pattern=TOOL_NAME_PATTERN)
    tool_version: str | None = Field(default=None, pattern=SEMVER_PATTERN)
    depends_on: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_tool_reference(self) -> Self:
        if (self.tool_name is None) != (self.tool_version is None):
            raise ValueError("tool_name and tool_version must be provided together")
        if self.step_id in self.depends_on:
            raise ValueError("plan step cannot depend on itself")
        return self


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=500)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_graph_references(self) -> Self:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step_id values must be unique")
        known: set[str] = set()
        for step in self.steps:
            if any(dependency not in known for dependency in step.depends_on):
                raise ValueError("plan dependencies must reference an earlier step")
            known.add(step.step_id)
        return self
