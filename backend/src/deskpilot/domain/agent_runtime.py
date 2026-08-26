"""Persistent Agent execution manifests and proof-checked read projections."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN, BoundAgentRef
from deskpilot.domain.task_plans import (
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    CapabilityRef,
    PlanNodeBudget,
)
from deskpilot.domain.workspace_files import (
    WorkspaceNodeTestRead,
    WorkspacePatchPreview,
    WorkspacePatchTestRead,
    WorkspacePythonTestRead,
)

RUN_ID_PATTERN = r"^run_[0-9a-f]{64}$"
HANDOFF_ID_PATTERN = r"^hnd_[0-9a-f]{64}$"
INVOCATION_ID_PATTERN = r"^inv_[0-9a-f]{64}$"
MODEL_TURN_ID_PATTERN = r"^amt_[0-9a-f]{64}$"
MODEL_DISPATCH_ID_PATTERN = r"^mdp_[0-9a-f]{64}$"
AGENT_DECISION_ID_PATTERN = r"^agd_[0-9a-f]{64}$"
AGENT_OBSERVATION_ID_PATTERN = r"^obs_[0-9a-f]{64}$"
AGENT_INPUT_REQUEST_ID_PATTERN = r"^air_[0-9a-f]{64}$"
AGENT_DELEGATION_ID_PATTERN = r"^dlg_[0-9a-f]{64}$"
AGENT_TASK_GRAPH_ID_PATTERN = r"^atg_[0-9a-f]{64}$"
AGENT_TASK_GRAPH_BINDING_ID_PATTERN = r"^tgb_[0-9a-f]{64}$"
RESULT_ID_PATTERN = r"^res_[0-9a-f]{64}$"


class ExecutionRunStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_VERIFICATION = "awaiting_verification"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class ExecutionNodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_CHILDREN = "waiting_children"
    AWAITING_VERIFICATION = "awaiting_verification"
    VERIFIED = "verified"
    CANCELLED = "cancelled"
    FAILED = "failed"


class InvocationExecutionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_CHILDREN = "waiting_children"
    RESULT_SUBMITTED = "result_submitted"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class InvocationVerificationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ModelTurnStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AgentTaskGraphResultRef(BaseModel):
    """Immutable server-authored reference from one verified DAG child result."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-task-graph-result-ref.v1"] = (
        "deskpilot.agent-task-graph-result-ref.v1"
    )
    graph_id: str = Field(pattern=AGENT_TASK_GRAPH_ID_PATTERN)
    producer_local_key: str = Field(min_length=1, max_length=64)
    producer_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    producer_invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    producer_result_id: str = Field(pattern=RESULT_ID_PATTERN)
    capability: CapabilityRef
    result_kind: Literal["file", "directory", "python_test", "node_test", "patch_test"]
    agent_result_digest: str = Field(pattern=DIGEST_PATTERN)
    workspace_result_digest: str = Field(pattern=DIGEST_PATTERN)
    result_ref_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_ref_digest"})
        if self.result_ref_digest != sha256_digest(material):
            raise ValueError("Agent task graph ResultRef digest does not match")
        return self


class AgentTaskGraphCapabilityInput(BaseModel):
    """Server-bound capability input selected from an authorized Route slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[
        "deskpilot.agent-task-graph-capability-input.v1",
        "deskpilot.agent-task-graph-capability-input.v2",
        "deskpilot.agent-task-graph-capability-input.v3",
        "deskpilot.agent-task-graph-capability-input.v4",
    ] = "deskpilot.agent-task-graph-capability-input.v1"
    source_key: Literal[
        "route_directory_path",
        "route_explicit_file_path",
        "route_python_test_spec",
        "route_node_test_spec",
        "route_patch_test_spec",
    ]
    source_ref: str = Field(min_length=1, max_length=500)
    read_kind: Literal["file", "directory", "python_test", "node_test", "patch_test"]
    path: str = Field(min_length=1, max_length=32_767)
    test_path: str | None = Field(default=None, min_length=1, max_length=32_767)
    target_path: str | None = Field(default=None, min_length=1, max_length=32_767)
    test_kind: Literal["python", "node"] | None = None
    objective: str | None = Field(default=None, min_length=1, max_length=500)
    binding_key: str | None = Field(default=None, min_length=1, max_length=64)
    route_parameter_digest: str = Field(pattern=DIGEST_PATTERN)
    input_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.schema_version == "deskpilot.agent-task-graph-capability-input.v1":
            if (
                self.source_key not in {"route_directory_path", "route_explicit_file_path"}
                or self.read_kind not in {"file", "directory"}
                or self.test_path is not None
                or self.target_path is not None
                or self.test_kind is not None
                or self.objective is not None
            ):
                raise ValueError("Legacy capability input contains a test binding")
        elif self.schema_version == "deskpilot.agent-task-graph-capability-input.v2" and (
            self.source_key not in {"route_python_test_spec", "route_node_test_spec"}
            or (
                self.source_key == "route_python_test_spec"
                and (self.read_kind != "python_test" or self.test_path is None)
            )
            or (
                self.source_key == "route_node_test_spec"
                and (self.read_kind != "node_test" or self.test_path is None)
            )
            or self.source_key
            in {
                "route_directory_path",
                "route_explicit_file_path",
            }
            or self.target_path is not None
            or self.test_kind is not None
            or self.objective is not None
        ):
            raise ValueError("Test capability input kind does not match its source")
        elif self.schema_version == "deskpilot.agent-task-graph-capability-input.v3" and (
            self.source_key != "route_patch_test_spec"
            or self.read_kind != "patch_test"
            or self.test_path is None
            or self.target_path is None
            or self.test_kind is None
            or self.objective is None
        ):
            raise ValueError("Patch approval input is incomplete")
        elif self.schema_version == "deskpilot.agent-task-graph-capability-input.v4" and (
            self.source_key != "route_patch_test_spec"
            or self.read_kind != "patch_test"
            or self.test_path is None
            or self.target_path is None
            or self.test_kind is None
            or self.objective is None
            or self.binding_key is None
        ):
            raise ValueError("Composable Patch approval input is incomplete")
        if (
            self.schema_version != "deskpilot.agent-task-graph-capability-input.v4"
            and self.binding_key is not None
        ):
            raise ValueError("Legacy capability input contains a binding key")
        excluded = {"input_digest"}
        if self.schema_version == "deskpilot.agent-task-graph-capability-input.v1":
            excluded.update({"test_path", "target_path", "test_kind", "objective", "binding_key"})
        elif self.schema_version == "deskpilot.agent-task-graph-capability-input.v2":
            excluded.update({"target_path", "test_kind", "objective", "binding_key"})
        elif self.schema_version == "deskpilot.agent-task-graph-capability-input.v3":
            excluded.add("binding_key")
        material = self.model_dump(mode="json", exclude=excluded)
        if self.input_digest != sha256_digest(material):
            raise ValueError("Agent task graph capability input digest does not match")
        return self


class HandoffEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.handoff.v1"] = "deskpilot.handoff.v1"
    handoff_id: str = Field(pattern=HANDOFF_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    target_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    target_agent: BoundAgentRef
    objective_ref: str = Field(min_length=1, max_length=500)
    acceptance_criteria: tuple[str, ...] = Field(max_length=50)
    constraint_refs: tuple[str, ...] = Field(max_length=50)
    allowed_context_sources: tuple[str, ...] = Field(max_length=20)
    capability: CapabilityRef | None = None
    effective_tool_scope_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    budget_allocation: PlanNodeBudget
    parent_invocation_id: str | None = Field(default=None, pattern=INVOCATION_ID_PATTERN)
    upstream_result_refs: tuple[AgentTaskGraphResultRef, ...] = Field(default=(), max_length=7)
    capability_input: AgentTaskGraphCapabilityInput | None = None
    handoff_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        excluded = {"handoff_digest"}
        # Handoff v1 existed before typed DAG inputs. Preserve proof validation for
        # old persisted manifests that do not contain the additive field.
        if "upstream_result_refs" not in self.model_fields_set:
            excluded.add("upstream_result_refs")
        if "capability_input" not in self.model_fields_set:
            excluded.add("capability_input")
        material = self.model_dump(mode="json", exclude=excluded)
        if self.handoff_digest != sha256_digest(material):
            raise ValueError("Handoff digest does not match")
        return self


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-result.v1"] = "deskpilot.agent-result.v1"
    result_id: str = Field(pattern=RESULT_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    disposition: Literal["candidate"] = "candidate"
    claim_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    citation_ids: tuple[str, ...] = Field(min_length=1, max_length=200)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)
    input_digest: str = Field(pattern=DIGEST_PATTERN)
    model_response_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Agent Result digest does not match")
        return self


class AgentOutputResult(BaseModel):
    """Generic candidate envelope validated against the bound Agent output schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-output-result.v1"] = "deskpilot.agent-output-result.v1"
    result_id: str = Field(pattern=RESULT_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    disposition: Literal["candidate"] = "candidate"
    output: dict[str, object]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=200)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)
    input_digest: str = Field(pattern=DIGEST_PATTERN)
    model_response_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Agent Output Result digest does not match")
        return self


