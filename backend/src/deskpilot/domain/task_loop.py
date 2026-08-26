"""Proof-bound task-loop records for server-composed model Planner drafts.

Stage 112 deliberately appends these records after a terminal stage-111
``multi_step_deferred`` binding.  None of the v1 Turn Planner manifests are
rewritten: the source binding remains the immutable authority boundary and a
model-planner Draft remains only a server-adjudicated proposal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.task_plans import (
    MESSAGE_ID_PATTERN,
    PLAN_ID_PATTERN,
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    TOKEN_PATTERN,
    DraftPlan,
    ExecutablePlan,
    PlanNodeBudget,
    TaskContract,
    TaskContractRef,
)
from deskpilot.domain.turn_planning import (
    REASON_CODE_PATTERN,
    TURN_PLAN_BINDING_ID_PATTERN,
    TURN_PLANNER_ADJUDICATION_ID_PATTERN,
    TURN_PLANNER_RUN_ID_PATTERN,
    TurnPlanningOfferRef,
    TurnPlanningParameterBinding,
    TurnPlanningRecipeRef,
)

TASK_LOOP_ID_PATTERN = r"^tlp_[0-9a-f]{64}$"
TASK_LOOP_EVENT_ID_PATTERN = r"^tle_[0-9a-f]{64}$"
MODEL_PLANNER_DRAFT_ID_PATTERN = r"^mpd_[0-9a-f]{64}$"
MODEL_PLANNER_STEP_BINDING_ID_PATTERN = r"^mps_[0-9a-f]{64}$"
MODEL_PLANNER_COMPOSER_VERSION: Literal["deskpilot.offer-composer.v1"] = (
    "deskpilot.offer-composer.v1"
)

TaskLoopPhase = Literal["observe", "plan"]
TaskLoopStatus = Literal["observed", "planned", "failed"]
TaskLoopEventKind = Literal["observed", "plan_bound", "plan_failed"]
ModelPlannerFailureCode = Literal[
    "MULTI_STEP_OFFER_REJECTED",
    "MULTI_STEP_BINDING_REJECTED",
    "MULTI_STEP_CONTRACT_REJECTED",
    "MULTI_STEP_BUDGET_EXCEEDED",
    "MULTI_STEP_PLAN_REJECTED",
    "MULTI_STEP_PERSISTENCE_REJECTED",
]


def _record_material(values: dict[str, Any], *excluded: str) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key not in excluded}


def _content_id(prefix: str, material: dict[str, Any]) -> str:
    return f"{prefix}_{sha256_digest(material)}"


def _task_loop_id(source: TaskLoopSourceRef) -> str:
    return _content_id("tlp", source.model_dump(mode="json"))


class TaskLoopSourceRef(BaseModel):
    """Exact immutable v1 proof that authorized local multi-step composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    turn_planner_run_id: str = Field(pattern=TURN_PLANNER_RUN_ID_PATTERN)
    turn_planner_run_digest: str = Field(pattern=DIGEST_PATTERN)
    adjudication_id: str = Field(pattern=TURN_PLANNER_ADJUDICATION_ID_PATTERN)
    adjudication_digest: str = Field(pattern=DIGEST_PATTERN)
    turn_plan_binding_id: str = Field(pattern=TURN_PLAN_BINDING_ID_PATTERN)
    turn_plan_binding_digest: str = Field(pattern=DIGEST_PATTERN)


