"""Durable, fail-closed model interpretation for otherwise unrouted user turns."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_model_requests import bind_agent_model_request
from deskpilot.application.agent_registry import (
    AgentRegistration,
    AgentRegistry,
    AgentRegistryError,
)
from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelProviderUnavailableError,
    ModelResponseInvalidError,
    ModelTimeoutError,
)
from deskpilot.application.model_planner_composer import RevalidatedOfferStep
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningError,
)
from deskpilot.application.route_recipe_catalog import (
    RouteId,
    RouteOfferDraft,
    RouteRecipeCatalog,
    RouteRecipeError,
)
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.model_contracts import (
    ModelExecutionBudget,
    ModelLocation,
    ModelMessage,
    ModelProviderDescriptor,
    ModelRequest,
    ModelResponse,
    PrivacyMode,
    StructuredOutputDefinition,
)
from deskpilot.domain.task_plans import (
    ExecutablePlan,
    PlanNodeBudget,
    TaskBudget,
    TaskContractRef,
)
from deskpilot.domain.task_workbench import (
    TurnRouteDecision,
    TurnRouteRead,
    TurnRouteStatus,
)
from deskpilot.domain.turn_planning import (
    TurnPlanBinding,
    TurnPlannerAdjudication,
    TurnPlannerDecision,
    TurnPlannerFailureCode,
    TurnPlannerFailureProof,
    TurnPlannerInput,
    TurnPlannerNeedsInputDecision,
    TurnPlannerProposeStepsDecision,
    TurnPlannerRun,
    TurnPlannerUnsupportedDecision,
    TurnPlanningOffer,
    TurnPlanningParameterBinding,
    TurnPlanningParameterSpec,
    TurnPlanningPlanRef,
    TurnPlanningRead,
    TurnPlanningRecipeRef,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    TaskExecutionRunRecord,
    TaskRecord,
    TurnPlanBindingRecord,
    TurnPlannerAdjudicationRecord,
    TurnPlannerRunRecord,
    TurnPlanningOfferRecord,
    TurnRouteRecord,
    utc_now,
)

TURN_PLANNER_AGENT_ID = "builtin.turn_planner"
TURN_PLANNER_AGENT_VERSION = "1.0.0"
TURN_PLANNER_SCHEMA_NAME = "turn_planner_decision"
MODEL_PLANNER_CLASSIFIER_VERSION = "deskpilot.turn-router.model-planner.v1"

_TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "failed", "outcome_unknown", "cancelled"}
)
_DIRECT_APPROVAL_ROUTES = frozenset(
    {
        "workspace_file_replace",
        "workspace_patch_bundle",
        "workspace_file_create",
        "workspace_file_rename",
        "mcp_text_metrics",
    }
)


class TurnPlannerRuntimeError(RuntimeError):
    code = "TURN_PLANNER_RUNTIME_REJECTED"


class TurnPlannerNotFoundError(TurnPlannerRuntimeError):
    code = "TURN_PLANNER_NOT_FOUND"


class TurnPlannerNotEligibleError(TurnPlannerRuntimeError):
    code = "TURN_PLANNER_NOT_ELIGIBLE"


class TurnPlannerProofRejectedError(TurnPlannerRuntimeError):
    code = "TURN_PLANNER_PROOF_REJECTED"


class TurnPlannerConflictError(TurnPlannerRuntimeError):
    code = "TURN_PLANNER_CONFLICT"


class BoundTurnRoute(BaseModel):
    """Trusted single-step route projection consumed by Workbench wiring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: RouteId
    route_version: Literal["2"] = "2"
    route_manifest_digest: str
    parameters: dict[str, str]
    status: TurnRouteStatus
    reason_code: Literal["MODEL_PLANNER_SINGLE_STEP"] = "MODEL_PLANNER_SINGLE_STEP"
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class _TurnScope:
    task: TaskRecord
    message: ConversationMessageRecord
    route: TurnRouteRecord
    content: str
    privacy_mode: PrivacyMode


@dataclass(frozen=True, slots=True)
class _PlanningBundle:
    offers: tuple[TurnPlanningOffer, ...]
    run: TurnPlannerRun
    adjudication: TurnPlannerAdjudication | None
    binding: TurnPlanBinding | None

    def read(self) -> TurnPlanningRead:
        return TurnPlanningRead.build(
            offers=self.offers,
            run=self.run,
            adjudication=self.adjudication,
            binding=self.binding,
        )


@dataclass(frozen=True, slots=True)
class RevalidatedDeferredPlan:
    """A terminal v1 deferred proposal revalidated without another model call."""

    planning: TurnPlanningRead
    steps: tuple[RevalidatedOfferStep, ...]


@dataclass(frozen=True, slots=True)
class _DispatchClaim:
    task_id: str
    run_id: str
    owner_id: str
    fencing_token: int
    revision: int


@dataclass(frozen=True, slots=True)
class _RecoverBinding:
    task_id: str


@dataclass(frozen=True, slots=True)
class _ValidatedDecision:
    response_manifest: dict[str, JsonValue]
    outcome: Literal[
        "single_step",
        "multi_step_deferred",
        "needs_user_input",
        "unsupported",
    ]
    reason_code: str
    selected_offers: tuple[TurnPlanningOffer, ...] = ()
    parameter_bindings: tuple[TurnPlanningParameterBinding, ...] = ()
    proposal_manifest: dict[str, JsonValue] | None = None


class _DecisionRejected(RuntimeError):
    def __init__(self, code: TurnPlannerFailureCode, detail: object) -> None:
        super().__init__(code)
        self.code = code
        self.detail_digest = sha256_digest(
            {"error_code": code, "detail": detail}
        )


class _AtomicFinalizeRetry(RuntimeError):
    """Roll back a tentative Plan and retry the same already-received result."""


class _AtomicFinalizeLate(RuntimeError):
    """Roll back a tentative Plan because this dispatch no longer owns the lease."""


class _AtomicFinalizeExpired(RuntimeError):
    """Roll back a tentative Plan before terminalizing an expired dispatch."""