class BoundAgentTaskGraphCondition(BaseModel):
    """Server-bound condition that may unlock one dynamic graph edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-task-graph-condition.v1"] = (
        "deskpilot.agent-task-graph-condition.v1"
    )
    source_local_key: str
    source_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    predicate: Literal["test_passed"] = "test_passed"
    condition_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.condition_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"condition_digest"})
        ):
            raise ValueError("Task graph condition digest does not match")
        return self


class AgentTaskGraphConditionDecision(BaseModel):
    """Persisted server adjudication of one bound test-result condition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-task-graph-condition-decision.v1"] = (
        "deskpilot.agent-task-graph-condition-decision.v1"
    )
    graph_id: str = Field(pattern=AGENT_TASK_GRAPH_ID_PATTERN)
    source_local_key: str
    source_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    target_local_key: str
    target_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    predicate: Literal["test_passed"] = "test_passed"
    actual_status: Literal[
        "passed",
        "failed",
        "error",
        "verified",
        "test_failed",
        "test_error",
    ]
    result_ref_digest: str = Field(pattern=DIGEST_PATTERN)
    matched: bool
    decision_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def proof_matches(self) -> Self:
        if self.matched != (self.actual_status in {"passed", "verified"}):
            raise ValueError("Task graph condition decision does not match test status")
        if self.decision_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"decision_digest"})
        ):
            raise ValueError("Task graph condition decision digest does not match")
        return self


class AgentTaskGraphApprovalBinding(BaseModel):
    """Server-authored proof that one graph node owns one fresh approval slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-task-graph-approval-binding.v1"] = (
        "deskpilot.agent-task-graph-approval-binding.v1"
    )
    approval_binding_id: str = Field(pattern=r"^apb_[0-9a-f]{64}$")
    approval_kind: Literal["workspace_patch"] = "workspace_patch"
    graph_id: str = Field(pattern=AGENT_TASK_GRAPH_ID_PATTERN)
    local_key: str = Field(min_length=1, max_length=64)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    capability_input_digest: str = Field(pattern=DIGEST_PATTERN)
    confirmation_policy: Literal["fresh_user_confirmation_per_node_v1"] = (
        "fresh_user_confirmation_per_node_v1"
    )
    manifest_policy: Literal["content_addressed_workspace_manifest_v1"] = (
        "content_addressed_workspace_manifest_v1"
    )
    approval_binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"approval_binding_digest"})
        if self.approval_binding_digest != sha256_digest(material):
            raise ValueError("Task graph approval binding digest does not match")
        return self


class BoundAgentTaskGraphNode(BaseModel):
    """Server-bound immutable node in a dynamically proposed child graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    local_key: str
    runtime_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    runtime_local_key: str
    binding_id: str = Field(pattern=r"^hbn_[0-9a-f]{64}$")
    target_agent: BoundAgentRef
    capability: CapabilityRef
    objective: str = Field(min_length=1, max_length=500)
    context_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    capability_input: AgentTaskGraphCapabilityInput | None = None
    depends_on: tuple[str, ...] = ()
    depends_on_node_ids: tuple[str, ...] = ()
    conditions: tuple[BoundAgentTaskGraphCondition, ...] = ()
    import_sources: tuple[str, ...] = ()
    imported_result_refs: tuple[AgentTaskGraphResultRef, ...] = ()
    approval_binding: AgentTaskGraphApprovalBinding | None = None
    budget_allocation: PlanNodeBudget
    node_spec_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        excluded = {"node_spec_digest"}
        if "capability_input" not in self.model_fields_set:
            excluded.add("capability_input")
        if "conditions" not in self.model_fields_set:
            excluded.add("conditions")
        if "import_sources" not in self.model_fields_set:
            excluded.add("import_sources")
        if "imported_result_refs" not in self.model_fields_set:
            excluded.add("imported_result_refs")
        if "approval_binding" not in self.model_fields_set:
            excluded.add("approval_binding")
        if (
            len(self.import_sources) != len(self.imported_result_refs)
            or len(self.import_sources) != len(set(self.import_sources))
            or len(self.imported_result_refs)
            != len({item.result_ref_digest for item in self.imported_result_refs})
        ):
            raise ValueError("Bound task graph imported ResultRefs do not match their sources")
        material = self.model_dump(mode="json", exclude=excluded)
        if self.node_spec_digest != sha256_digest(material):
            raise ValueError("Bound task graph node digest does not match")
        return self