class ModelPlannerFailureProof(BaseModel):
    """Stable terminal composition failure; it never authorizes model replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.model-planner-failure-proof.v1"] = (
        "deskpilot.model-planner-failure-proof.v1"
    )
    error_code: ModelPlannerFailureCode
    reason_code: str = Field(pattern=REASON_CODE_PATTERN)
    detail_digest: str = Field(pattern=DIGEST_PATTERN)
    retry_policy: Literal["never_automatic"] = "never_automatic"
    failure_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"failure_digest"})
        if self.failure_digest != sha256_digest(material):
            raise ValueError("Model Planner failure digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        error_code: ModelPlannerFailureCode,
        reason_code: str,
        detail_digest: str,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.model-planner-failure-proof.v1",
            "error_code": error_code,
            "reason_code": reason_code,
            "detail_digest": detail_digest,
            "retry_policy": "never_automatic",
        }
        return cls(**values, failure_digest=sha256_digest(values))


class ModelPlannerNodeMapping(BaseModel):
    """Exact source-to-composite node map; no model-authored graph is accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    source_local_key: str = Field(pattern=TOKEN_PATTERN)
    source_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    composite_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    composite_local_key: str = Field(pattern=TOKEN_PATTERN)
    composite_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    mapping_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"mapping_digest"})
        if self.mapping_digest != sha256_digest(material):
            raise ValueError("Model Planner node mapping digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        source_node_id: str,
        source_local_key: str,
        source_node_spec_digest: str,
        composite_node_id: str,
        composite_local_key: str,
        composite_node_spec_digest: str,
    ) -> Self:
        values = {
            "source_node_id": source_node_id,
            "source_local_key": source_local_key,
            "source_node_spec_digest": source_node_spec_digest,
            "composite_node_id": composite_node_id,
            "composite_local_key": composite_local_key,
            "composite_node_spec_digest": composite_node_spec_digest,
        }
        return cls(**values, mapping_digest=sha256_digest(values))


class ModelPlannerStepBindingRef(BaseModel):
    """Content-addressed least-authority reference included by a Draft proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=1, le=8)
    step_binding_id: str = Field(pattern=MODEL_PLANNER_STEP_BINDING_ID_PATTERN)
    step_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    offer: TurnPlanningOfferRef
    parameter_bindings_digest: str = Field(pattern=DIGEST_PATTERN)
    node_mappings_digest: str = Field(pattern=DIGEST_PATTERN)


class ModelPlannerStepBinding(BaseModel):
    """Internal exact inputs and bindings for one ordered opaque Offer step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.model-planner-step-binding.v1"] = (
        "deskpilot.model-planner-step-binding.v1"
    )
    step_binding_id: str = Field(pattern=MODEL_PLANNER_STEP_BINDING_ID_PATTERN)
    source: TaskLoopSourceRef
    ordinal: int = Field(ge=1, le=8)
    offer: TurnPlanningOfferRef
    recipe: TurnPlanningRecipeRef
    policy_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    source_plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    source_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    source_plan_binding_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    budget: PlanNodeBudget
    budget_digest: str = Field(pattern=DIGEST_PATTERN)
    parameter_bindings: tuple[TurnPlanningParameterBinding, ...] = Field(
        default=(), max_length=32
    )
    parameter_bindings_digest: str = Field(pattern=DIGEST_PATTERN)
    node_mappings: tuple[ModelPlannerNodeMapping, ...] = Field(min_length=1, max_length=20)
    node_mappings_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    step_binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def ref(self) -> ModelPlannerStepBindingRef:
        return ModelPlannerStepBindingRef(
            ordinal=self.ordinal,
            step_binding_id=self.step_binding_id,
            step_binding_digest=self.step_binding_digest,
            offer=self.offer,
            parameter_bindings_digest=self.parameter_bindings_digest,
            node_mappings_digest=self.node_mappings_digest,
        )

    @model_validator(mode="after")
    def lineage_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Model Planner step binding timestamp must be timezone-aware")
        parameter_keys = tuple(
            (item.offer_key, item.parameter_name) for item in self.parameter_bindings
        )
        if len(parameter_keys) != len(set(parameter_keys)):
            raise ValueError("Model Planner step contains duplicate parameter bindings")
        if any(item.offer_key != self.offer.offer_key for item in self.parameter_bindings):
            raise ValueError("Model Planner step parameter crosses its Offer")
        expected_parameter_digest = sha256_digest(
            {
                "parameter_bindings": [
                    item.model_dump(mode="json") for item in self.parameter_bindings
                ]
            }
        )
        if self.parameter_bindings_digest != expected_parameter_digest:
            raise ValueError("Model Planner parameter-binding digest does not match")
        source_nodes = tuple(item.source_node_id for item in self.node_mappings)
        composite_nodes = tuple(item.composite_node_id for item in self.node_mappings)
        source_keys = tuple(item.source_local_key for item in self.node_mappings)
        composite_keys = tuple(item.composite_local_key for item in self.node_mappings)
        if (
            len(source_nodes) != len(set(source_nodes))
            or len(composite_nodes) != len(set(composite_nodes))
            or len(source_keys) != len(set(source_keys))
            or len(composite_keys) != len(set(composite_keys))
        ):
            raise ValueError("Model Planner step node mapping is not one-to-one")
        expected_mapping_digest = sha256_digest(
            {"node_mappings": [item.model_dump(mode="json") for item in self.node_mappings]}
        )
        if self.node_mappings_digest != expected_mapping_digest:
            raise ValueError("Model Planner node-mapping digest does not match")
        if self.budget_digest != sha256_digest(self.budget):
            raise ValueError("Model Planner step budget digest does not match")
        values = self.model_dump(mode="json")
        identity = _record_material(values, "step_binding_id", "step_binding_digest")
        if self.step_binding_id != _content_id("mps", identity):
            raise ValueError("Model Planner step binding id does not match")
        if self.step_binding_digest != sha256_digest(
            _record_material(values, "step_binding_digest")
        ):
            raise ValueError("Model Planner step binding digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        source: TaskLoopSourceRef,
        ordinal: int,
        offer: TurnPlanningOfferRef,
        recipe: TurnPlanningRecipeRef,
        policy_snapshot_digest: str,
        source_plan_id: str,
        source_plan_manifest_digest: str,
        source_plan_binding_snapshot_digest: str,
        budget: PlanNodeBudget,
        node_mappings: tuple[ModelPlannerNodeMapping, ...],
        created_at: datetime,
        parameter_bindings: tuple[TurnPlanningParameterBinding, ...] = (),
    ) -> Self:
        parameter_bindings_digest = sha256_digest(
            {
                "parameter_bindings": [
                    item.model_dump(mode="json") for item in parameter_bindings
                ]
            }
        )
        node_mappings_digest = sha256_digest(
            {"node_mappings": [item.model_dump(mode="json") for item in node_mappings]}
        )
        values: dict[str, Any] = {
            "schema_version": "deskpilot.model-planner-step-binding.v1",
            "source": source,
            "ordinal": ordinal,
            "offer": offer,
            "recipe": recipe,
            "policy_snapshot_digest": policy_snapshot_digest,
            "source_plan_id": source_plan_id,
            "source_plan_manifest_digest": source_plan_manifest_digest,
            "source_plan_binding_snapshot_digest": source_plan_binding_snapshot_digest,
            "budget": budget,
            "budget_digest": sha256_digest(budget),
            "parameter_bindings": parameter_bindings,
            "parameter_bindings_digest": parameter_bindings_digest,
            "node_mappings": node_mappings,
            "node_mappings_digest": node_mappings_digest,
            "created_at": created_at,
        }
        step_binding_id = _content_id("mps", values)
        digest_values = {**values, "step_binding_id": step_binding_id}
        return cls(
            **digest_values,
            step_binding_digest=sha256_digest(digest_values),
        )


class ModelPlannerDraftRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_id: str = Field(pattern=MODEL_PLANNER_DRAFT_ID_PATTERN)
    draft_record_digest: str = Field(pattern=DIGEST_PATTERN)
    draft_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    step_count: int = Field(ge=1, le=8)


class ModelPlannerDraft(BaseModel):
    """Server-composed Draft and sealed preview derived from ordered v1 Offers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.model-planner-draft.v1"] = (
        "deskpilot.model-planner-draft.v1"
    )
    draft_id: str = Field(pattern=MODEL_PLANNER_DRAFT_ID_PATTERN)
    source: TaskLoopSourceRef
    composer_version: Literal["deskpilot.offer-composer.v1"] = (
        MODEL_PLANNER_COMPOSER_VERSION
    )
    steps: tuple[ModelPlannerStepBindingRef, ...] = Field(min_length=1, max_length=8)
    step_set_digest: str = Field(pattern=DIGEST_PATTERN)
    step_count: int = Field(ge=1, le=8)
    task_contract: TaskContract
    task_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    draft_plan: DraftPlan
    draft_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_plan: ExecutablePlan
    expected_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    draft_record_digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def ref(self) -> ModelPlannerDraftRef:
        return ModelPlannerDraftRef(
            draft_id=self.draft_id,
            draft_record_digest=self.draft_record_digest,
            draft_plan_digest=self.draft_plan_digest,
            expected_plan_manifest_digest=self.expected_plan_manifest_digest,
            step_count=self.step_count,
        )

    @model_validator(mode="after")
    def lineage_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Model Planner Draft timestamp must be timezone-aware")
        ordinals = tuple(item.ordinal for item in self.steps)
        if ordinals != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("Model Planner Draft step ordinals must be contiguous")
        offer_ids = tuple(item.offer.offer_id for item in self.steps)
        offer_keys = tuple(item.offer.offer_key for item in self.steps)
        step_ids = tuple(item.step_binding_id for item in self.steps)
        if (
            len(offer_ids) != len(set(offer_ids))
            or len(offer_keys) != len(set(offer_keys))
            or len(step_ids) != len(set(step_ids))
        ):
            raise ValueError("Model Planner Draft repeats a v1 Offer or step binding")
        if self.step_count != len(self.steps):
            raise ValueError("Model Planner Draft step count does not match")
        expected_step_digest = sha256_digest(
            {"steps": [item.model_dump(mode="json") for item in self.steps]}
        )
        if self.step_set_digest != expected_step_digest:
            raise ValueError("Model Planner Draft step-set digest does not match")
        if (
            self.task_contract.task_id != self.source.task_id
            or self.task_contract.version != 1
            or self.task_contract.previous_contract_digest is not None
            or self.task_contract_digest != self.task_contract.digest
        ):
            raise ValueError("Model Planner Draft Task Contract binding changed")
        if (
            self.draft_plan.task_id != self.source.task_id
            or self.draft_plan.contract_version != self.task_contract.version
            or self.draft_plan.producer.kind != "model_planner"
            or self.draft_plan.producer.producer_ref != self.composer_version
            or self.draft_plan_digest != sha256_digest(self.draft_plan)
        ):
            raise ValueError("Model Planner Draft manifest binding changed")
        expected_contract_ref = TaskContractRef(
            contract_id=self.task_contract.contract_id,
            version=self.task_contract.version,
            digest=self.task_contract.digest,
        )
        if (
            self.expected_plan.task_id != self.source.task_id
            or self.expected_plan.plan_generation != 1
            or self.expected_plan.task_contract != expected_contract_ref
            or self.expected_plan.producer != self.draft_plan.producer
            or self.expected_plan_manifest_digest
            != self.expected_plan.plan_manifest_digest
        ):
            raise ValueError("Model Planner expected Plan binding changed")
        values = self.model_dump(mode="json")
        identity = {
            "schema_version": values["schema_version"],
            "source": values["source"],
            "composer_version": values["composer_version"],
            "step_set_digest": values["step_set_digest"],
            "task_contract_digest": values["task_contract_digest"],
            "draft_plan_digest": values["draft_plan_digest"],
            "expected_plan_manifest_digest": values["expected_plan_manifest_digest"],
            "created_at": values["created_at"],
        }
        if self.draft_id != _content_id("mpd", identity):
            raise ValueError("Model Planner Draft id does not match")
        if self.draft_record_digest != sha256_digest(
            _record_material(values, "draft_record_digest")
        ):
            raise ValueError("Model Planner Draft record digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        source: TaskLoopSourceRef,
        steps: tuple[ModelPlannerStepBindingRef, ...],
        task_contract: TaskContract,
        draft_plan: DraftPlan,
        expected_plan: ExecutablePlan,
        created_at: datetime,
    ) -> Self:
        step_set_digest = sha256_digest(
            {"steps": [item.model_dump(mode="json") for item in steps]}
        )
        task_contract_digest = task_contract.digest
        draft_plan_digest = sha256_digest(draft_plan)
        identity: dict[str, Any] = {
            "schema_version": "deskpilot.model-planner-draft.v1",
            "source": source,
            "composer_version": MODEL_PLANNER_COMPOSER_VERSION,
            "step_set_digest": step_set_digest,
            "task_contract_digest": task_contract_digest,
            "draft_plan_digest": draft_plan_digest,
            "expected_plan_manifest_digest": expected_plan.plan_manifest_digest,
            "created_at": created_at,
        }
        draft_id = _content_id("mpd", identity)
        values: dict[str, Any] = {
            "schema_version": "deskpilot.model-planner-draft.v1",
            "draft_id": draft_id,
            "source": source,
            "composer_version": MODEL_PLANNER_COMPOSER_VERSION,
            "steps": steps,
            "step_set_digest": step_set_digest,
            "step_count": len(steps),
            "task_contract": task_contract,
            "task_contract_digest": task_contract_digest,
            "draft_plan": draft_plan,
            "draft_plan_digest": draft_plan_digest,
            "expected_plan": expected_plan,
            "expected_plan_manifest_digest": expected_plan.plan_manifest_digest,
            "created_at": created_at,
        }
        return cls(**values, draft_record_digest=sha256_digest(values))


class TaskLoopEvent(BaseModel):
    """One immutable Observe -> Plan transition in a per-loop digest chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-event.v1"] = (
        "deskpilot.task-loop-event.v1"
    )
    event_id: str = Field(pattern=TASK_LOOP_EVENT_ID_PATTERN)
    loop_id: str = Field(pattern=TASK_LOOP_ID_PATTERN)
    source: TaskLoopSourceRef
    sequence: int = Field(ge=1, le=2)
    previous_event_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    phase: TaskLoopPhase
    kind: TaskLoopEventKind
    draft: ModelPlannerDraftRef | None = None
    failure: ModelPlannerFailureProof | None = None
    progress_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    event_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Task-loop event timestamp must be timezone-aware")
        if self.loop_id != _task_loop_id(self.source):
            raise ValueError("Task-loop event source changed its loop identity")
        if self.kind == "observed":
            if (
                self.sequence != 1
                or self.phase != "observe"
                or self.previous_event_digest is not None
                or self.draft is not None
                or self.failure is not None
            ):
                raise ValueError("Observed task-loop event lifecycle is invalid")
        elif self.kind == "plan_bound":
            if (
                self.sequence != 2
                or self.phase != "plan"
                or self.previous_event_digest is None
                or self.draft is None
                or self.failure is not None
            ):
                raise ValueError("Planned task-loop event lifecycle is invalid")
        elif (
            self.sequence != 2
            or self.phase != "plan"
            or self.previous_event_digest is None
            or self.draft is not None
            or self.failure is None
        ):
            raise ValueError("Failed task-loop event lifecycle is invalid")
        progress_material = {
            "loop_id": self.loop_id,
            "source": self.source.model_dump(mode="json"),
            "sequence": self.sequence,
            "previous_event_digest": self.previous_event_digest,
            "phase": self.phase,
            "kind": self.kind,
            "draft": self.draft.model_dump(mode="json") if self.draft else None,
            "failure_digest": self.failure.failure_digest if self.failure else None,
        }
        if self.progress_digest != sha256_digest(progress_material):
            raise ValueError("Task-loop event progress digest does not match")
        values = self.model_dump(mode="json")
        identity = _record_material(values, "event_id", "event_digest")
        if self.event_id != _content_id("tle", identity):
            raise ValueError("Task-loop event id does not match")
        if self.event_digest != sha256_digest(
            _record_material(values, "event_digest")
        ):
            raise ValueError("Task-loop event digest does not match")
        return self

    @classmethod
    def observe(cls, *, source: TaskLoopSourceRef, created_at: datetime) -> Self:
        loop_id = _task_loop_id(source)
        progress_material = {
            "loop_id": loop_id,
            "source": source.model_dump(mode="json"),
            "sequence": 1,
            "previous_event_digest": None,
            "phase": "observe",
            "kind": "observed",
            "draft": None,
            "failure_digest": None,
        }
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-event.v1",
            "loop_id": loop_id,
            "source": source,
            "sequence": 1,
            "previous_event_digest": None,
            "phase": "observe",
            "kind": "observed",
            "draft": None,
            "failure": None,
            "progress_digest": sha256_digest(progress_material),
            "created_at": created_at,
        }
        event_id = _content_id("tle", values)
        digest_values = {**values, "event_id": event_id}
        return cls(**digest_values, event_digest=sha256_digest(digest_values))

    @classmethod
    def plan(
        cls,
        *,
        observed: TaskLoopEvent,
        created_at: datetime,
        draft: ModelPlannerDraftRef | None = None,
        failure: ModelPlannerFailureProof | None = None,
    ) -> Self:
        if observed.kind != "observed" or observed.sequence != 1:
            raise ValueError("Task-loop Plan event requires its Observe event")
        if (draft is None) == (failure is None):
            raise ValueError("Task-loop Plan event requires exactly one terminal result")
        if created_at < observed.created_at:
            raise ValueError("Task-loop Plan event predates its Observe event")
        kind: Literal["plan_bound", "plan_failed"] = (
            "plan_bound" if draft is not None else "plan_failed"
        )
        progress_material = {
            "loop_id": observed.loop_id,
            "source": observed.source.model_dump(mode="json"),
            "sequence": 2,
            "previous_event_digest": observed.event_digest,
            "phase": "plan",
            "kind": kind,
            "draft": draft.model_dump(mode="json") if draft else None,
            "failure_digest": failure.failure_digest if failure else None,
        }
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-event.v1",
            "loop_id": observed.loop_id,
            "source": observed.source,
            "sequence": 2,
            "previous_event_digest": observed.event_digest,
            "phase": "plan",
            "kind": kind,
            "draft": draft,
            "failure": failure,
            "progress_digest": sha256_digest(progress_material),
            "created_at": created_at,
        }
        event_id = _content_id("tle", values)
        digest_values = {**values, "event_id": event_id}
        return cls(**digest_values, event_digest=sha256_digest(digest_values))


