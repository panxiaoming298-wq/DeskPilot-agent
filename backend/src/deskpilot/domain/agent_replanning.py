"""Immutable failure snapshots and bounded Agent replan generation lineage."""

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.agent_runtime import (
    INVOCATION_ID_PATTERN,
    MODEL_TURN_ID_PATTERN,
    RUN_ID_PATTERN,
    AgentTaskGraphResultRef,
)
from deskpilot.domain.task_plans import (
    MESSAGE_ID_PATTERN,
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    PlanNodeBudget,
    TaskBudget,
)

AGENT_REPLAN_ID_PATTERN = r"^rpl_[0-9a-f]{64}$"
PlanNodeId = Annotated[str, Field(pattern=PLAN_NODE_ID_PATTERN)]
InvocationId = Annotated[str, Field(pattern=INVOCATION_ID_PATTERN)]
ModelTurnId = Annotated[str, Field(pattern=MODEL_TURN_ID_PATTERN)]
Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]
REPLAN_RESULT_SOURCE_KEY_PATTERN = r"^replan_result_[0-9a-f]{32}$"
AgentReplanContinuationCode = Literal["continue_failed_patch_repair"]
LEGACY_PATCH_REPLAN_CONSTRAINT = "one_user_requested_condition_failure_replan_v1"
BOUNDED_PATCH_REPAIR_LOOP_CONSTRAINT = "maximum_three_patch_plan_generations_v1"
CROSS_GENERATION_BUDGET_CONSTRAINT = "cross_generation_task_budget_v1"
FRESH_PATCH_CONFIRMATION_CONSTRAINT = "fresh_confirmation_after_replan_v1"
NO_AUTOMATIC_PATCH_REPLAN_CONSTRAINT = "no_automatic_replan_after_workspace_write_v1"
PATCH_REPAIR_MAX_PLAN_GENERATIONS = 3


def condition_replan_generation_limit(constraints: Iterable[str]) -> int | None:
    """Return the contract-authorized condition-repair generation ceiling."""

    values = frozenset(constraints)
    common = {
        FRESH_PATCH_CONFIRMATION_CONSTRAINT,
        NO_AUTOMATIC_PATCH_REPLAN_CONSTRAINT,
    }
    if not common.issubset(values):
        return None
    if {
        BOUNDED_PATCH_REPAIR_LOOP_CONSTRAINT,
        CROSS_GENERATION_BUDGET_CONSTRAINT,
    }.issubset(values):
        return PATCH_REPAIR_MAX_PLAN_GENERATIONS
    if LEGACY_PATCH_REPLAN_CONSTRAINT in values:
        return 2
    return None


def classify_agent_replan_continuation(
    content: str,
) -> AgentReplanContinuationCode | None:
    """Recognize only explicit, deterministic requests to repair the failed Patch."""

    normalized = " ".join(content.strip().casefold().split()).rstrip("。.!！")
    if normalized in {
        "继续修复",
        "生成新计划代",
        "重新规划并继续修复",
        "按新计划继续修复",
        "continue repair",
    }:
        return "continue_failed_patch_repair"
    return None


class AgentReplanContinuationIntent(BaseModel):
    """Exact active user message authorizing one condition-failure replan."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-replan-continuation-intent.v1"] = (
        "deskpilot.agent-replan-continuation-intent.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    message_digest: str = Field(pattern=DIGEST_PATTERN)
    intent_code: AgentReplanContinuationCode
    requested_via: Literal["conversation_turn", "workbench_action"]
    intent_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"intent_digest"})
        if self.intent_digest != sha256_digest(material):
            raise ValueError("Agent replan continuation intent digest does not match")
        return self


class AgentReplanBudgetTotals(BaseModel):
    """Additive execution allocations tracked across immutable Plan generations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    wall_seconds: int = Field(ge=0)
    retries: int = Field(ge=0)
    cost_micros: int = Field(ge=0)
    handoffs: int = Field(ge=0)

    @classmethod
    def zero(cls) -> Self:
        return cls(
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            wall_seconds=0,
            retries=0,
            cost_micros=0,
            handoffs=0,
        )

    @classmethod
    def from_task_budget(cls, budget: TaskBudget) -> Self:
        return cls(
            **{
                field: getattr(budget, f"max_{field}")
                for field in cls.model_fields
            }
        )

    @classmethod
    def from_plan_budgets(cls, budgets: Iterable[PlanNodeBudget]) -> Self:
        values = tuple(budgets)
        return cls(
            **{
                field: sum(getattr(item, field) for item in values)
                for field in cls.model_fields
            }
        )

    def plus(self, other: Self) -> Self:
        return type(self)(
            **{
                field: getattr(self, field) + getattr(other, field)
                for field in type(self).model_fields
            }
        )

    def remaining_after(self, allocated: Self) -> Self:
        return type(self)(
            **{
                field: getattr(self, field) - getattr(allocated, field)
                for field in type(self).model_fields
            }
        )

    def contains(self, requested: Self) -> bool:
        return all(
            getattr(requested, field) <= getattr(self, field)
            for field in type(self).model_fields
        )