class AgentTaskGraphManifest(BaseModel):
    """Immutable server binding of a model proposal to an executable child DAG."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[
        "deskpilot.agent-task-graph.v1",
        "deskpilot.agent-task-graph.v2",
        "deskpilot.agent-task-graph.v3",
        "deskpilot.agent-task-graph.v4",
        "deskpilot.agent-task-graph.v5",
        "deskpilot.agent-task-graph.v6",
        "deskpilot.agent-task-graph.v7",
        "deskpilot.agent-task-graph.v8",
    ] = "deskpilot.agent-task-graph.v7"
    graph_id: str = Field(pattern=AGENT_TASK_GRAPH_ID_PATTERN)
    binding_id: str = Field(pattern=AGENT_TASK_GRAPH_BINDING_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    plan_digest: str = Field(pattern=DIGEST_PATTERN)
    parent_invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    parent_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    decision_id: str = Field(pattern=AGENT_DECISION_ID_PATTERN)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    output_local_key: str | None = Field(default=None, min_length=1, max_length=64)
    output_node_id: str | None = Field(default=None, pattern=PLAN_NODE_ID_PATTERN)
    nodes: tuple[BoundAgentTaskGraphNode, ...] = Field(min_length=1, max_length=8)
    graph_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def graph_and_digest_match(self) -> Self:
        keys = tuple(item.local_key for item in self.nodes)
        if len(keys) != len(set(keys)):
            raise ValueError("Bound task graph node keys must be unique")
        known = set(keys)
        node_ids = {item.local_key: item.runtime_node_id for item in self.nodes}
        if self.schema_version in {
            "deskpilot.agent-task-graph.v2",
            "deskpilot.agent-task-graph.v3",
            "deskpilot.agent-task-graph.v4",
            "deskpilot.agent-task-graph.v5",
            "deskpilot.agent-task-graph.v6",
            "deskpilot.agent-task-graph.v7",
            "deskpilot.agent-task-graph.v8",
        } and (
            self.output_local_key not in known
            or self.output_node_id != node_ids.get(self.output_local_key or "")
        ):
            raise ValueError("Bound task graph output binding changed")
        if self.schema_version in {
            "deskpilot.agent-task-graph.v3",
            "deskpilot.agent-task-graph.v4",
            "deskpilot.agent-task-graph.v5",
            "deskpilot.agent-task-graph.v6",
            "deskpilot.agent-task-graph.v7",
            "deskpilot.agent-task-graph.v8",
        } and any(item.capability_input is None for item in self.nodes):
            raise ValueError("Bound task graph capability input is missing")
        if self.schema_version not in {
            "deskpilot.agent-task-graph.v5",
            "deskpilot.agent-task-graph.v6",
            "deskpilot.agent-task-graph.v7",
            "deskpilot.agent-task-graph.v8",
        } and any(item.import_sources or item.imported_result_refs for item in self.nodes):
            raise ValueError("Legacy task graph contains cross-generation ResultRefs")
        if self.schema_version not in {
            "deskpilot.agent-task-graph.v7",
            "deskpilot.agent-task-graph.v8",
        } and any(item.conditions for item in self.nodes):
            raise ValueError("Legacy task graph contains conditional edges")
        if self.schema_version != "deskpilot.agent-task-graph.v8" and any(
            item.approval_binding is not None for item in self.nodes
        ):
            raise ValueError("Legacy task graph contains approval bindings")
        if self.schema_version == "deskpilot.agent-task-graph.v8":
            for item in self.nodes:
                is_patch = item.capability.capability_id == "workspace.patch.propose.v1"
                binding = item.approval_binding
                capability_input = item.capability_input
                if is_patch and (
                    binding is None
                    or capability_input is None
                    or capability_input.schema_version
                    != "deskpilot.agent-task-graph-capability-input.v4"
                    or binding.graph_id != self.graph_id
                    or binding.local_key != item.local_key
                    or binding.node_id != item.runtime_node_id
                    or binding.capability_input_digest != capability_input.input_digest
                ):
                    raise ValueError("Composable Patch approval binding changed")
                if not is_patch and binding is not None:
                    raise ValueError("Non-Patch graph node contains an approval binding")
        graph = {item.local_key: item.depends_on for item in self.nodes}
        for node in self.nodes:
            if (
                len(node.depends_on) != len(set(node.depends_on))
                or node.local_key in node.depends_on
                or any(source not in known for source in node.depends_on)
            ):
                raise ValueError("Bound task graph dependency is unknown")
            if node.depends_on_node_ids != tuple(node_ids[source] for source in node.depends_on):
                raise ValueError("Bound task graph dependency IDs changed")
            condition_sources = tuple(item.source_local_key for item in node.conditions)
            if (
                len(condition_sources) != len(set(condition_sources))
                or any(source not in node.depends_on for source in condition_sources)
                or any(
                    item.source_node_id != node_ids[item.source_local_key]
                    for item in node.conditions
                )
            ):
                raise ValueError("Bound task graph conditions changed")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("Bound task graph contains a cycle")
            if key in visited:
                return
            visiting.add(key)
            for source in graph[key]:
                visit(source)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        if self.output_local_key is not None:
            contributing: set[str] = set()

            def collect(key: str) -> None:
                if key in contributing:
                    return
                contributing.add(key)
                for source in graph[key]:
                    collect(source)

            collect(self.output_local_key)
            if contributing != known:
                raise ValueError("Bound task graph output omits graph nodes")
        excluded = {"graph_digest"}
        if self.schema_version == "deskpilot.agent-task-graph.v1":
            if "output_local_key" not in self.model_fields_set:
                excluded.add("output_local_key")
            if "output_node_id" not in self.model_fields_set:
                excluded.add("output_node_id")
        material = self.model_dump(mode="json", exclude=excluded)
        if any(
            "capability_input" not in item.model_fields_set
            or "conditions" not in item.model_fields_set
            or "import_sources" not in item.model_fields_set
            or "imported_result_refs" not in item.model_fields_set
            or "approval_binding" not in item.model_fields_set
            for item in self.nodes
        ):
            material["nodes"] = [
                item.model_dump(
                    mode="json",
                    exclude={
                        field
                        for field in (
                            "capability_input",
                            "conditions",
                            "import_sources",
                            "imported_result_refs",
                            "approval_binding",
                        )
                        if field not in item.model_fields_set
                    },
                )
                for item in self.nodes
            ]
        if self.graph_digest != sha256_digest(material):
            raise ValueError("Agent task graph digest does not match")
        return self


class ExecutionNodeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    local_key: str
    status: ExecutionNodeStatus
    revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    claim_owner_id: str | None
    claim_fencing_token: int = Field(ge=0)
    claim_expires_at: datetime | None
    bound_agent: BoundAgentRef | None
    depends_on: tuple[str, ...] = ()
    handoff_parent_node_id: str | None = Field(default=None, pattern=PLAN_NODE_ID_PATTERN)
    budget: PlanNodeBudget
    runtime_enabled: bool


class AgentInvocationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    attempt: int = Field(ge=1)
    handoff_id: str = Field(pattern=HANDOFF_ID_PATTERN)
    parent_invocation_id: str | None = Field(default=None, pattern=INVOCATION_ID_PATTERN)
    agent: BoundAgentRef
    execution_status: InvocationExecutionStatus
    verification_status: InvocationVerificationStatus
    result_id: str | None = Field(default=None, pattern=RESULT_ID_PATTERN)
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AgentModelTurnRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    turn_id: str = Field(pattern=MODEL_TURN_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    turn_no: int = Field(ge=1)
    status: ModelTurnStatus
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    response_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    provider_id: str | None
    model: str | None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_micros: int = Field(ge=0)
    stable_error_code: str | None
    decision_kind: (
        Literal[
            "request_route",
            "submit_result",
            "needs_user_input",
            "propose_handoff",
            "propose_task_graph",
            "propose_file_set",
        ]
        | None
    ) = None
    decision_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    binding_id: str | None = None
    observation_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)


class AgentDelegationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    delegation_id: str = Field(pattern=AGENT_DELEGATION_ID_PATTERN)
    parent_invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    child_invocation_id: str | None = Field(default=None, pattern=INVOCATION_ID_PATTERN)
    parent_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    child_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    decision_id: str = Field(pattern=AGENT_DECISION_ID_PATTERN)
    binding_id: str = Field(pattern=r"^hbn_[0-9a-f]{64}$")
    status: Literal["waiting_child", "child_verified", "consumed", "cancelled", "failed"]
    depth: int = Field(ge=1, le=10)
    budget_allocation: PlanNodeBudget
    child_result_id: str | None = Field(default=None, pattern=RESULT_ID_PATTERN)
    observation_id: str | None = Field(default=None, pattern=AGENT_OBSERVATION_ID_PATTERN)
    created_at: datetime
    updated_at: datetime


class AgentTaskGraphNodeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    local_key: str
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    binding_id: str = Field(pattern=r"^hbn_[0-9a-f]{64}$")
    status: Literal["waiting_child", "child_verified", "consumed", "cancelled", "failed"]
    depends_on: tuple[str, ...]
    target_agent: BoundAgentRef
    capability: CapabilityRef
    capability_input: AgentTaskGraphCapabilityInput | None = None
    conditions: tuple[BoundAgentTaskGraphCondition, ...] = ()
    condition_decisions: tuple[AgentTaskGraphConditionDecision, ...] = ()
    import_sources: tuple[str, ...] = ()
    imported_result_refs: tuple[AgentTaskGraphResultRef, ...] = ()
    approval_binding: AgentTaskGraphApprovalBinding | None = None
    budget_allocation: PlanNodeBudget
    child_invocation_id: str | None = Field(default=None, pattern=INVOCATION_ID_PATTERN)
    child_result_id: str | None = Field(default=None, pattern=RESULT_ID_PATTERN)
    result_ref: AgentTaskGraphResultRef | None = None
    test_result: WorkspacePythonTestRead | WorkspaceNodeTestRead | None = None
    approval: WorkspacePatchPreview | None = None
    patch_result: WorkspacePatchTestRead | None = None


class AgentTaskGraphRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[
        "deskpilot.agent-task-graph.v1",
        "deskpilot.agent-task-graph.v2",
        "deskpilot.agent-task-graph.v3",
        "deskpilot.agent-task-graph.v4",
        "deskpilot.agent-task-graph.v5",
        "deskpilot.agent-task-graph.v6",
        "deskpilot.agent-task-graph.v7",
        "deskpilot.agent-task-graph.v8",
    ]
    graph_id: str = Field(pattern=AGENT_TASK_GRAPH_ID_PATTERN)
    binding_id: str = Field(pattern=AGENT_TASK_GRAPH_BINDING_ID_PATTERN)
    parent_invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    parent_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    decision_id: str = Field(pattern=AGENT_DECISION_ID_PATTERN)
    status: Literal["running", "verified", "consumed", "cancelled", "failed"]
    node_count: int = Field(ge=1, le=8)
    max_depth: int = Field(ge=1, le=8)
    graph_digest: str = Field(pattern=DIGEST_PATTERN)
    output_local_key: str | None = None
    output_node_id: str | None = Field(default=None, pattern=PLAN_NODE_ID_PATTERN)
    observation_id: str | None = Field(default=None, pattern=AGENT_OBSERVATION_ID_PATTERN)
    nodes: tuple[AgentTaskGraphNodeRead, ...]
    created_at: datetime
    updated_at: datetime


class AgentInputRequestRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_request_id: str = Field(pattern=AGENT_INPUT_REQUEST_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    decision_id: str = Field(pattern=AGENT_DECISION_ID_PATTERN)
    question_code: str
    question: str
    blocking_fields: tuple[str, ...] = Field(min_length=1, max_length=10)
    answer_schema: str
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    status: Literal["pending", "resolved", "cancelled"]
    resolved_task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    answer_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    created_at: datetime
    resolved_at: datetime | None


class ClaimedInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handoff: HandoffEnvelope
    invocation: AgentInvocationRead
    claim_owner_id: str
    claim_fencing_token: int = Field(ge=1)
    claim_expires_at: datetime


class ExecutionRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    plan_digest: str = Field(pattern=DIGEST_PATTERN)
    status: ExecutionRunStatus
    revision: int = Field(ge=1)
    nodes: tuple[ExecutionNodeRead, ...]
    invocations: tuple[AgentInvocationRead, ...]
    model_turns: tuple[AgentModelTurnRead, ...] = ()
    input_requests: tuple[AgentInputRequestRead, ...] = ()
    delegations: tuple[AgentDelegationRead, ...] = ()
    task_graphs: tuple[AgentTaskGraphRead, ...] = ()
    created_at: datetime
    updated_at: datetime


class ExecutionRunPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runs: tuple[ExecutionRunRead, ...]
