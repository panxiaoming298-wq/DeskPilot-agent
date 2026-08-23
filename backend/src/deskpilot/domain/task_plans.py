"""Immutable conversation, Task Contract, capability and plan manifests."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import (
    AGENT_ID_PATTERN,
    DIGEST_PATTERN,
    AgentToolGrant,
    BoundAgentRef,
)
from deskpilot.domain.model_contracts import ModelLocation, PrivacyMode
from deskpilot.domain.tool_contracts import SEMVER_PATTERN, TOOL_NAME_PATTERN, ToolRiskLevel

TASK_ID_PATTERN = r"^tsk_[0-9a-f]{32}$"
CONTRACT_ID_PATTERN = r"^tc_[0-9a-f]{32}$"
CONVERSATION_ID_PATTERN = r"^cnv_[0-9a-f]{32}$"
MESSAGE_ID_PATTERN = r"^msg_[0-9a-f]{32}$"
TURN_ID_PATTERN = r"^trn_[0-9a-f]{32}$"
PLAN_ID_PATTERN = r"^epl_[0-9a-f]{64}$"
PLAN_NODE_ID_PATTERN = r"^pnd_[0-9a-f]{64}$"
TOKEN_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
CAPABILITY_ID_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\.v[1-9]\d*$"


class TurnKind(StrEnum):
    ANSWER_ONLY = "answer_only"
    NEW_TASK = "new_task"
    TASK_AMENDMENT = "task_amendment"
    CLARIFICATION = "clarification"
    TYPED_COMMAND = "typed_command"


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.conversation.v1"] = "deskpilot.conversation.v1"
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.conversation-message.v1"] = (
        "deskpilot.conversation-message.v1"
    )
    message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    role: Literal["user", "assistant", "system"]
    content_ref: str = Field(min_length=1, max_length=500)


class TurnInterpretation(BaseModel):
    """A typed routing decision; it never carries approval or execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.turn-interpretation.v1"] = (
        "deskpilot.turn-interpretation.v1"
    )
    turn_id: str = Field(pattern=TURN_ID_PATTERN)
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    kind: TurnKind
    task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    summary: str = Field(min_length=1, max_length=500)
    clarification_codes: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def task_identity_matches_kind(self) -> Self:
        requires_task = self.kind in {
            TurnKind.TASK_AMENDMENT,
            TurnKind.CLARIFICATION,
            TurnKind.TYPED_COMMAND,
        }
        if requires_task != (self.task_id is not None):
            raise ValueError("Turn kind and task identity do not match")
        return self


class AcceptanceKind(StrEnum):
    STATE_ASSERTION = "state_assertion"
    ARTIFACT_REQUIREMENT = "artifact_requirement"
    CITATION_REQUIREMENT = "citation_requirement"
    SEMANTIC_QUALITY = "semantic_quality"
    SAFETY_INVARIANT = "safety_invariant"
    OUTPUT_REQUIREMENT = "output_requirement"


class VerificationRequirement(StrEnum):
    DETERMINISTIC = "deterministic_evidence"
    CITATION = "citation_evidence"
    ARTIFACT = "artifact_evidence"
    BROWSER = "browser_evidence"
    SEMANTIC = "semantic_review"
    USER = "user_confirmation"


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    criterion_id: str = Field(pattern=r"^ac_[a-z0-9_]{1,60}$")
    kind: AcceptanceKind
    description: str = Field(min_length=1, max_length=500)
    required: bool = True
    verification_requirement: VerificationRequirement
    freshness_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    origin: Literal["user", "trusted_template", "system_default", "policy"]

    @model_validator(mode="after")
    def safety_is_deterministic(self) -> Self:
        if (
            self.kind is AcceptanceKind.SAFETY_INVARIANT
            and self.verification_requirement is not VerificationRequirement.DETERMINISTIC
        ):
            raise ValueError("Safety criteria require deterministic evidence")
        return self


class TaskBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_model_calls: int = Field(ge=0, le=100)
    max_tool_calls: int = Field(ge=0, le=100)
    max_input_tokens: int = Field(ge=0, le=10_000_000)
    max_output_tokens: int = Field(ge=0, le=1_000_000)
    max_wall_seconds: int = Field(ge=1, le=86_400)
    max_retries: int = Field(ge=0, le=20)
    max_cost_micros: int = Field(ge=0, le=1_000_000_000_000)
    max_handoffs: int = Field(ge=0, le=20)
    max_plan_nodes: int = Field(default=20, ge=1, le=20)


class PlanNodeBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_calls: int = Field(ge=0, le=100)
    tool_calls: int = Field(ge=0, le=100)
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)
    wall_seconds: int = Field(ge=1, le=86_400)
    retries: int = Field(ge=0, le=20)
    cost_micros: int = Field(ge=0, le=1_000_000_000_000)
    handoffs: int = Field(ge=0, le=20)


class PrivacyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    classification: Literal["public", "internal", "sensitive"]
    allowed_provider_locations: tuple[ModelLocation, ...] = Field(min_length=1)
    allowed_privacy_modes: tuple[PrivacyMode, ...] = Field(min_length=1)
    external_egress_allowed: bool = False


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    media_type: Literal["application/json", "text/html", "text/markdown"]
    language: str = Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    require_citations: bool = False
    disclose_partial: bool = True


class CapabilityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capability_id: str = Field(pattern=CAPABILITY_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def key(self) -> tuple[str, str]:
        return self.capability_id, self.version


class ResearchContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_search_calls: int = Field(ge=1, le=20)
    max_page_reads: int = Field(ge=1, le=50)
    max_results_per_search: int = Field(ge=1, le=20)
    minimum_distinct_sources: int = Field(ge=1, le=10)
    allowed_domains: tuple[str, ...] = ()
    freshness_seconds: int | None = Field(default=None, ge=1, le=31_536_000)


class TaskWorkspaceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_ref: str = Field(pattern=r"^workspace://task/[0-9a-f]{32}$")
    allowed_extensions: tuple[Literal[".html", ".css", ".md", ".pdf"], ...] = Field(
        min_length=1
    )
    max_total_bytes: int = Field(ge=1, le=10_485_760)
    max_files: int = Field(ge=1, le=100)
    retention_days: int = Field(ge=1, le=365)
    allow_user_path_export: bool = False


class BrowserVerifyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: Literal["deskpilot.browser-static-html.v1"]
    network_enabled: Literal[False] = False
    authenticated_context: Literal[False] = False
    javascript_enabled: Literal[False] = False
    capture_screenshot: Literal[True] = True


class TaskContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.task-contract.v1"] = "deskpilot.task-contract.v1"
    contract_id: str = Field(pattern=CONTRACT_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    version: int = Field(ge=1)
    previous_contract_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    goal_ref: str = Field(min_length=1, max_length=500)
    normalized_objective: str = Field(min_length=1, max_length=1_000)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=50)
    constraints: tuple[str, ...] = Field(default=(), max_length=50)
    resource_scopes: tuple[str, ...] = Field(default=(), max_length=50)
    privacy_policy: PrivacyPolicy
    max_risk_level: ToolRiskLevel
    budget: TaskBudget
    output_contract: OutputContract
    capabilities: tuple[CapabilityRef, ...] = ()
    research: ResearchContract | None = None
    workspace: TaskWorkspaceContract | None = None
    browser_verify: BrowserVerifyContract | None = None
    created_by: Literal["local_user", "trusted_template", "policy"]

    @property
    def digest(self) -> str:
        return sha256_digest(self)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        criteria = [item.criterion_id for item in self.acceptance_criteria]
        if len(criteria) != len(set(criteria)):
            raise ValueError("Acceptance criterion IDs must be unique")
        capability_keys = [item.key for item in self.capabilities]
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("Task capabilities must be unique")
        if self.version == 1 and self.previous_contract_digest is not None:
            raise ValueError("Initial Task Contract cannot have a previous digest")
        if self.version > 1 and self.previous_contract_digest is None:
            raise ValueError("Amended Task Contract requires a previous digest")
        capability_ids = {item.capability_id for item in self.capabilities}
        required_contracts = {
            "research.read.v1": self.research,
            "artifact.html.v1": self.workspace,
            "browser.verify.v1": self.browser_verify,
        }
        for capability_id, contract in required_contracts.items():
            if (capability_id in capability_ids) != (contract is not None):
                raise ValueError(f"Capability {capability_id} and its contract must match")
        return self


class TaskAmendment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.task-amendment.v1"] = (
        "deskpilot.task-amendment.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    from_version: int = Field(ge=1)
    from_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    reason_code: str = Field(pattern=TOKEN_PATTERN)
    changed_fields: tuple[str, ...] = Field(min_length=1, max_length=20)
    initiated_by: Literal["local_user", "policy"]


class CapabilityPack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.capability-pack.v1"] = (
        "deskpilot.capability-pack.v1"
    )
    capability_id: str = Field(pattern=CAPABILITY_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = Field(min_length=1, max_length=500)
    allowed_operations: tuple[str, ...] = Field(min_length=1, max_length=20)
    max_risk_level: ToolRiskLevel
    external_ingress: bool
    external_egress: bool
    workspace_write: bool
    runtime_enabled: bool = False
    digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def key(self) -> tuple[str, str]:
        return self.capability_id, self.version

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"digest"})
        if self.digest != sha256_digest(material):
            raise ValueError("Capability Pack digest does not match")
        return self


