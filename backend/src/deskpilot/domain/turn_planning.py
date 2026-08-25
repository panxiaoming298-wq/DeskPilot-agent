"""Immutable, proof-bound records for model-assisted turn interpretation."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN, BoundAgentRef
from deskpilot.domain.model_contracts import ModelLocation, ModelProviderDescriptor
from deskpilot.domain.task_plans import (
    MESSAGE_ID_PATTERN,
    PLAN_ID_PATTERN,
    TASK_ID_PATTERN,
    TOKEN_PATTERN,
    CapabilityRef,
    ExecutablePlan,
    PlanNodeBudget,
    TaskContractRef,
)

TURN_PLANNING_OFFER_ID_PATTERN = r"^tpo_[0-9a-f]{64}$"
TURN_PLANNING_OFFER_KEY_PATTERN = r"^ofk_[0-9a-f]{64}$"
TURN_PLANNER_RUN_ID_PATTERN = r"^tpr_[0-9a-f]{64}$"
TURN_PLANNER_ADJUDICATION_ID_PATTERN = r"^tpa_[0-9a-f]{64}$"
TURN_PLAN_BINDING_ID_PATTERN = r"^tpb_[0-9a-f]{64}$"
REASON_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,99}$"

TurnPlannerRunStatus = Literal[
    "prepared",
    "dispatching",
    "succeeded",
    "failed",
    "outcome_unknown",
    "cancelled",
]
TurnPlannerFailureCode = Literal[
    "PLANNER_TIMEOUT",
    "PLANNER_SCHEMA_REJECTED",
    "PLANNER_UNKNOWN_OFFER",
    "PLANNER_PROVIDER_UNAVAILABLE",
    "PLANNER_BINDING_REJECTED",
    "PLANNER_OUTCOME_UNKNOWN",
    "PLANNER_CANCELLED",
]
TurnPlannerAdjudicationOutcome = Literal[
    "single_step",
    "multi_step_deferred",
    "deterministic_fallback",
    "needs_user_input",
    "unsupported",
]
TurnPlanBindingStatus = Literal[
    "bound",
    "multi_step_deferred",
    "task_loop_deferred",
    "not_applicable",
]
TurnPlannerParameterName = Annotated[str, Field(pattern=TOKEN_PATTERN)]


def _record_material(values: dict[str, Any], *excluded: str) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key not in excluded}


def _content_id(prefix: str, material: dict[str, Any]) -> str:
    return f"{prefix}_{sha256_digest(material)}"


def _execution_agents(plan: ExecutablePlan) -> tuple[BoundAgentRef, ...]:
    """Derive a canonical exact Agent/Prompt set from a precompiled Plan."""

    by_binding = {
        (
            agent.agent_id,
            agent.version,
            agent.contract_digest,
            agent.prompt_package_digest,
        ): agent
        for node in plan.nodes
        if (agent := node.bound_agent) is not None
    }
    return tuple(by_binding[key] for key in sorted(by_binding))


class TurnPlanningRecipeRef(BaseModel):
    """Server-owned recipe that remains authoritative after model selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str = Field(pattern=TOKEN_PATTERN)
    route_version: str = Field(pattern=r"^[1-9]\d*$")
    route_manifest_digest: str = Field(pattern=DIGEST_PATTERN)


class TurnPlanningParameterSpec(BaseModel):
    """Server-authored boundary for one string parameter a model may propose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter_name: str = Field(pattern=TOKEN_PATTERN)
    required: bool = True
    min_length: int = Field(default=1, ge=0, le=20_000)
    max_length: int = Field(default=20_000, ge=1, le=20_000)

    @model_validator(mode="after")
    def length_range_is_valid(self) -> Self:
        if self.min_length > self.max_length:
            raise ValueError("Turn planning parameter length range is invalid")
        return self


class TurnPlanningParameterBinding(BaseModel):
    """One adjudicated, exact user-message substring for a selected offer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    parameter_name: str = Field(pattern=TOKEN_PATTERN)
    value: str = Field(min_length=1, max_length=20_000)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=1)
    value_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def span_and_digest_match(self) -> Self:
        if self.source_end - self.source_start != len(self.value):
            raise ValueError("Turn planning parameter span does not match its value")
        if self.value_digest != sha256_digest({"value": self.value}):
            raise ValueError("Turn planning parameter value digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        offer_key: str,
        parameter_name: str,
        value: str,
        source_start: int,
        source_end: int,
    ) -> Self:
        return cls(
            offer_key=offer_key,
            parameter_name=parameter_name,
            value=value,
            source_start=source_start,
            source_end=source_end,
            value_digest=sha256_digest({"value": value}),
        )


class TurnPlanningOfferRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offer_id: str = Field(pattern=TURN_PLANNING_OFFER_ID_PATTERN)
    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    offer_digest: str = Field(pattern=DIGEST_PATTERN)