class AgentReplanBudgetProof(BaseModel):
    """Server-derived proof that replacement Plans share one Task budget envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-replan-budget-proof.v1"] = (
        "deskpilot.agent-replan-budget-proof.v1"
    )
    contract_digest: str = Field(pattern=DIGEST_PATTERN)
    maximum_plan_generations: int = Field(ge=2, le=5)
    source_plan_generation: int = Field(ge=1)
    target_plan_generation: int = Field(ge=2)
    budget_limit: AgentReplanBudgetTotals
    allocated_before: AgentReplanBudgetTotals
    target_plan_allocation: AgentReplanBudgetTotals
    allocated_after_activation: AgentReplanBudgetTotals
    remaining_after_activation: AgentReplanBudgetTotals
    budget_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_totals_and_digest_match(self) -> Self:
        if (
            self.target_plan_generation != self.source_plan_generation + 1
            or self.target_plan_generation > self.maximum_plan_generations
            or self.allocated_before.plus(self.target_plan_allocation)
            != self.allocated_after_activation
            or self.allocated_after_activation.plus(self.remaining_after_activation)
            != self.budget_limit
        ):
            raise ValueError("Agent replan cross-generation budget proof does not match")
        material = self.model_dump(mode="json", exclude={"budget_digest"})
        if self.budget_digest != sha256_digest(material):
            raise ValueError("Agent replan budget digest does not match")
        return self


class AgentRepairLoopStatus(BaseModel):
    """Workbench projection of the server-owned generation and budget boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-repair-loop-status.v1"] = (
        "deskpilot.agent-repair-loop-status.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    current_plan_generation: int = Field(ge=1)
    maximum_plan_generations: int = Field(ge=2, le=5)
    remaining_replans: int = Field(ge=0, le=4)
    budget_limit: AgentReplanBudgetTotals
    budget_allocated: AgentReplanBudgetTotals
    budget_remaining: AgentReplanBudgetTotals
    next_plan_allocation: AgentReplanBudgetTotals
    next_replan_available: bool
    reason_code: Literal[
        "AVAILABLE",
        "GENERATION_LIMIT_REACHED",
        "CROSS_GENERATION_BUDGET_EXHAUSTED",
    ]
    status_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def totals_and_digest_match(self) -> Self:
        expected_remaining = max(
            0,
            self.maximum_plan_generations - self.current_plan_generation,
        )
        available = bool(
            expected_remaining > 0
            and self.budget_remaining.contains(self.next_plan_allocation)
        )
        expected_reason = (
            "AVAILABLE"
            if available
            else (
                "GENERATION_LIMIT_REACHED"
                if expected_remaining == 0
                else "CROSS_GENERATION_BUDGET_EXHAUSTED"
            )
        )
        if (
            self.remaining_replans != expected_remaining
            or self.budget_allocated.plus(self.budget_remaining) != self.budget_limit
            or self.next_replan_available is not available
            or self.reason_code != expected_reason
        ):
            raise ValueError("Agent repair-loop status does not match its server budget")
        material = self.model_dump(mode="json", exclude={"status_digest"})
        if self.status_digest != sha256_digest(material):
            raise ValueError("Agent repair-loop status digest does not match")
        return self


