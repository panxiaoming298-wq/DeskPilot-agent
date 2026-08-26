"""Strict decisions for the bounded reusable Agent Model Loop."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from deskpilot.domain.task_plans import (
    CAPABILITY_ID_PATTERN,
    TOKEN_PATTERN,
    PlanNodeBudget,
)


class WorkspaceRouteRequestDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["request_route"] = "request_route"
    route_binding_id: str = Field(pattern=r"^rbn_[0-9a-f]{64}$")
    path: str = Field(min_length=1, max_length=500)
    test_path: str | None = Field(default=None, min_length=1, max_length=500)
    decision_summary: str = Field(min_length=1, max_length=300)


class AgentNeedsUserInputDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["needs_user_input"] = "needs_user_input"
    question_code: Literal["WORKSPACE_FILE_PATH_REQUIRED"]
    question: str = Field(min_length=1, max_length=300)
    blocking_fields: tuple[Literal["path"], ...] = Field(min_length=1, max_length=1)
    answer_schema: Literal["workspace_relative_file_path.v1"]
    insufficient_context: str = Field(min_length=1, max_length=300)
    completed_actions: tuple[str, ...] = Field(default=(), max_length=10)
    pending_actions: tuple[str, ...] = Field(min_length=1, max_length=10)
    decision_summary: str = Field(min_length=1, max_length=300)


class WorkspaceSubmitResultDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["submit_result"] = "submit_result"
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_summary: str = Field(min_length=1, max_length=300)


class WorkspacePatchChangeProposal(BaseModel):
    """One untrusted exact replacement proposed without write authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=500)
    old_text: str = Field(min_length=1, max_length=4_096)
    new_text: str = Field(max_length=4_096)
    rationale: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def replacement_changes_content(self) -> Self:
        if self.old_text == self.new_text:
            raise ValueError("Workspace patch proposal cannot be a no-op")
        return self


class WorkspacePatchSubmitProposalDecision(BaseModel):
    """Candidate patch proposal; submit_result does not authorize its application."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["submit_result"] = "submit_result"
    patch_binding_id: str = Field(pattern=r"^ptb_[0-9a-f]{64}$")
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: tuple[WorkspacePatchChangeProposal, ...] = Field(min_length=1, max_length=1)
    decision_summary: str = Field(min_length=1, max_length=300)


WorkspacePatchLoopDecisionValue = Annotated[
    WorkspaceRouteRequestDecision | WorkspacePatchSubmitProposalDecision,
    Field(discriminator="kind"),
]


class WorkspacePatchLoopDecision(RootModel[WorkspacePatchLoopDecisionValue]):
    pass


class AgentProposeHandoffDecision(BaseModel):
    """Untrusted request for one server-bound, precompiled child Agent slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["propose_handoff"] = "propose_handoff"
    handoff_binding_id: str = Field(pattern=r"^hbn_[0-9a-f]{64}$")
    target_capability_id: Literal["workspace.directory.read.v1"]
    objective_ref: str = Field(min_length=1, max_length=500)
    context_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    budget_slice: PlanNodeBudget
    decision_summary: str = Field(min_length=1, max_length=300)


class CoordinatorSubmitResultDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["submit_result"] = "submit_result"
    child_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_summary: str = Field(min_length=1, max_length=300)


class AgentTaskGraphConditionProposal(BaseModel):
    """Untrusted request for one server-defined test-result predicate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_local_key: str = Field(pattern=TOKEN_PATTERN)
    predicate: Literal["test_passed"] = "test_passed"


class AgentTaskGraphNodeProposal(BaseModel):
    """One untrusted node in a model-proposed, server-bound child DAG."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    local_key: str = Field(pattern=TOKEN_PATTERN)
    target_capability_id: str = Field(pattern=CAPABILITY_ID_PATTERN)
    objective: str = Field(min_length=1, max_length=500)
    context_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    input_source: Literal[
        "route_directory_path",
        "route_explicit_file_path",
        "route_python_test_spec",
        "route_node_test_spec",
        "route_patch_test_spec",
    ]
    input_binding_key: Annotated[str, Field(pattern=TOKEN_PATTERN)] | None = None
    depends_on: tuple[str, ...] = Field(default=(), max_length=7)
    conditions: tuple[AgentTaskGraphConditionProposal, ...] = Field(default=(), max_length=7)
    import_sources: tuple[Annotated[str, Field(pattern=TOKEN_PATTERN)], ...] = Field(
        default=(), max_length=7
    )
    budget_slice: PlanNodeBudget

    @model_validator(mode="after")
    def references_are_unique(self) -> Self:
        if len(self.context_refs) != len(set(self.context_refs)):
            raise ValueError("Task graph context references must be unique")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Task graph dependencies must be unique")
        condition_sources = tuple(item.source_local_key for item in self.conditions)
        if len(condition_sources) != len(set(condition_sources)):
            raise ValueError("Task graph condition sources must be unique")
        if any(source not in self.depends_on for source in condition_sources):
            raise ValueError("Task graph conditions must reference a dependency")
        if len(self.import_sources) != len(set(self.import_sources)):
            raise ValueError("Task graph imported sources must be unique")
        if self.local_key in self.depends_on:
            raise ValueError("Task graph node cannot depend on itself")
        return self


class AgentProposeTaskGraphDecision(BaseModel):
    """Untrusted complete DAG proposal; only the Supervisor can bind it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["propose_task_graph"] = "propose_task_graph"
    nodes: tuple[AgentTaskGraphNodeProposal, ...] = Field(min_length=1, max_length=8)
    output_node_key: str = Field(pattern=TOKEN_PATTERN)
    decision_summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def graph_is_a_dag(self) -> Self:
        keys = tuple(item.local_key for item in self.nodes)
        if len(keys) != len(set(keys)):
            raise ValueError("Task graph node keys must be unique")
        known = set(keys)
        if self.output_node_key not in known:
            raise ValueError("Task graph output node is unknown")
        graph = {item.local_key: item.depends_on for item in self.nodes}
        for item in self.nodes:
            if any(source not in known for source in item.depends_on):
                raise ValueError("Task graph dependency is unknown")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("Task graph contains a cycle")
            if key in visited:
                return
            visiting.add(key)
            for source in graph[key]:
                visit(source)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        contributing: set[str] = set()

        def collect(key: str) -> None:
            if key in contributing:
                return
            contributing.add(key)
            for source in graph[key]:
                collect(source)

        collect(self.output_node_key)
        if contributing != known:
            raise ValueError("Task graph output node must depend on every graph node")
        return self


class WorkspaceBoundedCodingGraphNodeProposal(AgentTaskGraphNodeProposal):
    """A coding-only node whose Patch join may consume all eight planners."""

    depends_on: tuple[str, ...] = Field(default=(), max_length=8)


class WorkspaceBoundedCodingGraphDecision(BaseModel):
    """Exact 3..8-file coding DAG confirmation under a separate Agent contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v2"] = (
        "deskpilot.agent-decision.v2"
    )
    kind: Literal["propose_task_graph"] = "propose_task_graph"
    nodes: tuple[WorkspaceBoundedCodingGraphNodeProposal, ...] = Field(
        min_length=9,
        max_length=19,
    )
    output_node_key: str = Field(pattern=TOKEN_PATTERN)
    decision_summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def graph_is_a_dag(self) -> Self:
        keys = tuple(item.local_key for item in self.nodes)
        if len(keys) != len(set(keys)):
            raise ValueError("Task graph node keys must be unique")
        known = set(keys)
        if self.output_node_key not in known:
            raise ValueError("Task graph output node is unknown")
        graph = {item.local_key: item.depends_on for item in self.nodes}
        for item in self.nodes:
            if any(source not in known for source in item.depends_on):
                raise ValueError("Task graph dependency is unknown")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("Task graph contains a cycle")
            if key in visited:
                return
            visiting.add(key)
            for source in graph[key]:
                visit(source)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        contributing: set[str] = set()

        def collect(key: str) -> None:
            if key in contributing:
                return
            contributing.add(key)
            for source in graph[key]:
                collect(source)

        collect(self.output_node_key)
        if contributing != known:
            raise ValueError("Task graph output node must depend on every graph node")
        return self


class DynamicCoordinatorSubmitResultDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-decision.v1"] = "deskpilot.agent-decision.v1"
    kind: Literal["submit_result"] = "submit_result"
    task_graph_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_summary: str = Field(min_length=1, max_length=300)


WorkspaceLoopDecisionValue = Annotated[
    WorkspaceRouteRequestDecision | AgentNeedsUserInputDecision | WorkspaceSubmitResultDecision,
    Field(discriminator="kind"),
]


class WorkspaceLoopDecision(RootModel[WorkspaceLoopDecisionValue]):
    pass


CoordinatorLoopDecisionValue = Annotated[
    AgentProposeHandoffDecision | CoordinatorSubmitResultDecision,
    Field(discriminator="kind"),
]


class CoordinatorLoopDecision(RootModel[CoordinatorLoopDecisionValue]):
    pass


DynamicCoordinatorLoopDecisionValue = Annotated[
    AgentProposeTaskGraphDecision | DynamicCoordinatorSubmitResultDecision,
    Field(discriminator="kind"),
]


class DynamicCoordinatorLoopDecision(RootModel[DynamicCoordinatorLoopDecisionValue]):
    pass


class WorkspaceBoundedCodingCoordinatorDecision(
    RootModel[WorkspaceBoundedCodingGraphDecision]
):
    pass