class TurnPlanningOffer(BaseModel):
    """Opaque server-precompiled capability offer; selecting it grants no authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planning-offer.v1"] = (
        "deskpilot.turn-planning-offer.v1"
    )
    offer_id: str = Field(pattern=TURN_PLANNING_OFFER_ID_PATTERN)
    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    intent_description: str = Field(min_length=1, max_length=500)
    task_contract: TaskContractRef
    execution_agents: tuple[BoundAgentRef, ...] = Field(default=(), max_length=20)
    expected_plan: ExecutablePlan
    capabilities: tuple[CapabilityRef, ...] = Field(min_length=1, max_length=16)
    provider: ModelProviderDescriptor
    provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    trusted_recipe: TurnPlanningRecipeRef
    budget: PlanNodeBudget
    parameter_specs: tuple[TurnPlanningParameterSpec, ...] = Field(
        default=(),
        max_length=32,
    )
    policy_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    offer_digest: str = Field(pattern=DIGEST_PATTERN)

    @property
    def ref(self) -> TurnPlanningOfferRef:
        return TurnPlanningOfferRef(
            offer_id=self.offer_id,
            offer_key=self.offer_key,
            offer_digest=self.offer_digest,
        )

    @property
    def capabilities_digest(self) -> str:
        return sha256_digest(
            {
                "capabilities": [
                    item.model_dump(mode="json") for item in self.capabilities
                ]
            }
        )

    @property
    def execution_agents_digest(self) -> str:
        return sha256_digest(
            {
                "execution_agents": [
                    item.model_dump(mode="json") for item in self.execution_agents
                ]
            }
        )

    @property
    def budget_digest(self) -> str:
        return sha256_digest(self.budget)

    @property
    def parameter_schema_digest(self) -> str:
        return sha256_digest(
            {
                "parameter_specs": [
                    item.model_dump(mode="json") for item in self.parameter_specs
                ]
            }
        )

    def validate_recompiled_plan(self, candidate: ExecutablePlan) -> ExecutablePlan:
        """Fail closed when Registry, Prompt, capability, or compiler binding drifted."""

        if candidate != self.expected_plan:
            raise ValueError("Turn planning offer expected Plan binding drifted")
        return candidate

    @model_validator(mode="after")
    def bindings_identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Turn planning offer timestamp must be timezone-aware")
        names = tuple(item.parameter_name for item in self.parameter_specs)
        if len(names) != len(set(names)):
            raise ValueError("Turn planning offer contains duplicate parameter specs")
        capability_keys = tuple(item.key for item in self.capabilities)
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("Turn planning offer contains duplicate capabilities")
        expected_agents = _execution_agents(self.expected_plan)
        if self.execution_agents != expected_agents:
            raise ValueError("Turn planning offer execution Agent binding changed")
        if (
            self.expected_plan.task_id != self.task_id
            or self.expected_plan.plan_generation != 1
            or self.expected_plan.task_contract != self.task_contract
        ):
            raise ValueError("Turn planning offer expected Plan scope changed")
        if not self.expected_plan.runtime_enabled:
            raise ValueError("Turn planning offer expected Plan is not runtime enabled")
        if self.expected_plan.producer.kind == "model_planner":
            raise ValueError("Turn planning offer expected Plan must be server compiled")
        offered_capabilities = {
            (item.capability_id, item.version, item.digest)
            for item in self.capabilities
        }
        planned_capabilities = {
            (item.capability.capability_id, item.capability.version, item.capability.digest)
            for item in self.expected_plan.nodes
            if item.capability is not None
        }
        if not planned_capabilities.issubset(offered_capabilities):
            raise ValueError("Turn planning offer expected Plan exceeds its capabilities")
        if (
            self.provider.location is not ModelLocation.LOCAL
            or self.provider_snapshot_digest != sha256_digest(self.provider)
        ):
            raise ValueError("Turn planning offer Provider binding changed")
        values = self.model_dump(mode="json")
        identity = _record_material(values, "offer_id", "offer_digest")
        if self.offer_id != _content_id("tpo", identity):
            raise ValueError("Turn planning offer id does not match")
        if self.offer_digest != sha256_digest(
            _record_material(values, "offer_digest")
        ):
            raise ValueError("Turn planning offer digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        offer_key: str,
        task_id: str,
        user_message_id: str,
        user_message_digest: str,
        intent_description: str,
        task_contract: TaskContractRef,
        expected_plan: ExecutablePlan,
        capabilities: tuple[CapabilityRef, ...],
        provider: ModelProviderDescriptor,
        trusted_recipe: TurnPlanningRecipeRef,
        budget: PlanNodeBudget,
        parameter_specs: tuple[TurnPlanningParameterSpec, ...] = (),
        policy_snapshot_digest: str,
        created_at: datetime,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planning-offer.v1",
            "offer_key": offer_key,
            "task_id": task_id,
            "user_message_id": user_message_id,
            "user_message_digest": user_message_digest,
            "intent_description": intent_description,
            "task_contract": task_contract,
            "execution_agents": _execution_agents(expected_plan),
            "expected_plan": expected_plan,
            "capabilities": capabilities,
            "provider": provider,
            "provider_snapshot_digest": sha256_digest(provider),
            "trusted_recipe": trusted_recipe,
            "budget": budget,
            "parameter_specs": parameter_specs,
            "policy_snapshot_digest": policy_snapshot_digest,
            "created_at": created_at,
        }
        offer_id = _content_id("tpo", values)
        digest_values = {**values, "offer_id": offer_id}
        return cls(
            **digest_values,
            offer_digest=sha256_digest(digest_values),
        )


class TurnPlannerInputOffer(BaseModel):
    """Least-authority projection of an Offer supplied to the planner model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    offer: TurnPlanningOfferRef
    intent_description: str = Field(min_length=1, max_length=500)
    parameter_specs: tuple[TurnPlanningParameterSpec, ...] = Field(
        default=(),
        max_length=32,
    )