class TurnPlannerRuntime:
    """Persistent planner reservation that never depends on Plan or Invocation state."""

    def __init__(
        self,
        database: Database,
        agents: AgentRegistry,
        gateway: ModelGateway,
        capabilities: CapabilityCatalog,
        planning: PlanCompilationService,
        *,
        provider_hint: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 90,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not 5 <= lease_seconds <= 600:
            raise ValueError("Turn Planner lease must be between 5 and 600 seconds")
        self._database = database
        self._agents = agents
        self._gateway = gateway
        self._capabilities = capabilities
        self._planning = planning
        self._provider_hint = provider_hint
        self._worker_id = worker_id or f"turn-planner-{uuid4().hex}"
        self._lease_seconds = lease_seconds
        self._clock = clock

    @property
    def enabled(self) -> bool:
        try:
            registration = self._registration()
        except AgentRegistryError:
            return False
        return any(
            descriptor.location in registration.contract.model_policy.allowed_locations
            for descriptor in self._gateway.descriptors()
        )

    async def prepare(
        self,
        task_id: str,
        user_message_id: str,
        fallback_route: TurnRouteRead,
        eligible_variant_keys: frozenset[str],
    ) -> TurnPlanningRead:
        """Persist exact Offers and a prepared reservation without calling a model."""

        existing = await self.get(task_id)
        if existing is not None:
            if existing.user_message_id != user_message_id:
                raise TurnPlannerConflictError(
                    "Task already has a Planner reservation for another message"
                )
            return existing
        if fallback_route.decision is TurnRouteDecision.ROUTED:
            raise TurnPlannerNotEligibleError(
                "A deterministic routed Turn must bypass the model planner"
            )
        async with self._database.session() as session:
            scope = await self._load_scope(
                session,
                task_id=task_id,
                user_message_id=user_message_id,
                for_update=False,
            )
            self._validate_fallback_route(scope.route, fallback_route)
        registration = self._registration(scope.privacy_mode)
        provider = self._select_provider_for_prepare(
            task_id=task_id,
            privacy_mode=scope.privacy_mode,
            registration=registration,
            message_digest=scope.message.message_digest,
        )
        now = self._now()
        offers = self._compile_offers(
            scope=scope,
            registration=registration,
            provider=provider,
            created_at=now,
            eligible_variant_keys=eligible_variant_keys,
        )
        planner_input = TurnPlannerInput.build(
            task_id=task_id,
            user_message_id=user_message_id,
            user_message_digest=scope.message.message_digest,
            user_message=scope.content,
            offers=offers,
        )
        request = self._build_request(
            planner_input=planner_input,
            privacy_mode=scope.privacy_mode,
            registration=registration,
            provider_hint=provider.provider_id,
        )
        selected = self._gateway.select_provider_snapshot(request).descriptor
        if selected != provider:
            raise TurnPlannerProofRejectedError(
                "Turn Planner Provider selection changed before reservation"
            )
        self._agents.validate_model_route(
            TURN_PLANNER_AGENT_ID,
            registration.contract.version,
            contract_digest=registration.contract.digest,
            prompt_package_digest=registration.prompt_package.digest,
            request=request,
            provider=provider,
        )
        run = TurnPlannerRun.reserve(
            task_id=task_id,
            user_message_id=user_message_id,
            user_message_digest=scope.message.message_digest,
            fallback_candidate_digest=fallback_route.candidate_digest,
            planner_agent=self._bound_agent(registration),
            provider=provider,
            offers=tuple(item.ref for item in offers),
            request_digest=sha256_digest(request),
            created_at=now,
        )
        try:
            async with self._database.session() as session, session.begin():
                locked_scope = await self._load_scope(
                    session,
                    task_id=task_id,
                    user_message_id=user_message_id,
                    for_update=True,
                )
                duplicate = await session.scalar(
                    select(TurnPlannerRunRecord).where(
                        TurnPlannerRunRecord.task_id == task_id,
                        TurnPlannerRunRecord.user_message_id == user_message_id,
                    )
                )
                if duplicate is None:
                    self._validate_fallback_route(
                        locked_scope.route,
                        fallback_route,
                    )
                    session.add_all(self._offer_record(item) for item in offers)
                    session.add(self._run_record(run))
                    # The Route reservation anchor and the immutable Run are
                    # committed together.  It remains present for fallback,
                    # deferred, unsupported, cancelled, and failed outcomes.
                    await session.flush()
                    locked_scope.route.turn_planner_run_id = run.run_id
                    locked_scope.route.turn_planning_reservation_digest = (
                        run.reservation_digest
                    )
                else:
                    persisted = self._run_from_record(duplicate)
                    if (
                        persisted.run_id != run.run_id
                        or persisted.reservation_digest != run.reservation_digest
                    ):
                        raise TurnPlannerConflictError(
                            "Task already has a different Planner reservation"
                        )
                    self._validate_fallback_route(
                        locked_scope.route,
                        fallback_route,
                        reservation=run,
                    )
        except IntegrityError:
            # The unique task/message reservation is the cross-process idempotency gate.
            pass
        prepared = await self.get(task_id)
        if prepared is None:
            raise TurnPlannerConflictError("Turn Planner reservation was not persisted")
        return prepared

    async def interpret_turn(self, task_id: str) -> TurnPlanningRead:
        """Compatibility name used by the background Workbench action."""

        return await self.interpret(task_id)

    async def interpret(self, task_id: str) -> TurnPlanningRead:
        """Claim and dispatch once, or recover a terminal outcome without replay."""

        claimed = await self._claim_for_dispatch(task_id)
        if isinstance(claimed, _RecoverBinding):
            return await self._recover_single_step_binding(task_id)
        if isinstance(claimed, TurnPlanningRead):
            return claimed
        claim, bundle = claimed
        try:
            request = await self._rebuild_request(bundle)
            response = await self._gateway.complete(request)
            decision = self._validate_response(
                response=response,
                offers=bundle.offers,
                message=await self._message_content(bundle.run),
            )
            settled = await self._settle_success(claim, decision)
        except asyncio.CancelledError:
            await self._settle_failure(
                claim,
                code="PLANNER_OUTCOME_UNKNOWN",
                detail={"reason": "dispatch_cancelled_after_reservation"},
                status="outcome_unknown",
            )
            raise
        except _DecisionRejected as error:
            settled = await self._settle_failure(
                claim,
                code=error.code,
                detail={"detail_digest": error.detail_digest},
                status="failed",
            )
        except ModelTimeoutError as error:
            settled = await self._settle_failure(
                claim,
                code="PLANNER_TIMEOUT",
                detail=self._gateway_error_detail(error),
                status="failed",
            )
        except ModelResponseInvalidError as error:
            settled = await self._settle_failure(
                claim,
                code="PLANNER_SCHEMA_REJECTED",
                detail=self._gateway_error_detail(error),
                status="failed",
            )
        except (ModelProviderUnavailableError, ModelGatewayError) as error:
            settled = await self._settle_failure(
                claim,
                code="PLANNER_PROVIDER_UNAVAILABLE",
                detail=self._gateway_error_detail(error),
                status="failed",
            )
        except (
            AgentRegistryError,
            RouteRecipeError,
            PlanningError,
            TurnPlannerProofRejectedError,
        ) as error:
            settled = await self._settle_failure(
                claim,
                code="PLANNER_BINDING_REJECTED",
                detail={"error_type": type(error).__name__},
                status="failed",
            )
        if (
            settled.adjudication is not None
            and settled.adjudication.outcome == "single_step"
            and settled.binding is None
        ):
            return await self._recover_single_step_binding(task_id)
        return settled.read()

    async def get(self, task_id: str) -> TurnPlanningRead | None:
        async with self._database.session() as session:
            bundle = await self._load_bundle(session, task_id)
            if bundle is None:
                return None
            if bundle.run.status in _TERMINAL_RUN_STATUSES:
                if bundle.adjudication is None:
                    raise TurnPlannerProofRejectedError(
                        "Terminal Turn Planner Run lacks its adjudication"
                    )
                if bundle.binding is None:
                    if (
                        bundle.run.status == "succeeded"
                        and bundle.adjudication.outcome == "single_step"
                    ):
                        # A GET is never an authorization to create a Plan or
                        # rewrite a Route. Coordinator recovery or explicit
                        # interpret performs this one legal finalize window.
                        return None
                    else:
                        raise TurnPlannerProofRejectedError(
                            "Terminal Turn Planner outcome lacks its Binding"
                        )
            await self._validate_parameter_sources(session, bundle)
            return bundle.read()

    async def revalidate_deferred_plan(self, task_id: str) -> RevalidatedDeferredPlan:
        """Resolve a terminal multi-step proposal entirely from persisted proof.

        This is the only bridge into the stage-112 offer composer.  It performs
        no Provider dispatch and returns immutable, server-recompiled recipe
        inputs in the exact adjudicated order.
        """

        async with self._database.session() as session:
            bundle = await self._load_bundle(session, task_id)
            if bundle is None:
                raise TurnPlannerNotFoundError("Turn Planner proof is missing")
            adjudication = bundle.adjudication
            binding = bundle.binding
            if (
                bundle.run.status != "succeeded"
                or adjudication is None
                or binding is None
                or adjudication.outcome != "multi_step_deferred"
                or binding.status != "multi_step_deferred"
                or binding.reason_code != "MULTI_STEP_PLAN_DEFERRED"
                or not 2 <= len(adjudication.selected_offers) <= 8
            ):
                raise TurnPlannerNotEligibleError(
                    "Turn Planner outcome is not a deferred multi-step proposal"
                )
            await self._validate_parameter_sources(session, bundle)
            message = await self._message_content(bundle.run, session=session)
            offers_by_key = {item.offer_key: item for item in bundle.offers}
            steps: list[RevalidatedOfferStep] = []
            for selected in adjudication.selected_offers:
                offer = offers_by_key.get(selected.offer_key)
                if offer is None or offer.ref != selected:
                    raise TurnPlannerProofRejectedError(
                        "Deferred Planner selected Offer proof changed"
                    )
                route = self._draft_for_offer(offer)
                parameters = self._canonical_parameters(
                    adjudication=adjudication,
                    offer=offer,
                    draft=route,
                    message=message,
                )
                raw_binding_digest = self._proposal_step(
                    adjudication,
                    offer.offer_key,
                ).get("parameter_binding_digest")
                if not isinstance(raw_binding_digest, str):
                    raise TurnPlannerProofRejectedError(
                        "Deferred Planner parameter-binding digest is missing"
                    )
                steps.append(
                    RevalidatedOfferStep(
                        offer=offer,
                        route=route,
                        parameters=MappingProxyType(dict(parameters)),
                        parameter_binding_digest=raw_binding_digest,
                        planner_agent=bundle.run.planner_agent,
                    )
                )
            return RevalidatedDeferredPlan(
                planning=bundle.read(),
                steps=tuple(steps),
            )

    async def revalidate_task_loop_plan(self, task_id: str) -> RevalidatedDeferredPlan:
        """Revalidate a deferred composition or one planner-only Offer for Task Loop.

        Historical single-step Offers for the deterministic Route surface retain
        their stage-111 direct execution path.  Stage-113 planner-only Routes have
        no legacy executor, so their exact bound Offer is admitted only to the
        generic Task Loop without another Provider call.
        """

        async with self._database.session() as session:
            bundle = await self._load_bundle(session, task_id)
            if bundle is None:
                raise TurnPlannerNotFoundError("Turn Planner proof is missing")
            adjudication = bundle.adjudication
            binding = bundle.binding
            if (
                bundle.run.status != "succeeded"
                or adjudication is None
                or binding is None
            ):
                raise TurnPlannerNotEligibleError(
                    "Turn Planner outcome is not eligible for the generic Task Loop"
                )
            is_deferred = bool(
                adjudication.outcome == "multi_step_deferred"
                and binding.status == "multi_step_deferred"
                and binding.reason_code == "MULTI_STEP_PLAN_DEFERRED"
                and 2 <= len(adjudication.selected_offers) <= 8
            )
            is_planner_only_single = bool(
                adjudication.outcome == "single_step"
                and binding.status == "bound"
                and binding.reason_code == "MODEL_PLANNER_SINGLE_STEP"
                and len(adjudication.selected_offers) == 1
            )
            if not is_deferred and not is_planner_only_single:
                raise TurnPlannerNotEligibleError(
                    "Turn Planner outcome is not eligible for the generic Task Loop"
                )
            await self._validate_parameter_sources(session, bundle)
            message = await self._message_content(bundle.run, session=session)
            offers_by_key = {item.offer_key: item for item in bundle.offers}
            steps: list[RevalidatedOfferStep] = []
            for selected in adjudication.selected_offers:
                offer = offers_by_key.get(selected.offer_key)
                if offer is None or offer.ref != selected:
                    raise TurnPlannerProofRejectedError(
                        "Task Loop Planner selected Offer proof changed"
                    )
                if is_planner_only_single and not RouteRecipeCatalog.is_planner_only_route(
                    offer.trusted_recipe.route_id
                ):
                    raise TurnPlannerNotEligibleError(
                        "Legacy single-step Offers must retain direct execution"
                    )
                if is_planner_only_single and (
                    binding.offer != offer.ref
                    or binding.plan is None
                    or binding.plan.plan_id != offer.expected_plan.plan_id
                    or binding.plan.plan_manifest_digest
                    != offer.expected_plan.plan_manifest_digest
                ):
                    raise TurnPlannerProofRejectedError(
                        "Planner-only single-step Binding changed from its Offer"
                    )
                route = self._draft_for_offer(offer)
                parameters = self._canonical_parameters(
                    adjudication=adjudication,
                    offer=offer,
                    draft=route,
                    message=message,
                )
                raw_binding_digest = self._proposal_step(
                    adjudication,
                    offer.offer_key,
                ).get("parameter_binding_digest")
                if not isinstance(raw_binding_digest, str):
                    raise TurnPlannerProofRejectedError(
                        "Task Loop Planner parameter-binding digest is missing"
                    )
                steps.append(
                    RevalidatedOfferStep(
                        offer=offer,
                        route=route,
                        parameters=MappingProxyType(dict(parameters)),
                        parameter_binding_digest=raw_binding_digest,
                        planner_agent=bundle.run.planner_agent,
                    )
                )
            return RevalidatedDeferredPlan(
                planning=bundle.read(),
                steps=tuple(steps),
            )

    async def get_bound_route(self, task_id: str) -> BoundTurnRoute | None:
        planning = await self.get(task_id)
        if planning is None:
            return None
        return self.bound_route(planning)

    @staticmethod
    def bound_route(planning: TurnPlanningRead) -> BoundTurnRoute | None:
        adjudication = planning.adjudication
        binding = planning.binding
        if (
            adjudication is None
            or binding is None
            or adjudication.outcome != "single_step"
            or binding.status != "bound"
            or len(adjudication.selected_offers) != 1
            or adjudication.proposal_manifest is None
        ):
            return None
        offer = next(
            item
            for item in planning.offers
            if item.offer_key == adjudication.selected_offers[0].offer_key
        )
        parameters = TurnPlannerRuntime._proposal_parameters(
            adjudication.proposal_manifest,
            offer.offer_key,
        )
        status = (
            TurnRouteStatus.NEEDS_USER_ACTION
            if offer.trusted_recipe.route_id in _DIRECT_APPROVAL_ROUTES
            else TurnRouteStatus.READY
        )
        reason_code = "MODEL_PLANNER_SINGLE_STEP"
        candidate_digest = sha256_digest(
            {
                "classifier_version": MODEL_PLANNER_CLASSIFIER_VERSION,
                "message_digest": planning.user_message_digest,
                "decision": TurnRouteDecision.ROUTED.value,
                "route_id": offer.trusted_recipe.route_id,
                "parameters": parameters,
                "reason_code": reason_code,
            }
        )
        return BoundTurnRoute(
            route_id=cast(RouteId, offer.trusted_recipe.route_id),
            route_manifest_digest=offer.trusted_recipe.route_manifest_digest,
            parameters=parameters,
            status=status,
            candidate_digest=candidate_digest,
        )

    async def cancel(self, task_id: str) -> TurnPlanningRead | None:
        """Fence an uncompleted reservation; a late Provider result cannot bind."""

        for _ in range(3):
            async with self._database.session() as session, session.begin():
                record = await self._run_record_for_task(session, task_id, for_update=True)
                if record is None:
                    return None
                run = self._run_from_record(record)
                if run.status in _TERMINAL_RUN_STATUSES:
                    bundle = await self._load_bundle(session, task_id)
                    if bundle is None or bundle.binding is None:
                        break
                    return bundle.read()
                now = self._now()
                failure = TurnPlannerFailureProof.build(
                    error_code="PLANNER_CANCELLED",
                    detail_digest=sha256_digest(
                        {"run_id": run.run_id, "revision": run.revision}
                    ),
                )
                cancelled = run.evolve(
                    status="cancelled",
                    revision=run.revision + 1,
                    updated_at=now,
                    claim_fencing_token=run.claim_fencing_token + 1,
                    request_dispatched_at=run.request_dispatched_at,
                    completed_at=now,
                    failure=failure,
                )
                changed = await self._cas_run(session, run, cancelled)
                if not changed:
                    continue
                await self._persist_fallback_outcome(
                    session,
                    cancelled,
                    reason_code="PLANNER_CANCELLED",
                )
            result = await self.get(task_id)
            if result is None:
                raise TurnPlannerConflictError("Cancelled Planner proof is incomplete")
            return result
        result = await self.get(task_id)
        if result is None:
            raise TurnPlannerConflictError("Turn Planner cancellation raced with settlement")
        return result

    async def recoverable_task_ids(self, limit: int = 100) -> tuple[str, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Turn Planner recovery limit is invalid")
        now = self._now()
        async with self._database.session() as session:
            statement = (
                select(TurnPlannerRunRecord.task_id)
                .outerjoin(
                    TurnPlannerAdjudicationRecord,
                    TurnPlannerAdjudicationRecord.run_id
                    == TurnPlannerRunRecord.run_id,
                )
                .outerjoin(
                    TurnPlanBindingRecord,
                    TurnPlanBindingRecord.adjudication_id
                    == TurnPlannerAdjudicationRecord.adjudication_id,
                )
                .outerjoin(
                    TaskExecutionRunRecord,
                    TaskExecutionRunRecord.task_id == TurnPlannerRunRecord.task_id,
                )
                .where(
                    or_(
                        TurnPlannerRunRecord.status == "prepared",
                        (
                            (TurnPlannerRunRecord.status == "dispatching")
                            & (TurnPlannerRunRecord.claim_expires_at <= now)
                        ),
                        (
                            (TurnPlannerRunRecord.status == "succeeded")
                            & (TurnPlannerAdjudicationRecord.outcome == "single_step")
                            & (TurnPlanBindingRecord.binding_id.is_(None))
                        ),
                        (
                            (TurnPlanBindingRecord.status == "bound")
                            & (TaskExecutionRunRecord.run_id.is_(None))
                        ),
                    )
                )
                .order_by(TurnPlannerRunRecord.created_at, TurnPlannerRunRecord.task_id)
                .limit(limit)
            )
            return tuple((await session.scalars(statement)).all())

    def _registration(
        self,
        privacy_mode: PrivacyMode | None = None,
    ) -> AgentRegistration:
        if privacy_mode is None:
            return self._agents.resolve_preferred(TURN_PLANNER_AGENT_ID)
        allowed_locations = (
            (ModelLocation.LOCAL,)
            if privacy_mode in {"local_only", "local_preferred"}
            else (ModelLocation.LOCAL, ModelLocation.CLOUD)
        )
        return self._agents.resolve_preferred_compatible(
            TURN_PLANNER_AGENT_ID,
            allowed_locations=allowed_locations,
            allowed_privacy_modes=(privacy_mode,),
        )

    def _select_provider_for_prepare(
        self,
        *,
        task_id: str,
        privacy_mode: PrivacyMode,
        registration: AgentRegistration,
        message_digest: str,
    ) -> ModelProviderDescriptor:
        probe = self._base_request(
            request_id=f"turn-planner-probe-{message_digest[:32]}",
            task_id=task_id,
            privacy_mode=privacy_mode,
            registration=registration,
            provider_hint=self._provider_hint,
            user_content=(
                "Select the exact approved Provider for the persisted Turn digest "
                f"{message_digest}. No model call is authorized by this probe."
            ),
            metadata={"turn_planner_probe": True, "user_message_digest": message_digest},
        )
        provider = self._gateway.select_provider_snapshot(probe).descriptor
        self._agents.validate_model_route(
            TURN_PLANNER_AGENT_ID,
            registration.contract.version,
            contract_digest=registration.contract.digest,
            prompt_package_digest=registration.prompt_package.digest,
            request=probe,
            provider=provider,
        )
        return provider

    def _compile_offers(
        self,
        *,
        scope: _TurnScope,
        registration: AgentRegistration,
        provider: ModelProviderDescriptor,
        created_at: datetime,
        eligible_variant_keys: frozenset[str],
    ) -> tuple[TurnPlanningOffer, ...]:
        planner = self._bound_agent(registration)
        drafts = tuple(
            item
            for item in RouteRecipeCatalog.offers_for(
                task_id=scope.task.task_id,
                capabilities=self._capabilities,
                eligible_variant_keys=eligible_variant_keys,
            )
        )
        if not drafts:
            raise TurnPlannerNotEligibleError(
                "No currently executable Route is eligible for a Planner Offer"
            )
        result: list[TurnPlanningOffer] = []
        for draft in drafts:
            expected_plan = self._planning.preview_initial(
                draft.contract,
                draft.draft,
            )
            execution_agents = tuple(
                sorted(
                    {
                        (
                            node.bound_agent.agent_id,
                            node.bound_agent.version,
                            node.bound_agent.contract_digest,
                            node.bound_agent.prompt_package_digest,
                        ): node.bound_agent
                        for node in expected_plan.nodes
                        if node.bound_agent is not None
                    }.values(),
                    key=lambda item: (
                        item.agent_id,
                        item.version,
                        item.contract_digest,
                        item.prompt_package_digest,
                    ),
                )
            )
            parameter_specs = tuple(
                TurnPlanningParameterSpec(
                    parameter_name=item.name,
                    required=item.required,
                    min_length=1,
                    max_length=4_000,
                )
                for item in draft.parameter_specs
            )
            budget = self._route_budget(draft.contract.budget)
            policy_snapshot_digest = sha256_digest(
                {
                    "schema_version": "deskpilot.turn-planning-policy-snapshot.v1",
                    "task_contract_digest": draft.contract.digest,
                    "planner_agent": planner.model_dump(mode="json"),
                    "agent_contract_digest": registration.contract.digest,
                    "prompt_package_digest": registration.prompt_package.digest,
                    "execution_agents": [
                        item.model_dump(mode="json") for item in execution_agents
                    ],
                    "execution_agents_digest": sha256_digest(
                        {
                            "execution_agents": [
                                item.model_dump(mode="json")
                                for item in execution_agents
                            ]
                        }
                    ),
                    "expected_plan_id": expected_plan.plan_id,
                    "expected_plan_manifest_digest": (
                        expected_plan.plan_manifest_digest
                    ),
                    "expected_plan_binding_snapshot_digest": (
                        expected_plan.binding_snapshot_digest
                    ),
                    "provider_snapshot_digest": sha256_digest(provider),
                    "capabilities": [
                        item.model_dump(mode="json")
                        for item in draft.contract.capabilities
                    ],
                    "trusted_recipe_digest": draft.recipe_digest,
                    "budget": budget.model_dump(mode="json"),
                    "parameter_specs": [
                        item.model_dump(mode="json") for item in parameter_specs
                    ],
                }
            )
            offer_key = turn_planning_offer_key(
                task_id=scope.task.task_id,
                user_message_id=scope.message.message_id,
                user_message_digest=scope.message.message_digest,
                variant_key=draft.variant_key,
                task_contract_digest=draft.contract.digest,
                execution_agents=execution_agents,
                expected_plan_id=expected_plan.plan_id,
                expected_plan_manifest_digest=expected_plan.plan_manifest_digest,
                expected_plan_binding_snapshot_digest=(
                    expected_plan.binding_snapshot_digest
                ),
                provider_snapshot_digest=sha256_digest(provider),
                recipe_digest=draft.recipe_digest,
                policy_snapshot_digest=policy_snapshot_digest,
            )
            result.append(
                TurnPlanningOffer.build(
                    offer_key=offer_key,
                    task_id=scope.task.task_id,
                    user_message_id=scope.message.message_id,
                    user_message_digest=scope.message.message_digest,
                    intent_description=RouteRecipeCatalog.intent_description(draft),
                    task_contract=TaskContractRef(
                        contract_id=draft.contract.contract_id,
                        version=draft.contract.version,
                        digest=draft.contract.digest,
                    ),
                    expected_plan=expected_plan,
                    capabilities=draft.contract.capabilities,
                    provider=provider,
                    trusted_recipe=TurnPlanningRecipeRef(
                        route_id=draft.route_id,
                        route_version=draft.route_version,
                        route_manifest_digest=draft.recipe_digest,
                    ),
                    budget=budget,
                    parameter_specs=parameter_specs,
                    policy_snapshot_digest=policy_snapshot_digest,
                    created_at=created_at,
                )
            )
        return tuple(result)

    def _build_request(
        self,
        *,
        planner_input: TurnPlannerInput,
        privacy_mode: PrivacyMode,
        registration: AgentRegistration,
        provider_hint: str,
    ) -> ModelRequest:
        request_identity = sha256_digest(
            {
                "task_id": planner_input.task_id,
                "user_message_id": planner_input.user_message_id,
                "input_digest": planner_input.input_digest,
                "provider_id": provider_hint,
                "agent_contract_digest": registration.contract.digest,
                "prompt_package_digest": registration.prompt_package.digest,
            }
        )
        return self._base_request(
            request_id=f"turn-planner-{request_identity}",
            task_id=planner_input.task_id,
            privacy_mode=privacy_mode,
            registration=registration,
            provider_hint=provider_hint,
            user_content=canonical_json_bytes(planner_input).decode("utf-8"),
            metadata={
                "turn_planner_input_digest": planner_input.input_digest,
                "turn_planner_offer_set_digest": planner_input.offer_set_digest,
                "turn_planner_user_message_id": planner_input.user_message_id,
                "turn_planner_user_message_digest": planner_input.user_message_digest,
            },
        )

    @staticmethod
    def _base_request(
        *,
        request_id: str,
        task_id: str,
        privacy_mode: PrivacyMode,
        registration: AgentRegistration,
        provider_hint: str | None,
        user_content: str,
        metadata: dict[str, object],
    ) -> ModelRequest:
        contract = registration.contract
        request = ModelRequest(
            request_id=request_id,
            task_id=task_id,
            role=contract.model_policy.role,
            messages=(
                ModelMessage(role="system", content=registration.prompt_package.instruction),
                ModelMessage(role="user", content=user_content),
            ),
            privacy_mode=privacy_mode,
            requirements=contract.model_policy.requirements,
            output_schema=StructuredOutputDefinition.from_model(
                name=TURN_PLANNER_SCHEMA_NAME,
                description=(
                    "One unprivileged selection of opaque server Offers, a bounded "
                    "missing-input decision, or unsupported"
                ),
                model=TurnPlannerDecision,
                strict=True,
            ),
            provider_hint=provider_hint,
            cloud_fallback_approved=False,
            temperature=0,
            max_output_tokens=contract.budget_policy.max_output_tokens,
            timeout_seconds=float(contract.budget_policy.max_wall_seconds),
            execution_budget=ModelExecutionBudget(
                max_attempts=1,
                max_retry_delay_seconds=0,
                max_task_cost_micros=contract.budget_policy.max_cost_micros,
            ),
            metadata=cast(dict[str, JsonValue], metadata),
        )
        return bind_agent_model_request(
            request,
            agent_id=contract.agent_id,
            agent_version=contract.version,
            contract_digest=contract.digest,
            prompt_package_digest=registration.prompt_package.digest,
            prompt_instruction=registration.prompt_package.instruction,
        )

    async def _rebuild_request(self, bundle: _PlanningBundle) -> ModelRequest:
        run = bundle.run
        registration = self._agents.resolve_exact(
            run.planner_agent.agent_id,
            run.planner_agent.version,
            contract_digest=run.planner_agent.contract_digest,
            prompt_package_digest=run.planner_agent.prompt_package_digest,
        )
        async with self._database.session() as session:
            scope = await self._load_scope(
                session,
                task_id=run.task_id,
                user_message_id=run.user_message_id,
                for_update=False,
            )
        planner_input = TurnPlannerInput.build(
            task_id=run.task_id,
            user_message_id=run.user_message_id,
            user_message_digest=run.user_message_digest,
            user_message=scope.content,
            offers=bundle.offers,
        )
        request = self._build_request(
            planner_input=planner_input,
            privacy_mode=scope.privacy_mode,
            registration=registration,
            provider_hint=run.provider.provider_id,
        )
        if sha256_digest(request) != run.request_digest:
            raise TurnPlannerProofRejectedError("Turn Planner request binding changed")
        provider = self._gateway.descriptor(run.provider.provider_id)
        if provider != run.provider:
            raise TurnPlannerProofRejectedError("Turn Planner Provider snapshot changed")
        selected = self._gateway.select_provider(request).descriptor
        if selected != run.provider:
            raise TurnPlannerProofRejectedError("Turn Planner exact Provider route changed")
        self._agents.validate_model_route(
            run.planner_agent.agent_id,
            run.planner_agent.version,
            contract_digest=run.planner_agent.contract_digest,
            prompt_package_digest=run.planner_agent.prompt_package_digest,
            request=request,
            provider=provider,
        )
        return request

    async def _claim_for_dispatch(
        self,
        task_id: str,
    ) -> tuple[_DispatchClaim, _PlanningBundle] | TurnPlanningRead | _RecoverBinding:
        for _ in range(5):
            async with self._database.session() as session, session.begin():
                record = await self._run_record_for_task(session, task_id, for_update=True)
                if record is None:
                    raise TurnPlannerNotFoundError("Task has no Turn Planner reservation")
                run = self._run_from_record(record)
                bundle = await self._load_bundle(session, task_id)
                if bundle is None:
                    raise TurnPlannerProofRejectedError(
                        "Turn Planner reservation lost its Offer set"
                    )
                if run.status in _TERMINAL_RUN_STATUSES:
                    if (
                        bundle.adjudication is not None
                        and bundle.adjudication.outcome == "single_step"
                        and bundle.binding is None
                    ):
                        return _RecoverBinding(task_id)
                    if bundle.adjudication is None or bundle.binding is None:
                        raise TurnPlannerProofRejectedError(
                            "Terminal Turn Planner reservation lacks outcome proof"
                        )
                    await self._validate_parameter_sources(session, bundle)
                    return bundle.read()
                now = self._now()
                if run.status == "dispatching":
                    expires_at = self._aware(run.claim_expires_at)
                    if expires_at is not None and expires_at > now:
                        return bundle.read()
                    unknown = self._terminal_run(
                        run,
                        status="outcome_unknown",
                        code="PLANNER_OUTCOME_UNKNOWN",
                        now=now,
                        detail={"reason": "dispatch_lease_expired"},
                        fence_increment=1,
                    )
                    if not await self._cas_run(session, run, unknown):
                        continue
                    await self._persist_fallback_outcome(
                        session,
                        unknown,
                        reason_code="PLANNER_OUTCOME_UNKNOWN",
                    )
                else:
                    dispatching = run.evolve(
                        status="dispatching",
                        revision=run.revision + 1,
                        updated_at=now,
                        claim_owner_id=self._worker_id,
                        claim_fencing_token=run.claim_fencing_token + 1,
                        claim_expires_at=now + timedelta(seconds=self._lease_seconds),
                        request_dispatched_at=now,
                    )
                    if not await self._cas_run(session, run, dispatching):
                        continue
                    claimed_bundle = _PlanningBundle(
                        offers=bundle.offers,
                        run=dispatching,
                        adjudication=None,
                        binding=None,
                    )
                    return (
                        _DispatchClaim(
                            task_id=task_id,
                            run_id=run.run_id,
                            owner_id=self._worker_id,
                            fencing_token=dispatching.claim_fencing_token,
                            revision=dispatching.revision,
                        ),
                        claimed_bundle,
                    )
            result = await self.get(task_id)
            if result is None:
                raise TurnPlannerConflictError(
                    "Expired Turn Planner reservation did not terminalize"
                )
            return result
        raise TurnPlannerConflictError("Turn Planner dispatch claim changed concurrently")

    def _validate_response(
        self,
        *,
        response: ModelResponse,
        offers: tuple[TurnPlanningOffer, ...],
        message: str,
    ) -> _ValidatedDecision:
        if response.structured_output is None:
            raise _DecisionRejected(
                "PLANNER_SCHEMA_REJECTED",
                {"reason": "structured_output_missing"},
            )
        try:
            decision = TurnPlannerDecision.model_validate_json(
                canonical_json_bytes(response.structured_output),
                strict=True,
            )
        except ValidationError as error:
            raise _DecisionRejected(
                "PLANNER_SCHEMA_REJECTED",
                {
                    "reason": "strict_schema_rejected",
                    "error_count": error.error_count(),
                    "response_digest": sha256_digest(response.structured_output),
                },
            ) from error
        response_manifest = cast(
            dict[str, JsonValue],
            {
                "schema_version": "deskpilot.turn-planner-response.v1",
                "decision": decision.model_dump(mode="json"),
                "provider_id": response.provider_id,
                "model": response.model,
                "native_response_id": response.native_response_id,
                "finish_reason": response.finish_reason.value,
                "usage": response.usage.model_dump(mode="json"),
                "latency_ms": response.latency_ms,
            },
        )
        root = decision.root
        offers_by_key = {item.offer_key: item for item in offers}
        decision_manifest = decision.model_dump(mode="json")
        if isinstance(root, TurnPlannerUnsupportedDecision):
            return _ValidatedDecision(
                response_manifest=response_manifest,
                outcome="unsupported",
                reason_code="MODEL_PLANNER_UNSUPPORTED",
                proposal_manifest=cast(
                    dict[str, JsonValue],
                    {
                        "schema_version": (
                            "deskpilot.turn-planner-validated-proposal.v1"
                        ),
                        "decision": decision_manifest,
                        "steps": [],
                        "grants_authority": False,
                    },
                ),
            )
        if isinstance(root, TurnPlannerNeedsInputDecision):
            selected_offer: tuple[TurnPlanningOffer, ...] = ()
            if root.offer_key is not None:
                offer = offers_by_key.get(root.offer_key)
                if offer is None:
                    raise _DecisionRejected(
                        "PLANNER_UNKNOWN_OFFER",
                        {"offer_key_digest": sha256_digest({"value": root.offer_key})},
                    )
                known = {item.parameter_name for item in offer.parameter_specs}
                if any(item not in known for item in root.missing_parameters):
                    raise _DecisionRejected(
                        "PLANNER_SCHEMA_REJECTED",
                        {"reason": "needs_input_parameter_not_offered"},
                    )
                selected_offer = (offer,)
            elif any(
                not any(
                    item.parameter_name == missing
                    for offer in offers
                    for item in offer.parameter_specs
                )
                for missing in root.missing_parameters
            ):
                raise _DecisionRejected(
                    "PLANNER_SCHEMA_REJECTED",
                    {"reason": "needs_input_parameter_not_offered"},
                )
            return _ValidatedDecision(
                response_manifest=response_manifest,
                outcome="needs_user_input",
                reason_code="MODEL_PLANNER_NEEDS_USER_INPUT",
                selected_offers=selected_offer,
                proposal_manifest=cast(
                    dict[str, JsonValue],
                    {
                        "schema_version": (
                            "deskpilot.turn-planner-validated-proposal.v1"
                        ),
                        "decision": decision_manifest,
                        "steps": [],
                        "grants_authority": False,
                    },
                ),
            )
        if not isinstance(root, TurnPlannerProposeStepsDecision):
            raise _DecisionRejected(
                "PLANNER_SCHEMA_REJECTED",
                {"reason": "decision_variant_unknown"},
            )
        selected: list[TurnPlanningOffer] = []
        bindings: list[TurnPlanningParameterBinding] = []
        validated_steps: list[dict[str, JsonValue]] = []
        for step in root.steps:
            offer = offers_by_key.get(step.offer_key)
            if offer is None:
                raise _DecisionRejected(
                    "PLANNER_UNKNOWN_OFFER",
                    {"offer_key_digest": sha256_digest({"value": step.offer_key})},
                )
            draft = self._draft_for_offer(offer)
            proposed = {item.name: item.value for item in step.parameters}
            for name, value in proposed.items():
                source_start = message.find(value)
                if source_start < 0:
                    raise _DecisionRejected(
                        "PLANNER_SCHEMA_REJECTED",
                        {
                            "reason": "parameter_not_verbatim",
                            "offer_key": offer.offer_key,
                            "parameter_name": name,
                            "value_digest": sha256_digest({"value": value}),
                        },
                    )
                source_end = source_start + len(value)
                if message[source_start:source_end] != value:
                    raise _DecisionRejected(
                        "PLANNER_SCHEMA_REJECTED",
                        {"reason": "parameter_span_changed"},
                    )
                bindings.append(
                    TurnPlanningParameterBinding.build(
                        offer_key=offer.offer_key,
                        parameter_name=name,
                        value=value,
                        source_start=source_start,
                        source_end=source_end,
                    )
                )
            try:
                parameters, parameter_manifest, parameter_digest = (
                    RouteRecipeCatalog.bind_parameters(
                        draft.route_id,
                        message,
                        proposed,
                        fixed_parameters=draft.fixed_parameters,
                    )
                )
            except RouteRecipeError as error:
                raise _DecisionRejected(
                    "PLANNER_SCHEMA_REJECTED",
                    {
                        "reason": "trusted_recipe_parameter_rejected",
                        "error_type": type(error).__name__,
                    },
                ) from error
            selected.append(offer)
            validated_steps.append(
                cast(
                    dict[str, JsonValue],
                    {
                        "offer_key": offer.offer_key,
                        "offer_digest": offer.offer_digest,
                        "route_id": draft.route_id,
                        "route_version": draft.route_version,
                        "recipe_digest": draft.recipe_digest,
                        "parameters": parameters,
                        "parameter_binding": parameter_manifest,
                        "parameter_binding_digest": parameter_digest,
                        "task_contract_digest": draft.contract.digest,
                    },
                )
            )
        proposal_manifest = cast(
            dict[str, JsonValue],
            {
                "schema_version": "deskpilot.turn-planner-validated-proposal.v1",
                "decision": decision.model_dump(mode="json"),
                "steps": validated_steps,
                "grants_authority": False,
            },
        )
        if len(selected) == 1:
            return _ValidatedDecision(
                response_manifest=response_manifest,
                outcome="single_step",
                reason_code="MODEL_PLANNER_SINGLE_STEP",
                selected_offers=tuple(selected),
                parameter_bindings=tuple(bindings),
                proposal_manifest=proposal_manifest,
            )
        return _ValidatedDecision(
            response_manifest=response_manifest,
            outcome="multi_step_deferred",
            reason_code="MULTI_STEP_PLAN_DEFERRED",
            selected_offers=tuple(selected),
            parameter_bindings=tuple(bindings),
            proposal_manifest=proposal_manifest,
        )

    async def _settle_success(
        self,
        claim: _DispatchClaim,
        decision: _ValidatedDecision,
    ) -> _PlanningBundle:
        if decision.outcome == "single_step":
            return await self._settle_single_step_success(claim, decision)
        for _ in range(3):
            late_result = False
            async with self._database.session() as session, session.begin():
                record = await self._run_record_for_task(
                    session,
                    claim.task_id,
                    for_update=True,
                )
                if record is None:
                    raise TurnPlannerNotFoundError("Turn Planner reservation disappeared")
                run = self._run_from_record(record)
                if not self._owns_claim(run, claim):
                    late_result = True
                else:
                    now = self._now()
                    expires_at = self._aware(run.claim_expires_at)
                    if expires_at is None or expires_at <= now:
                        terminal = self._terminal_run(
                            run,
                            status="outcome_unknown",
                            code="PLANNER_OUTCOME_UNKNOWN",
                            now=now,
                            detail={"reason": "result_arrived_after_lease"},
                            fence_increment=1,
                        )
                        if not await self._cas_run(session, run, terminal):
                            continue
                        await self._persist_fallback_outcome(
                            session,
                            terminal,
                            reason_code="PLANNER_OUTCOME_UNKNOWN",
                        )
                    else:
                        terminal = run.evolve(
                            status="succeeded",
                            revision=run.revision + 1,
                            updated_at=now,
                            claim_fencing_token=run.claim_fencing_token,
                            request_dispatched_at=run.request_dispatched_at,
                            completed_at=now,
                            response_manifest=decision.response_manifest,
                        )
                        if not await self._cas_run(session, run, terminal):
                            continue
                        adjudication = TurnPlannerAdjudication.build(
                            task_id=terminal.task_id,
                            user_message_id=terminal.user_message_id,
                            user_message_digest=terminal.user_message_digest,
                            run_id=terminal.run_id,
                            run_digest=terminal.run_digest,
                            outcome=decision.outcome,
                            selected_offers=tuple(
                                item.ref for item in decision.selected_offers
                            ),
                            parameter_bindings=decision.parameter_bindings,
                            proposal_manifest=decision.proposal_manifest,
                            reason_code=decision.reason_code,
                            created_at=now,
                        )
                        session.add(self._adjudication_record(adjudication))
                        # No ORM relationship declares the composite lineage,
                        # so enforce FK insertion order explicitly.
                        await session.flush()
                        binding = TurnPlanBinding.build(
                            task_id=terminal.task_id,
                            user_message_id=terminal.user_message_id,
                            user_message_digest=terminal.user_message_digest,
                            adjudication_id=adjudication.adjudication_id,
                            adjudication_digest=adjudication.adjudication_digest,
                            status=(
                                "multi_step_deferred"
                                if decision.outcome == "multi_step_deferred"
                                else "not_applicable"
                            ),
                            reason_code=decision.reason_code,
                            created_at=now,
                        )
                        session.add(self._binding_record(binding))
                        await session.flush()
                        bundle = await self._load_bundle(session, claim.task_id)
                        if bundle is None:
                            raise TurnPlannerProofRejectedError(
                                "Settled Turn Planner proof could not be reloaded"
                            )
                        return _PlanningBundle(
                            offers=bundle.offers,
                            run=terminal,
                            adjudication=adjudication,
                            binding=binding,
                        )
            if late_result:
                return await self._bundle_or_recover(claim.task_id)
            result = await self.get(claim.task_id)
            if result is None:
                raise TurnPlannerConflictError("Unknown Planner outcome is incomplete")
            return _PlanningBundle(
                offers=result.offers,
                run=result.run,
                adjudication=result.adjudication,
                binding=result.binding,
            )
        raise TurnPlannerConflictError("Turn Planner result settlement changed concurrently")

    async def _settle_single_step_success(
        self,
        claim: _DispatchClaim,
        decision: _ValidatedDecision,
    ) -> _PlanningBundle:
        """Atomically bind a fresh single-step result or persist no authority.

        PlanCompilationService establishes the global Task -> PlanningState lock
        order.  The Planner Run and then the Turn Route are locked afterwards.
        Any exception therefore rolls the tentative generation-1 Plan, terminal
        Run, adjudication, Binding, and Route rewrite back as one unit; the
        caller can safely settle the still-dispatching Run as a rejected binding.
        """

        if len(decision.selected_offers) != 1:
            raise TurnPlannerProofRejectedError(
                "Single-step Planner result must select exactly one Offer"
            )
        offer = decision.selected_offers[0]
        if offer.task_id != claim.task_id:
            raise TurnPlannerProofRejectedError(
                "Single-step Planner Offer changed task scope"
            )
        draft = self._draft_for_offer(offer)
        for _ in range(3):
            try:
                async with self._database.session() as session, session.begin():
                    activated = (
                        await self._planning.activate_initial_once_in_session(
                            session,
                            draft.contract,
                            draft.draft,
                        )
                    )
                    if activated.plan != offer.expected_plan:
                        raise TurnPlannerProofRejectedError(
                            "Activated Plan changed from the precompiled Offer"
                        )

                    record = await self._run_record_for_task(
                        session,
                        claim.task_id,
                        for_update=True,
                    )
                    if record is None:
                        raise TurnPlannerProofRejectedError(
                            "Turn Planner reservation disappeared during finalize"
                        )
                    run = self._run_from_record(record)
                    if not self._owns_claim(run, claim):
                        raise _AtomicFinalizeLate
                    now = self._now()
                    expires_at = self._aware(run.claim_expires_at)
                    if expires_at is None or expires_at <= now:
                        raise _AtomicFinalizeExpired

                    terminal = run.evolve(
                        status="succeeded",
                        revision=run.revision + 1,
                        updated_at=now,
                        claim_fencing_token=run.claim_fencing_token,
                        request_dispatched_at=run.request_dispatched_at,
                        completed_at=now,
                        response_manifest=decision.response_manifest,
                    )
                    adjudication = TurnPlannerAdjudication.build(
                        task_id=terminal.task_id,
                        user_message_id=terminal.user_message_id,
                        user_message_digest=terminal.user_message_digest,
                        run_id=terminal.run_id,
                        run_digest=terminal.run_digest,
                        outcome="single_step",
                        selected_offers=(offer.ref,),
                        parameter_bindings=decision.parameter_bindings,
                        proposal_manifest=decision.proposal_manifest,
                        reason_code=decision.reason_code,
                        created_at=now,
                    )
                    message = await self._message_content(run, session=session)
                    parameters = self._canonical_parameters(
                        adjudication=adjudication,
                        offer=offer,
                        draft=draft,
                        message=message,
                    )
                    if not await self._cas_run(session, run, terminal):
                        raise _AtomicFinalizeRetry
                    session.add(self._adjudication_record(adjudication))
                    await session.flush()

                    route = await session.scalar(
                        select(TurnRouteRecord)
                        .where(TurnRouteRecord.task_id == claim.task_id)
                        .with_for_update()
                    )
                    if route is None:
                        raise TurnPlannerProofRejectedError(
                            "Turn Route disappeared before Planner binding"
                        )
                    await self._bind_single_step_in_session(
                        session,
                        run=terminal,
                        adjudication=adjudication,
                        offer=offer,
                        parameters=parameters,
                        plan=activated.plan,
                        route=route,
                        now=now,
                    )
                    completed = await self._load_bundle(session, claim.task_id)
                    if completed is None or completed.binding is None:
                        raise TurnPlannerProofRejectedError(
                            "Atomic single-step Planner proof could not be reloaded"
                        )
                    await self._validate_parameter_sources(session, completed)
                    return completed
            except _AtomicFinalizeLate:
                return await self._bundle_or_recover(claim.task_id)
            except _AtomicFinalizeExpired:
                return await self._settle_failure(
                    claim,
                    code="PLANNER_OUTCOME_UNKNOWN",
                    detail={"reason": "result_arrived_after_lease"},
                    status="outcome_unknown",
                )
            except (_AtomicFinalizeRetry, IntegrityError):
                # This retries only local persistence of the already-received
                # response.  It never invokes the Provider a second time.
                continue
        raise TurnPlannerProofRejectedError(
            "Atomic single-step Planner finalize changed concurrently"
        )

    async def _settle_failure(
        self,
        claim: _DispatchClaim,
        *,
        code: TurnPlannerFailureCode,
        detail: object,
        status: Literal["failed", "outcome_unknown"],
    ) -> _PlanningBundle:
        for _ in range(3):
            async with self._database.session() as session, session.begin():
                record = await self._run_record_for_task(
                    session,
                    claim.task_id,
                    for_update=True,
                )
                if record is None:
                    raise TurnPlannerNotFoundError("Turn Planner reservation disappeared")
                run = self._run_from_record(record)
                if not self._owns_claim(run, claim):
                    break
                now = self._now()
                expires_at = self._aware(run.claim_expires_at)
                terminal_code = code
                terminal_status = status
                fence_increment = 0
                if expires_at is None or expires_at <= now:
                    terminal_code = "PLANNER_OUTCOME_UNKNOWN"
                    terminal_status = "outcome_unknown"
                    detail = {"reason": "failure_arrived_after_lease"}
                    fence_increment = 1
                terminal = self._terminal_run(
                    run,
                    status=terminal_status,
                    code=terminal_code,
                    now=now,
                    detail=detail,
                    fence_increment=fence_increment,
                )
                if not await self._cas_run(session, run, terminal):
                    continue
                await self._persist_fallback_outcome(
                    session,
                    terminal,
                    reason_code=terminal_code,
                )
            return await self._bundle_or_recover(claim.task_id)
        return await self._bundle_or_recover(claim.task_id)

    async def _persist_fallback_outcome(
        self,
        session: AsyncSession,
        run: TurnPlannerRun,
        *,
        reason_code: str,
    ) -> None:
        now = self._now()
        adjudication = TurnPlannerAdjudication.build(
            task_id=run.task_id,
            user_message_id=run.user_message_id,
            user_message_digest=run.user_message_digest,
            run_id=run.run_id,
            run_digest=run.run_digest,
            outcome="deterministic_fallback",
            reason_code=reason_code,
            created_at=now,
        )
        binding = TurnPlanBinding.build(
            task_id=run.task_id,
            user_message_id=run.user_message_id,
            user_message_digest=run.user_message_digest,
            adjudication_id=adjudication.adjudication_id,
            adjudication_digest=adjudication.adjudication_digest,
            status="not_applicable",
            reason_code=reason_code,
            created_at=now,
        )
        session.add(self._adjudication_record(adjudication))
        # The Binding composite FK targets the immutable adjudication digest.
        await session.flush()
        session.add(self._binding_record(binding))

    async def _bundle_or_recover(self, task_id: str) -> _PlanningBundle:
        async with self._database.session() as session:
            bundle = await self._load_bundle(session, task_id)
            if bundle is None:
                raise TurnPlannerNotFoundError("Turn Planner proof is missing")
            if (
                bundle.run.status == "succeeded"
                and bundle.adjudication is not None
                and bundle.adjudication.outcome == "single_step"
                and bundle.binding is None
            ):
                recovered = await self._recover_single_step_binding(task_id)
                return _PlanningBundle(
                    offers=recovered.offers,
                    run=recovered.run,
                    adjudication=recovered.adjudication,
                    binding=recovered.binding,
                )
            if bundle.adjudication is None or bundle.binding is None:
                raise TurnPlannerProofRejectedError(
                    "Terminal Turn Planner proof is incomplete"
                )
            await self._validate_parameter_sources(session, bundle)
            return bundle

    async def _bind_single_step_in_session(
        self,
        session: AsyncSession,
        *,
        run: TurnPlannerRun,
        adjudication: TurnPlannerAdjudication,
        offer: TurnPlanningOffer,
        parameters: dict[str, str],
        plan: ExecutablePlan,
        route: TurnRouteRecord,
        now: datetime,
    ) -> TurnPlanBinding:
        """Persist one trusted Binding and Route rewrite in the caller transaction."""

        if (
            route.user_message_id != run.user_message_id
            or route.candidate_digest != run.fallback_candidate_digest
            or route.parameter_digest != sha256_digest(route.parameters)
            or route.turn_planner_run_id != run.run_id
            or route.turn_planning_reservation_digest != run.reservation_digest
            or route.decision == TurnRouteDecision.ROUTED.value
            or route.route_id is not None
            or route.route_version is not None
            or route.route_manifest_digest is not None
            or route.turn_planning_adjudication_id is not None
            or route.turn_plan_binding_id is not None
            or route.turn_plan_binding_digest is not None
            or route.turn_planning_provenance_digest is not None
            or route.result_manifest is not None
            or route.result_digest is not None
            or route.error_code is not None
        ):
            raise TurnPlannerProofRejectedError(
                "Deterministic fallback Route changed before binding"
            )
        if plan != offer.expected_plan:
            raise TurnPlannerProofRejectedError(
                "Bound Plan changed from the precompiled Offer"
            )
        plan_ref = TurnPlanningPlanRef(
            plan_id=plan.plan_id,
            plan_generation=plan.plan_generation,
            plan_manifest_digest=plan.plan_manifest_digest,
            task_contract=plan.task_contract,
        )
        binding = TurnPlanBinding.build(
            task_id=run.task_id,
            user_message_id=run.user_message_id,
            user_message_digest=run.user_message_digest,
            adjudication_id=adjudication.adjudication_id,
            adjudication_digest=adjudication.adjudication_digest,
            status="bound",
            offer=offer.ref,
            plan=plan_ref,
            reason_code="MODEL_PLANNER_SINGLE_STEP",
            created_at=now,
        )
        session.add(self._binding_record(binding))
        await session.flush()
        bound = self._bound_route_values(
            message_digest=run.user_message_digest,
            offer=offer,
            parameters=parameters,
        )
        if (
            adjudication.proposal_digest is None
            or adjudication.parameter_bindings_digest is None
        ):
            raise TurnPlannerProofRejectedError(
                "Single-step adjudication lacks proposal proof"
            )
        provenance_digest = turn_planning_provenance_digest(
            task_id=run.task_id,
            user_message_id=run.user_message_id,
            user_message_digest=run.user_message_digest,
            deterministic_candidate_digest=run.fallback_candidate_digest,
            planner_run_id=run.run_id,
            planner_run_digest=run.run_digest,
            adjudication_id=adjudication.adjudication_id,
            adjudication_digest=adjudication.adjudication_digest,
            binding_id=binding.binding_id,
            binding_digest=binding.binding_digest,
            offer_id=offer.offer_id,
            offer_digest=offer.offer_digest,
            recipe_digest=offer.trusted_recipe.route_manifest_digest,
            parameter_bindings_digest=adjudication.parameter_bindings_digest,
            contract_digest=offer.task_contract.digest,
            plan_id=plan.plan_id,
            plan_manifest_digest=plan.plan_manifest_digest,
            model_candidate_digest=adjudication.proposal_digest,
            candidate_digest=bound.candidate_digest,
        )
        route.decision = TurnRouteDecision.ROUTED.value
        route.route_id = bound.route_id
        route.route_version = bound.route_version
        route.route_manifest_digest = bound.route_manifest_digest
        route.candidate_digest = bound.candidate_digest
        route.parameters = bound.parameters
        route.parameter_digest = sha256_digest(bound.parameters)
        route.turn_planning_adjudication_id = adjudication.adjudication_id
        route.turn_plan_binding_id = binding.binding_id
        route.turn_plan_binding_digest = binding.binding_digest
        route.turn_planning_provenance_digest = provenance_digest
        route.reason_code = bound.reason_code
        route.status = bound.status.value
        route.result_manifest = None
        route.result_digest = None
        route.error_code = None
        route.revision += 1
        route.updated_at = now
        await session.flush()
        return binding

    async def _recover_single_step_binding(self, task_id: str) -> TurnPlanningRead:
        """Bind a terminal single-step adjudication without creating generation 2."""

        for _ in range(3):
            try:
                async with self._database.session() as session, session.begin():
                    # PlanCompilationService locks Task -> PlanningState.  Keep that
                    # global order before locking Planner proof and the Turn Route.
                    initial = await self._load_bundle(session, task_id)
                    if initial is None:
                        raise TurnPlannerNotFoundError(
                            "Turn Planner proof is missing"
                        )
                    run = initial.run
                    adjudication = initial.adjudication
                    if (
                        run.status != "succeeded"
                        or adjudication is None
                        or adjudication.outcome != "single_step"
                        or len(adjudication.selected_offers) != 1
                    ):
                        if initial.binding is None:
                            raise TurnPlannerProofRejectedError(
                                "Turn Planner outcome cannot be bound"
                            )
                        await self._validate_parameter_sources(session, initial)
                        return initial.read()
                    if initial.binding is not None:
                        await self._validate_parameter_sources(session, initial)
                        return initial.read()
                    offer = next(
                        (
                            item
                            for item in initial.offers
                            if item.ref == adjudication.selected_offers[0]
                        ),
                        None,
                    )
                    if offer is None:
                        raise TurnPlannerProofRejectedError(
                            "Adjudicated Turn Planner Offer is missing"
                        )
                    message = await self._message_content(run, session=session)
                    draft = self._draft_for_offer(offer)
                    parameters = self._canonical_parameters(
                        adjudication=adjudication,
                        offer=offer,
                        draft=draft,
                        message=message,
                    )
                    activated = await self._planning.activate_initial_once_in_session(
                        session,
                        draft.contract,
                        draft.draft,
                    )
                    if activated.plan != offer.expected_plan:
                        raise TurnPlannerProofRejectedError(
                            "Activated Plan changed from the precompiled Offer"
                        )
                    locked_run_record = await self._run_record_for_task(
                        session,
                        task_id,
                        for_update=True,
                    )
                    if locked_run_record is None:
                        raise TurnPlannerProofRejectedError(
                            "Turn Planner Run disappeared during binding"
                        )
                    locked_run = self._run_from_record(locked_run_record)
                    if locked_run != run:
                        raise TurnPlannerConflictError(
                            "Turn Planner Run changed during binding"
                        )
                    adjudication_record = await session.scalar(
                        select(TurnPlannerAdjudicationRecord)
                        .where(
                            TurnPlannerAdjudicationRecord.adjudication_id
                            == adjudication.adjudication_id
                        )
                        .with_for_update()
                    )
                    if adjudication_record is None or (
                        self._adjudication_from_record(adjudication_record)
                        != adjudication
                    ):
                        raise TurnPlannerProofRejectedError(
                            "Turn Planner adjudication changed during binding"
                        )
                    existing_record = await session.scalar(
                        select(TurnPlanBindingRecord)
                        .where(
                            TurnPlanBindingRecord.adjudication_id
                            == adjudication.adjudication_id
                        )
                        .with_for_update()
                    )
                    if existing_record is not None:
                        existing = self._binding_from_record(existing_record)
                        completed = _PlanningBundle(
                            offers=initial.offers,
                            run=run,
                            adjudication=adjudication,
                            binding=existing,
                        )
                        await self._validate_parameter_sources(session, completed)
                        return completed.read()
                    route = await session.scalar(
                        select(TurnRouteRecord)
                        .where(TurnRouteRecord.task_id == task_id)
                        .with_for_update()
                    )
                    if route is None:
                        raise TurnPlannerProofRejectedError(
                            "Turn Route disappeared before Planner binding"
                        )
                    now = self._now()
                    binding = await self._bind_single_step_in_session(
                        session,
                        run=run,
                        adjudication=adjudication,
                        offer=offer,
                        parameters=parameters,
                        plan=activated.plan,
                        route=route,
                        now=now,
                    )
                    completed = _PlanningBundle(
                        offers=initial.offers,
                        run=run,
                        adjudication=adjudication,
                        binding=binding,
                    )
                    await self._validate_parameter_sources(session, completed)
                    return completed.read()
            except IntegrityError:
                # SQLite has no row-level locks.  Exact uniqueness can elect a
                # concurrent binding winner, which is accepted only after a full read.
                result = await self.get(task_id)
                if result is not None:
                    return result
        raise TurnPlannerConflictError(
            "Turn Planner single-step binding changed concurrently"
        )

    async def _load_scope(
        self,
        session: AsyncSession,
        *,
        task_id: str,
        user_message_id: str,
        for_update: bool,
    ) -> _TurnScope:
        task_statement = select(TaskRecord).where(TaskRecord.task_id == task_id)
        message_statement = select(ConversationMessageRecord).where(
            ConversationMessageRecord.message_id == user_message_id
        )
        route_statement = select(TurnRouteRecord).where(
            TurnRouteRecord.task_id == task_id
        )
        if for_update:
            task_statement = task_statement.with_for_update()
            message_statement = message_statement.with_for_update()
            route_statement = route_statement.with_for_update()
        task = await session.scalar(task_statement)
        if task is None:
            raise TurnPlannerNotFoundError("Task does not exist")
        message = await session.scalar(message_statement)
        route = await session.scalar(route_statement)
        if message is None or route is None:
            raise TurnPlannerNotFoundError("Turn message or deterministic Route is missing")
        created_at = self._aware(message.created_at)
        if created_at is None:
            raise TurnPlannerProofRejectedError("Turn message timestamp is missing")
        message_material = {
            "message_id": message.message_id,
            "conversation_id": message.conversation_id,
            "task_id": message.task_id,
            "role": message.role,
            "content": message.content,
            "content_ref": message.content_ref,
            "classification": message.classification,
            "created_at": created_at,
        }
        if (
            message.task_id != task_id
            or task.conversation_id != message.conversation_id
            or route.conversation_id != message.conversation_id
            or route.user_message_id != user_message_id
            or message.role != "user"
            or message.status != "active"
            or message.content is None
            or message.content_ref is not None
            or message.message_digest != sha256_digest(message_material)
            or route.parameter_digest != sha256_digest(route.parameters)
        ):
            raise TurnPlannerProofRejectedError(
                "Persisted Turn scope or message proof changed"
            )
        if task.privacy_mode not in {
            "local_only",
            "local_preferred",
            "balanced",
            "quality_first",
        }:
            raise TurnPlannerProofRejectedError("Task privacy mode is invalid")
        return _TurnScope(
            task=task,
            message=message,
            route=route,
            content=message.content,
            privacy_mode=cast(PrivacyMode, task.privacy_mode),
        )

    @staticmethod
    def _validate_fallback_route(
        record: TurnRouteRecord,
        fallback: TurnRouteRead,
        *,
        reservation: TurnPlannerRun | None = None,
    ) -> None:
        expected_run_id = reservation.run_id if reservation is not None else None
        expected_reservation_digest = (
            reservation.reservation_digest if reservation is not None else None
        )
        if (
            record.task_id != fallback.task_id
            or record.conversation_id != fallback.conversation_id
            or record.user_message_id != fallback.user_message_id
            or record.decision != fallback.decision.value
            or record.route_id != fallback.route_id
            or record.route_version != fallback.route_version
            or record.route_manifest_digest != fallback.route_manifest_digest
            or record.candidate_digest != fallback.candidate_digest
            or record.parameter_digest != fallback.parameter_digest
            or record.resolved_from_task_id != fallback.resolved_from_task_id
            or record.resolution_rule != fallback.resolution_rule
            or record.resolution_digest != fallback.resolution_digest
            or record.turn_planner_run_id != expected_run_id
            or record.turn_planning_reservation_digest
            != expected_reservation_digest
            or record.turn_planning_adjudication_id is not None
            or record.turn_plan_binding_id is not None
            or record.turn_plan_binding_digest is not None
            or record.turn_planning_provenance_digest is not None
            or record.reason_code != fallback.reason_code
            or record.status != fallback.status.value
            or record.result_digest != fallback.result_digest
            or record.error_code != fallback.error_code
            or record.revision != fallback.revision
        ):
            raise TurnPlannerConflictError(
                "Deterministic fallback Route changed before Planner reservation"
            )

    async def _load_bundle(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> _PlanningBundle | None:
        # SQLite's legacy transaction mode does not necessarily BEGIN for a
        # SELECT.  Reading these three mutable rows separately could therefore
        # combine a pre-settlement Run with post-settlement outcome proofs.
        # One JOIN statement makes the lineage snapshot indivisible on every DB.
        lineage_statement = (
            select(
                TurnPlannerRunRecord,
                TurnPlannerAdjudicationRecord,
                TurnPlanBindingRecord,
            )
            .outerjoin(
                TurnPlannerAdjudicationRecord,
                TurnPlannerAdjudicationRecord.run_id == TurnPlannerRunRecord.run_id,
            )
            .outerjoin(
                TurnPlanBindingRecord,
                TurnPlanBindingRecord.adjudication_id
                == TurnPlannerAdjudicationRecord.adjudication_id,
            )
            .where(TurnPlannerRunRecord.task_id == task_id)
            .order_by(TurnPlannerRunRecord.created_at, TurnPlannerRunRecord.run_id)
            .limit(2)
            .execution_options(populate_existing=True)
        )
        lineage = tuple((await session.execute(lineage_statement)).all())
        if len(lineage) > 1:
            raise TurnPlannerProofRejectedError(
                "Task has multiple Turn Planner reservations"
            )
        if not lineage:
            route = await session.get(TurnRouteRecord, task_id)
            if route is not None and (
                route.turn_planner_run_id is not None
                or route.turn_planning_reservation_digest is not None
            ):
                raise TurnPlannerProofRejectedError(
                    "Turn Route reservation anchor lost its Planner Run"
                )
            return None
        run_record, adjudication_record, binding_record = lineage[0]
        run = self._run_from_record(run_record)
        route = await session.get(TurnRouteRecord, task_id)
        if (
            route is None
            or route.user_message_id != run.user_message_id
            or route.turn_planner_run_id != run.run_id
            or route.turn_planning_reservation_digest != run.reservation_digest
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Route reservation anchor changed"
            )
        offer_records = tuple(
            (
                await session.scalars(
                    select(TurnPlanningOfferRecord).where(
                        TurnPlanningOfferRecord.offer_id.in_(
                            tuple(item.offer_id for item in run.offers)
                        )
                    )
                )
            ).all()
        )
        by_id = {
            item.offer_id: self._offer_from_record(item) for item in offer_records
        }
        try:
            offers = tuple(by_id[item.offer_id] for item in run.offers)
        except KeyError as error:
            raise TurnPlannerProofRejectedError(
                "Turn Planner Run references a missing Offer"
            ) from error
        if tuple(item.ref for item in offers) != run.offers:
            raise TurnPlannerProofRejectedError("Turn Planner Offer set changed")
        adjudication = (
            self._adjudication_from_record(adjudication_record)
            if adjudication_record is not None
            else None
        )
        if binding_record is not None and adjudication is None:
            raise TurnPlannerProofRejectedError(
                "Turn Plan Binding lacks its adjudication"
            )
        binding = (
            self._binding_from_record(binding_record)
            if binding_record is not None
            else None
        )
        return _PlanningBundle(
            offers=offers,
            run=run,
            adjudication=adjudication,
            binding=binding,
        )

    async def _run_record_for_task(
        self,
        session: AsyncSession,
        task_id: str,
        *,
        for_update: bool,
    ) -> TurnPlannerRunRecord | None:
        statement = (
            select(TurnPlannerRunRecord)
            .where(TurnPlannerRunRecord.task_id == task_id)
            .order_by(TurnPlannerRunRecord.created_at, TurnPlannerRunRecord.run_id)
            .limit(2)
        )
        if for_update:
            statement = statement.with_for_update()
        records = tuple((await session.scalars(statement)).all())
        if len(records) > 1:
            raise TurnPlannerProofRejectedError(
                "Task has multiple Turn Planner reservations"
            )
        return records[0] if records else None

    async def _message_content(
        self,
        run: TurnPlannerRun,
        *,
        session: AsyncSession | None = None,
    ) -> str:
        if session is None:
            async with self._database.session() as owned_session:
                scope = await self._load_scope(
                    owned_session,
                    task_id=run.task_id,
                    user_message_id=run.user_message_id,
                    for_update=False,
                )
        else:
            scope = await self._load_scope(
                session,
                task_id=run.task_id,
                user_message_id=run.user_message_id,
                for_update=False,
            )
        if scope.message.message_digest != run.user_message_digest:
            raise TurnPlannerProofRejectedError(
                "Turn Planner user message digest changed"
            )
        return scope.content

    async def _validate_parameter_sources(
        self,
        session: AsyncSession,
        bundle: _PlanningBundle,
    ) -> None:
        adjudication = bundle.adjudication
        if adjudication is None or not adjudication.selected_offers:
            return
        message = await self._message_content(bundle.run, session=session)
        offers_by_key = {item.offer_key: item for item in bundle.offers}
        for binding in adjudication.parameter_bindings:
            if (
                binding.source_end > len(message)
                or message[binding.source_start : binding.source_end] != binding.value
            ):
                raise TurnPlannerProofRejectedError(
                    "Turn Planner parameter source changed"
                )
        if adjudication.outcome not in {"single_step", "multi_step_deferred"}:
            return
        for selected in adjudication.selected_offers:
            try:
                offer = offers_by_key[selected.offer_key]
            except KeyError as error:
                raise TurnPlannerProofRejectedError(
                    "Turn Planner selected Offer disappeared"
                ) from error
            self._canonical_parameters(
                adjudication=adjudication,
                offer=offer,
                draft=self._draft_for_offer(offer),
                message=message,
            )

    def _canonical_parameters(
        self,
        *,
        adjudication: TurnPlannerAdjudication,
        offer: TurnPlanningOffer,
        draft: RouteOfferDraft,
        message: str,
    ) -> dict[str, str]:
        proposed = {
            item.parameter_name: item.value
            for item in adjudication.parameter_bindings
            if item.offer_key == offer.offer_key
        }
        try:
            parameters, manifest, binding_digest = RouteRecipeCatalog.bind_parameters(
                draft.route_id,
                message,
                proposed,
                fixed_parameters=draft.fixed_parameters,
            )
        except RouteRecipeError as error:
            raise TurnPlannerProofRejectedError(
                "Persisted Turn Planner parameter proof is no longer valid"
            ) from error
        step = self._proposal_step(adjudication, offer.offer_key)
        if (
            step.get("parameters") != parameters
            or step.get("parameter_binding") != manifest
            or step.get("parameter_binding_digest") != binding_digest
            or step.get("recipe_digest") != draft.recipe_digest
            or step.get("task_contract_digest") != draft.contract.digest
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Planner trusted parameter binding changed"
            )
        return parameters

    @staticmethod
    def _proposal_step(
        adjudication: TurnPlannerAdjudication,
        offer_key: str,
    ) -> dict[str, JsonValue]:
        manifest = adjudication.proposal_manifest
        if manifest is None:
            raise TurnPlannerProofRejectedError(
                "Turn Planner proposal manifest is missing"
            )
        steps = manifest.get("steps")
        if not isinstance(steps, list):
            raise TurnPlannerProofRejectedError(
                "Turn Planner proposal steps are invalid"
            )
        matches = [
            item
            for item in steps
            if isinstance(item, dict) and item.get("offer_key") == offer_key
        ]
        if len(matches) != 1:
            raise TurnPlannerProofRejectedError(
                "Turn Planner proposal Offer proof is ambiguous"
            )
        return matches[0]

    @staticmethod
    def _proposal_parameters(
        manifest: Mapping[str, JsonValue],
        offer_key: str,
    ) -> dict[str, str]:
        steps = manifest.get("steps")
        if not isinstance(steps, list):
            raise TurnPlannerProofRejectedError(
                "Turn Planner proposal steps are invalid"
            )
        matches = [
            item
            for item in steps
            if isinstance(item, dict) and item.get("offer_key") == offer_key
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("parameters"), dict):
            raise TurnPlannerProofRejectedError(
                "Turn Planner proposal parameters are invalid"
            )
        raw = cast(dict[object, object], matches[0]["parameters"])
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw.items()
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Planner proposal parameter type changed"
            )
        return cast(dict[str, str], dict(raw))

    def _draft_for_offer(self, offer: TurnPlanningOffer) -> RouteOfferDraft:
        matches: list[RouteOfferDraft] = []
        for draft in RouteRecipeCatalog.offers_for(
            task_id=offer.task_id,
            capabilities=self._capabilities,
        ):
            expected_plan = self._planning.preview_initial(
                draft.contract,
                draft.draft,
            )
            expected_specs = tuple(
                TurnPlanningParameterSpec(
                    parameter_name=item.name,
                    required=item.required,
                    min_length=1,
                    max_length=4_000,
                )
                for item in draft.parameter_specs
            )
            if (
                draft.recipe_digest == offer.trusted_recipe.route_manifest_digest
                and draft.route_id == offer.trusted_recipe.route_id
                and draft.route_version == offer.trusted_recipe.route_version
                and draft.contract.digest == offer.task_contract.digest
                and expected_plan == offer.expected_plan
                and draft.contract.capabilities == offer.capabilities
                and self._route_budget(draft.contract.budget) == offer.budget
                and expected_specs == offer.parameter_specs
            ):
                matches.append(draft)
        if len(matches) != 1:
            raise TurnPlannerProofRejectedError(
                "Persisted Turn Planner Offer no longer resolves exactly"
            )
        return matches[0]

    @staticmethod
    def _offer_record(offer: TurnPlanningOffer) -> TurnPlanningOfferRecord:
        return TurnPlanningOfferRecord(
            offer_id=offer.offer_id,
            offer_key=offer.offer_key,
            task_id=offer.task_id,
            user_message_id=offer.user_message_id,
            user_message_digest=offer.user_message_digest,
            contract_id=offer.task_contract.contract_id,
            contract_version=offer.task_contract.version,
            contract_digest=offer.task_contract.digest,
            execution_agents_manifest=[
                item.model_dump(mode="json") for item in offer.execution_agents
            ],
            execution_agents_digest=offer.execution_agents_digest,
            expected_plan_manifest=offer.expected_plan.model_dump(mode="json"),
            expected_plan_id=offer.expected_plan.plan_id,
            expected_plan_generation=offer.expected_plan.plan_generation,
            expected_plan_manifest_digest=(
                offer.expected_plan.plan_manifest_digest
            ),
            expected_plan_binding_snapshot_digest=(
                offer.expected_plan.binding_snapshot_digest
            ),
            capabilities_manifest=[
                item.model_dump(mode="json") for item in offer.capabilities
            ],
            capabilities_digest=offer.capabilities_digest,
            provider_id=offer.provider.provider_id,
            provider_model=offer.provider.model,
            provider_snapshot_digest=offer.provider_snapshot_digest,
            recipe_id=offer.trusted_recipe.route_id,
            recipe_version=offer.trusted_recipe.route_version,
            recipe_digest=offer.trusted_recipe.route_manifest_digest,
            budget_manifest=offer.budget.model_dump(mode="json"),
            budget_digest=offer.budget_digest,
            parameter_schema_manifest=[
                item.model_dump(mode="json") for item in offer.parameter_specs
            ],
            parameter_schema_digest=offer.parameter_schema_digest,
            policy_snapshot_digest=offer.policy_snapshot_digest,
            manifest=offer.model_dump(mode="json"),
            offer_digest=offer.offer_digest,
            created_at=offer.created_at,
        )

    @staticmethod
    def _offer_from_record(record: TurnPlanningOfferRecord) -> TurnPlanningOffer:
        try:
            offer = TurnPlanningOffer.model_validate(record.manifest)
        except ValidationError as error:
            raise TurnPlannerProofRejectedError(
                "Turn Planner Offer manifest is invalid"
            ) from error
        if (
            record.offer_id != offer.offer_id
            or record.offer_key != offer.offer_key
            or record.task_id != offer.task_id
            or record.user_message_id != offer.user_message_id
            or record.user_message_digest != offer.user_message_digest
            or record.contract_id != offer.task_contract.contract_id
            or record.contract_version != offer.task_contract.version
            or record.contract_digest != offer.task_contract.digest
            or record.execution_agents_manifest
            != [item.model_dump(mode="json") for item in offer.execution_agents]
            or record.execution_agents_digest != offer.execution_agents_digest
            or record.expected_plan_manifest
            != offer.expected_plan.model_dump(mode="json")
            or record.expected_plan_id != offer.expected_plan.plan_id
            or record.expected_plan_generation
            != offer.expected_plan.plan_generation
            or record.expected_plan_manifest_digest
            != offer.expected_plan.plan_manifest_digest
            or record.expected_plan_binding_snapshot_digest
            != offer.expected_plan.binding_snapshot_digest
            or record.capabilities_manifest
            != [item.model_dump(mode="json") for item in offer.capabilities]
            or record.capabilities_digest != offer.capabilities_digest
            or record.provider_id != offer.provider.provider_id
            or record.provider_model != offer.provider.model
            or record.provider_snapshot_digest != offer.provider_snapshot_digest
            or record.recipe_id != offer.trusted_recipe.route_id
            or record.recipe_version != offer.trusted_recipe.route_version
            or record.recipe_digest != offer.trusted_recipe.route_manifest_digest
            or record.budget_manifest != offer.budget.model_dump(mode="json")
            or record.budget_digest != offer.budget_digest
            or record.parameter_schema_manifest
            != [item.model_dump(mode="json") for item in offer.parameter_specs]
            or record.parameter_schema_digest != offer.parameter_schema_digest
            or record.policy_snapshot_digest != offer.policy_snapshot_digest
            or record.offer_digest != offer.offer_digest
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Planner Offer columns changed"
            )
        return offer

    @staticmethod
    def _run_record(run: TurnPlannerRun) -> TurnPlannerRunRecord:
        return TurnPlannerRunRecord(
            run_id=run.run_id,
            task_id=run.task_id,
            user_message_id=run.user_message_id,
            user_message_digest=run.user_message_digest,
            fallback_candidate_digest=run.fallback_candidate_digest,
            planner_agent_id=run.planner_agent.agent_id,
            planner_agent_version=run.planner_agent.version,
            planner_contract_digest=run.planner_agent.contract_digest,
            planner_prompt_package_digest=run.planner_agent.prompt_package_digest,
            provider_id=run.provider.provider_id,
            provider_model=run.provider.model,
            provider_snapshot_digest=run.provider_snapshot_digest,
            offer_set_digest=run.offer_set_digest,
            request_digest=run.request_digest,
            reservation_digest=run.reservation_digest,
            status=run.status,
            revision=run.revision,
            claim_owner_id=run.claim_owner_id,
            claim_fencing_token=run.claim_fencing_token,
            claim_expires_at=run.claim_expires_at,
            request_dispatched_at=run.request_dispatched_at,
            response_digest=run.response_digest,
            failure_code=run.failure.error_code if run.failure is not None else None,
            failure_digest=run.failure.failure_digest if run.failure is not None else None,
            manifest=run.model_dump(mode="json"),
            run_digest=run.run_digest,
            completed_at=run.completed_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    @staticmethod
    def _run_from_record(record: TurnPlannerRunRecord) -> TurnPlannerRun:
        try:
            run = TurnPlannerRun.model_validate(record.manifest)
        except ValidationError as error:
            raise TurnPlannerProofRejectedError(
                "Turn Planner Run manifest is invalid"
            ) from error
        if (
            record.run_id != run.run_id
            or record.task_id != run.task_id
            or record.user_message_id != run.user_message_id
            or record.user_message_digest != run.user_message_digest
            or record.fallback_candidate_digest != run.fallback_candidate_digest
            or record.planner_agent_id != run.planner_agent.agent_id
            or record.planner_agent_version != run.planner_agent.version
            or record.planner_contract_digest != run.planner_agent.contract_digest
            or record.planner_prompt_package_digest
            != run.planner_agent.prompt_package_digest
            or record.provider_id != run.provider.provider_id
            or record.provider_model != run.provider.model
            or record.provider_snapshot_digest != run.provider_snapshot_digest
            or record.offer_set_digest != run.offer_set_digest
            or record.request_digest != run.request_digest
            or record.reservation_digest != run.reservation_digest
            or record.status != run.status
            or record.revision != run.revision
            or record.claim_owner_id != run.claim_owner_id
            or record.claim_fencing_token != run.claim_fencing_token
            or TurnPlannerRuntime._aware(record.claim_expires_at)
            != run.claim_expires_at
            or TurnPlannerRuntime._aware(record.request_dispatched_at)
            != run.request_dispatched_at
            or record.response_digest != run.response_digest
            or record.failure_code
            != (run.failure.error_code if run.failure is not None else None)
            or record.failure_digest
            != (run.failure.failure_digest if run.failure is not None else None)
            or record.run_digest != run.run_digest
            or TurnPlannerRuntime._aware(record.completed_at) != run.completed_at
            or TurnPlannerRuntime._aware(record.created_at) != run.created_at
            or TurnPlannerRuntime._aware(record.updated_at) != run.updated_at
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Planner Run columns changed"
            )
        return run

    @staticmethod
    def _adjudication_record(
        adjudication: TurnPlannerAdjudication,
    ) -> TurnPlannerAdjudicationRecord:
        return TurnPlannerAdjudicationRecord(
            adjudication_id=adjudication.adjudication_id,
            task_id=adjudication.task_id,
            user_message_id=adjudication.user_message_id,
            user_message_digest=adjudication.user_message_digest,
            run_id=adjudication.run_id,
            run_digest=adjudication.run_digest,
            outcome=adjudication.outcome,
            selected_offer_count=len(adjudication.selected_offers),
            parameter_bindings_manifest=(
                [
                    item.model_dump(mode="json")
                    for item in adjudication.parameter_bindings
                ]
                if adjudication.parameter_bindings_digest is not None
                else None
            ),
            parameter_bindings_digest=adjudication.parameter_bindings_digest,
            proposal_digest=adjudication.proposal_digest,
            reason_code=adjudication.reason_code,
            manifest=adjudication.model_dump(mode="json"),
            adjudication_digest=adjudication.adjudication_digest,
            created_at=adjudication.created_at,
        )

    @staticmethod
    def _adjudication_from_record(
        record: TurnPlannerAdjudicationRecord,
    ) -> TurnPlannerAdjudication:
        try:
            adjudication = TurnPlannerAdjudication.model_validate(record.manifest)
        except ValidationError as error:
            raise TurnPlannerProofRejectedError(
                "Turn Planner adjudication manifest is invalid"
            ) from error
        expected_bindings = (
            [
                item.model_dump(mode="json")
                for item in adjudication.parameter_bindings
            ]
            if adjudication.parameter_bindings_digest is not None
            else None
        )
        if (
            record.adjudication_id != adjudication.adjudication_id
            or record.task_id != adjudication.task_id
            or record.user_message_id != adjudication.user_message_id
            or record.user_message_digest != adjudication.user_message_digest
            or record.run_id != adjudication.run_id
            or record.run_digest != adjudication.run_digest
            or record.outcome != adjudication.outcome
            or record.selected_offer_count != len(adjudication.selected_offers)
            or record.parameter_bindings_manifest != expected_bindings
            or record.parameter_bindings_digest
            != adjudication.parameter_bindings_digest
            or record.proposal_digest != adjudication.proposal_digest
            or record.reason_code != adjudication.reason_code
            or record.adjudication_digest != adjudication.adjudication_digest
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Planner adjudication columns changed"
            )
        return adjudication

    @staticmethod
    def _binding_record(binding: TurnPlanBinding) -> TurnPlanBindingRecord:
        plan = binding.plan
        offer = binding.offer
        return TurnPlanBindingRecord(
            binding_id=binding.binding_id,
            task_id=binding.task_id,
            user_message_id=binding.user_message_id,
            user_message_digest=binding.user_message_digest,
            adjudication_id=binding.adjudication_id,
            adjudication_digest=binding.adjudication_digest,
            status=binding.status,
            offer_id=offer.offer_id if offer is not None else None,
            offer_digest=offer.offer_digest if offer is not None else None,
            plan_id=plan.plan_id if plan is not None else None,
            plan_generation=plan.plan_generation if plan is not None else None,
            plan_manifest_digest=(
                plan.plan_manifest_digest if plan is not None else None
            ),
            contract_id=(
                plan.task_contract.contract_id if plan is not None else None
            ),
            contract_version=(
                plan.task_contract.version if plan is not None else None
            ),
            contract_digest=(
                plan.task_contract.digest if plan is not None else None
            ),
            reason_code=binding.reason_code,
            manifest=binding.model_dump(mode="json"),
            binding_digest=binding.binding_digest,
            created_at=binding.created_at,
        )

    @staticmethod
    def _binding_from_record(record: TurnPlanBindingRecord) -> TurnPlanBinding:
        try:
            binding = TurnPlanBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise TurnPlannerProofRejectedError(
                "Turn Plan Binding manifest is invalid"
            ) from error
        plan = binding.plan
        offer = binding.offer
        if (
            record.binding_id != binding.binding_id
            or record.task_id != binding.task_id
            or record.user_message_id != binding.user_message_id
            or record.user_message_digest != binding.user_message_digest
            or record.adjudication_id != binding.adjudication_id
            or record.adjudication_digest != binding.adjudication_digest
            or record.status != binding.status
            or record.offer_id != (offer.offer_id if offer is not None else None)
            or record.offer_digest
            != (offer.offer_digest if offer is not None else None)
            or record.plan_id != (plan.plan_id if plan is not None else None)
            or record.plan_generation
            != (plan.plan_generation if plan is not None else None)
            or record.plan_manifest_digest
            != (plan.plan_manifest_digest if plan is not None else None)
            or record.contract_id
            != (plan.task_contract.contract_id if plan is not None else None)
            or record.contract_version
            != (plan.task_contract.version if plan is not None else None)
            or record.contract_digest
            != (plan.task_contract.digest if plan is not None else None)
            or record.reason_code != binding.reason_code
            or record.binding_digest != binding.binding_digest
        ):
            raise TurnPlannerProofRejectedError(
                "Turn Plan Binding columns changed"
            )
        return binding

    async def _cas_run(
        self,
        session: AsyncSession,
        previous: TurnPlannerRun,
        target: TurnPlannerRun,
    ) -> bool:
        result = cast(
            CursorResult[object],
            await session.execute(
            update(TurnPlannerRunRecord)
            .where(
                TurnPlannerRunRecord.run_id == previous.run_id,
                TurnPlannerRunRecord.revision == previous.revision,
                TurnPlannerRunRecord.status == previous.status,
                TurnPlannerRunRecord.run_digest == previous.run_digest,
            )
            .values(
                status=target.status,
                revision=target.revision,
                claim_owner_id=target.claim_owner_id,
                claim_fencing_token=target.claim_fencing_token,
                claim_expires_at=target.claim_expires_at,
                request_dispatched_at=target.request_dispatched_at,
                response_digest=target.response_digest,
                failure_code=(
                    target.failure.error_code if target.failure is not None else None
                ),
                failure_digest=(
                    target.failure.failure_digest if target.failure is not None else None
                ),
                manifest=target.model_dump(mode="json"),
                run_digest=target.run_digest,
                completed_at=target.completed_at,
                updated_at=target.updated_at,
            )
            .execution_options(synchronize_session=False)
            ),
        )
        return bool(result.rowcount == 1)

    @staticmethod
    def _bound_agent(registration: AgentRegistration) -> BoundAgentRef:
        return BoundAgentRef(
            agent_id=registration.contract.agent_id,
            version=registration.contract.version,
            contract_digest=registration.contract.digest,
            prompt_package_digest=registration.prompt_package.digest,
        )

    @staticmethod
    def _route_budget(budget: TaskBudget) -> PlanNodeBudget:
        return PlanNodeBudget(
            model_calls=budget.max_model_calls,
            tool_calls=budget.max_tool_calls,
            input_tokens=budget.max_input_tokens,
            output_tokens=budget.max_output_tokens,
            wall_seconds=budget.max_wall_seconds,
            retries=budget.max_retries,
            cost_micros=budget.max_cost_micros,
            handoffs=budget.max_handoffs,
        )

    @staticmethod
    def _owns_claim(run: TurnPlannerRun, claim: _DispatchClaim) -> bool:
        return (
            run.status == "dispatching"
            and run.run_id == claim.run_id
            and run.claim_owner_id == claim.owner_id
            and run.claim_fencing_token == claim.fencing_token
            and run.revision == claim.revision
        )

    @staticmethod
    def _terminal_run(
        run: TurnPlannerRun,
        *,
        status: Literal["failed", "outcome_unknown"],
        code: TurnPlannerFailureCode,
        now: datetime,
        detail: object,
        fence_increment: int,
    ) -> TurnPlannerRun:
        failure = TurnPlannerFailureProof.build(
            error_code=code,
            detail_digest=sha256_digest(
                {
                    "run_id": run.run_id,
                    "revision": run.revision,
                    "detail": detail,
                }
            ),
        )
        return run.evolve(
            status=status,
            revision=run.revision + 1,
            updated_at=now,
            claim_fencing_token=run.claim_fencing_token + fence_increment,
            request_dispatched_at=run.request_dispatched_at,
            completed_at=now,
            failure=failure,
        )

    @staticmethod
    def _gateway_error_detail(error: ModelGatewayError) -> dict[str, JsonValue]:
        return {
            "error_code": error.code,
            "provider_id": error.provider_id,
            "retryable": error.retryable,
            "retry_after_seconds": error.retry_after_seconds,
        }

    @staticmethod
    def _bound_route_values(
        *,
        message_digest: str,
        offer: TurnPlanningOffer,
        parameters: dict[str, str],
    ) -> BoundTurnRoute:
        status = (
            TurnRouteStatus.NEEDS_USER_ACTION
            if offer.trusted_recipe.route_id in _DIRECT_APPROVAL_ROUTES
            else TurnRouteStatus.READY
        )
        reason_code = "MODEL_PLANNER_SINGLE_STEP"
        candidate_digest = sha256_digest(
            {
                "classifier_version": MODEL_PLANNER_CLASSIFIER_VERSION,
                "message_digest": message_digest,
                "decision": TurnRouteDecision.ROUTED.value,
                "route_id": offer.trusted_recipe.route_id,
                "parameters": parameters,
                "reason_code": reason_code,
            }
        )
        return BoundTurnRoute(
            route_id=cast(RouteId, offer.trusted_recipe.route_id),
            route_manifest_digest=offer.trusted_recipe.route_manifest_digest,
            parameters=parameters,
            status=status,
            candidate_digest=candidate_digest,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise TurnPlannerProofRejectedError(
                "Turn Planner clock must be timezone-aware"
            )
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def turn_planning_offer_key(
    *,
    task_id: str,
    user_message_id: str,
    user_message_digest: str,
    variant_key: str,
    task_contract_digest: str,
    execution_agents: tuple[BoundAgentRef, ...],
    expected_plan_id: str,
    expected_plan_manifest_digest: str,
    expected_plan_binding_snapshot_digest: str,
    provider_snapshot_digest: str,
    recipe_digest: str,
    policy_snapshot_digest: str,
) -> str:
    """Derive an opaque, non-authorizing key from an exact server Offer."""

    material = {
        "schema_version": "deskpilot.turn-planning-offer-key.v1",
        "task_id": task_id,
        "user_message_id": user_message_id,
        "user_message_digest": user_message_digest,
        "variant_key": variant_key,
        "task_contract_digest": task_contract_digest,
        "execution_agents": [
            item.model_dump(mode="json") for item in execution_agents
        ],
        "expected_plan_id": expected_plan_id,
        "expected_plan_manifest_digest": expected_plan_manifest_digest,
        "expected_plan_binding_snapshot_digest": (
            expected_plan_binding_snapshot_digest
        ),
        "provider_snapshot_digest": provider_snapshot_digest,
        "recipe_digest": recipe_digest,
        "policy_snapshot_digest": policy_snapshot_digest,
    }
    return f"ofk_{sha256_digest(material)}"


def turn_planning_provenance_digest(
    *,
    task_id: str,
    user_message_id: str,
    user_message_digest: str,
    deterministic_candidate_digest: str,
    planner_run_id: str,
    planner_run_digest: str,
    adjudication_id: str,
    adjudication_digest: str,
    binding_id: str,
    binding_digest: str,
    offer_id: str,
    offer_digest: str,
    recipe_digest: str,
    parameter_bindings_digest: str,
    contract_digest: str,
    plan_id: str,
    plan_manifest_digest: str,
    model_candidate_digest: str,
    candidate_digest: str,
) -> str:
    """Digest the complete deterministic-to-model Route lineage."""

    return sha256_digest(
        {
            "schema_version": "deskpilot.turn-planning-provenance.v1",
            "task_id": task_id,
            "user_message_id": user_message_id,
            "user_message_digest": user_message_digest,
            "deterministic_candidate_digest": deterministic_candidate_digest,
            "planner_run_id": planner_run_id,
            "planner_run_digest": planner_run_digest,
            "adjudication_id": adjudication_id,
            "adjudication_digest": adjudication_digest,
            "binding_id": binding_id,
            "binding_digest": binding_digest,
            "offer_id": offer_id,
            "offer_digest": offer_digest,
            "recipe_digest": recipe_digest,
            "parameter_bindings_digest": parameter_bindings_digest,
            "contract_digest": contract_digest,
            "plan_id": plan_id,
            "plan_manifest_digest": plan_manifest_digest,
            "model_candidate_digest": model_candidate_digest,
            "candidate_digest": candidate_digest,
        }
    )
