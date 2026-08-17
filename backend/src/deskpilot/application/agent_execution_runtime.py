"""Persistent ready/claim/lease runtime; results stop at verification boundary."""

from datetime import UTC, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_registry import AgentRegistry
from deskpilot.application.plan_compiler import PlanCompiler, PlanCompilerError
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.agent_runtime import (
    AgentInvocationRead,
    AgentOutputResult,
    AgentResult,
    ClaimedInvocation,
    ExecutionNodeRead,
    ExecutionNodeStatus,
    ExecutionRunPage,
    ExecutionRunRead,
    ExecutionRunStatus,
    HandoffEnvelope,
    InvocationExecutionStatus,
    InvocationVerificationStatus,
    ModelTurnStatus,
)
from deskpilot.domain.task_plans import ExecutablePlan, TaskContract
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentHandoffRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentResultRecord,
    TaskContractVersionRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskPlanGenerationRecord,
    utc_now,
)


class AgentRuntimeError(RuntimeError):
    code = "AGENT_RUNTIME_ERROR"


class AgentRuntimeNotFoundError(AgentRuntimeError):
    code = "AGENT_RUNTIME_NOT_FOUND"


class AgentRuntimeDisabledError(AgentRuntimeError):
    code = "AGENT_RUNTIME_DISABLED"


class AgentRuntimeConflictError(AgentRuntimeError):
    code = "AGENT_RUNTIME_CONFLICT"


class AgentLeaseRejectedError(AgentRuntimeError):
    code = "AGENT_LEASE_REJECTED"


class AgentRuntimeProofRejectedError(AgentRuntimeError):
    code = "AGENT_RUNTIME_PROOF_REJECTED"