class TurnPlannerInput(BaseModel):
    """Prompt payload containing no executable capability or permission material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-input.v1"] = (
        "deskpilot.turn-planner-input.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    user_message: str = Field(min_length=1, max_length=200_000)
    offers: tuple[TurnPlannerInputOffer, ...] = Field(min_length=1, max_length=64)
    offer_set_digest: str = Field(pattern=DIGEST_PATTERN)
    input_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def offers_and_digest_match(self) -> Self:
        refs = tuple(item.offer for item in self.offers)
        keys = tuple(item.offer_key for item in refs)
        ids = tuple(item.offer_id for item in refs)
        if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
            raise ValueError("Turn planner input contains duplicate offers")
        if self.offer_set_digest != sha256_digest(
            {"offers": [item.model_dump(mode="json") for item in refs]}
        ):
            raise ValueError("Turn planner input offer-set digest does not match")
        material = self.model_dump(mode="json", exclude={"input_digest"})
        if self.input_digest != sha256_digest(material):
            raise ValueError("Turn planner input digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        user_message_id: str,
        user_message_digest: str,
        user_message: str,
        offers: tuple[TurnPlanningOffer, ...],
    ) -> Self:
        scope = (task_id, user_message_id, user_message_digest)
        if any(
            (item.task_id, item.user_message_id, item.user_message_digest) != scope
            for item in offers
        ):
            raise ValueError("Turn planner input offers cross task or message scope")
        projected = tuple(
            TurnPlannerInputOffer(
                offer=item.ref,
                intent_description=item.intent_description,
                parameter_specs=item.parameter_specs,
            )
            for item in offers
        )
        offer_set_digest = sha256_digest(
            {
                "offers": [
                    item.offer.model_dump(mode="json") for item in projected
                ]
            }
        )
        values: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planner-input.v1",
            "task_id": task_id,
            "user_message_id": user_message_id,
            "user_message_digest": user_message_digest,
            "user_message": user_message,
            "offers": projected,
            "offer_set_digest": offer_set_digest,
        }
        return cls(**values, input_digest=sha256_digest(values))


class TurnPlannerParameterProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=TOKEN_PATTERN)
    value: str = Field(min_length=1, max_length=4_000)


class TurnPlannerStepProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    parameters: tuple[TurnPlannerParameterProposal, ...] = Field(
        default=(),
        max_length=32,
    )

    @model_validator(mode="after")
    def parameter_names_are_unique(self) -> Self:
        names = tuple(item.name for item in self.parameters)
        if len(names) != len(set(names)):
            raise ValueError("Turn planner step contains duplicate parameters")
        return self


class TurnPlannerProposeStepsDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-decision.v1"] = (
        "deskpilot.turn-planner-decision.v1"
    )
    kind: Literal["propose_steps"]
    steps: tuple[TurnPlannerStepProposal, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def offer_keys_are_unique(self) -> Self:
        keys = tuple(item.offer_key for item in self.steps)
        if len(keys) != len(set(keys)):
            raise ValueError("Turn planner decision selected an offer more than once")
        return self


class TurnPlannerNeedsInputDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-decision.v1"] = (
        "deskpilot.turn-planner-decision.v1"
    )
    kind: Literal["needs_input"]
    offer_key: str | None = Field(
        default=None,
        pattern=TURN_PLANNING_OFFER_KEY_PATTERN,
    )
    missing_parameters: tuple[TurnPlannerParameterName, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def missing_parameters_are_unique(self) -> Self:
        if len(self.missing_parameters) != len(set(self.missing_parameters)):
            raise ValueError("Turn planner decision repeated a missing parameter")
        return self


class TurnPlannerUnsupportedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-decision.v1"] = (
        "deskpilot.turn-planner-decision.v1"
    )
    kind: Literal["unsupported"]


TurnPlannerDecisionValue = Annotated[
    TurnPlannerProposeStepsDecision
    | TurnPlannerNeedsInputDecision
    | TurnPlannerUnsupportedDecision,
    Field(discriminator="kind"),
]


class TurnPlannerDecision(RootModel[TurnPlannerDecisionValue]):
    model_config = ConfigDict(frozen=True)


class TurnPlannerFailureProof(BaseModel):
    """Minimized terminal proof; it explicitly forbids automatic model replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-failure-proof.v1"] = (
        "deskpilot.turn-planner-failure-proof.v1"
    )
    error_code: TurnPlannerFailureCode
    detail_digest: str = Field(pattern=DIGEST_PATTERN)
    retry_policy: Literal["never_automatic"] = "never_automatic"
    failure_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"failure_digest"})
        if self.failure_digest != sha256_digest(material):
            raise ValueError("Turn planner failure proof digest does not match")
        return self

    @classmethod
    def build(cls, *, error_code: TurnPlannerFailureCode, detail_digest: str) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planner-failure-proof.v1",
            "error_code": error_code,
            "detail_digest": detail_digest,
            "retry_policy": "never_automatic",
        }
        return cls(
            schema_version="deskpilot.turn-planner-failure-proof.v1",
            error_code=error_code,
            detail_digest=detail_digest,
            retry_policy="never_automatic",
            failure_digest=sha256_digest(values),
        )