class TaskLoop(BaseModel):
    """Mutable pointer whose every transition is backed by immutable events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop.v1"] = "deskpilot.task-loop.v1"
    loop_id: str = Field(pattern=TASK_LOOP_ID_PATTERN)
    source: TaskLoopSourceRef
    phase: TaskLoopPhase
    status: TaskLoopStatus
    revision: int = Field(ge=1, le=2)
    event_count: int = Field(ge=1, le=2)
    latest_event_id: str = Field(pattern=TASK_LOOP_EVENT_ID_PATTERN)
    latest_event_digest: str = Field(pattern=DIGEST_PATTERN)
    progress_digest: str = Field(pattern=DIGEST_PATTERN)
    active_draft: ModelPlannerDraftRef | None = None
    failure: ModelPlannerFailureProof | None = None
    created_at: datetime
    updated_at: datetime
    loop_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_and_digests_match(self) -> Self:
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Task-loop timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Task-loop update predates creation")
        if self.loop_id != _task_loop_id(self.source):
            raise ValueError("Task-loop id does not match its source")
        if self.status == "observed":
            if (
                self.phase != "observe"
                or self.revision != 1
                or self.event_count != 1
                or self.active_draft is not None
                or self.failure is not None
            ):
                raise ValueError("Observed task-loop lifecycle is invalid")
        elif self.status == "planned":
            if (
                self.phase != "plan"
                or self.revision != 2
                or self.event_count != 2
                or self.active_draft is None
                or self.failure is not None
            ):
                raise ValueError("Planned task-loop lifecycle is invalid")
        elif (
            self.phase != "plan"
            or self.revision != 2
            or self.event_count != 2
            or self.active_draft is not None
            or self.failure is None
        ):
            raise ValueError("Failed task-loop lifecycle is invalid")
        values = self.model_dump(mode="json")
        if self.loop_digest != sha256_digest(_record_material(values, "loop_digest")):
            raise ValueError("Task-loop digest does not match")
        return self

    @classmethod
    def observed(cls, event: TaskLoopEvent) -> Self:
        if event.kind != "observed":
            raise ValueError("Initial task-loop state requires an Observe event")
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop.v1",
            "loop_id": event.loop_id,
            "source": event.source,
            "phase": "observe",
            "status": "observed",
            "revision": 1,
            "event_count": 1,
            "latest_event_id": event.event_id,
            "latest_event_digest": event.event_digest,
            "progress_digest": event.progress_digest,
            "active_draft": None,
            "failure": None,
            "created_at": event.created_at,
            "updated_at": event.created_at,
        }
        return cls(**values, loop_digest=sha256_digest(values))

    def settle_plan(self, event: TaskLoopEvent) -> Self:
        if (
            self.status != "observed"
            or event.loop_id != self.loop_id
            or event.source != self.source
            or event.sequence != 2
            or event.previous_event_digest != self.latest_event_digest
            or event.created_at < self.updated_at
        ):
            raise ValueError("Task-loop Plan transition is stale or crosses lineage")
        status: Literal["planned", "failed"] = (
            "planned" if event.kind == "plan_bound" else "failed"
        )
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop.v1",
            "loop_id": self.loop_id,
            "source": self.source,
            "phase": "plan",
            "status": status,
            "revision": 2,
            "event_count": 2,
            "latest_event_id": event.event_id,
            "latest_event_digest": event.event_digest,
            "progress_digest": event.progress_digest,
            "active_draft": event.draft,
            "failure": event.failure,
            "created_at": self.created_at,
            "updated_at": event.created_at,
        }
        return type(self)(**values, loop_digest=sha256_digest(values))


class TaskLoopFailureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: ModelPlannerFailureCode
    reason_code: str = Field(pattern=REASON_CODE_PATTERN)
    retry_policy: Literal["never_automatic"]
    failure_digest: str = Field(pattern=DIGEST_PATTERN)


class TaskLoopWorkbenchRead(BaseModel):
    """Sanitized public projection; it cannot reveal inputs or authority manifests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-workbench.v1"] = (
        "deskpilot.task-loop-workbench.v1"
    )
    loop_id: str = Field(pattern=TASK_LOOP_ID_PATTERN)
    phase: TaskLoopPhase
    status: TaskLoopStatus
    revision: int = Field(ge=1, le=2)
    event_count: int = Field(ge=1, le=2)
    step_count: int = Field(ge=0, le=8)
    source_turn_plan_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    draft_record_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    expected_plan_manifest_digest: str | None = Field(
        default=None, pattern=DIGEST_PATTERN
    )
    progress_digest: str = Field(pattern=DIGEST_PATTERN)
    failure: TaskLoopFailureSummary | None = None
    recoverable: bool
    updated_at: datetime
    projection_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_and_digest_match(self) -> Self:
        if self.updated_at.tzinfo is None:
            raise ValueError("Task-loop public timestamp must be timezone-aware")
        has_plan = (
            self.draft_record_digest is not None
            and self.expected_plan_manifest_digest is not None
            and self.step_count >= 1
        )
        if self.status == "observed":
            if has_plan or self.step_count != 0 or self.failure is not None or not self.recoverable:
                raise ValueError("Observed task-loop public lifecycle is invalid")
        elif self.status == "planned":
            if not has_plan or self.failure is not None or self.recoverable:
                raise ValueError("Planned task-loop public lifecycle is invalid")
        elif has_plan or self.step_count != 0 or self.failure is None or self.recoverable:
            raise ValueError("Failed task-loop public lifecycle is invalid")
        material = self.model_dump(mode="json", exclude={"projection_digest"})
        if self.projection_digest != sha256_digest(material):
            raise ValueError("Task-loop public projection digest does not match")
        return self

    @classmethod
    def from_internal(cls, loop: TaskLoop) -> Self:
        draft = loop.active_draft
        failure = loop.failure
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-workbench.v1",
            "loop_id": loop.loop_id,
            "phase": loop.phase,
            "status": loop.status,
            "revision": loop.revision,
            "event_count": loop.event_count,
            "step_count": draft.step_count if draft is not None else 0,
            "source_turn_plan_binding_digest": loop.source.turn_plan_binding_digest,
            "draft_record_digest": (
                draft.draft_record_digest if draft is not None else None
            ),
            "expected_plan_manifest_digest": (
                draft.expected_plan_manifest_digest if draft is not None else None
            ),
            "progress_digest": loop.progress_digest,
            "failure": (
                TaskLoopFailureSummary(
                    error_code=failure.error_code,
                    reason_code=failure.reason_code,
                    retry_policy=failure.retry_policy,
                    failure_digest=failure.failure_digest,
                )
                if failure is not None
                else None
            ),
            "recoverable": loop.status == "observed",
            "updated_at": loop.updated_at,
        }
        return cls(**values, projection_digest=sha256_digest(values))