class AgentReplanResultSource(BaseModel):
    """One server-named, fully verified ResultRef eligible for read-only reuse."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-replan-result-source.v1"] = (
        "deskpilot.agent-replan-result-source.v1"
    )
    source_key: str = Field(pattern=REPLAN_RESULT_SOURCE_KEY_PATTERN)
    source_run_id: str = Field(pattern=RUN_ID_PATTERN)
    source_plan_generation: int = Field(ge=1)
    source_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    source_graph_digest: str = Field(pattern=DIGEST_PATTERN)
    result_ref: AgentTaskGraphResultRef
    source_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"source_digest"})
        if self.source_digest != sha256_digest(material):
            raise ValueError("Agent replan ResultRef source digest does not match")
        return self


class AgentReplanRepairAdvice(BaseModel):
    """Server-authored repair guidance that grants no capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[
        "deskpilot.agent-replan-repair-advice.v1",
        "deskpilot.agent-replan-repair-advice.v2",
    ] = (
        "deskpilot.agent-replan-repair-advice.v2"
    )
    failure_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    stable_error_code: Literal[
        "AGENT_TASK_GRAPH_REJECTED",
        "AGENT_ROUTE_BINDING_REJECTED",
        "AGENT_LOOP_NO_PROGRESS",
        "AGENT_GRAPH_TEST_CONDITION_NOT_MET",
    ]
    strategy_code: Literal[
        "rebuild_graph_from_current_offer",
        "reuse_verified_evidence_and_rebind_route",
        "simplify_graph_and_consume_verified_evidence",
        "propose_fresh_patch_after_failed_test",
    ]
    objective: str = Field(min_length=1, max_length=500)
    granted_capability_ids: tuple[str, ...] = Field(default=(), max_length=0)
    result_sources: tuple[AgentReplanResultSource, ...] = Field(default=(), max_length=7)
    advice_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def sources_and_digest_match(self) -> Self:
        if self.schema_version == "deskpilot.agent-replan-repair-advice.v1" and (
            self.stable_error_code == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
            or self.strategy_code == "propose_fresh_patch_after_failed_test"
        ):
            raise ValueError("Legacy Agent repair advice contains a condition-failure strategy")
        keys = tuple(item.source_key for item in self.result_sources)
        refs = tuple(item.result_ref.result_ref_digest for item in self.result_sources)
        if len(keys) != len(set(keys)) or len(refs) != len(set(refs)):
            raise ValueError("Agent replan repair advice contains duplicate ResultRef sources")
        material = self.model_dump(mode="json", exclude={"advice_digest"})
        if self.advice_digest != sha256_digest(material):
            raise ValueError("Agent replan repair advice digest does not match")
        return self