class TurnPlannerRun(BaseModel):
    """Fenced planner reservation; terminal snapshots are immutable and never replayed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-run.v1"] = (
        "deskpilot.turn-planner-run.v1"
    )
    run_id: str = Field(pattern=TURN_PLANNER_RUN_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    planner_agent: BoundAgentRef
    provider: ModelProviderDescriptor
    provider_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    offers: tuple[TurnPlanningOfferRef, ...] = Field(min_length=1, max_length=64)
    offer_set_digest: str = Field(pattern=DIGEST_PATTERN)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    fallback_candidate_digest: str = Field(pattern=DIGEST_PATTERN)
    reservation_digest: str = Field(pattern=DIGEST_PATTERN)
    status: TurnPlannerRunStatus
    revision: int = Field(ge=1)
    claim_owner_id: str | None = Field(default=None, min_length=1, max_length=100)
    claim_fencing_token: int = Field(default=0, ge=0)
    claim_expires_at: datetime | None = None
    request_dispatched_at: datetime | None = None
    response_manifest: dict[str, JsonValue] | None = None
    response_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    failure: TurnPlannerFailureProof | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    run_digest: str = Field(pattern=DIGEST_PATTERN)

    @staticmethod
    def _identity(values: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "schema_version",
            "task_id",
            "user_message_id",
            "user_message_digest",
            "planner_agent",
            "provider",
            "provider_snapshot_digest",
            "offers",
            "offer_set_digest",
            "request_digest",
            "fallback_candidate_digest",
        )
        return {key: values[key] for key in keys}

    @model_validator(mode="after")
    def lifecycle_identity_and_digest_match(self) -> Self:
        timestamps = (
            self.created_at,
            self.updated_at,
            self.claim_expires_at,
            self.request_dispatched_at,
            self.completed_at,
        )
        if any(item is not None and item.tzinfo is None for item in timestamps):
            raise ValueError("Turn planner run timestamps must be timezone-aware")
        if self.updated_at < self.created_at or (
            self.completed_at is not None and self.completed_at < self.created_at
        ):
            raise ValueError("Turn planner run timestamp order is invalid")
        if (
            self.request_dispatched_at is not None
            and self.request_dispatched_at < self.created_at
        ):
            raise ValueError("Turn planner dispatch timestamp is invalid")
        if (
            self.planner_agent.agent_id != "builtin.turn_planner"
            or self.planner_agent.version != "1.0.0"
            or self.provider.location is not ModelLocation.LOCAL
        ):
            raise ValueError("Turn planner run must use the local builtin planner")
        if self.provider_snapshot_digest != sha256_digest(self.provider):
            raise ValueError("Turn planner Provider snapshot changed")
        keys = tuple(item.offer_key for item in self.offers)
        ids = tuple(item.offer_id for item in self.offers)
        if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
            raise ValueError("Turn planner run contains duplicate offers")
        expected_offer_set_digest = sha256_digest(
            {"offers": [item.model_dump(mode="json") for item in self.offers]}
        )
        if self.offer_set_digest != expected_offer_set_digest:
            raise ValueError("Turn planner offer-set digest does not match")
        has_response = self.response_manifest is not None and self.response_digest is not None
        if (self.response_manifest is None) != (self.response_digest is None):
            raise ValueError("Turn planner response manifest and digest must be paired")
        if has_response and self.response_digest != sha256_digest(self.response_manifest or {}):
            raise ValueError("Turn planner response digest does not match")
        if self.status == "prepared":
            if any(
                item is not None
                for item in (
                    self.claim_owner_id,
                    self.claim_expires_at,
                    self.request_dispatched_at,
                    self.completed_at,
                    self.response_manifest,
                    self.response_digest,
                    self.failure,
                )
            ) or self.claim_fencing_token != 0:
                raise ValueError("Prepared turn planner run contains dispatch state")
        elif self.status == "dispatching":
            if (
                self.claim_owner_id is None
                or self.claim_expires_at is None
                or self.claim_fencing_token < 1
                or self.request_dispatched_at is None
                or self.completed_at is not None
                or has_response
                or self.failure is not None
            ):
                raise ValueError("Dispatching turn planner run lacks a fenced claim")
            if self.claim_expires_at <= self.updated_at:
                raise ValueError("Turn planner claim must expire after its update")
        else:
            if (
                self.claim_owner_id is not None
                or self.claim_expires_at is not None
                or self.completed_at is None
            ):
                raise ValueError("Terminal turn planner run retained an active claim")
            if self.status == "succeeded":
                if (
                    self.request_dispatched_at is None
                    or not has_response
                    or self.failure is not None
                ):
                    raise ValueError("Successful turn planner run requires only a response")
            elif self.status == "failed":
                if (
                    self.request_dispatched_at is None
                    or has_response
                    or self.failure is None
                    or self.failure.error_code
                    in {"PLANNER_OUTCOME_UNKNOWN", "PLANNER_CANCELLED"}
                ):
                    raise ValueError("Failed turn planner run requires a known failure proof")
            elif self.status == "outcome_unknown":
                if (
                    self.request_dispatched_at is None
                    or has_response
                    or self.failure is None
                    or self.failure.error_code != "PLANNER_OUTCOME_UNKNOWN"
                ):
                    raise ValueError("Unknown planner outcome requires its dedicated proof")
            elif (
                has_response
                or self.failure is None
                or self.failure.error_code != "PLANNER_CANCELLED"
            ):
                raise ValueError("Cancelled turn planner run requires cancellation proof")
        values = self.model_dump(mode="json")
        identity = self._identity(values)
        expected_reservation_digest = sha256_digest(identity)
        if self.reservation_digest != expected_reservation_digest:
            raise ValueError("Turn planner reservation digest does not match")
        if self.run_id != f"tpr_{expected_reservation_digest}":
            raise ValueError("Turn planner run id does not match its request identity")
        if self.run_digest != sha256_digest(_record_material(values, "run_digest")):
            raise ValueError("Turn planner run digest does not match")
        return self

    @classmethod
    def reserve(
        cls,
        *,
        task_id: str,
        user_message_id: str,
        user_message_digest: str,
        planner_agent: BoundAgentRef,
        provider: ModelProviderDescriptor,
        offers: tuple[TurnPlanningOfferRef, ...],
        request_digest: str,
        fallback_candidate_digest: str,
        created_at: datetime,
    ) -> Self:
        offer_set_digest = sha256_digest(
            {"offers": [item.model_dump(mode="json") for item in offers]}
        )
        identity: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planner-run.v1",
            "task_id": task_id,
            "user_message_id": user_message_id,
            "user_message_digest": user_message_digest,
            "planner_agent": planner_agent,
            "provider": provider,
            "provider_snapshot_digest": sha256_digest(provider),
            "offers": offers,
            "offer_set_digest": offer_set_digest,
            "request_digest": request_digest,
            "fallback_candidate_digest": fallback_candidate_digest,
        }
        reservation_digest = sha256_digest(identity)
        run_id = f"tpr_{reservation_digest}"
        values: dict[str, Any] = {
            **identity,
            "run_id": run_id,
            "reservation_digest": reservation_digest,
            "status": "prepared",
            "revision": 1,
            "claim_owner_id": None,
            "claim_fencing_token": 0,
            "claim_expires_at": None,
            "request_dispatched_at": None,
            "response_manifest": None,
            "response_digest": None,
            "failure": None,
            "created_at": created_at,
            "updated_at": created_at,
            "completed_at": None,
        }
        return cls(**values, run_digest=sha256_digest(values))

    def evolve(
        self,
        *,
        status: TurnPlannerRunStatus,
        revision: int,
        updated_at: datetime,
        claim_owner_id: str | None = None,
        claim_fencing_token: int,
        claim_expires_at: datetime | None = None,
        request_dispatched_at: datetime | None = None,
        completed_at: datetime | None = None,
        response_manifest: dict[str, JsonValue] | None = None,
        failure: TurnPlannerFailureProof | None = None,
    ) -> Self:
        allowed_transitions: dict[str, frozenset[str]] = {
            "prepared": frozenset({"dispatching", "cancelled"}),
            "dispatching": frozenset(
                {"succeeded", "failed", "outcome_unknown", "cancelled"}
            ),
        }
        if (
            status not in allowed_transitions.get(self.status, frozenset())
            or revision != self.revision + 1
            or updated_at < self.updated_at
        ):
            raise ValueError("Turn planner run transition is stale or invalid")
        values = self.model_dump(mode="python", exclude={"run_digest"})
        values.update(
            {
                "status": status,
                "revision": revision,
                "claim_owner_id": claim_owner_id,
                "claim_fencing_token": claim_fencing_token,
                "claim_expires_at": claim_expires_at,
                "request_dispatched_at": request_dispatched_at,
                "response_manifest": response_manifest,
                "response_digest": (
                    sha256_digest(response_manifest)
                    if response_manifest is not None
                    else None
                ),
                "failure": failure,
                "updated_at": updated_at,
                "completed_at": completed_at,
            }
        )
        return type(self)(**values, run_digest=sha256_digest(values))


class TurnPlannerAdjudication(BaseModel):
    """Server decision over untrusted planner output and exact persisted offers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-adjudication.v1"] = (
        "deskpilot.turn-planner-adjudication.v1"
    )
    adjudication_id: str = Field(pattern=TURN_PLANNER_ADJUDICATION_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    run_id: str = Field(pattern=TURN_PLANNER_RUN_ID_PATTERN)
    run_digest: str = Field(pattern=DIGEST_PATTERN)
    outcome: TurnPlannerAdjudicationOutcome
    selected_offers: tuple[TurnPlanningOfferRef, ...] = Field(default=(), max_length=8)
    parameter_bindings: tuple[TurnPlanningParameterBinding, ...] = Field(
        default=(),
        max_length=64,
    )
    parameter_bindings_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    proposal_manifest: dict[str, JsonValue] | None = None
    proposal_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    reason_code: str = Field(pattern=REASON_CODE_PATTERN)
    created_at: datetime
    adjudication_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def selection_identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Turn planner adjudication timestamp must be timezone-aware")
        if (self.proposal_manifest is None) != (self.proposal_digest is None):
            raise ValueError("Turn planner proposal manifest and digest must be paired")
        if self.proposal_manifest is not None and self.proposal_digest != sha256_digest(
            self.proposal_manifest
        ):
            raise ValueError("Turn planner proposal digest does not match")
        expected_parameter_digest = (
            sha256_digest(
                {
                    "parameter_bindings": [
                        item.model_dump(mode="json")
                        for item in self.parameter_bindings
                    ]
                }
            )
            if self.proposal_manifest is not None
            else None
        )
        if self.parameter_bindings_digest != expected_parameter_digest:
            raise ValueError("Turn planner parameter-binding digest does not match")
        keys = tuple(item.offer_key for item in self.selected_offers)
        ids = tuple(item.offer_id for item in self.selected_offers)
        if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
            raise ValueError("Turn planner adjudication selected duplicate offers")
        binding_keys = tuple(
            (item.offer_key, item.parameter_name) for item in self.parameter_bindings
        )
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("Turn planner adjudication contains duplicate parameter bindings")
        if any(item.offer_key not in set(keys) for item in self.parameter_bindings):
            raise ValueError("Turn planner parameter binding references an unselected offer")
        if self.outcome == "single_step":
            if (
                len(self.selected_offers) != 1
                or self.proposal_manifest is None
            ):
                raise ValueError("Single-step adjudication requires one proposed offer")
        elif self.outcome == "multi_step_deferred":
            if (
                not 2 <= len(self.selected_offers) <= 8
                or self.proposal_manifest is None
            ):
                raise ValueError("Deferred multi-step adjudication requires 2-8 offers")
            if self.reason_code != "MULTI_STEP_PLAN_DEFERRED":
                raise ValueError("Deferred multi-step adjudication reason changed")
        elif self.outcome == "needs_user_input":
            if (
                len(self.selected_offers) > 1
                or self.parameter_bindings
                or self.proposal_manifest is None
            ):
                raise ValueError("Needs-input adjudication proof is invalid")
        elif self.outcome == "unsupported":
            if self.selected_offers or self.parameter_bindings or self.proposal_manifest is None:
                raise ValueError("Unsupported adjudication proof is invalid")
        elif self.selected_offers or self.parameter_bindings or self.proposal_manifest is not None:
            raise ValueError("Fallback adjudication cannot select planner offers")
        values = self.model_dump(mode="json")
        identity = _record_material(values, "adjudication_id", "adjudication_digest")
        if self.adjudication_id != _content_id("tpa", identity):
            raise ValueError("Turn planner adjudication id does not match")
        if self.adjudication_digest != sha256_digest(
            _record_material(values, "adjudication_digest")
        ):
            raise ValueError("Turn planner adjudication digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        user_message_id: str,
        user_message_digest: str,
        run_id: str,
        run_digest: str,
        outcome: TurnPlannerAdjudicationOutcome,
        reason_code: str,
        created_at: datetime,
        selected_offers: tuple[TurnPlanningOfferRef, ...] = (),
        parameter_bindings: tuple[TurnPlanningParameterBinding, ...] = (),
        proposal_manifest: dict[str, JsonValue] | None = None,
    ) -> Self:
        proposal_digest = (
            sha256_digest(proposal_manifest) if proposal_manifest is not None else None
        )
        parameter_bindings_digest = (
            sha256_digest(
                {
                    "parameter_bindings": [
                        item.model_dump(mode="json") for item in parameter_bindings
                    ]
                }
            )
            if proposal_manifest is not None
            else None
        )
        values: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planner-adjudication.v1",
            "task_id": task_id,
            "user_message_id": user_message_id,
            "user_message_digest": user_message_digest,
            "run_id": run_id,
            "run_digest": run_digest,
            "outcome": outcome,
            "selected_offers": selected_offers,
            "parameter_bindings": parameter_bindings,
            "parameter_bindings_digest": parameter_bindings_digest,
            "proposal_manifest": proposal_manifest,
            "proposal_digest": proposal_digest,
            "reason_code": reason_code,
            "created_at": created_at,
        }
        adjudication_id = _content_id("tpa", values)
        digest_values = {**values, "adjudication_id": adjudication_id}
        return cls(
            **digest_values,
            adjudication_digest=sha256_digest(digest_values),
        )


class TurnPlanningPlanRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    task_contract: TaskContractRef


class TurnPlanBinding(BaseModel):
    """Trusted server binding (or explicit non-binding) for one adjudication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-plan-binding.v1"] = (
        "deskpilot.turn-plan-binding.v1"
    )
    binding_id: str = Field(pattern=TURN_PLAN_BINDING_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    adjudication_id: str = Field(pattern=TURN_PLANNER_ADJUDICATION_ID_PATTERN)
    adjudication_digest: str = Field(pattern=DIGEST_PATTERN)
    status: TurnPlanBindingStatus
    offer: TurnPlanningOfferRef | None = None
    plan: TurnPlanningPlanRef | None = None
    reason_code: str = Field(pattern=REASON_CODE_PATTERN)
    created_at: datetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def target_identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Turn plan binding timestamp must be timezone-aware")
        if self.status == "bound":
            if self.offer is None or self.plan is None:
                raise ValueError("Bound turn plan requires an offer and executable plan")
        elif self.status == "task_loop_deferred":
            if self.offer is None or self.plan is not None:
                raise ValueError(
                    "Task-loop deferred binding requires one offer and no active plan"
                )
        elif self.offer is not None or self.plan is not None:
            raise ValueError("Non-bound turn plan cannot reference an offer or plan")
        if self.status == "multi_step_deferred" and (
            self.reason_code != "MULTI_STEP_PLAN_DEFERRED"
        ):
            raise ValueError("Deferred turn plan binding reason changed")
        if self.status == "task_loop_deferred" and (
            self.reason_code != "MODEL_PLANNER_SINGLE_STEP"
        ):
            raise ValueError("Task-loop deferred binding reason changed")
        values = self.model_dump(mode="json")
        identity = _record_material(values, "binding_id", "binding_digest")
        if self.binding_id != _content_id("tpb", identity):
            raise ValueError("Turn plan binding id does not match")
        if self.binding_digest != sha256_digest(
            _record_material(values, "binding_digest")
        ):
            raise ValueError("Turn plan binding digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        user_message_id: str,
        user_message_digest: str,
        adjudication_id: str,
        adjudication_digest: str,
        status: TurnPlanBindingStatus,
        reason_code: str,
        created_at: datetime,
        offer: TurnPlanningOfferRef | None = None,
        plan: TurnPlanningPlanRef | None = None,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.turn-plan-binding.v1",
            "task_id": task_id,
            "user_message_id": user_message_id,
            "user_message_digest": user_message_digest,
            "adjudication_id": adjudication_id,
            "adjudication_digest": adjudication_digest,
            "status": status,
            "offer": offer,
            "plan": plan,
            "reason_code": reason_code,
            "created_at": created_at,
        }
        binding_id = _content_id("tpb", values)
        digest_values = {**values, "binding_id": binding_id}
        return cls(**digest_values, binding_digest=sha256_digest(digest_values))


class TurnPlanningRead(BaseModel):
    """One transactionally assembled, internally consistent planning projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planning-read.v1"] = (
        "deskpilot.turn-planning-read.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    user_message_digest: str = Field(pattern=DIGEST_PATTERN)
    offers: tuple[TurnPlanningOffer, ...] = Field(min_length=1, max_length=64)
    run: TurnPlannerRun
    adjudication: TurnPlannerAdjudication | None = None
    binding: TurnPlanBinding | None = None
    revision: int = Field(ge=1)
    planning_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digest_match(self) -> Self:
        scope = (self.task_id, self.user_message_id, self.user_message_digest)
        optional_records = tuple(
            item for item in (self.adjudication, self.binding) if item is not None
        )
        records = (*self.offers, self.run, *optional_records)
        if any(
            (item.task_id, item.user_message_id, item.user_message_digest) != scope
            for item in records
        ):
            raise ValueError("Turn planning projection crosses task or message scope")
        refs = tuple(item.ref for item in self.offers)
        if refs != self.run.offers:
            raise ValueError("Turn planning projection offer set changed")
        if self.revision != self.run.revision:
            raise ValueError("Turn planning projection revision changed")
        if self.run.status in {"prepared", "dispatching"}:
            if self.adjudication is not None or self.binding is not None:
                raise ValueError("Non-terminal turn planning projection contains an outcome")
            material = self.model_dump(mode="json", exclude={"planning_digest"})
            if self.planning_digest != sha256_digest(material):
                raise ValueError("Turn planning projection digest does not match")
            return self
        if self.adjudication is None or self.binding is None:
            raise ValueError("Terminal turn planning projection lacks an adjudication")
        if (
            self.run.status == "succeeded"
            and self.adjudication.outcome == "deterministic_fallback"
        ) or (
            self.run.status != "succeeded"
            and self.adjudication.outcome != "deterministic_fallback"
        ):
            raise ValueError("Turn planning run outcome and adjudication do not match")
        if (
            self.adjudication.run_id != self.run.run_id
            or self.adjudication.run_digest != self.run.run_digest
            or self.binding.adjudication_id != self.adjudication.adjudication_id
            or self.binding.adjudication_digest
            != self.adjudication.adjudication_digest
        ):
            raise ValueError("Turn planning projection proof lineage changed")
        known_refs = set(refs)
        if any(item not in known_refs for item in self.adjudication.selected_offers):
            raise ValueError("Turn planning adjudication selected an unknown offer")
        offers_by_key = {item.offer_key: item for item in self.offers}
        bindings_by_offer: dict[str, dict[str, TurnPlanningParameterBinding]] = {}
        for binding in self.adjudication.parameter_bindings:
            bindings_by_offer.setdefault(binding.offer_key, {})[
                binding.parameter_name
            ] = binding
        for selected in self.adjudication.selected_offers:
            offer = offers_by_key[selected.offer_key]
            specs = {item.parameter_name: item for item in offer.parameter_specs}
            bindings = bindings_by_offer.get(selected.offer_key, {})
            if any(name not in specs for name in bindings):
                raise ValueError("Turn planning proposal contains an unknown parameter")
            if self.adjudication.outcome in {"single_step", "multi_step_deferred"}:
                required = {name for name, spec in specs.items() if spec.required}
                if not required.issubset(bindings):
                    raise ValueError("Turn planning proposal omitted a required parameter")
            if any(
                not specs[name].min_length <= len(binding.value) <= specs[name].max_length
                for name, binding in bindings.items()
            ):
                raise ValueError("Turn planning proposal parameter length is invalid")
        expected_status: TurnPlanBindingStatus = (
            (
                "task_loop_deferred"
                if self.binding.reason_code == "MODEL_PLANNER_SINGLE_STEP"
                and self.binding.plan is None
                else "bound"
            )
            if self.adjudication.outcome == "single_step"
            else (
                "multi_step_deferred"
                if self.adjudication.outcome == "multi_step_deferred"
                else "not_applicable"
            )
        )
        if self.binding.status != expected_status:
            raise ValueError("Turn plan binding status does not match adjudication")
        if self.binding.status in {"bound", "task_loop_deferred"} and (
            self.binding.offer != self.adjudication.selected_offers[0]
        ):
            raise ValueError("Turn plan binding changed the adjudicated offer")
        if self.binding.status == "bound":
            if self.binding.plan is None or self.binding.offer is None:
                raise ValueError("Bound Turn plan proof is incomplete")
            selected_offer = offers_by_key[self.binding.offer.offer_key]
            expected_plan = selected_offer.expected_plan
            if (
                self.binding.plan.task_contract != selected_offer.task_contract
                or self.binding.plan.plan_id != expected_plan.plan_id
                or self.binding.plan.plan_generation != expected_plan.plan_generation
                or self.binding.plan.plan_manifest_digest
                != expected_plan.plan_manifest_digest
            ):
                raise ValueError("Turn plan binding changed the offered executable Plan")
        material = self.model_dump(mode="json", exclude={"planning_digest"})
        if self.planning_digest != sha256_digest(material):
            raise ValueError("Turn planning projection digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        offers: tuple[TurnPlanningOffer, ...],
        run: TurnPlannerRun,
        adjudication: TurnPlannerAdjudication | None = None,
        binding: TurnPlanBinding | None = None,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.turn-planning-read.v1",
            "task_id": run.task_id,
            "user_message_id": run.user_message_id,
            "user_message_digest": run.user_message_digest,
            "offers": offers,
            "run": run,
            "adjudication": adjudication,
            "binding": binding,
            "revision": run.revision,
        }
        return cls(**values, planning_digest=sha256_digest(values))