class PlanProducer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["trusted_template", "single_agent_template", "model_planner"]
    producer_ref: str = Field(min_length=1, max_length=200)


class DraftNodeKind(StrEnum):
    AGENT = "agent"
    CAPABILITY = "capability"
    JOIN = "join"
    FINAL_ACCEPTANCE = "final_acceptance"
    DELIVERY = "delivery"


class VerificationProfile(StrEnum):
    DETERMINISTIC = "deterministic.v1"
    CITATION = "citation.v1"
    ARTIFACT = "artifact.v1"
    BROWSER = "browser.v1"
    SEMANTIC = "semantic.v1"


class DraftPlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    local_key: str = Field(pattern=TOKEN_PATTERN)
    kind: DraftNodeKind
    objective: str = Field(min_length=1, max_length=500)
    agent_selector: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    capability_selector: str | None = Field(default=None, pattern=CAPABILITY_ID_PATTERN)
    capability_requirements: tuple[str, ...] = Field(default=(), max_length=20)
    tool_name: str | None = Field(default=None, pattern=TOOL_NAME_PATTERN)
    tool_version: str | None = Field(default=None, pattern=SEMVER_PATTERN)
    depends_on: tuple[str, ...] = Field(default=(), max_length=19)
    handoff_parent: str | None = Field(default=None, pattern=TOKEN_PATTERN)
    acceptance_refs: tuple[str, ...] = Field(default=(), max_length=50)
    verification_profile: VerificationProfile
    budget: PlanNodeBudget

    @model_validator(mode="after")
    def fields_match_kind(self) -> Self:
        if (self.tool_name is None) != (self.tool_version is None):
            raise ValueError("Tool name and version must be provided together")
        if self.kind is DraftNodeKind.AGENT:
            if self.agent_selector is None or self.capability_selector is not None:
                raise ValueError("Agent node requires only an Agent selector")
        elif self.kind is DraftNodeKind.CAPABILITY:
            if self.capability_selector is None or self.agent_selector is not None:
                raise ValueError("Capability node requires only a capability selector")
            if self.tool_name is not None or self.handoff_parent is not None:
                raise ValueError("Capability node cannot request Agent Tool or handoff")
        elif any(
            value is not None
            for value in (
                self.agent_selector,
                self.capability_selector,
                self.tool_name,
                self.handoff_parent,
            )
        ):
            raise ValueError("Control nodes cannot select Agent, Tool or capability")
        if self.kind in {
            DraftNodeKind.JOIN,
            DraftNodeKind.FINAL_ACCEPTANCE,
            DraftNodeKind.DELIVERY,
        } and self.capability_requirements:
            raise ValueError("Control nodes cannot request Agent capabilities")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Plan dependencies must be unique")
        if len(self.acceptance_refs) != len(set(self.acceptance_refs)):
            raise ValueError("Acceptance references must be unique")
        return self


class DraftPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.draft-plan.v1"] = "deskpilot.draft-plan.v1"
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    contract_version: int = Field(ge=1)
    producer: PlanProducer
    nodes: tuple[DraftPlanNode, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        keys = [node.local_key for node in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("Draft node keys must be unique")
        known = set(keys)
        graph: dict[str, tuple[str, ...]] = {}
        for node in self.nodes:
            if node.local_key in node.depends_on:
                raise ValueError("Draft node cannot depend on itself")
            if any(item not in known for item in node.depends_on):
                raise ValueError("Draft dependency is unknown")
            if node.handoff_parent is not None and node.handoff_parent not in known:
                raise ValueError("Draft handoff parent is unknown")
            if node.handoff_parent == node.local_key:
                raise ValueError("Draft node cannot hand off to itself")
            if node.handoff_parent is not None and node.handoff_parent in node.depends_on:
                raise ValueError("Optional handoff activation is separate from verified edges")
            graph[node.local_key] = node.depends_on
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("Draft Plan contains a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in graph[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        final_nodes = [node for node in self.nodes if node.kind is DraftNodeKind.FINAL_ACCEPTANCE]
        delivery_nodes = [node for node in self.nodes if node.kind is DraftNodeKind.DELIVERY]
        if len(final_nodes) != 1 or len(delivery_nodes) != 1:
            raise ValueError("Draft Plan requires one final acceptance and one delivery node")
        if final_nodes[0].local_key not in delivery_nodes[0].depends_on:
            raise ValueError("Delivery must depend on final acceptance")
        return self


class TaskContractRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_id: str = Field(pattern=CONTRACT_ID_PATTERN)
    version: int = Field(ge=1)
    digest: str = Field(pattern=DIGEST_PATTERN)


class AcceptanceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    criterion_id: str
    node_ids: tuple[str, ...] = Field(min_length=1)
    verification_requirement: VerificationRequirement


class ExecutablePlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    local_key: str = Field(pattern=TOKEN_PATTERN)
    kind: DraftNodeKind
    objective: str
    bound_agent: BoundAgentRef | None = None
    bound_tool: AgentToolGrant | None = None
    capability: CapabilityRef | None = None
    capability_requirements: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    handoff_parent_node_id: str | None = Field(default=None, pattern=PLAN_NODE_ID_PATTERN)
    acceptance_refs: tuple[str, ...] = ()
    verification_profile: VerificationProfile
    verification_profile_digest: str = Field(pattern=DIGEST_PATTERN)
    budget: PlanNodeBudget
    runtime_enabled: bool
    node_spec_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"node_spec_digest"})
        if self.node_spec_digest != sha256_digest(material):
            raise ValueError("Executable node digest does not match")
        return self


class ExecutablePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.executable-plan.v1"] = (
        "deskpilot.executable-plan.v1"
    )
    canonicalization_version: Literal[1] = 1
    compiler_version: Literal["deskpilot.plan-compiler.v1"] = (
        "deskpilot.plan-compiler.v1"
    )
    plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    task_contract: TaskContractRef
    producer: PlanProducer
    nodes: tuple[ExecutablePlanNode, ...] = Field(min_length=1, max_length=20)
    acceptance_coverage: tuple[AcceptanceCoverage, ...]
    runtime_enabled: bool
    binding_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"plan_manifest_digest"})
        if self.plan_manifest_digest != sha256_digest(material):
            raise ValueError("Executable Plan digest does not match")
        return self


class TaskContractVersionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract: TaskContract
    contract_digest: str = Field(pattern=DIGEST_PATTERN)
    active: bool


class ExecutablePlanRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan: ExecutablePlan
    status: Literal["active", "superseded"]


class PlanningStateRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    active_contract_version: int = Field(ge=1)
    active_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    active_plan_generation: int = Field(ge=1)
    active_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    revision: int = Field(ge=1)


class CapabilityPackPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capabilities: tuple[CapabilityPack, ...]


class TaskContractVersionPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contracts: tuple[TaskContractVersionRead, ...]


class ExecutablePlanPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plans: tuple[ExecutablePlanRead, ...]