class AgentReplanFailureSnapshot(BaseModel):
    """Server-derived, minimized evidence that can authorize one safe replan."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[
        "deskpilot.agent-replan-failure-snapshot.v1",
        "deskpilot.agent-replan-failure-snapshot.v2",
    ] = (
        "deskpilot.agent-replan-failure-snapshot.v2"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    source_run_id: str = Field(pattern=RUN_ID_PATTERN)
    source_plan_generation: int = Field(ge=1)
    source_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    contract_version: int = Field(ge=1)
    contract_digest: str = Field(pattern=DIGEST_PATTERN)
    route_id: Literal["workspace_directory_analyze", "workspace_dynamic_patch_test"]
    route_parameter_digest: str = Field(pattern=DIGEST_PATTERN)
    route_revision: int = Field(ge=1)
    stable_error_code: Literal[
        "AGENT_TASK_GRAPH_REJECTED",
        "AGENT_ROUTE_BINDING_REJECTED",
        "AGENT_LOOP_NO_PROGRESS",
        "AGENT_GRAPH_TEST_CONDITION_NOT_MET",
    ]
    failed_node_ids: tuple[PlanNodeId, ...] = Field(min_length=1)
    failed_invocation_ids: tuple[InvocationId, ...] = Field(min_length=1)
    failed_model_turn_ids: tuple[ModelTurnId, ...] = ()
    condition_decision_digests: tuple[Digest, ...] | None = Field(
        default=None,
        max_length=7,
        exclude_if=lambda value: value is None,
    )
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.schema_version == "deskpilot.agent-replan-failure-snapshot.v1":
            if (
                self.route_id != "workspace_directory_analyze"
                or self.stable_error_code == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                or not self.failed_model_turn_ids
                or self.condition_decision_digests is not None
            ):
                raise ValueError("Legacy Agent replan failure snapshot is incompatible")
        elif (
            self.route_id != "workspace_dynamic_patch_test"
            or self.stable_error_code != "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
            or self.failed_model_turn_ids
            or not self.condition_decision_digests
            or len(self.condition_decision_digests)
            != len(set(self.condition_decision_digests))
        ):
            raise ValueError("Conditional Agent replan failure snapshot is incompatible")
        material = self.model_dump(mode="json", exclude={"snapshot_digest"})
        if self.snapshot_digest != sha256_digest(material):
            raise ValueError("Agent replan failure snapshot digest does not match")
        return self


class AgentReplanRead(BaseModel):
    """Proof-bound lineage from one terminal Run to its replacement generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[
        "deskpilot.agent-replan.v1",
        "deskpilot.agent-replan.v2",
        "deskpilot.agent-replan.v3",
        "deskpilot.agent-replan.v4",
        "deskpilot.agent-replan.v5",
    ] = (
        "deskpilot.agent-replan.v5"
    )
    replan_id: str = Field(pattern=AGENT_REPLAN_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    source_run_id: str = Field(pattern=RUN_ID_PATTERN)
    source_plan_generation: int = Field(ge=1)
    source_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    target_run_id: str = Field(pattern=RUN_ID_PATTERN)
    target_plan_generation: int = Field(ge=2)
    target_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    contract_version: int = Field(ge=1)
    contract_digest: str = Field(pattern=DIGEST_PATTERN)
    failure_snapshot: AgentReplanFailureSnapshot
    repair_advice: AgentReplanRepairAdvice | None = None
    continuation_intent: AgentReplanContinuationIntent | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    budget_proof: AgentReplanBudgetProof | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    status: Literal["activated"] = "activated"
    created_at: datetime
    replan_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digest_match(self) -> Self:
        if (
            self.target_plan_generation != self.source_plan_generation + 1
            or self.failure_snapshot.task_id != self.task_id
            or self.failure_snapshot.source_run_id != self.source_run_id
            or self.failure_snapshot.source_plan_generation != self.source_plan_generation
            or self.failure_snapshot.source_plan_digest != self.source_plan_digest
            or self.failure_snapshot.contract_version != self.contract_version
            or self.failure_snapshot.contract_digest != self.contract_digest
        ):
            raise ValueError("Agent replan generation lineage does not match")
        if self.schema_version == "deskpilot.agent-replan.v1":
            if self.repair_advice is not None:
                raise ValueError("Legacy Agent replan contains repair advice")
        elif (
            self.repair_advice is None
            or self.repair_advice.failure_snapshot_digest != self.failure_snapshot.snapshot_digest
            or self.repair_advice.stable_error_code != self.failure_snapshot.stable_error_code
        ):
            raise ValueError("Agent replan repair advice does not match its failure snapshot")
        if self.schema_version in {
            "deskpilot.agent-replan.v1",
            "deskpilot.agent-replan.v2",
        } and self.failure_snapshot.schema_version != (
            "deskpilot.agent-replan-failure-snapshot.v1"
        ):
            raise ValueError("Legacy Agent replan contains a conditional failure snapshot")
        if self.schema_version in {
            "deskpilot.agent-replan.v3",
            "deskpilot.agent-replan.v4",
            "deskpilot.agent-replan.v5",
        } and (
            self.failure_snapshot.schema_version
            != "deskpilot.agent-replan-failure-snapshot.v2"
            or self.repair_advice is None
            or self.repair_advice.schema_version
            != "deskpilot.agent-replan-repair-advice.v2"
        ):
            raise ValueError("Conditional Agent replan proof versions do not match")
        if self.schema_version in {
            "deskpilot.agent-replan.v4",
            "deskpilot.agent-replan.v5",
        }:
            if (
                self.continuation_intent is None
                or self.continuation_intent.task_id != self.task_id
            ):
                raise ValueError("Agent replan continuation intent does not match")
        elif self.continuation_intent is not None:
            raise ValueError("Legacy Agent replan contains a continuation intent")
        if self.schema_version == "deskpilot.agent-replan.v5":
            if (
                self.budget_proof is None
                or self.budget_proof.contract_digest != self.contract_digest
                or self.budget_proof.source_plan_generation
                != self.source_plan_generation
                or self.budget_proof.target_plan_generation
                != self.target_plan_generation
            ):
                raise ValueError("Agent replan budget proof does not match")
        elif self.budget_proof is not None:
            raise ValueError("Legacy Agent replan contains a cross-generation budget proof")
        excluded = {"replan_digest"}
        if self.schema_version == "deskpilot.agent-replan.v1" and (
            "repair_advice" not in self.model_fields_set
        ):
            excluded.add("repair_advice")
        if self.schema_version != "deskpilot.agent-replan.v5" and (
            "budget_proof" not in self.model_fields_set
        ):
            excluded.add("budget_proof")
        material = self.model_dump(mode="json", exclude=excluded)
        if self.replan_digest != sha256_digest(material):
            raise ValueError("Agent replan digest does not match")
        return self


class AgentReplanPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    replans: tuple[AgentReplanRead, ...]