class TurnPlannerRunWorkbenchSummary(BaseModel):
    """Public, non-authoritative summary of a private planner Run proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planner-run-workbench-summary.v1"] = (
        "deskpilot.turn-planner-run-workbench-summary.v1"
    )
    status: TurnPlannerRunStatus
    offer_count: int = Field(ge=1, le=64)
    offer_set_digest: str = Field(pattern=DIGEST_PATTERN)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    response_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    failure: TurnPlannerFailureProof | None = None
    revision: int = Field(ge=1)
    run_digest: str = Field(pattern=DIGEST_PATTERN)

    @classmethod
    def from_internal(cls, run: TurnPlannerRun) -> Self:
        return cls(
            status=run.status,
            offer_count=len(run.offers),
            offer_set_digest=run.offer_set_digest,
            request_digest=run.request_digest,
            response_digest=run.response_digest,
            failure=run.failure,
            revision=run.revision,
            run_digest=run.run_digest,
        )


class TurnPlannerAdjudicationWorkbenchSummary(BaseModel):
    """Public outcome summary without selected Offers, parameters, or model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "deskpilot.turn-planner-adjudication-workbench-summary.v1"
    ] = "deskpilot.turn-planner-adjudication-workbench-summary.v1"
    outcome: TurnPlannerAdjudicationOutcome
    selected_offer_count: int = Field(ge=0, le=8)
    reason_code: str = Field(pattern=REASON_CODE_PATTERN)
    adjudication_digest: str = Field(pattern=DIGEST_PATTERN)

    @classmethod
    def from_internal(cls, adjudication: TurnPlannerAdjudication) -> Self:
        return cls(
            outcome=adjudication.outcome,
            selected_offer_count=len(adjudication.selected_offers),
            reason_code=adjudication.reason_code,
            adjudication_digest=adjudication.adjudication_digest,
        )