class AgentExecutionRuntime:
    def __init__(
        self,
        database: Database,
        compiler: PlanCompiler,
        agents: AgentRegistry,
        *,
        max_parallel: int = 3,
    ) -> None:
        self._database = database
        self._compiler = compiler
        self._agents = agents
        self._max_parallel = max_parallel

    async def start(self, task_id: str) -> ExecutionRunRead:
        try:
            async with self._database.session() as session, session.begin():
                record = await session.scalar(
                    select(TaskPlanGenerationRecord)
                    .where(
                        TaskPlanGenerationRecord.task_id == task_id,
                        TaskPlanGenerationRecord.status == "active",
                    )
                    .with_for_update()
                )
                if record is None:
                    raise AgentRuntimeNotFoundError("Task has no active Executable Plan")
                plan = self._validated_plan(record)
                runnable = [
                    node
                    for node in plan.nodes
                    if node.bound_agent is not None and node.runtime_enabled
                ]
                if not runnable:
                    raise AgentRuntimeDisabledError("Active plan has no enabled Agent node")
                run_identity = {
                    "task_id": task_id,
                    "generation": record.generation,
                    "plan_digest": plan.plan_manifest_digest,
                }
                run_id = f"run_{sha256_digest(run_identity)}"
                existing = await session.get(TaskExecutionRunRecord, run_id)
                if existing is not None:
                    return await self._read_run(session, existing)
                now = utc_now()
                run = TaskExecutionRunRecord(
                    run_id=run_id,
                    task_id=task_id,
                    plan_generation=record.generation,
                    plan_digest=plan.plan_manifest_digest,
                    status=ExecutionRunStatus.ACTIVE.value,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                await session.flush()
                for node in plan.nodes:
                    is_ready = (
                        not node.depends_on
                        and node.bound_agent is not None
                        and node.runtime_enabled
                    )
                    session.add(
                        TaskExecutionNodeRecord(
                            node_id=node.node_id,
                            run_id=run_id,
                            local_key=node.local_key,
                            node_kind=node.kind.value,
                            node_spec_digest=node.node_spec_digest,
                            depends_on=list(node.depends_on),
                            bound_agent=(
                                node.bound_agent.model_dump(mode="json")
                                if node.bound_agent is not None
                                else None
                            ),
                            capability=(
                                node.capability.model_dump(mode="json")
                                if node.capability is not None
                                else None
                            ),
                            acceptance_refs=list(node.acceptance_refs),
                            budget=node.budget.model_dump(mode="json"),
                            runtime_enabled=node.runtime_enabled,
                            status=(
                                ExecutionNodeStatus.READY.value
                                if is_ready
                                else ExecutionNodeStatus.PENDING.value
                            ),
                            revision=1,
                            attempt_count=0,
                            claim_fencing_token=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                await session.flush()
                for node in plan.nodes:
                    for source in node.depends_on:
                        session.add(
                            TaskExecutionEdgeRecord(
                                run_id=run_id,
                                from_node_id=source,
                                to_node_id=node.node_id,
                                requirement="verified",
                            )
                        )
                await session.flush()
                return await self._read_run(session, run)
        except IntegrityError as error:
            raise AgentRuntimeConflictError("Execution run changed concurrently") from error

    async def claim_next(
        self, run_id: str, owner_id: str, *, lease_seconds: int = 60
    ) -> ClaimedInvocation | None:
        if not 5 <= lease_seconds <= 600:
            raise ValueError("Lease must be between 5 and 600 seconds")
        async with self._database.session() as session, session.begin():
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == run_id)
                .with_for_update()
            )
            if run is None:
                raise AgentRuntimeNotFoundError("Execution run does not exist")
            if run.status != ExecutionRunStatus.ACTIVE.value:
                return None
            await self._reap_expired(session, run)
            active_count = await session.scalar(
                select(func.count())
                .select_from(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.run_id == run_id,
                    TaskExecutionNodeRecord.status.in_(("claimed", "running")),
                )
            )
            if int(active_count or 0) >= self._max_parallel:
                return None
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.run_id == run_id,
                    TaskExecutionNodeRecord.status == ExecutionNodeStatus.READY.value,
                    TaskExecutionNodeRecord.runtime_enabled.is_(True),
                    TaskExecutionNodeRecord.bound_agent.is_not(None),
                )
                .order_by(TaskExecutionNodeRecord.local_key)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if node is None:
                return None
            claimed_at = utc_now()
            node.status = ExecutionNodeStatus.CLAIMED.value
            node.attempt_count += 1
            node.claim_fencing_token += 1
            node.claim_owner_id = owner_id
            node.claim_acquired_at = claimed_at
            node.claim_heartbeat_at = claimed_at
            node.claim_expires_at = claimed_at + timedelta(seconds=lease_seconds)
            node.revision += 1
            node.updated_at = claimed_at
            handoff = await self._create_handoff(session, run, node)
            invocation_identity = {
                "run_id": run_id,
                "node_id": node.node_id,
                "attempt": node.attempt_count,
            }
            invocation_id = f"inv_{sha256_digest(invocation_identity)}"
            invocation = AgentInvocationRecord(
                invocation_id=invocation_id,
                run_id=run_id,
                node_id=node.node_id,
                attempt=node.attempt_count,
                handoff_id=handoff.handoff_id,
                agent_id=handoff.target_agent.agent_id,
                agent_version=handoff.target_agent.version,
                agent_contract_digest=handoff.target_agent.contract_digest,
                prompt_package_digest=handoff.target_agent.prompt_package_digest,
                execution_status=InvocationExecutionStatus.CREATED.value,
                verification_status=InvocationVerificationStatus.NOT_REQUESTED.value,
                revision=1,
                created_at=claimed_at,
            )
            session.add(invocation)
            await session.flush()
            return ClaimedInvocation(
                handoff=handoff,
                invocation=self._invocation_read(invocation),
                claim_owner_id=owner_id,
                claim_fencing_token=node.claim_fencing_token,
                claim_expires_at=node.claim_expires_at,
            )

    async def start_invocation(
        self, invocation_id: str, owner_id: str, fencing_token: int
    ) -> AgentInvocationRead:
        async with self._database.session() as session, session.begin():
            invocation, node = await self._locked_invocation(session, invocation_id)
            self._assert_lease(node, owner_id, fencing_token)
            if invocation.execution_status != InvocationExecutionStatus.CREATED.value:
                raise AgentRuntimeConflictError("Invocation is not claimable")
            now = utc_now()
            invocation.execution_status = InvocationExecutionStatus.RUNNING.value
            invocation.started_at = now
            invocation.revision += 1
            node.status = ExecutionNodeStatus.RUNNING.value
            node.revision += 1
            node.updated_at = now
            await session.flush()
            return self._invocation_read(invocation)

    async def submit_result(
        self,
        result: AgentResult | AgentOutputResult,
        *,
        owner_id: str,
        fencing_token: int,
    ) -> AgentInvocationRead:
        async with self._database.session() as session, session.begin():
            invocation, node = await self._locked_invocation(session, result.invocation_id)
            self._assert_lease(node, owner_id, fencing_token)
            if invocation.execution_status != InvocationExecutionStatus.RUNNING.value:
                raise AgentRuntimeConflictError("Invocation is not accepting a result")
            session.add(
                AgentResultRecord(
                    result_id=result.result_id,
                    invocation_id=result.invocation_id,
                    manifest=result.model_dump(mode="json"),
                    result_digest=result.result_digest,
                    created_at=utc_now(),
                )
            )
            now = utc_now()
            invocation.result_id = result.result_id
            invocation.execution_status = InvocationExecutionStatus.RESULT_SUBMITTED.value
            invocation.verification_status = InvocationVerificationStatus.PENDING.value
            invocation.finished_at = now
            invocation.revision += 1
            node.status = ExecutionNodeStatus.AWAITING_VERIFICATION.value
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
            run = await session.get(TaskExecutionRunRecord, invocation.run_id)
            if run is None:
                raise AgentRuntimeProofRejectedError("Invocation run is missing")
            run.status = ExecutionRunStatus.AWAITING_VERIFICATION.value
            run.revision += 1
            run.updated_at = now
            await session.flush()
            return self._invocation_read(invocation)

    async def get(self, run_id: str) -> ExecutionRunRead:
        async with self._database.session() as session:
            run = await session.get(TaskExecutionRunRecord, run_id)
            if run is None:
                raise AgentRuntimeNotFoundError("Execution run does not exist")
            return await self._read_run(session, run)

    async def list_for_task(self, task_id: str) -> ExecutionRunPage:
        async with self._database.session() as session:
            runs = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionRunRecord)
                        .where(TaskExecutionRunRecord.task_id == task_id)
                        .order_by(TaskExecutionRunRecord.created_at)
                    )
                ).all()
            )
            return ExecutionRunPage(
                runs=tuple([await self._read_run(session, item) for item in runs])
            )

    def _validated_plan(self, record: TaskPlanGenerationRecord) -> ExecutablePlan:
        try:
            plan = ExecutablePlan.model_validate(record.manifest)
            if plan.plan_manifest_digest != record.plan_manifest_digest:
                raise ValueError
            self._compiler.validate_manifest(plan)
            return plan
        except (ValidationError, ValueError, PlanCompilerError) as error:
            raise AgentRuntimeProofRejectedError("Executable Plan proof was rejected") from error

    async def _create_handoff(
        self,
        session: AsyncSession,
        run: TaskExecutionRunRecord,
        node: TaskExecutionNodeRecord,
    ) -> HandoffEnvelope:
        if node.bound_agent is None:
            raise AgentRuntimeProofRejectedError("Agent node has no bound Agent")
        bound = BoundAgentRef.model_validate(node.bound_agent)
        registration = self._agents.resolve_exact(
            bound.agent_id,
            bound.version,
            contract_digest=bound.contract_digest,
            prompt_package_digest=bound.prompt_package_digest,
        )
        plan_record = await session.get(
            TaskPlanGenerationRecord, (run.task_id, run.plan_generation)
        )
        if plan_record is None:
            raise AgentRuntimeProofRejectedError("Execution plan is missing")
        contract_record = await session.get(
            TaskContractVersionRecord,
            (run.task_id, plan_record.contract_version),
        )
        if contract_record is None:
            raise AgentRuntimeProofRejectedError("Task Contract is missing")
        contract = TaskContract.model_validate(contract_record.manifest)
        handoff_identity = {
            "run_id": run.run_id,
            "node_id": node.node_id,
            "attempt": node.attempt_count,
        }
        handoff_id = f"hnd_{sha256_digest(handoff_identity)}"
        material = {
            "schema_version": "deskpilot.handoff.v1",
            "handoff_id": handoff_id,
            "task_id": run.task_id,
            "run_id": run.run_id,
            "target_node_id": node.node_id,
            "target_agent": bound,
            "objective_ref": f"plan://{plan_record.plan_id}/nodes/{node.node_id}/objective",
            "acceptance_criteria": tuple(node.acceptance_refs),
            "constraint_refs": tuple(
                f"task-contract://{contract.contract_id}/constraints/{index}"
                for index, _ in enumerate(contract.constraints)
            ),
            "allowed_context_sources": registration.contract.context_policy.allowed_sources,
            "capability": node.capability,
            "effective_tool_scope_digest": sha256_digest(
                {
                    "grants": [
                        item.model_dump(mode="json")
                        for item in registration.contract.tool_policy.grants
                    ]
                }
            ),
            "output_schema_digest": sha256_digest(registration.contract.output_schema),
            "budget_allocation": node.budget,
            "parent_invocation_id": None,
        }
        handoff = HandoffEnvelope.model_validate(
            {**material, "handoff_digest": sha256_digest(material)}
        )
        session.add(
            AgentHandoffRecord(
                handoff_id=handoff.handoff_id,
                run_id=run.run_id,
                target_node_id=node.node_id,
                manifest=handoff.model_dump(mode="json"),
                handoff_digest=handoff.handoff_digest,
                created_at=utc_now(),
            )
        )
        return handoff

    async def _reap_expired(
        self, session: AsyncSession, run: TaskExecutionRunRecord
    ) -> None:
        now = utc_now()
        expired = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .where(
                        TaskExecutionNodeRecord.run_id == run.run_id,
                        TaskExecutionNodeRecord.status.in_(("claimed", "running")),
                        TaskExecutionNodeRecord.claim_expires_at <= now,
                    )
                    .with_for_update()
                )
            ).all()
        )
        for node in expired:
            invocation = await session.scalar(
                select(AgentInvocationRecord)
                .where(
                    AgentInvocationRecord.node_id == node.node_id,
                    AgentInvocationRecord.attempt == node.attempt_count,
                )
                .with_for_update()
            )
            if invocation is not None:
                turns = tuple(
                    (
                        await session.scalars(
                            select(AgentModelTurnRecord)
                            .where(
                                AgentModelTurnRecord.invocation_id
                                == invocation.invocation_id,
                                AgentModelTurnRecord.status
                                == ModelTurnStatus.DISPATCHING.value,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                for turn in turns:
                    turn.status = ModelTurnStatus.OUTCOME_UNKNOWN.value
                    turn.stable_error_code = "LEASE_EXPIRED_DURING_DISPATCH"
                    turn.updated_at = now
                invocation.execution_status = InvocationExecutionStatus.FAILED_RETRYABLE.value
                invocation.finished_at = now
                invocation.revision += 1
            retries = int(node.budget.get("retries", 0))
            node.status = (
                ExecutionNodeStatus.READY.value
                if node.attempt_count < retries + 1
                else ExecutionNodeStatus.FAILED.value
            )
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
        if expired:
            run.revision += 1
            run.updated_at = now

    async def _locked_invocation(
        self, session: AsyncSession, invocation_id: str
    ) -> tuple[AgentInvocationRecord, TaskExecutionNodeRecord]:
        invocation = await session.scalar(
            select(AgentInvocationRecord)
            .where(AgentInvocationRecord.invocation_id == invocation_id)
            .with_for_update()
        )
        if invocation is None:
            raise AgentRuntimeNotFoundError("Invocation does not exist")
        node = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == invocation.node_id)
            .with_for_update()
        )
        if node is None:
            raise AgentRuntimeProofRejectedError("Invocation node is missing")
        return invocation, node

    @staticmethod
    def _assert_lease(
        node: TaskExecutionNodeRecord, owner_id: str, fencing_token: int
    ) -> None:
        now = utc_now()
        expires = node.claim_expires_at
        normalized_expires = (
            expires.replace(tzinfo=UTC)
            if expires is not None and expires.tzinfo is None
            else expires
        )
        if (
            node.claim_owner_id != owner_id
            or node.claim_fencing_token != fencing_token
            or normalized_expires is None
            or normalized_expires <= now
        ):
            raise AgentLeaseRejectedError("Invocation lease or fencing token is stale")

    async def _read_run(
        self, session: AsyncSession, run: TaskExecutionRunRecord
    ) -> ExecutionRunRead:
        plan_record = await session.get(
            TaskPlanGenerationRecord, (run.task_id, run.plan_generation)
        )
        if plan_record is None:
            raise AgentRuntimeProofRejectedError("Execution plan is missing")
        plan = self._validated_plan(plan_record)
        if plan.plan_manifest_digest != run.plan_digest:
            raise AgentRuntimeProofRejectedError("Execution run plan digest drifted")
        nodes = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .where(TaskExecutionNodeRecord.run_id == run.run_id)
                    .order_by(TaskExecutionNodeRecord.local_key)
                )
            ).all()
        )
        invocations = tuple(
            (
                await session.scalars(
                    select(AgentInvocationRecord)
                    .where(AgentInvocationRecord.run_id == run.run_id)
                    .order_by(AgentInvocationRecord.created_at)
                )
            ).all()
        )
        return ExecutionRunRead(
            run_id=run.run_id,
            task_id=run.task_id,
            plan_generation=run.plan_generation,
            plan_digest=run.plan_digest,
            status=ExecutionRunStatus(run.status),
            revision=run.revision,
            nodes=tuple(
                ExecutionNodeRead(
                    node_id=item.node_id,
                    local_key=item.local_key,
                    status=ExecutionNodeStatus(item.status),
                    revision=item.revision,
                    attempt_count=item.attempt_count,
                    claim_owner_id=item.claim_owner_id,
                    claim_fencing_token=item.claim_fencing_token,
                    claim_expires_at=item.claim_expires_at,
                    bound_agent=(
                        BoundAgentRef.model_validate(item.bound_agent)
                        if item.bound_agent is not None
                        else None
                    ),
                    runtime_enabled=item.runtime_enabled,
                )
                for item in nodes
            ),
            invocations=tuple(self._invocation_read(item) for item in invocations),
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    @staticmethod
    def _invocation_read(record: AgentInvocationRecord) -> AgentInvocationRead:
        return AgentInvocationRead(
            invocation_id=record.invocation_id,
            node_id=record.node_id,
            attempt=record.attempt,
            handoff_id=record.handoff_id,
            agent=BoundAgentRef(
                agent_id=record.agent_id,
                version=record.agent_version,
                contract_digest=record.agent_contract_digest,
                prompt_package_digest=record.prompt_package_digest,
            ),
            execution_status=InvocationExecutionStatus(record.execution_status),
            verification_status=InvocationVerificationStatus(record.verification_status),
            result_id=record.result_id,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )
