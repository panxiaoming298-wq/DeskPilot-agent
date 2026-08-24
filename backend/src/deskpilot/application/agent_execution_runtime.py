"""Persistent ready/claim/lease runtime; results stop at verification boundary."""

from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_registry import AgentRegistry
from deskpilot.application.plan_compiler import PlanCompiler, PlanCompilerError
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.agent_runtime import (
    AgentDelegationRead,
    AgentInputRequestRead,
    AgentInvocationRead,
    AgentModelTurnRead,
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
from deskpilot.domain.task_plans import ExecutablePlan, PlanNodeBudget, TaskContract
from deskpilot.domain.task_workbench import TurnRouteStatus
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentDelegationRecord,
    AgentHandoffRecord,
    AgentInputRequestRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentObservationRecord,
    AgentResultRecord,
    AgentTaskGraphNodeRecord,
    AgentTaskGraphRecord,
    ModelDispatchAttemptRecord,
    TaskContractVersionRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskPlanGenerationRecord,
    TurnRouteRecord,
    utc_now,
)

AGENT_LEASE_RETRY_EXHAUSTED = "AGENT_LEASE_RETRY_EXHAUSTED"


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
                    if (node.bound_agent is not None or node.capability is not None)
                    and node.runtime_enabled
                ]
                if not runnable:
                    raise AgentRuntimeDisabledError(
                        "Active plan has no enabled Agent or capability node"
                    )
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
                        and node.handoff_parent_node_id is None
                        and (node.bound_agent is not None or node.capability is not None)
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
                            handoff_parent_node_id=node.handoff_parent_node_id,
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

    async def cancel(self, run_id: str) -> ExecutionRunRead:
        """Fence all unfinished work so a stale Agent result cannot unlock successors."""
        async with self._database.session() as session, session.begin():
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == run_id)
                .with_for_update()
            )
            if run is None:
                raise AgentRuntimeNotFoundError("Execution run does not exist")
            if run.status in {
                ExecutionRunStatus.CANCELLED.value,
                ExecutionRunStatus.FAILED.value,
                ExecutionRunStatus.SUCCEEDED.value,
                ExecutionRunStatus.SUPERSEDED.value,
            }:
                return await self._read_run(session, run)

            now = utc_now()
            run.status = ExecutionRunStatus.CANCELLED.value
            run.revision += 1
            run.updated_at = now
            nodes = (
                await session.scalars(
                    select(TaskExecutionNodeRecord).where(
                        TaskExecutionNodeRecord.run_id == run_id,
                        TaskExecutionNodeRecord.status.not_in(
                            (
                                ExecutionNodeStatus.VERIFIED.value,
                                ExecutionNodeStatus.FAILED.value,
                                ExecutionNodeStatus.CANCELLED.value,
                            )
                        ),
                    )
                )
            ).all()
            for node in nodes:
                node.status = ExecutionNodeStatus.CANCELLED.value
                node.claim_fencing_token += 1
                node.claim_owner_id = None
                node.claim_acquired_at = None
                node.claim_heartbeat_at = None
                node.claim_expires_at = None
                node.revision += 1
                node.updated_at = now
            invocations = (
                await session.scalars(
                    select(AgentInvocationRecord).where(
                        AgentInvocationRecord.run_id == run_id,
                        AgentInvocationRecord.execution_status.in_(
                            (
                                InvocationExecutionStatus.CREATED.value,
                                InvocationExecutionStatus.RUNNING.value,
                                InvocationExecutionStatus.WAITING_USER.value,
                                InvocationExecutionStatus.WAITING_CHILDREN.value,
                                InvocationExecutionStatus.FAILED_RETRYABLE.value,
                            )
                        ),
                    )
                )
            ).all()
            for invocation in invocations:
                invocation.execution_status = InvocationExecutionStatus.CANCELLED.value
                invocation.finished_at = now
                invocation.revision += 1
            delegations = (
                await session.scalars(
                    select(AgentDelegationRecord).where(
                        AgentDelegationRecord.run_id == run_id,
                        AgentDelegationRecord.status.in_(
                            ("waiting_child", "child_verified")
                        ),
                    )
                )
            ).all()
            for delegation in delegations:
                delegation.status = "cancelled"
                delegation.updated_at = now
            task_graphs = tuple(
                (
                    await session.scalars(
                        select(AgentTaskGraphRecord).where(
                            AgentTaskGraphRecord.run_id == run_id,
                            AgentTaskGraphRecord.status.in_(("running", "verified")),
                        )
                    )
                ).all()
            )
            for graph in task_graphs:
                graph.status = "cancelled"
                graph.updated_at = now
            if task_graphs:
                graph_ids = tuple(item.graph_id for item in task_graphs)
                task_graph_nodes = tuple(
                    (
                        await session.scalars(
                            select(AgentTaskGraphNodeRecord).where(
                                AgentTaskGraphNodeRecord.graph_id.in_(graph_ids),
                                AgentTaskGraphNodeRecord.status.in_(
                                    ("waiting_child", "child_verified")
                                ),
                            )
                        )
                    ).all()
                )
                for graph_node in task_graph_nodes:
                    graph_node.status = "cancelled"
                    graph_node.updated_at = now
            invocation_ids = tuple(item.invocation_id for item in invocations)
            if invocation_ids:
                input_requests = (
                    await session.scalars(
                        select(AgentInputRequestRecord).where(
                            AgentInputRequestRecord.invocation_id.in_(invocation_ids),
                            AgentInputRequestRecord.status == "pending",
                        )
                    )
                ).all()
                for input_request in input_requests:
                    input_request.status = "cancelled"
                    input_request.resolved_at = now
            await session.flush()
            return await self._read_run(session, run)

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
            if run.status != ExecutionRunStatus.ACTIVE.value:
                await session.flush()
                return None
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
            resumable = await session.scalar(
                select(AgentInvocationRecord)
                .where(
                    AgentInvocationRecord.node_id == node.node_id,
                    AgentInvocationRecord.execution_status
                    == InvocationExecutionStatus.WAITING_CHILDREN.value,
                )
                .with_for_update()
            )
            claimed_at = utc_now()
            node.status = ExecutionNodeStatus.CLAIMED.value
            node.claim_fencing_token += 1
            node.claim_owner_id = owner_id
            node.claim_acquired_at = claimed_at
            node.claim_heartbeat_at = claimed_at
            node.claim_expires_at = claimed_at + timedelta(seconds=lease_seconds)
            node.revision += 1
            node.updated_at = claimed_at
            if resumable is not None:
                handoff = await self._load_handoff(session, resumable.handoff_id)
                await session.flush()
                return ClaimedInvocation(
                    handoff=handoff,
                    invocation=self._invocation_read(resumable),
                    claim_owner_id=owner_id,
                    claim_fencing_token=node.claim_fencing_token,
                    claim_expires_at=node.claim_expires_at,
                )
            node.attempt_count += 1
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
                parent_invocation_id=handoff.parent_invocation_id,
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
            if handoff.parent_invocation_id is not None:
                delegation = await session.scalar(
                    select(AgentDelegationRecord)
                    .where(
                        AgentDelegationRecord.run_id == run_id,
                        AgentDelegationRecord.child_node_id == node.node_id,
                        AgentDelegationRecord.status == "waiting_child",
                    )
                    .with_for_update()
                )
                graph_node = await session.scalar(
                    select(AgentTaskGraphNodeRecord)
                    .where(
                        AgentTaskGraphNodeRecord.child_node_id == node.node_id,
                        AgentTaskGraphNodeRecord.status == "waiting_child",
                    )
                    .with_for_update()
                )
                if (delegation is None) == (graph_node is None):
                    raise AgentRuntimeProofRejectedError(
                        "Child Invocation has no unique delegation or task graph binding"
                    )
                if delegation is not None:
                    if delegation.child_invocation_id is not None:
                        raise AgentRuntimeProofRejectedError(
                            "Child Invocation delegation was already consumed"
                        )
                    delegation.child_invocation_id = invocation_id
                    delegation.updated_at = claimed_at
                else:
                    assert graph_node is not None
                    if graph_node.child_invocation_id is not None:
                        raise AgentRuntimeProofRejectedError(
                            "Task graph child Invocation was already created"
                        )
                    graph_node.child_invocation_id = invocation_id
                    graph_node.updated_at = claimed_at
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
            run, node, invocation = await self._locked_worker_mutation(
                session, invocation_id
            )
            self._assert_worker_lease(
                run,
                node,
                invocation,
                owner_id,
                fencing_token,
                allowed_run_statuses=(ExecutionRunStatus.ACTIVE.value,),
                allowed_node_statuses=(ExecutionNodeStatus.CLAIMED.value,),
            )
            if invocation.execution_status not in {
                InvocationExecutionStatus.CREATED.value,
                InvocationExecutionStatus.WAITING_CHILDREN.value,
            }:
                raise AgentRuntimeConflictError("Invocation is not claimable")
            now = utc_now()
            invocation.execution_status = InvocationExecutionStatus.RUNNING.value
            if invocation.started_at is None:
                invocation.started_at = now
            invocation.revision += 1
            node.status = ExecutionNodeStatus.RUNNING.value
            node.revision += 1
            node.updated_at = now
            await session.flush()
            return self._invocation_read(invocation)

    async def claim_ready_batch(
        self,
        run_id: str,
        owner_prefix: str,
        *,
        max_count: int | None = None,
        lease_seconds: int = 60,
    ) -> tuple[ClaimedInvocation, ...]:
        """Claim a bounded ready wave so independent dynamic children can run in parallel."""

        limit = self._max_parallel if max_count is None else min(max_count, self._max_parallel)
        if limit < 1:
            raise ValueError("Claim batch size must be positive")
        claimed: list[ClaimedInvocation] = []
        for index in range(limit):
            item = await self.claim_next(
                run_id,
                f"{owner_prefix}-{index}",
                lease_seconds=lease_seconds,
            )
            if item is None:
                break
            claimed.append(item)
        return tuple(claimed)

    async def submit_result(
        self,
        result: AgentResult | AgentOutputResult,
        *,
        owner_id: str,
        fencing_token: int,
    ) -> AgentInvocationRead:
        async with self._database.session() as session, session.begin():
            run, node, invocation = await self._locked_worker_mutation(
                session, result.invocation_id
            )
            self._assert_worker_lease(
                run,
                node,
                invocation,
                owner_id,
                fencing_token,
                allowed_run_statuses=(
                    ExecutionRunStatus.ACTIVE.value,
                    ExecutionRunStatus.AWAITING_VERIFICATION.value,
                ),
                allowed_node_statuses=(ExecutionNodeStatus.RUNNING.value,),
            )
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
            active_sibling_id = await session.scalar(
                select(TaskExecutionNodeRecord.node_id)
                .where(
                    TaskExecutionNodeRecord.run_id == run.run_id,
                    TaskExecutionNodeRecord.node_id != node.node_id,
                    TaskExecutionNodeRecord.status.in_(
                        (
                            ExecutionNodeStatus.READY.value,
                            ExecutionNodeStatus.CLAIMED.value,
                            ExecutionNodeStatus.RUNNING.value,
                        )
                    ),
                )
                .limit(1)
            )
            run.status = (
                ExecutionRunStatus.ACTIVE.value
                if active_sibling_id is not None
                else ExecutionRunStatus.AWAITING_VERIFICATION.value
            )
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
        delegation = await session.scalar(
            select(AgentDelegationRecord).where(
                AgentDelegationRecord.run_id == run.run_id,
                AgentDelegationRecord.child_node_id == node.node_id,
                AgentDelegationRecord.status == "waiting_child",
            )
        )
        if node.handoff_parent_node_id is not None:
            graph_node = await session.scalar(
                select(AgentTaskGraphNodeRecord).where(
                    AgentTaskGraphNodeRecord.child_node_id == node.node_id,
                    AgentTaskGraphNodeRecord.status == "waiting_child",
                )
            )
            if (delegation is None) == (graph_node is None):
                raise AgentRuntimeProofRejectedError(
                    "Child node has no unique accepted delegation or task graph binding"
                )
            if delegation is not None:
                if delegation.parent_node_id != node.handoff_parent_node_id:
                    raise AgentRuntimeProofRejectedError(
                        "Optional child delegation parent changed"
                    )
                parent_invocation_id = delegation.parent_invocation_id
                budget_allocation = delegation.budget_allocation
                objective_ref = f"plan-node://{node.node_id}/objective"
            else:
                assert graph_node is not None
                graph = await session.get(AgentTaskGraphRecord, graph_node.graph_id)
                if (
                    graph is None
                    or graph.parent_node_id != node.handoff_parent_node_id
                    or graph.status != "running"
                ):
                    raise AgentRuntimeProofRejectedError(
                        "Dynamic child graph parent binding changed"
                    )
                parent_invocation_id = graph.parent_invocation_id
                budget_allocation = graph_node.budget_allocation
                objective_ref = (
                    f"agent-task-graph://{graph.graph_id}/nodes/{graph_node.local_key}/objective"
                )
                from deskpilot.application.agent_supervisor_runtime import (
                    AgentSupervisorRuntime,
                )

                upstream_result_refs = (
                    await AgentSupervisorRuntime.verified_upstream_result_refs(
                        session, graph, graph_node
                    )
                )
                capability_input = AgentSupervisorRuntime.verified_capability_input(
                    graph, graph_node
                )
        else:
            if delegation is not None:
                raise AgentRuntimeProofRejectedError("Root node cannot consume a delegation")
            parent_invocation_id = None
            budget_allocation = node.budget
            objective_ref = f"plan://{plan_record.plan_id}/nodes/{node.node_id}/objective"
        if node.handoff_parent_node_id is None or delegation is not None:
            upstream_result_refs = ()
            capability_input = None
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
            "objective_ref": objective_ref,
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
            "budget_allocation": budget_allocation,
            "parent_invocation_id": parent_invocation_id,
            "upstream_result_refs": upstream_result_refs,
            "capability_input": capability_input,
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

    @staticmethod
    async def _load_handoff(session: AsyncSession, handoff_id: str) -> HandoffEnvelope:
        record = await session.get(AgentHandoffRecord, handoff_id)
        if record is None:
            raise AgentRuntimeProofRejectedError("Invocation handoff is missing")
        try:
            handoff = HandoffEnvelope.model_validate(record.manifest)
        except ValidationError as error:
            raise AgentRuntimeProofRejectedError("Invocation handoff proof was rejected") from error
        if handoff.handoff_digest != record.handoff_digest:
            raise AgentRuntimeProofRejectedError("Invocation handoff digest drifted")
        return handoff

    async def _reap_expired(self, session: AsyncSession, run: TaskExecutionRunRecord) -> None:
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
        reaped = False
        for node in expired:
            # One exhausted lease terminalizes the whole Run below.  The original
            # SELECT may still contain siblings that were claimed before that
            # terminalization, so never process their stale in-memory rows twice.
            if node.status not in {
                ExecutionNodeStatus.CLAIMED.value,
                ExecutionNodeStatus.RUNNING.value,
            }:
                continue
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
                                AgentModelTurnRecord.invocation_id == invocation.invocation_id,
                                AgentModelTurnRecord.status == ModelTurnStatus.DISPATCHING.value,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                for turn in turns:
                    turn.status = ModelTurnStatus.OUTCOME_UNKNOWN.value
                    turn.stable_error_code = "LEASE_EXPIRED_DURING_DISPATCH"
                    turn.updated_at = now
                    attempt = await session.scalar(
                        select(ModelDispatchAttemptRecord)
                        .where(ModelDispatchAttemptRecord.turn_id == turn.turn_id)
                        .with_for_update()
                    )
                    if attempt is not None:
                        attempt.status = "outcome_unknown"
                        attempt.stable_error_code = "LEASE_EXPIRED_DURING_DISPATCH"
                        attempt.updated_at = now
            retries = int(node.budget.get("retries", 0))
            exhausted = node.attempt_count >= retries + 1
            if invocation is not None:
                self._finish_invocation(
                    invocation,
                    (
                        InvocationExecutionStatus.FAILED_TERMINAL.value
                        if exhausted
                        else InvocationExecutionStatus.FAILED_RETRYABLE.value
                    ),
                    now,
                )
            node.status = (
                ExecutionNodeStatus.FAILED.value
                if exhausted
                else ExecutionNodeStatus.READY.value
            )
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            if exhausted:
                # Fence the worker immediately even though no replacement claim
                # will ever be issued for this terminal attempt.
                node.claim_fencing_token += 1
            node.revision += 1
            node.updated_at = now
            reaped = True
            if exhausted:
                await self._terminalize_exhausted_run(session, run, node, now)
        if reaped:
            run.revision += 1
            run.updated_at = now

    async def _terminalize_exhausted_run(
        self,
        session: AsyncSession,
        run: TaskExecutionRunRecord,
        exhausted_node: TaskExecutionNodeRecord,
        now: datetime,
    ) -> None:
        """Fail one exhausted lineage and fence every unfinished sibling in its Run."""

        failed_node_ids = {exhausted_node.node_id}
        if exhausted_node.handoff_parent_node_id is not None:
            failed_node_ids.add(exhausted_node.handoff_parent_node_id)

        delegation = await session.scalar(
            select(AgentDelegationRecord)
            .where(
                AgentDelegationRecord.run_id == run.run_id,
                (
                    (AgentDelegationRecord.child_node_id == exhausted_node.node_id)
                    | (AgentDelegationRecord.parent_node_id == exhausted_node.node_id)
                ),
                AgentDelegationRecord.status.in_(("waiting_child", "child_verified")),
            )
            .with_for_update()
        )
        graph_node = await session.scalar(
            select(AgentTaskGraphNodeRecord)
            .where(AgentTaskGraphNodeRecord.child_node_id == exhausted_node.node_id)
            .with_for_update()
        )
        graph = None
        if graph_node is not None:
            graph = await session.scalar(
                select(AgentTaskGraphRecord)
                .where(AgentTaskGraphRecord.graph_id == graph_node.graph_id)
                .with_for_update()
            )
        elif exhausted_node.handoff_parent_node_id is None:
            graph = await session.scalar(
                select(AgentTaskGraphRecord)
                .where(
                    AgentTaskGraphRecord.parent_node_id == exhausted_node.node_id,
                    AgentTaskGraphRecord.status.in_(("running", "verified")),
                )
                .with_for_update()
            )

        if delegation is not None:
            delegation.status = "failed"
            delegation.updated_at = now
            failed_node_ids.add(delegation.parent_node_id)
        if graph is not None:
            graph.status = "failed"
            graph.updated_at = now
            failed_node_ids.add(graph.parent_node_id)
            graph_children = tuple(
                (
                    await session.scalars(
                        select(AgentTaskGraphNodeRecord)
                        .where(AgentTaskGraphNodeRecord.graph_id == graph.graph_id)
                        .with_for_update()
                    )
                ).all()
            )
            for child in graph_children:
                if child.child_node_id == exhausted_node.node_id:
                    child.status = "failed"
                    child.updated_at = now
                elif child.status == "waiting_child":
                    child.status = "cancelled"
                    child.updated_at = now

        unfinished_statuses = (
            ExecutionNodeStatus.PENDING.value,
            ExecutionNodeStatus.READY.value,
            ExecutionNodeStatus.CLAIMED.value,
            ExecutionNodeStatus.RUNNING.value,
            ExecutionNodeStatus.WAITING_USER.value,
            ExecutionNodeStatus.WAITING_CHILDREN.value,
            ExecutionNodeStatus.AWAITING_VERIFICATION.value,
        )
        unfinished_nodes = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .where(
                        TaskExecutionNodeRecord.run_id == run.run_id,
                        TaskExecutionNodeRecord.status.in_(unfinished_statuses),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for node in unfinished_nodes:
            node.status = (
                ExecutionNodeStatus.FAILED.value
                if node.node_id in failed_node_ids
                else ExecutionNodeStatus.CANCELLED.value
            )
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.claim_fencing_token += 1
            node.revision += 1
            node.updated_at = now

        active_invocation_statuses = (
            InvocationExecutionStatus.CREATED.value,
            InvocationExecutionStatus.RUNNING.value,
            InvocationExecutionStatus.WAITING_USER.value,
            InvocationExecutionStatus.WAITING_CHILDREN.value,
        )
        active_invocations = tuple(
            (
                await session.scalars(
                    select(AgentInvocationRecord)
                    .where(
                        AgentInvocationRecord.run_id == run.run_id,
                        AgentInvocationRecord.execution_status.in_(
                            active_invocation_statuses
                        ),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for invocation in active_invocations:
            self._finish_invocation(
                invocation,
                (
                    InvocationExecutionStatus.FAILED_TERMINAL.value
                    if invocation.node_id in failed_node_ids
                    else InvocationExecutionStatus.CANCELLED.value
                ),
                now,
            )

        active_invocation_ids = tuple(item.invocation_id for item in active_invocations)
        if active_invocation_ids:
            pending_inputs = tuple(
                (
                    await session.scalars(
                        select(AgentInputRequestRecord)
                        .where(
                            AgentInputRequestRecord.invocation_id.in_(
                                active_invocation_ids
                            ),
                            AgentInputRequestRecord.status == "pending",
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for request in pending_inputs:
                request.status = "cancelled"
                request.resolved_at = now

        remaining_delegations = tuple(
            (
                await session.scalars(
                    select(AgentDelegationRecord)
                    .where(
                        AgentDelegationRecord.run_id == run.run_id,
                        AgentDelegationRecord.status.in_(
                            ("waiting_child", "child_verified")
                        ),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for sibling in remaining_delegations:
            sibling.status = "cancelled"
            sibling.updated_at = now

        remaining_graphs = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphRecord)
                    .where(
                        AgentTaskGraphRecord.run_id == run.run_id,
                        AgentTaskGraphRecord.status.in_(("running", "verified")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for sibling_graph in remaining_graphs:
            sibling_graph.status = "cancelled"
            sibling_graph.updated_at = now
            sibling_graph_nodes = tuple(
                (
                    await session.scalars(
                        select(AgentTaskGraphNodeRecord)
                        .where(
                            AgentTaskGraphNodeRecord.graph_id == sibling_graph.graph_id,
                            AgentTaskGraphNodeRecord.status == "waiting_child",
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for sibling_graph_node in sibling_graph_nodes:
                sibling_graph_node.status = "cancelled"
                sibling_graph_node.updated_at = now

        route = await session.scalar(
            select(TurnRouteRecord)
            .where(TurnRouteRecord.task_id == run.task_id)
            .with_for_update()
        )
        if route is not None and (
            route.status != TurnRouteStatus.FAILED.value
            or route.result_manifest is not None
            or route.result_digest is not None
            or route.error_code != AGENT_LEASE_RETRY_EXHAUSTED
        ):
            route.status = TurnRouteStatus.FAILED.value
            route.result_manifest = None
            route.result_digest = None
            route.error_code = AGENT_LEASE_RETRY_EXHAUSTED
            route.revision += 1
            route.updated_at = now
        run.status = ExecutionRunStatus.FAILED.value

    @staticmethod
    def _finish_invocation(
        invocation: AgentInvocationRecord,
        status: str,
        now: datetime,
    ) -> None:
        changed = False
        if invocation.execution_status != status:
            invocation.execution_status = status
            changed = True
        if invocation.finished_at is None:
            invocation.finished_at = now
            changed = True
        if changed:
            invocation.revision += 1

    async def _locked_worker_mutation(
        self, session: AsyncSession, invocation_id: str
    ) -> tuple[
        TaskExecutionRunRecord,
        TaskExecutionNodeRecord,
        AgentInvocationRecord,
    ]:
        identity = (
            await session.execute(
                select(
                    AgentInvocationRecord.run_id,
                    AgentInvocationRecord.node_id,
                ).where(AgentInvocationRecord.invocation_id == invocation_id)
            )
        ).one_or_none()
        if identity is None:
            raise AgentRuntimeNotFoundError("Invocation does not exist")
        run_id, node_id = identity
        # The identity read above is deliberately non-locking.  Every mutation
        # then takes the global worker order: Run -> Node -> Invocation.  The
        # exact immutable lineage is rechecked by the locked predicates below.
        run = await session.scalar(
            select(TaskExecutionRunRecord)
            .where(TaskExecutionRunRecord.run_id == run_id)
            .with_for_update()
        )
        if run is None:
            raise AgentRuntimeProofRejectedError("Invocation run is missing")
        node = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(
                TaskExecutionNodeRecord.node_id == node_id,
                TaskExecutionNodeRecord.run_id == run.run_id,
            )
            .with_for_update()
        )
        if node is None:
            raise AgentRuntimeProofRejectedError("Invocation node is missing")
        invocation = await session.scalar(
            select(AgentInvocationRecord)
            .where(
                AgentInvocationRecord.invocation_id == invocation_id,
                AgentInvocationRecord.run_id == run.run_id,
                AgentInvocationRecord.node_id == node.node_id,
            )
            .with_for_update()
        )
        if invocation is None:
            raise AgentRuntimeProofRejectedError("Invocation lineage changed concurrently")
        return run, node, invocation

    @classmethod
    def _assert_worker_lease(
        cls,
        run: TaskExecutionRunRecord,
        node: TaskExecutionNodeRecord,
        invocation: AgentInvocationRecord,
        owner_id: str,
        fencing_token: int,
        *,
        allowed_run_statuses: tuple[str, ...],
        allowed_node_statuses: tuple[str, ...],
    ) -> None:
        if (
            run.status not in allowed_run_statuses
            or node.status not in allowed_node_statuses
            or invocation.run_id != run.run_id
            or invocation.node_id != node.node_id
            or invocation.attempt != node.attempt_count
        ):
            raise AgentLeaseRejectedError("Invocation lineage is no longer active")
        cls._assert_lease(node, owner_id, fencing_token)

    @staticmethod
    def _assert_lease(node: TaskExecutionNodeRecord, owner_id: str, fencing_token: int) -> None:
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
        invocation_ids = tuple(item.invocation_id for item in invocations)
        delegations = tuple(
            (
                await session.scalars(
                    select(AgentDelegationRecord)
                    .where(AgentDelegationRecord.run_id == run.run_id)
                    .order_by(AgentDelegationRecord.created_at)
                )
            ).all()
        )
        input_requests = (
            tuple(
                (
                    await session.scalars(
                        select(AgentInputRequestRecord)
                        .where(AgentInputRequestRecord.invocation_id.in_(invocation_ids))
                        .order_by(AgentInputRequestRecord.created_at)
                    )
                ).all()
            )
            if invocation_ids
            else ()
        )
        # A Model Turn and its accepted decision change atomically. Read them in
        # one statement so SQLite's legacy SELECT transaction behavior cannot
        # combine a pre-commit Turn with a post-commit decision.
        turn_decision_rows = (
            tuple(
                (
                    await session.execute(
                        select(AgentModelTurnRecord, AgentDecisionRecord)
                        .outerjoin(
                            AgentDecisionRecord,
                            AgentDecisionRecord.turn_id == AgentModelTurnRecord.turn_id,
                        )
                        .where(AgentModelTurnRecord.invocation_id.in_(invocation_ids))
                        .order_by(
                            AgentModelTurnRecord.invocation_id,
                            AgentModelTurnRecord.turn_no,
                        )
                    )
                ).all()
            )
            if invocation_ids
            else ()
        )
        turns = tuple(row[0] for row in turn_decision_rows)
        decisions = tuple(row[1] for row in turn_decision_rows if row[1] is not None)
        decision_ids = tuple(item.decision_id for item in decisions)
        observations = (
            tuple(
                (
                    await session.scalars(
                        select(AgentObservationRecord).where(
                            AgentObservationRecord.decision_id.in_(decision_ids)
                        )
                    )
                ).all()
            )
            if decision_ids
            else ()
        )
        decision_by_turn = {item.turn_id: item for item in decisions}
        decision_by_id = {item.decision_id: item for item in decisions}
        observation_by_decision = {item.decision_id: item for item in observations}
        observation_by_id = {item.observation_id: item for item in observations}
        invocation_by_id = {item.invocation_id: item for item in invocations}
        node_by_id = {item.node_id: item for item in nodes}
        from deskpilot.application.agent_supervisor_runtime import read_agent_task_graphs

        task_graphs = await read_agent_task_graphs(
            session,
            run,
            nodes=node_by_id,
            invocations=invocation_by_id,
            decisions=decision_by_id,
            observations=observation_by_id,
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
                    depends_on=tuple(item.depends_on),
                    handoff_parent_node_id=item.handoff_parent_node_id,
                    budget=PlanNodeBudget.model_validate(item.budget),
                    runtime_enabled=item.runtime_enabled,
                )
                for item in nodes
            ),
            invocations=tuple(self._invocation_read(item) for item in invocations),
            model_turns=tuple(
                self._model_turn_read(
                    item,
                    invocation_by_id[item.invocation_id],
                    decision_by_turn.get(item.turn_id),
                    observation_by_decision,
                )
                for item in turns
            ),
            input_requests=tuple(self._input_request_read(item) for item in input_requests),
            delegations=tuple(
                self._delegation_read(
                    item,
                    node_by_id,
                    invocation_by_id,
                    decision_by_id,
                    observation_by_id,
                )
                for item in delegations
            ),
            task_graphs=task_graphs,
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
            parent_invocation_id=record.parent_invocation_id,
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

    @staticmethod
    def _model_turn_read(
        turn: AgentModelTurnRecord,
        invocation: AgentInvocationRecord,
        decision: AgentDecisionRecord | None,
        observations: dict[str, AgentObservationRecord],
    ) -> AgentModelTurnRead:
        if (
            (
                (
                    invocation.agent_id == "builtin.web_researcher"
                    and invocation.agent_version == "1.1.0"
                )
                or (
                    invocation.agent_id
                    in {
                        "builtin.workspace_reader",
                        "builtin.workspace_coordinator",
                        "builtin.workspace_patch_planner",
                    }
                )
            )
            and turn.status == ModelTurnStatus.SUCCEEDED.value
            and decision is None
        ):
            raise AgentRuntimeProofRejectedError("Agent Model Turn decision is missing")
        observation = observations.get(decision.decision_id) if decision is not None else None
        if decision is not None:
            expected = sha256_digest(
                {
                    "turn_id": turn.turn_id,
                    "invocation_id": turn.invocation_id,
                    "decision": decision.manifest,
                    "response_digest": turn.response_digest,
                }
            )
            if decision.decision_digest != expected:
                raise AgentRuntimeProofRejectedError("Agent decision proof drifted")
        if observation is not None:
            material = {
                "observation_id": observation.observation_id,
                "invocation_id": observation.invocation_id,
                "decision_id": observation.decision_id,
                "source_kind": observation.source_kind,
                "binding_id": observation.binding_id,
                "status": observation.status,
                "result_ref": observation.result_ref,
                "projection": observation.projection,
            }
            if observation.observation_digest != sha256_digest(material):
                raise AgentRuntimeProofRejectedError("Agent observation proof drifted")
        return AgentModelTurnRead(
            turn_id=turn.turn_id,
            invocation_id=turn.invocation_id,
            turn_no=turn.turn_no,
            status=ModelTurnStatus(turn.status),
            request_digest=turn.request_digest,
            response_digest=turn.response_digest,
            provider_id=turn.provider_id,
            model=turn.model,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cost_micros=turn.cost_micros,
            stable_error_code=turn.stable_error_code,
            decision_kind=(
                cast(
                    Literal[
                        "request_route",
                        "submit_result",
                        "needs_user_input",
                        "propose_handoff",
                        "propose_task_graph",
                    ],
                    decision.kind,
                )
                if decision is not None
                else None
            ),
            decision_digest=decision.decision_digest if decision is not None else None,
            binding_id=decision.binding_id if decision is not None else None,
            observation_digest=(
                observation.observation_digest if observation is not None else None
            ),
        )

    @staticmethod
    def _input_request_read(record: AgentInputRequestRecord) -> AgentInputRequestRead:
        material = {
            "input_request_id": record.input_request_id,
            "invocation_id": record.invocation_id,
            "decision_id": record.decision_id,
            "question_code": record.question_code,
            "question": record.question,
            "blocking_fields": record.blocking_fields,
            "answer_schema": record.answer_schema,
        }
        if record.request_digest != sha256_digest(material):
            raise AgentRuntimeProofRejectedError("Agent input request proof drifted")
        if record.status == "pending" and any(
            value is not None
            for value in (record.resolved_task_id, record.answer_digest, record.resolved_at)
        ):
            raise AgentRuntimeProofRejectedError("Pending Agent input request was resolved")
        return AgentInputRequestRead(
            input_request_id=record.input_request_id,
            invocation_id=record.invocation_id,
            decision_id=record.decision_id,
            question_code=record.question_code,
            question=record.question,
            blocking_fields=tuple(record.blocking_fields),
            answer_schema=record.answer_schema,
            request_digest=record.request_digest,
            status=cast(Literal["pending", "resolved", "cancelled"], record.status),
            resolved_task_id=record.resolved_task_id,
            answer_digest=record.answer_digest,
            created_at=record.created_at,
            resolved_at=record.resolved_at,
        )

    @staticmethod
    def _delegation_read(
        record: AgentDelegationRecord,
        nodes: dict[str, TaskExecutionNodeRecord],
        invocations: dict[str, AgentInvocationRecord],
        decisions: dict[str, AgentDecisionRecord],
        observations: dict[str, AgentObservationRecord],
    ) -> AgentDelegationRead:
        parent = invocations.get(record.parent_invocation_id)
        child = (
            invocations.get(record.child_invocation_id)
            if record.child_invocation_id is not None
            else None
        )
        parent_node = nodes.get(record.parent_node_id)
        child_node = nodes.get(record.child_node_id)
        decision = decisions.get(record.decision_id)
        observation = (
            observations.get(record.observation_id)
            if record.observation_id is not None
            else None
        )
        budget = PlanNodeBudget.model_validate(record.budget_allocation)
        child_budget = (
            PlanNodeBudget.model_validate(child_node.budget)
            if child_node is not None
            else None
        )
        budget_fields = tuple(PlanNodeBudget.model_fields)
        if (
            record.proposal_digest != sha256_digest(record.proposal_manifest)
            or parent is None
            or parent.node_id != record.parent_node_id
            or parent_node is None
            or child_node is None
            or child_node.handoff_parent_node_id != parent_node.node_id
            or decision is None
            or decision.invocation_id != parent.invocation_id
            or decision.kind != "propose_handoff"
            or decision.binding_id != record.binding_id
            or decision.manifest != record.proposal_manifest
            or decision.manifest.get("budget_slice") != budget.model_dump(mode="json")
            or child_budget is None
            or any(
                getattr(budget, field) > getattr(child_budget, field)
                for field in budget_fields
            )
            or parent.parent_invocation_id is not None
            or record.depth != 1
        ):
            raise AgentRuntimeProofRejectedError("Agent delegation proof drifted")
        if child is not None and (
            child.node_id != child_node.node_id
            or child.parent_invocation_id != parent.invocation_id
        ):
            raise AgentRuntimeProofRejectedError("Child Invocation lineage drifted")
        verified_status = record.status in {"child_verified", "consumed"}
        if (record.child_result_id is None) != (record.observation_id is None):
            raise AgentRuntimeProofRejectedError("Delegation terminal proof is incomplete")
        has_terminal_proof = (
            child is not None
            and record.child_result_id is not None
            and record.observation_id is not None
        )
        if verified_status and not has_terminal_proof:
            raise AgentRuntimeProofRejectedError("Verified delegation has no child proof")
        if has_terminal_proof and (
            child is None
            or child.result_id != record.child_result_id
            or child.verification_status != InvocationVerificationStatus.VERIFIED.value
            or observation is None
            or observation.invocation_id != parent.invocation_id
            or observation.decision_id != decision.decision_id
            or observation.binding_id != record.binding_id
            or observation.source_kind != "handoff"
            or observation.status != "succeeded"
        ):
            raise AgentRuntimeProofRejectedError("Verified child observation lineage drifted")
        if record.status == "waiting_child" and any(
            value is not None for value in (record.child_result_id, record.observation_id)
        ):
            raise AgentRuntimeProofRejectedError("Waiting delegation contains a result")
        return AgentDelegationRead(
            delegation_id=record.delegation_id,
            parent_invocation_id=record.parent_invocation_id,
            child_invocation_id=record.child_invocation_id,
            parent_node_id=record.parent_node_id,
            child_node_id=record.child_node_id,
            decision_id=record.decision_id,
            binding_id=record.binding_id,
            status=cast(
                Literal[
                    "waiting_child",
                    "child_verified",
                    "consumed",
                    "cancelled",
                    "failed",
                ],
                record.status,
            ),
            depth=record.depth,
            budget_allocation=budget,
            child_result_id=record.child_result_id,
            observation_id=record.observation_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
