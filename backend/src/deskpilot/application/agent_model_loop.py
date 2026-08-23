"""Small reusable durable boundary for one strict Agent decision per Model Turn."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_execution_runtime import (
    AgentExecutionRuntime,
    AgentRuntimeConflictError,
    AgentRuntimeNotFoundError,
)
from deskpilot.application.agent_model_requests import bind_agent_model_request
from deskpilot.application.agent_registry import AgentRegistry, AgentRegistryError
from deskpilot.application.context_memory_runtime import ContextMemoryError, ContextMemoryRuntime
from deskpilot.application.model_gateway import ModelGateway, ModelGatewayError
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import (
    ClaimedInvocation,
    ExecutionNodeStatus,
    ExecutionRunStatus,
    InvocationExecutionStatus,
    ModelTurnStatus,
)
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentObservationRecord,
    ModelDispatchAttemptRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    utc_now,
)

DecisionT = TypeVar("DecisionT", bound=BaseModel)
DecisionReducer = Callable[
    [AsyncSession, AgentDecisionRecord, AgentModelTurnRecord, datetime], Awaitable[None]
]


class AgentModelLoopError(RuntimeError):
    code = "AGENT_MODEL_LOOP_ERROR"


class AgentModelLoopRouteRejectedError(AgentModelLoopError):
    code = "AGENT_MODEL_ROUTE_REJECTED"


class AgentModelLoopOutcomeUnknownError(AgentModelLoopError):
    code = "AGENT_MODEL_OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class DispatchedAgentDecision:
    decision: BaseModel
    response: ModelResponse
    turn_id: str
    dispatch_attempt_id: str
    request_digest: str


@dataclass(frozen=True)
class _LockedClaimState:
    run: TaskExecutionRunRecord
    invocation: AgentInvocationRecord
    node: TaskExecutionNodeRecord


class AgentModelLoopRuntime:
    def __init__(
        self,
        database: Database,
        execution: AgentExecutionRuntime,
        agents: AgentRegistry,
        gateway: ModelGateway,
        context_memory: ContextMemoryRuntime,
    ) -> None:
        self._database = database
        self._execution = execution
        self._agents = agents
        self._gateway = gateway
        self._context_memory = context_memory

    def response_cost_micros(self, response: ModelResponse) -> int:
        pricing = self._gateway.policy.pricing_for(response.provider_id)
        return pricing.cost_micros(response.usage) if pricing is not None else 0

    async def dispatch(
        self,
        claimed: ClaimedInvocation,
        *,
        turn_no: int,
        request: ModelRequest,
        decision_model: type[DecisionT],
    ) -> DispatchedAgentDecision:
        try:
            registration = self._agents.resolve_exact(
                claimed.handoff.target_agent.agent_id,
                claimed.handoff.target_agent.version,
                contract_digest=claimed.handoff.target_agent.contract_digest,
                prompt_package_digest=(
                    claimed.handoff.target_agent.prompt_package_digest
                ),
            )
        except AgentRegistryError as error:
            raise AgentModelLoopRouteRejectedError(
                "The bound Agent Contract or Prompt Package is unavailable"
            ) from error
        request = bind_agent_model_request(
            request,
            agent_id=registration.contract.agent_id,
            agent_version=registration.contract.version,
            contract_digest=registration.contract.digest,
            prompt_package_digest=registration.prompt_package.digest,
            prompt_instruction=registration.prompt_package.instruction,
        )
        self._validate_request_budget(claimed, request)
        try:
            provider = self._gateway.select_provider(request)
            provider_descriptor = provider.descriptor
            self._agents.validate_model_route(
                claimed.handoff.target_agent.agent_id,
                claimed.handoff.target_agent.version,
                contract_digest=claimed.handoff.target_agent.contract_digest,
                prompt_package_digest=(
                    claimed.handoff.target_agent.prompt_package_digest
                ),
                request=request,
                provider=provider_descriptor,
            )
        except (AgentRegistryError, ModelGatewayError) as error:
            raise AgentModelLoopRouteRejectedError(
                "No model route satisfies the Agent decision contract"
            ) from error
        request = request.model_copy(update={"provider_hint": provider_descriptor.provider_id})
        turn_identity = {
            "invocation_id": claimed.invocation.invocation_id,
            "turn_no": turn_no,
        }
        turn_id = f"amt_{sha256_digest(turn_identity)}"
        dispatch_attempt_id = f"mdp_{sha256_digest({'turn_id': turn_id, 'attempt_no': 1})}"
        await self._prepare(
            claimed,
            turn_id,
            dispatch_attempt_id,
            turn_no,
            sha256_digest(request),
            provider_descriptor.provider_id,
            provider_descriptor.model,
        )
        try:
            _, request = await self._context_memory.build_for_turn(
                claimed,
                turn_id,
                provider_descriptor.location,
                provider_descriptor.provider_id,
                request,
            )
        except ContextMemoryError as error:
            await self.fail(claimed, turn_id, error.code, sha256_digest({"error": error.code}))
            raise AgentModelLoopRouteRejectedError(
                "Context selection or provider egress was rejected"
            ) from error
        try:
            dispatch_provider = self._gateway.select_provider(request)
            if dispatch_provider.descriptor != provider_descriptor:
                raise AgentModelLoopRouteRejectedError(
                    "Selected Agent Provider snapshot changed before dispatch"
                )
            self._agents.validate_model_route(
                claimed.handoff.target_agent.agent_id,
                claimed.handoff.target_agent.version,
                contract_digest=claimed.handoff.target_agent.contract_digest,
                prompt_package_digest=(
                    claimed.handoff.target_agent.prompt_package_digest
                ),
                request=request,
                provider=provider_descriptor,
            )
            self._validate_request_budget(claimed, request)
        except (
            AgentModelLoopRouteRejectedError,
            AgentRegistryError,
            ModelGatewayError,
        ) as error:
            await self.fail(
                claimed,
                turn_id,
                AgentModelLoopRouteRejectedError.code,
                sha256_digest({"error": AgentModelLoopRouteRejectedError.code}),
            )
            raise AgentModelLoopRouteRejectedError(
                "Context-expanded request violates the Agent decision contract"
            ) from error
        request_digest = sha256_digest(request)
        await self._mark_dispatching(claimed, turn_id, dispatch_attempt_id, request_digest)
        try:
            decision, response = await self._gateway.complete_structured(request, decision_model)
        except (ModelGatewayError, ValidationError, ValueError) as error:
            await self._mark_unknown(claimed, turn_id, type(error).__name__)
            raise AgentModelLoopOutcomeUnknownError(
                "Dispatched Agent Model Turn has an unknown usable outcome"
            ) from error
        return DispatchedAgentDecision(
            decision=decision,
            response=response,
            turn_id=turn_id,
            dispatch_attempt_id=dispatch_attempt_id,
            request_digest=request_digest,
        )

    @staticmethod
    def _validate_request_budget(
        claimed: ClaimedInvocation,
        request: ModelRequest,
    ) -> None:
        allocation = claimed.handoff.budget_allocation
        execution = request.execution_budget
        if (
            request.max_output_tokens > allocation.output_tokens
            or request.timeout_seconds > allocation.wall_seconds
            or execution.max_attempts is None
            or execution.max_attempts > allocation.retries + 1
            or execution.max_task_cost_micros is None
            or execution.max_task_cost_micros > allocation.cost_micros
        ):
            raise AgentModelLoopRouteRejectedError(
                "Model request exceeds the bound Agent node budget"
            )

    async def accept(
        self,
        claimed: ClaimedInvocation,
        dispatched: DispatchedAgentDecision,
        decision: BaseModel,
        *,
        binding_id: str | None = None,
        reducer: DecisionReducer | None = None,
    ) -> str:
        response_digest = sha256_digest(dispatched.response)
        cost_micros = self.response_cost_micros(dispatched.response)
        async with self._database.session() as session, session.begin():
            claim = await self._locked_active_claim(session, claimed)
            turn, attempt = await self._locked_turn_attempt(
                session,
                claim,
                claimed,
                dispatched.turn_id,
                dispatch_attempt_id=dispatched.dispatch_attempt_id,
            )
            if turn.status != ModelTurnStatus.DISPATCHING.value or attempt.status != "dispatching":
                raise AgentRuntimeConflictError("Agent Model Turn is not accepting a decision")
            if (
                turn.request_digest != dispatched.request_digest
                or attempt.request_digest != dispatched.request_digest
                or turn.provider_id != dispatched.response.provider_id
                or attempt.provider_id != dispatched.response.provider_id
                or turn.model != dispatched.response.model
                or attempt.model != dispatched.response.model
            ):
                raise AgentRuntimeConflictError("Agent Model Turn response binding changed")
            now = utc_now()
            manifest = decision.model_dump(mode="json")
            decision_id = f"agd_{sha256_digest({'turn_id': turn.turn_id})}"
            material = {
                "turn_id": turn.turn_id,
                "invocation_id": claimed.invocation.invocation_id,
                "decision": manifest,
                "response_digest": response_digest,
            }
            record = AgentDecisionRecord(
                decision_id=decision_id,
                turn_id=turn.turn_id,
                invocation_id=claimed.invocation.invocation_id,
                kind=str(manifest["kind"]),
                binding_id=binding_id,
                manifest=manifest,
                decision_digest=sha256_digest(material),
                created_at=now,
            )
            session.add(record)
            self._settle(turn, attempt, dispatched.response, response_digest, cost_micros, now)
            if reducer is not None:
                await reducer(session, record, turn, now)
            return record.decision_id

    async def observe(
        self,
        claimed: ClaimedInvocation,
        *,
        decision_id: str,
        binding_id: str,
        result_ref: str,
        projection: dict[str, object],
    ) -> str:
        material = {
            "observation_id": f"obs_{sha256_digest({'decision_id': decision_id})}",
            "invocation_id": claimed.invocation.invocation_id,
            "decision_id": decision_id,
            "source_kind": "route",
            "binding_id": binding_id,
            "status": "succeeded",
            "result_ref": result_ref,
            "projection": projection,
        }
        digest = sha256_digest(material)
        async with self._database.session() as session, session.begin():
            claim = await self._locked_active_claim(session, claimed)
            turn_id = await session.scalar(
                select(AgentDecisionRecord.turn_id).where(
                    AgentDecisionRecord.decision_id == decision_id
                )
            )
            if turn_id is None:
                raise AgentRuntimeConflictError("Bound Route decision is missing")
            turn, attempt = await self._locked_turn_attempt(
                session,
                claim,
                claimed,
                turn_id,
            )
            decision = await session.scalar(
                select(AgentDecisionRecord)
                .where(AgentDecisionRecord.decision_id == decision_id)
                .with_for_update()
            )
            if (
                decision is None
                or decision.invocation_id != claimed.invocation.invocation_id
                or decision.binding_id != binding_id
                or decision.turn_id != turn.turn_id
                or turn.status != ModelTurnStatus.SUCCEEDED.value
                or attempt.status != "succeeded"
            ):
                raise AgentRuntimeConflictError("Bound Route decision is missing")
            session.add(
                AgentObservationRecord(
                    **material,
                    observation_digest=digest,
                    created_at=utc_now(),
                )
            )
        return digest

    async def fail(
        self,
        claimed: ClaimedInvocation,
        turn_id: str,
        error_code: str,
        response_digest: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            claim = await self._locked_active_claim(session, claimed)
            turn, attempt = await self._locked_turn_attempt(
                session,
                claim,
                claimed,
                turn_id,
            )
            if (turn.status, attempt.status) not in {
                (ModelTurnStatus.PREPARED.value, "prepared"),
                (ModelTurnStatus.DISPATCHING.value, "dispatching"),
            }:
                raise AgentRuntimeConflictError("Agent Model Turn is not accepting a failure")
            now = utc_now()
            turn.status = ModelTurnStatus.FAILED.value
            turn.response_digest = response_digest
            turn.stable_error_code = error_code
            turn.updated_at = now
            attempt.status = "failed"
            attempt.response_digest = response_digest
            attempt.stable_error_code = error_code
            attempt.updated_at = now

    async def _prepare(
        self,
        claimed: ClaimedInvocation,
        turn_id: str,
        dispatch_attempt_id: str,
        turn_no: int,
        request_digest: str,
        provider_id: str,
        model: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            claim = await self._locked_active_claim(session, claimed)
            now = utc_now()
            session.add(
                AgentModelTurnRecord(
                    turn_id=turn_id,
                    invocation_id=claim.invocation.invocation_id,
                    turn_no=turn_no,
                    status=ModelTurnStatus.PREPARED.value,
                    request_digest=request_digest,
                    provider_id=provider_id,
                    model=model,
                    input_tokens=0,
                    output_tokens=0,
                    cost_micros=0,
                    claim_owner_id=claimed.claim_owner_id,
                    claim_fencing_token=claimed.claim_fencing_token,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ModelDispatchAttemptRecord(
                    dispatch_attempt_id=dispatch_attempt_id,
                    turn_id=turn_id,
                    attempt_no=1,
                    status="prepared",
                    provider_id=provider_id,
                    model=model,
                    request_digest=request_digest,
                    input_tokens=0,
                    output_tokens=0,
                    cost_micros=0,
                    claim_owner_id=claimed.claim_owner_id,
                    claim_fencing_token=claimed.claim_fencing_token,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def _mark_dispatching(
        self,
        claimed: ClaimedInvocation,
        turn_id: str,
        dispatch_attempt_id: str,
        request_digest: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            claim = await self._locked_active_claim(session, claimed)
            turn, attempt = await self._locked_turn_attempt(
                session,
                claim,
                claimed,
                turn_id,
                dispatch_attempt_id=dispatch_attempt_id,
            )
            if turn.status != ModelTurnStatus.PREPARED.value or attempt.status != "prepared":
                raise AgentRuntimeConflictError("Agent Model Turn is not dispatchable")
            now = utc_now()
            turn.status = ModelTurnStatus.DISPATCHING.value
            turn.request_digest = request_digest
            turn.updated_at = now
            attempt.status = "dispatching"
            attempt.request_digest = request_digest
            attempt.updated_at = now

    async def _mark_unknown(
        self, claimed: ClaimedInvocation, turn_id: str, error_code: str
    ) -> None:
        async with self._database.session() as session, session.begin():
            claim = await self._locked_active_claim(session, claimed)
            turn, attempt = await self._locked_turn_attempt(
                session,
                claim,
                claimed,
                turn_id,
            )
            if turn.status != ModelTurnStatus.DISPATCHING.value or attempt.status != "dispatching":
                raise AgentRuntimeConflictError("Agent Model Turn outcome is not pending")
            now = utc_now()
            turn.status = ModelTurnStatus.OUTCOME_UNKNOWN.value
            turn.stable_error_code = error_code
            turn.updated_at = now
            attempt.status = "outcome_unknown"
            attempt.stable_error_code = error_code
            attempt.updated_at = now

    async def _locked_active_claim(
        self,
        session: AsyncSession,
        claimed: ClaimedInvocation,
    ) -> _LockedClaimState:
        """Lock and revalidate one exact live claim in the global runtime order."""

        run = await session.scalar(
            select(TaskExecutionRunRecord)
            .where(TaskExecutionRunRecord.run_id == claimed.handoff.run_id)
            .with_for_update()
        )
        node = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == claimed.invocation.node_id)
            .with_for_update()
        )
        invocation = await session.scalar(
            select(AgentInvocationRecord)
            .where(
                AgentInvocationRecord.invocation_id == claimed.invocation.invocation_id
            )
            .with_for_update()
        )
        if run is None or invocation is None or node is None:
            raise AgentRuntimeNotFoundError("Agent Model Loop claim state is missing")
        if (
            invocation.run_id != run.run_id
            or invocation.node_id != node.node_id
            or node.run_id != run.run_id
            or run.task_id != claimed.handoff.task_id
            or node.node_id != claimed.handoff.target_node_id
            or invocation.handoff_id != claimed.handoff.handoff_id
            or invocation.attempt != claimed.invocation.attempt
            or invocation.agent_id != claimed.handoff.target_agent.agent_id
            or invocation.agent_version != claimed.handoff.target_agent.version
            or invocation.agent_contract_digest
            != claimed.handoff.target_agent.contract_digest
            or invocation.prompt_package_digest
            != claimed.handoff.target_agent.prompt_package_digest
        ):
            raise AgentRuntimeConflictError("Agent Model Loop claim binding changed")
        AgentExecutionRuntime._assert_lease(
            node,
            claimed.claim_owner_id,
            claimed.claim_fencing_token,
        )
        active_pair = (
            invocation.execution_status,
            node.status,
        ) in {
            (
                InvocationExecutionStatus.CREATED.value,
                ExecutionNodeStatus.CLAIMED.value,
            ),
            (
                InvocationExecutionStatus.RUNNING.value,
                ExecutionNodeStatus.RUNNING.value,
            ),
        }
        if run.status != ExecutionRunStatus.ACTIVE.value or not active_pair:
            raise AgentRuntimeConflictError("Agent Model Loop claim is no longer active")
        return _LockedClaimState(run=run, invocation=invocation, node=node)

    @staticmethod
    async def _locked_turn_attempt(
        session: AsyncSession,
        claim: _LockedClaimState,
        claimed: ClaimedInvocation,
        turn_id: str,
        *,
        dispatch_attempt_id: str | None = None,
    ) -> tuple[AgentModelTurnRecord, ModelDispatchAttemptRecord]:
        turn = await session.scalar(
            select(AgentModelTurnRecord)
            .where(AgentModelTurnRecord.turn_id == turn_id)
            .with_for_update()
        )
        attempt_statement = select(ModelDispatchAttemptRecord).where(
            ModelDispatchAttemptRecord.turn_id == turn_id
        )
        if dispatch_attempt_id is not None:
            attempt_statement = attempt_statement.where(
                ModelDispatchAttemptRecord.dispatch_attempt_id == dispatch_attempt_id
            )
        attempt = await session.scalar(attempt_statement.with_for_update())
        if turn is None or attempt is None:
            raise AgentRuntimeNotFoundError("Agent Model Turn persistence is missing")
        if (
            turn.invocation_id != claim.invocation.invocation_id
            or attempt.turn_id != turn.turn_id
            or turn.claim_owner_id != claimed.claim_owner_id
            or turn.claim_fencing_token != claimed.claim_fencing_token
            or attempt.claim_owner_id != claimed.claim_owner_id
            or attempt.claim_fencing_token != claimed.claim_fencing_token
        ):
            raise AgentRuntimeConflictError("Agent Model Turn claim binding changed")
        return turn, attempt

    @staticmethod
    def _settle(
        turn: AgentModelTurnRecord,
        attempt: ModelDispatchAttemptRecord,
        response: ModelResponse,
        response_digest: str,
        cost_micros: int,
        now: datetime,
    ) -> None:
        turn.status = ModelTurnStatus.SUCCEEDED.value
        turn.response_digest = response_digest
        turn.provider_id = response.provider_id
        turn.model = response.model
        turn.input_tokens = response.usage.input_tokens
        turn.output_tokens = response.usage.output_tokens
        turn.cost_micros = cost_micros
        turn.updated_at = now
        attempt.status = "succeeded"
        attempt.response_digest = response_digest
        attempt.input_tokens = response.usage.input_tokens
        attempt.output_tokens = response.usage.output_tokens
        attempt.cost_micros = cost_micros
        attempt.updated_at = now
