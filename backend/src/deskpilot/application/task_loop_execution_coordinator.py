"""One-step coordinator for persistent model-planner Task Loops.

The coordinator never accepts model-authored arguments.  It reduces only a
proof-checked projection, then delegates one exact server-bound Agent,
Capability, or control transition.  Every subsequent call reconstructs the
execution from durable state, which makes process restart the normal recovery
path rather than a reason to replay a Provider call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.artifact_delivery_runtime import (
    ArtifactDeliveryError,
    ArtifactDeliveryRuntime,
)
from deskpilot.application.capability_execution_runtime import (
    CapabilityExecutionRuntime,
)
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_agent_runtime import (
    AgentSourcePlanProof,
    SourceBoundAgentClaim,
    TaskLoopAgentRuntime,
)
from deskpilot.application.task_loop_reducer import (
    TaskLoopReducer,
    TaskLoopReducerCommand,
    TaskLoopReducerNode,
    TaskLoopReducerSnapshot,
)
from deskpilot.application.turn_planner_runtime import TurnPlannerRuntime
from deskpilot.application.verified_edges import mark_verified_and_unlock
from deskpilot.domain.capability_execution import CapabilityResultKind
from deskpilot.domain.task_loop_execution import (
    TaskLoopExecution,
    TaskLoopExecutionRead,
    TaskLoopVerifiedResult,
)
from deskpilot.domain.task_plans import DraftNodeKind
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopExecutionEventRecord,
    TaskLoopExecutionRecord,
    TaskLoopVerifiedResultRecord,
    TaskRecord,
    utc_now,
)


class TaskLoopExecutionCoordinatorError(RuntimeError):
    code = "TASK_LOOP_EXECUTION_COORDINATOR_ERROR"


class TaskLoopExecutionCoordinatorUnavailableError(TaskLoopExecutionCoordinatorError):
    code = "TASK_LOOP_EXECUTION_RUNTIME_UNAVAILABLE"


class TaskLoopExecutionCoordinatorProofRejectedError(TaskLoopExecutionCoordinatorError):
    code = "TASK_LOOP_EXECUTION_PROOF_REJECTED"


class TaskLoopFinalAcceptanceRejectedError(TaskLoopExecutionCoordinatorError):
    code = "TASK_LOOP_FINAL_ACCEPTANCE_REJECTED"


@dataclass(frozen=True, slots=True)
class TaskLoopAdvanceResult:
    command: TaskLoopReducerCommand
    read: TaskLoopExecutionRead


class TaskLoopExecutionCoordinator:
    """Advance one durable Task Loop by exactly one reducer command."""

    def __init__(
        self,
        database: Database,
        activation: TaskLoopActivationRuntime,
        *,
        capabilities: CapabilityExecutionRuntime | None = None,
        agents: TaskLoopAgentRuntime | None = None,
        artifacts: ArtifactDeliveryRuntime | None = None,
        turn_planner: TurnPlannerRuntime | None = None,
        reducer: TaskLoopReducer | None = None,
    ) -> None:
        self._database = database
        self._activation = activation
        self._capabilities = capabilities
        self._agents = agents
        self._artifacts = artifacts
        self._turn_planner = turn_planner
        self._reducer = reducer or TaskLoopReducer()

    async def advance(self, task_id: str, owner_id: str) -> TaskLoopAdvanceResult:
        """Revalidate, decide, and execute one bounded command."""

        before = await self._required_read(task_id)
        snapshot = self._snapshot(before)
        command = self._reducer.decide(snapshot)

        if command.kind == "activate_plan":
            await self._activation.activate(task_id)
        elif command.kind == "execute_capability":
            await self._execute_capability(task_id, owner_id, command)
        elif command.kind == "execute_agent":
            await self._execute_agent(before, owner_id, command)
        elif command.kind == "verify_candidate":
            await self._verify_candidate(before, task_id, owner_id, command)
        elif command.kind == "reduce_control_node":
            await self._reduce_control(before, command)
        elif command.kind in {
            "terminate_failure",
            "terminate_budget_exhausted",
            "terminate_no_progress",
            "record_no_progress",
        }:
            await self._fail_execution(before, command.reason_code)
        elif command.kind == "terminate_success":
            await self._transition_terminal(before, status="succeeded")
        elif command.kind in {"noop", "wait_user"}:
            pass
        else:  # pragma: no cover - the closed Literal is a defensive boundary.
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Reducer returned an unsupported command"
            )

        return TaskLoopAdvanceResult(
            command=command,
            read=await self._required_read(task_id),
        )

    async def _execute_capability(
        self,
        task_id: str,
        owner_id: str,
        command: TaskLoopReducerCommand,
    ) -> None:
        if self._capabilities is None:
            raise TaskLoopExecutionCoordinatorUnavailableError(
                "Capability Task Loop runtime is unavailable"
            )
        outcome = await self._capabilities.run_once(task_id, owner_id)
        if outcome is None or outcome.node_id != command.node_id:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Capability runtime did not claim the reducer-selected node"
            )

    async def _execute_agent(
        self,
        read: TaskLoopExecutionRead,
        owner_id: str,
        command: TaskLoopReducerCommand,
    ) -> None:
        execution = self._required_execution(read)
        if self._agents is None:
            raise TaskLoopExecutionCoordinatorUnavailableError(
                "Agent Task Loop runtime is unavailable"
            )
        source = await self._agents.claim_next(execution.execution_id, owner_id)
        if source is None or source.binding.composite_node_id != command.node_id:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Agent runtime did not claim the reducer-selected node"
            )
        if source.route_id == "research_to_html":
            await self._agents.run_research(source)
        elif source.route_id == "workspace_file_read":
            await self._agents.run_workspace_file_candidate(source)
        else:  # pragma: no cover - registry and dataclass Literals are closed.
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Agent adapter returned an unsupported source Route"
            )

    async def _verify_candidate(
        self,
        read: TaskLoopExecutionRead,
        task_id: str,
        owner_id: str,
        command: TaskLoopReducerCommand,
    ) -> None:
        node = next((item for item in read.nodes if item.node_id == command.node_id), None)
        if node is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Reducer verification target disappeared"
            )
        if node.kind is DraftNodeKind.CAPABILITY:
            await self._execute_capability(task_id, owner_id, command)
            return
        if node.kind is not DraftNodeKind.AGENT or self._agents is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Only Agent or Capability nodes may expose candidates"
            )
        execution = self._required_execution(read)
        source = await self._agents.recover_pending(execution.execution_id)
        if source is None or source.binding.composite_node_id != command.node_id:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Persisted Agent candidate differs from the reducer target"
            )
        proof = await self._source_plan_proof(task_id, source)
        if source.route_id == "research_to_html":
            if self._artifacts is None:
                raise TaskLoopExecutionCoordinatorUnavailableError(
                    "Independent Research verifier is unavailable"
                )
            verification = await self._artifacts.verify_research_node(
                execution.run_id,
                node_id=source.binding.composite_node_id,
                defer_task_loop_edge=True,
            )
            if verification.outcome != "verified":
                await self._agents.settle_rejected(
                    source,
                    error_code="RESEARCH_CLAIMS_REJECTED",
                )
                return
        await self._agents.persist_verified_result(source, proof)

    async def _source_plan_proof(
        self,
        task_id: str,
        source: SourceBoundAgentClaim,
    ) -> AgentSourcePlanProof:
        if self._turn_planner is None:
            raise TaskLoopExecutionCoordinatorUnavailableError(
                "Turn Planner proof runtime is unavailable"
            )
        deferred = await self._turn_planner.revalidate_deferred_plan(task_id)
        ordinal = source.binding.step_ordinal
        if not 1 <= ordinal <= len(deferred.steps):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Agent source step ordinal changed"
            )
        step = deferred.steps[ordinal - 1]
        if (
            step.offer.offer_id != source.binding.offer_id
            or step.offer.offer_key != source.binding.offer_key
            or step.offer.offer_digest != source.binding.offer_digest
            or step.offer.trusted_recipe != source.binding.recipe
            or step.offer.expected_plan.plan_id != source.binding.source_plan_id
            or step.offer.expected_plan.plan_manifest_digest
            != source.binding.source_plan_manifest_digest
            or step.route.contract.digest != source.binding.source_contract_digest
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Agent source Plan proof changed after activation"
            )
        return AgentSourcePlanProof(
            source_contract=step.route.contract,
            source_plan=step.offer.expected_plan,
        )

    async def _reduce_control(
        self,
        read: TaskLoopExecutionRead,
        command: TaskLoopReducerCommand,
    ) -> None:
        execution = self._required_execution(read)
        node = next((item for item in read.nodes if item.node_id == command.node_id), None)
        if node is None or node.kind in {DraftNodeKind.AGENT, DraftNodeKind.CAPABILITY}:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Reducer control target is not a control node"
        )
        if node.local_key == "final_acceptance":
            try:
                result_kinds = await self._preflight_final_results(execution)
            except TaskLoopFinalAcceptanceRejectedError:
                await self._fail_execution(read, "FINAL_ACCEPTANCE_REJECTED")
                return
            artifact_kinds = {
                CapabilityResultKind.VERIFIED_CLAIMS.value,
                CapabilityResultKind.ARTIFACT.value,
                CapabilityResultKind.BROWSER_VERIFICATION.value,
            }
            if result_kinds & artifact_kinds:
                if not artifact_kinds.issubset(result_kinds) or self._artifacts is None:
                    await self._fail_execution(read, "ARTIFACT_RESULT_SET_INCOMPLETE")
                    return
                try:
                    await self._artifacts.finalize(execution.run_id)
                except ArtifactDeliveryError:
                    await self._fail_execution(read, "FINAL_ACCEPTANCE_REJECTED")
                    return
                await self._transition_terminal(read, status="succeeded")
                return
            await self._verify_control_node(execution, node.node_id)
            return
        if node.local_key == "delivery":
            await self._complete_generic_delivery(execution, node.node_id)
            return
        raise TaskLoopExecutionCoordinatorProofRejectedError(
            "Unknown server control node local key"
        )

    async def _preflight_final_results(self, execution: TaskLoopExecution) -> set[str]:
        async with self._database.session() as session:
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).where(
                            TaskExecutionNodeRecord.run_id == execution.run_id
                        )
                    )
                ).all()
            )
            runnable = {
                item.node_id
                for item in nodes
                if item.node_kind
                in {DraftNodeKind.AGENT.value, DraftNodeKind.CAPABILITY.value}
            }
            records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopVerifiedResultRecord).where(
                            TaskLoopVerifiedResultRecord.execution_id
                            == execution.execution_id
                        )
                    )
                ).all()
            )
        try:
            results = tuple(
                self._verified_result_from_record(item) for item in records
            )
        except ValidationError as error:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Final ResultRef Schema proof was rejected"
            ) from error
        if (
            {item.node_id for item in results} != runnable
            or any(item.run_id != execution.run_id for item in results)
            or any(item.status != "verified" for item in nodes if item.node_id in runnable)
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Final acceptance requires one verified ResultRef per runnable node"
            )
        for result in results:
            self._assert_success_semantics(result)
        return {item.result_kind for item in results}

    @staticmethod
    def _verified_result_from_record(
        record: TaskLoopVerifiedResultRecord,
    ) -> TaskLoopVerifiedResult:
        created_at = record.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        values = {
            "schema_version": "deskpilot.task-loop-verified-result.v1",
            **{
                field: getattr(record, field)
                for field in (
                    "result_ref_id",
                    "attempt_id",
                    "execution_id",
                    "node_binding_id",
                    "node_binding_digest",
                    "run_id",
                    "node_id",
                    "producer_kind",
                    "capability_manifest",
                    "capability_digest",
                    "agent_binding_manifest",
                    "agent_binding_digest",
                    "executor_manifest_digest",
                    "agent_result_proof_digest",
                    "input_binding_digest",
                    "context_digest",
                    "candidate_digest",
                    "result_kind",
                    "output_manifest",
                    "output_schema_digest",
                    "output_digest",
                    "verification_manifest",
                    "verification_digest",
                    "result_ref_manifest",
                    "result_ref_digest",
                )
            },
            "created_at": created_at,
        }
        return TaskLoopVerifiedResult.model_validate(values)

    @staticmethod
    def _assert_success_semantics(result: TaskLoopVerifiedResult) -> None:
        output = result.output_manifest
        if result.result_kind in {
            CapabilityResultKind.WORKSPACE_CHECK.value,
            CapabilityResultKind.PYTHON_TEST.value,
            CapabilityResultKind.NODE_TEST.value,
        } and output.get("status") != "passed":
            raise TaskLoopFinalAcceptanceRejectedError(
                "Verified check evidence does not satisfy final acceptance"
            )
        if result.result_kind == CapabilityResultKind.ARTIFACT.value:
            workspace = output.get("workspace")
            if not isinstance(workspace, dict) or workspace.get("status") != "active":
                raise TaskLoopFinalAcceptanceRejectedError(
                    "Artifact ResultRef has no active Workspace"
                )
        if result.result_kind == CapabilityResultKind.BROWSER_VERIFICATION.value:
            browser = output.get("browser")
            if (
                not isinstance(browser, dict)
                or browser.get("status") != "passed"
                or browser.get("external_request_count") != 0
            ):
                raise TaskLoopFinalAcceptanceRejectedError(
                    "Browser ResultRef failed its offline acceptance invariant"
                )

    async def _verify_control_node(
        self,
        execution: TaskLoopExecution,
        node_id: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            record = await self._locked_execution(session, execution)
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == execution.run_id)
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == node_id)
                .with_for_update()
            )
            if record.status != "active" or run is None or node is None or node.status != "ready":
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Control-node state changed before reduction"
                )
            await mark_verified_and_unlock(session, run, node)

    async def _complete_generic_delivery(
        self,
        execution: TaskLoopExecution,
        node_id: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            record = await self._locked_execution(session, execution)
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == execution.run_id)
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == node_id)
                .with_for_update()
            )
            task = await session.scalar(
                select(TaskRecord)
                .where(TaskRecord.task_id == execution.task_id)
                .with_for_update()
            )
            if (
                record.status != "active"
                or run is None
                or node is None
                or task is None
                or node.local_key != "delivery"
                or node.status != "ready"
            ):
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Delivery control state changed before completion"
                )
            now = utc_now()
            node.status = "verified"
            node.revision += 1
            node.updated_at = now
            run.status = "succeeded"
            run.revision += 1
            run.updated_at = now
            task.status = "succeeded"
            task.updated_at = now
            await self._append_transition(
                session,
                record,
                status="succeeded",
                kind="succeeded",
                now=now,
            )

    async def _fail_execution(
        self,
        read: TaskLoopExecutionRead,
        reason_code: str,
    ) -> None:
        del reason_code  # The immutable node/attempt proof retains the detailed failure.
        execution = self._required_execution(read)
        async with self._database.session() as session, session.begin():
            record = await self._locked_execution(session, execution)
            if record.status == "failed":
                return
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == execution.run_id)
                .with_for_update()
            )
            task = await session.scalar(
                select(TaskRecord)
                .where(TaskRecord.task_id == execution.task_id)
                .with_for_update()
            )
            if run is None or task is None:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Task Loop terminal scope disappeared"
                )
            now = utc_now()
            run.status = "failed"
            run.revision += 1
            run.updated_at = now
            task.status = "failed"
            task.updated_at = now
            await self._append_transition(
                session,
                record,
                status="failed",
                kind="failed",
                now=now,
            )

    async def _transition_terminal(
        self,
        read: TaskLoopExecutionRead,
        *,
        status: Literal["failed", "succeeded"],
    ) -> None:
        execution = self._required_execution(read)
        async with self._database.session() as session, session.begin():
            record = await self._locked_execution(session, execution)
            if record.status == status:
                return
            await self._append_transition(
                session,
                record,
                status=status,
                kind=status,
                now=utc_now(),
            )

    @staticmethod
    async def _locked_execution(
        session: AsyncSession,
        execution: TaskLoopExecution,
    ) -> TaskLoopExecutionRecord:
        record = await session.scalar(
            select(TaskLoopExecutionRecord)
            .where(TaskLoopExecutionRecord.execution_id == execution.execution_id)
            .with_for_update()
        )
        if record is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task Loop execution disappeared"
            )
        try:
            current = TaskLoopExecution.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task Loop execution manifest was rejected"
            ) from error
        if current != execution or record.execution_digest != execution.execution_digest:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task Loop execution changed after reducer decision"
            )
        return record

    @staticmethod
    async def _append_transition(
        session: AsyncSession,
        record: TaskLoopExecutionRecord,
        *,
        status: Literal["failed", "succeeded"],
        kind: Literal["failed", "succeeded"],
        now: datetime,
    ) -> None:
        current = TaskLoopExecution.model_validate(record.manifest)
        effective_now = max(now, current.updated_at)
        transitioned, event = current.transition(
            status=status,
            kind=kind,
            updated_at=effective_now,
        )
        record.status = transitioned.status
        record.revision = transitioned.revision
        record.event_count = transitioned.event_count
        record.latest_event_id = transitioned.latest_event_id
        record.latest_event_digest = transitioned.latest_event_digest
        record.manifest = transitioned.model_dump(mode="json")
        record.execution_digest = transitioned.execution_digest
        record.updated_at = transitioned.updated_at
        session.add(
            TaskLoopExecutionEventRecord(
                event_id=event.event_id,
                execution_id=event.execution_id,
                task_id=event.task_id,
                sequence=event.sequence,
                previous_event_digest=event.previous_event_digest,
                kind=event.kind,
                plan_manifest_digest=event.plan_manifest_digest,
                run_id=event.run_id,
                binding_set_digest=event.binding_set_digest,
                manifest=event.model_dump(mode="json"),
                event_digest=event.event_digest,
                created_at=event.created_at,
            )
        )

    async def _required_read(self, task_id: str) -> TaskLoopExecutionRead:
        read = await self._activation.get(task_id)
        if read is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task has no persistent model-planner Task Loop"
            )
        return read

    @staticmethod
    def _required_execution(read: TaskLoopExecutionRead) -> TaskLoopExecution:
        if read.execution is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task Loop has no active execution"
            )
        return read.execution

    @staticmethod
    def _snapshot(read: TaskLoopExecutionRead) -> TaskLoopReducerSnapshot:
        execution = read.execution
        if execution is None:
            return TaskLoopReducerSnapshot.build(
                task_id=read.task_id,
                execution_id=None,
                execution_status="planned",
                execution_revision=0,
            )
        nodes = tuple(
            TaskLoopReducerNode(
                node_id=node.node_id,
                local_key=node.local_key,
                channel=(
                    "agent"
                    if node.kind is DraftNodeKind.AGENT
                    else (
                        "capability"
                        if node.kind is DraftNodeKind.CAPABILITY
                        else "control"
                    )
                ),
                status=node.status,
                depends_on=tuple(sorted(node.depends_on)),
                verified_dependency_node_ids=tuple(
                    sorted(node.verified_dependency_node_ids)
                ),
                candidate_present=node.candidate_present,
                verified_result_present=node.verified_result_present,
                attempt_count=node.attempt_count,
                max_attempts=node.max_attempts,
            )
            for node in read.nodes
        )
        return TaskLoopReducerSnapshot.build(
            task_id=read.task_id,
            execution_id=execution.execution_id,
            execution_status=execution.status,
            execution_revision=execution.revision,
            nodes=nodes,
            active_claim_count=sum(
                item.status in {"claimed", "running"} for item in nodes
            ),
            pending_user_revision=(
                execution.revision if execution.status == "awaiting_user" else None
            ),
        )


__all__ = [
    "TaskLoopAdvanceResult",
    "TaskLoopExecutionCoordinator",
    "TaskLoopExecutionCoordinatorError",
    "TaskLoopExecutionCoordinatorProofRejectedError",
    "TaskLoopExecutionCoordinatorUnavailableError",
    "TaskLoopFinalAcceptanceRejectedError",
]
