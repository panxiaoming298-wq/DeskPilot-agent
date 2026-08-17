"""Immutable, data-only Agent Contract and public Registry projections."""

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelLocation,
    ModelRole,
    PrivacyMode,
)
from deskpilot.domain.tool_contracts import SEMVER_PATTERN, TOOL_NAME_PATTERN, ToolRiskLevel

AGENT_ID_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class AgentKind(StrEnum):
    WORKER = "worker"
    SYNTHESIZER = "synthesizer"


class AgentRegistryStatus(StrEnum):
    ENABLED = "enabled"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    REVOKED = "revoked"


class PromptPackageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    package_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    renderer_version: int = Field(ge=1, le=100)
    digest: str = Field(pattern=DIGEST_PATTERN)


class AgentToolGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    contract_digest: str = Field(pattern=DIGEST_PATTERN)
    max_calls: int = Field(ge=1, le=100)

    @property
    def key(self) -> tuple[str, str]:
        return self.name, self.version


class AgentToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_risk_level: ToolRiskLevel
    grants: tuple[AgentToolGrant, ...] = ()

    @model_validator(mode="after")
    def unique_grants(self) -> Self:
        keys = [grant.key for grant in self.grants]
        if len(keys) != len(set(keys)):
            raise ValueError("Agent Tool grants must be unique")
        return self


class AgentHandoffRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)

    @property
    def key(self) -> tuple[str, str]:
        return self.agent_id, self.version


class AgentHandoffPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    may_delegate_to: tuple[AgentHandoffRef, ...] = ()
    may_receive_from: tuple[AgentHandoffRef, ...] = ()
    max_outgoing_handoffs: int = Field(default=0, ge=0, le=20)
    max_depth: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def unique_edges(self) -> Self:
        outgoing = [item.key for item in self.may_delegate_to]
        incoming = [item.key for item in self.may_receive_from]
        if len(outgoing) != len(set(outgoing)) or len(incoming) != len(set(incoming)):
            raise ValueError("Agent handoff edges must be unique")
        return self


class AgentModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: ModelRole
    allowed_locations: tuple[ModelLocation, ...] = Field(min_length=1)
    allowed_privacy_modes: tuple[PrivacyMode, ...] = Field(min_length=1)
    requirements: ModelCapabilityRequirements


class AgentContextPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed_sources: tuple[str, ...]
    memory_read_scopes: tuple[str, ...] = ()
    memory_write_scopes: tuple[str, ...] = ()
    rag_collections: tuple[str, ...] = ()


class AgentBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_model_calls: int = Field(ge=1, le=100)
    max_tool_calls: int = Field(ge=0, le=100)
    max_input_tokens: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    max_wall_seconds: int = Field(ge=1, le=86_400)
    max_retries: int = Field(ge=0, le=20)
    max_cost_micros: int = Field(ge=0, le=1_000_000_000_000)
    max_handoffs: int = Field(default=0, ge=0, le=20)


class AgentResultPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    required_evidence: tuple[str, ...] = ()
    require_citations: bool = False
    allow_unreferenced_claims: bool = False


class AgentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = Field(pattern=r"^deskpilot\.agent-contract\.v1$")
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    kind: AgentKind
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    provides: tuple[str, ...]
    prompt_package: PromptPackageRef
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tool_policy: AgentToolPolicy
    handoff_policy: AgentHandoffPolicy
    model_policy: AgentModelPolicy
    context_policy: AgentContextPolicy
    budget_policy: AgentBudgetPolicy
    result_policy: AgentResultPolicy

    @property
    def key(self) -> tuple[str, str]:
        return self.agent_id, self.version

    @property
    def digest(self) -> str:
        return sha256_digest(self)

    @model_validator(mode="after")
    def validate_internal_limits(self) -> Self:
        if len(self.provides) != len(set(self.provides)):
            raise ValueError("Agent capabilities must be unique")
        if self.budget_policy.max_tool_calls < sum(
            grant.max_calls for grant in self.tool_policy.grants
        ):
            raise ValueError("Agent Tool grants exceed the Tool call budget")
        if self.budget_policy.max_handoffs > self.handoff_policy.max_outgoing_handoffs:
            raise ValueError("Agent handoff budget exceeds its handoff policy")
        if self.context_policy.memory_write_scopes:
            raise ValueError("Built-in v1 Agents cannot write Memory")
        return self


class AgentDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str
    version: str
    kind: AgentKind
    display_name: str
    description: str
    status: AgentRegistryStatus
    status_reason: str | None
    source: str
    contract_digest: str
    prompt_package: PromptPackageRef
    provides: tuple[str, ...]
    tool_policy: AgentToolPolicy
    handoff_policy: AgentHandoffPolicy
    model_policy: AgentModelPolicy
    context_policy: AgentContextPolicy
    budget_policy: AgentBudgetPolicy
    result_policy: AgentResultPolicy
    input_schema_digest: str
    output_schema_digest: str


class AgentRegistrySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "deskpilot.agent-registry-snapshot.v1"
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    agents: tuple[AgentDescriptor, ...]


class AgentDescriptorPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agents: tuple[AgentDescriptor, ...]


class BoundAgentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    contract_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_package_digest: str = Field(pattern=DIGEST_PATTERN)


class AgentPlanBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_calls: int = Field(ge=1)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=1)
    output_tokens: int = Field(ge=1)
    wall_seconds: int = Field(ge=1)
    retries: int = Field(ge=0)
    cost_micros: int = Field(ge=0)
    handoffs: int = Field(ge=0)


class AgentPlanDraftStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    agent_selector: str = Field(pattern=AGENT_ID_PATTERN)
    tool_name: str | None = Field(default=None, pattern=TOOL_NAME_PATTERN)
    tool_version: str | None = Field(default=None, pattern=SEMVER_PATTERN)
    budget: AgentPlanBudget

    @model_validator(mode="after")
    def complete_tool_key(self) -> Self:
        if (self.tool_name is None) != (self.tool_version is None):
            raise ValueError("Tool name and version must be provided together")
        return self


class BoundAgentPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str
    agent: BoundAgentRef
    tool: AgentToolGrant | None
    budget: AgentPlanBudget