class TurnPlanBindingWorkbenchSummary(BaseModel):
    """Public binding state without the selected Offer or executable Plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-plan-binding-workbench-summary.v1"] = (
        "deskpilot.turn-plan-binding-workbench-summary.v1"
    )
    status: TurnPlanBindingStatus
    reason_code: str = Field(pattern=REASON_CODE_PATTERN)
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @classmethod
    def from_internal(cls, binding: TurnPlanBinding) -> Self:
        return cls(
            status=binding.status,
            reason_code=binding.reason_code,
            binding_digest=binding.binding_digest,
        )


class TurnPlanningWorkbenchRead(BaseModel):
    """Minimized Workbench projection; never an execution-authority input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.turn-planning-workbench-read.v1"] = (
        "deskpilot.turn-planning-workbench-read.v1"
    )
    run: TurnPlannerRunWorkbenchSummary
    adjudication: TurnPlannerAdjudicationWorkbenchSummary | None = None
    binding: TurnPlanBindingWorkbenchSummary | None = None
    revision: int = Field(ge=1)
    planning_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> Self:
        if self.revision != self.run.revision:
            raise ValueError("Turn planning Workbench revision changed")
        terminal = self.run.status not in {"prepared", "dispatching"}
        if terminal != (self.adjudication is not None and self.binding is not None):
            raise ValueError("Turn planning Workbench outcome completeness changed")
        if not terminal:
            return self
        if self.adjudication is None or self.binding is None:
            raise ValueError("Terminal Turn planning Workbench summary lacks an outcome")
        expected_binding: TurnPlanBindingStatus = (
            (
                "task_loop_deferred"
                if self.binding.reason_code == "MODEL_PLANNER_SINGLE_STEP"
                and self.binding.status == "task_loop_deferred"
                else "bound"
            )
            if self.adjudication.outcome == "single_step"
            else (
                "multi_step_deferred"
                if self.adjudication.outcome == "multi_step_deferred"
                else "not_applicable"
            )
        )
        if self.binding.status != expected_binding:
            raise ValueError("Turn planning Workbench binding state changed")
        if self.adjudication.selected_offer_count > self.run.offer_count:
            raise ValueError("Turn planning Workbench selected Offer count changed")
        return self

    @classmethod
    def from_internal(cls, planning: TurnPlanningRead) -> Self:
        return cls(
            run=TurnPlannerRunWorkbenchSummary.from_internal(planning.run),
            adjudication=(
                TurnPlannerAdjudicationWorkbenchSummary.from_internal(
                    planning.adjudication
                )
                if planning.adjudication is not None
                else None
            ),
            binding=(
                TurnPlanBindingWorkbenchSummary.from_internal(planning.binding)
                if planning.binding is not None
                else None
            ),
            revision=planning.revision,
            planning_digest=planning.planning_digest,
        )
