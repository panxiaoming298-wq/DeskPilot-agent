"""One-step coordinator for persistent model-planner Task Loops.

The coordinator never accepts model-authored arguments.  It reduces only a
proof-checked projection, then delegates one exact server-bound Agent,
Capability, or control transition.  Every subsequent call reconstructs the
execution from durable state, which makes process restart the normal recovery
path rather than a reason to replay a Provider call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.artifact_delivery_runtime import (
    ArtifactDeliveryError,
    ArtifactDeliveryRuntime,
)
from deskpilot.application.builtin_capability_executors import (
    WorkspacePatchCapabilityOutput,
)
from deskpilot.application.capability_execution_runtime import (
    CapabilityExecutionRuntime,
    CapabilityExecutionRuntimeError,
)
from deskpilot.application.capability_input_binding_catalog import (
    BoundCapabilityInput,
    WorkspacePatchBundleExecutorInput,
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
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import CapabilityResultKind
from deskpilot.domain.task_loop_cycle import TaskLoopCycleEvent, TaskLoopCycleEventKind
from deskpilot.domain.task_loop_execution import (
    TaskLoopExecution,
    TaskLoopExecutionEventKind,
    TaskLoopExecutionRead,
    TaskLoopExecutionStatus,
    TaskLoopVerifiedResult,
)
from deskpilot.domain.task_plans import DraftNodeKind, PlanNodeBudget
from deskpilot.domain.workspace_coding_amendments import (
    WorkspaceCodingAmendmentBinding,
)
from deskpilot.domain.workspace_files import (
    WorkspaceFileRead,
    WorkspaceNodeTestRead,
    WorkspacePatchPreview,
    WorkspacePythonTestRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentInvocationRecord,
    AgentModelTurnRecord,
    ModelPlannerNodeBindingRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopCapabilityApprovalRecord,
    TaskLoopCycleEventRecord,
    TaskLoopExecutionEventRecord,
    TaskLoopExecutionRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskRecord,
    WorkspaceCodingDeliveryRecord,
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

    async def cancel_for_amendment(
        self,
        task_id: str,
    ) -> TaskLoopExecutionRead | None:
        """Seal one recoverable TaskLoop before a same-conversation replacement."""

        return await self._activation.cancel_for_amendment(task_id)

    async def bind_conversation_amendment(
        self,
        source_task_id: str,
        successor_task_id: str,
    ) -> WorkspaceCodingAmendmentBinding:
        """Persist the exact old-generation to successor-turn lineage."""

        return await self._activation.bind_conversation_amendment(
            source_task_id,
            successor_task_id,
        )

    async def advance(self, task_id: str, owner_id: str) -> TaskLoopAdvanceResult:
        """Revalidate, decide, and execute one bounded command."""

        if self._capabilities is not None:
            try:
                await self._capabilities.recover_expired(task_id)
            except CapabilityExecutionRuntimeError as error:
                raise TaskLoopExecutionCoordinatorProofRejectedError(str(error)) from error
        await self._reconcile_incomplete_repair(task_id)
        before = await self._required_read(task_id)
        snapshot = await self._snapshot(before)
        command = self._reducer.decide(snapshot)

        if command.kind == "activate_plan":
            await self._activation.activate(task_id)
        elif command.kind == "execute_capability":
            await self._execute_capability(task_id, owner_id, command)
        elif command.kind == "execute_agent":
            await self._execute_agent(before, owner_id, command)
        elif command.kind == "execute_agent_batch":
            await self._execute_agent_batch(before, owner_id, command)
        elif command.kind == "verify_candidate":
            await self._verify_candidate(before, task_id, owner_id, command)
        elif command.kind == "reduce_control_node":
            await self._reduce_control(before, command)
        elif command.kind == "record_no_progress":
            await self._record_no_progress(before, command)
        elif command.kind == "start_repair":
            await self._repair_failed_node(before, command)
        elif command.kind == "terminate_no_progress":
            await self._terminate_with_cycle_event(
                before,
                command,
                kind="no_progress_terminated",
            )
        elif command.kind == "terminate_budget_exhausted":
            await self._terminate_with_cycle_event(
                before,
                command,
                kind="budget_exhausted",
            )
        elif command.kind == "terminate_failure":
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

    async def approve_workspace_patch(
        self,
        task_id: str,
        confirmation_digest: str,
        *,
        expected_execution_revision: int,
    ) -> WorkspacePatchPreview:
        if self._capabilities is None:
            raise TaskLoopExecutionCoordinatorUnavailableError(
                "Capability Task Loop runtime is unavailable"
            )
        try:
            return await self._capabilities.approve_workspace_patch(
                task_id,
                confirmation_digest,
                expected_execution_revision=expected_execution_revision,
            )
        except CapabilityExecutionRuntimeError as error:
            raise TaskLoopExecutionCoordinatorProofRejectedError(str(error)) from error

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
        source = await self._agents.claim_next(
            execution.execution_id,
            owner_id,
            node_id=command.node_id,
        )
        if source is None or source.binding.composite_node_id != command.node_id:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Agent runtime did not claim the reducer-selected node"
            )
        await self._run_agent_candidate(source)

    async def _execute_agent_batch(
        self,
        read: TaskLoopExecutionRead,
        owner_id: str,
        command: TaskLoopReducerCommand,
    ) -> None:
        execution = self._required_execution(read)
        if self._agents is None or len(command.node_ids) != 2:
            raise TaskLoopExecutionCoordinatorUnavailableError(
                "Parallel Agent Task Loop runtime is unavailable"
            )
        selected = {
            item.node_id: item
            for item in read.nodes
            if item.node_id in set(command.node_ids)
        }
        if (
            set(selected) != set(command.node_ids)
            or any(
                item.kind is not DraftNodeKind.AGENT
                or item.status != "ready"
                or not item.dependencies_verified
                for item in selected.values()
            )
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Parallel Agent batch changed after reducer selection"
            )
        claims = await self._agents.claim_batch(
            execution.execution_id,
            owner_id,
            command.node_ids,
        )
        await asyncio.gather(
            *(self._run_agent_candidate(source) for source in claims)
        )

    async def _run_agent_candidate(self, source: SourceBoundAgentClaim) -> None:
        if self._agents is None:
            raise TaskLoopExecutionCoordinatorUnavailableError(
                "Agent Task Loop runtime is unavailable"
            )
        if source.route_id == "research_to_html":
            await self._agents.run_research(source)
        elif source.route_id == "workspace_file_read":
            await self._agents.run_workspace_file_candidate(source)
        elif source.route_id == "workspace_coding_loop":
            if source.binding.mapping.source_local_key == "coordinate_coding":
                await self._agents.run_coding_coordinator_candidate(source)
            elif source.binding.mapping.source_local_key in {
                "inspect_primary",
                "inspect_secondary",
            }:
                await self._agents.run_workspace_file_candidate(source)
            elif source.binding.mapping.source_local_key in {
                "plan_primary_patch",
                "plan_secondary_patch",
            }:
                await self._agents.run_patch_planner_candidate(source)
            else:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Coding Agent node is not registered by the fixed DAG"
                )
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
        source = await self._agents.recover_pending(
            execution.execution_id,
            node_id=command.node_id,
        )
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
        deferred = await self._turn_planner.revalidate_task_loop_plan(task_id)
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
        accepted_results = tuple(
            item
            for item in results
            if not (
                item.result_kind
                in {
                    CapabilityResultKind.WORKSPACE_CHECK.value,
                    CapabilityResultKind.PYTHON_TEST.value,
                    CapabilityResultKind.NODE_TEST.value,
                    CapabilityResultKind.COMMAND_PROFILE.value,
                }
                and item.output_manifest.get("status") != "passed"
            )
        )
        if (
            len({item.node_id for item in accepted_results}) != len(accepted_results)
            or {item.node_id for item in accepted_results} != runnable
            or any(item.run_id != execution.run_id for item in results)
            or any(item.status != "verified" for item in nodes if item.node_id in runnable)
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Final acceptance requires one verified ResultRef per runnable node"
            )
        for result in accepted_results:
            self._assert_success_semantics(result)
        return {item.result_kind for item in accepted_results}

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
            CapabilityResultKind.COMMAND_PROFILE.value,
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
            await self._persist_workspace_coding_delivery(
                session,
                execution,
                now=now,
            )
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

    async def _persist_workspace_coding_delivery(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        *,
        now: datetime,
    ) -> None:
        bindings = tuple(
            (
                await session.scalars(
                    select(ModelPlannerNodeBindingRecord).where(
                        ModelPlannerNodeBindingRecord.execution_id
                        == execution.execution_id
                    )
                )
            ).all()
        )
        coding_bindings = tuple(
            item
            for item in bindings
            if item.recipe_manifest.get("route_id") == "workspace_coding_loop"
        )
        if not coding_bindings:
            return
        if len(coding_bindings) != 7 or len(coding_bindings) != len(bindings):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery has an incomplete Route binding set"
            )
        result_records = tuple(
            (
                await session.scalars(
                    select(TaskLoopVerifiedResultRecord)
                    .where(
                        TaskLoopVerifiedResultRecord.execution_id
                        == execution.execution_id
                    )
                    .order_by(
                        TaskLoopVerifiedResultRecord.node_id,
                        TaskLoopVerifiedResultRecord.created_at,
                    )
                )
            ).all()
        )
        try:
            results = tuple(
                self._verified_result_from_record(item) for item in result_records
            )
        except ValidationError as error:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery ResultRefs are invalid"
            ) from error
        attempts = {
            item.attempt_id: item
            for item in (
                await session.scalars(
                    select(TaskLoopNodeAttemptRecord).where(
                        TaskLoopNodeAttemptRecord.execution_id
                        == execution.execution_id
                    )
                )
            ).all()
        }
        if any(item.attempt_id not in attempts for item in results):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery lost a producer attempt"
            )

        reader_results = tuple(
            item
            for item in results
            if item.result_kind == CapabilityResultKind.WORKSPACE_FILE.value
        )
        coordinator_results = tuple(
            item
            for item in results
            if item.result_kind == CapabilityResultKind.COORDINATION_PLAN.value
        )
        patch_results = tuple(
            item
            for item in results
            if item.result_kind == CapabilityResultKind.PATCH_RECEIPT.value
        )
        proposal_results = tuple(
            item
            for item in results
            if item.result_kind == CapabilityResultKind.PATCH_PROPOSAL.value
        )
        test_results = tuple(
            item
            for item in results
            if item.result_kind
            in {
                CapabilityResultKind.PYTHON_TEST.value,
                CapabilityResultKind.NODE_TEST.value,
            }
        )
        if (
            len(coordinator_results) != 1
            or len(reader_results) != 2
            or len(proposal_results) != 2
            or len(patch_results) != 1
            or not 1 <= len(test_results) <= 2
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery evidence cardinality changed"
            )
        coordination_result = coordinator_results[0]
        coordination_attempt = attempts[coordination_result.attempt_id]
        coordination_output = coordination_result.output_manifest
        coordination_verification = coordination_result.verification_manifest
        raw_graph_nodes = coordination_output.get("nodes")
        output_node_key = coordination_output.get("output_node_key")
        graph_digest = coordination_output.get("graph_digest")
        coordinator_agent = coordination_result.agent_binding_manifest or {}
        coordinator_binding = next(
            (
                item
                for item in coding_bindings
                if item.mapping_manifest.get("source_local_key")
                == "coordinate_coding"
            ),
            None,
        )
        reader_bindings = tuple(
            item
            for item in coding_bindings
            if item.mapping_manifest.get("source_local_key")
            in {"inspect_primary", "inspect_secondary"}
        )
        reader_nodes = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord).where(
                        TaskExecutionNodeRecord.node_id.in_(
                            [item.composite_node_id for item in reader_bindings]
                        )
                    )
                )
            ).all()
        )
        if (
            coordination_attempt.status != "verified"
            or coordinator_binding is None
            or len(reader_bindings) != 2
            or len(reader_nodes) != 2
            or any(
                item.depends_on != [coordinator_binding.composite_node_id]
                for item in reader_nodes
            )
            or not isinstance(raw_graph_nodes, list)
            or len(raw_graph_nodes) != 6
            or output_node_key != "run_fixed_test"
            or not isinstance(graph_digest, str)
            or graph_digest
            != sha256_digest(
                {
                    "nodes": raw_graph_nodes,
                    "output_node_key": output_node_key,
                }
            )
            or coordination_verification.get("graph_digest") != graph_digest
            or not isinstance(coordination_verification.get("decision_digest"), str)
            or coordinator_agent.get("agent_id")
            != "builtin.workspace_coordinator"
            or coordinator_agent.get("version") != "1.1.0"
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding Coordinator graph proof changed"
            )
        coordinator_evidence = {
            "agent_id": "builtin.workspace_coordinator",
            "agent_version": "1.1.0",
            "node_count": len(raw_graph_nodes),
            "output_node_key": output_node_key,
            "graph_digest": graph_digest,
            "decision_digest": coordination_verification["decision_digest"],
            "result_ref_digest": coordination_result.result_ref_digest,
            "verification_digest": coordination_result.verification_digest,
        }
        try:
            readers = tuple(
                WorkspaceFileRead.model_validate(item.output_manifest)
                for item in reader_results
            )
            patch_result = patch_results[0]
            patch_output = WorkspacePatchCapabilityOutput.model_validate(
                patch_result.output_manifest
            )
            patch_attempt = attempts[patch_result.attempt_id]
            bound_patch = BoundCapabilityInput.model_validate(
                patch_attempt.input_manifest
            )
        except (KeyError, ValidationError) as error:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery evidence Schema changed"
            ) from error
        if (
            patch_output.receipt.status != "committed"
            or not isinstance(
                bound_patch.arguments,
                WorkspacePatchBundleExecutorInput,
            )
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery has no committed exact Patch"
            )
        changes = tuple(bound_patch.arguments.changes)
        change_receipts = {
            item.relative_path: item for item in patch_output.receipt.change_receipts
        }
        changed_paths = {item.path for item in changes}
        proposal_evidence: list[dict[str, Any]] = []
        proposal_changes: dict[str, tuple[str, str]] = {}
        for proposal_result in proposal_results:
            proposal_attempt = attempts[proposal_result.attempt_id]
            raw_changes = proposal_result.output_manifest.get("changes")
            verification = proposal_result.verification_manifest
            if (
                proposal_attempt.status != "verified"
                or not isinstance(raw_changes, list)
                or len(raw_changes) != 1
                or not isinstance(raw_changes[0], dict)
                or not all(
                    isinstance(raw_changes[0].get(name), str)
                    for name in ("path", "old_text", "new_text", "rationale")
                )
                or not isinstance(verification.get("model_turn_id"), str)
                or not isinstance(verification.get("decision_id"), str)
                or not isinstance(verification.get("decision_digest"), str)
            ):
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Workspace coding delivery Patch Planner proof changed"
                )
            proposed = raw_changes[0]
            path = str(proposed["path"])
            if path in proposal_changes:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Workspace coding delivery repeats a Patch Planner path"
                )
            proposal_changes[path] = (
                str(proposed["old_text"]),
                str(proposed["new_text"]),
            )
            proposal_evidence.append(
                {
                    "path": path,
                    "agent_id": "builtin.workspace_patch_planner",
                    "agent_version": (
                        proposal_result.agent_binding_manifest or {}
                    ).get("version"),
                    "model_turn_id": verification["model_turn_id"],
                    "decision_id": verification["decision_id"],
                    "decision_digest": verification["decision_digest"],
                    "result_ref_digest": proposal_result.result_ref_digest,
                    "verification_digest": proposal_result.verification_digest,
                }
            )
        expected_proposals = {
            item.path: (item.old_text, item.new_text) for item in changes
        }
        consumed_proposal_refs = {
            item.result_ref_digest
            for item in bound_patch.dependency_result_refs
            if item.result_kind is CapabilityResultKind.PATCH_PROPOSAL
        }
        if (
            len(changes) != 2
            or len(change_receipts) != 2
            or set(change_receipts) != changed_paths
            or {item.relative_path for item in readers} != changed_paths
            or proposal_changes != expected_proposals
            or consumed_proposal_refs
            != {item.result_ref_digest for item in proposal_results}
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery Patch differs from Reader scope"
            )

        test_evidence: list[dict[str, Any]] = []
        failure_history: list[dict[str, Any]] = []
        passed_count = 0
        for result in test_results:
            try:
                output = (
                    WorkspacePythonTestRead.model_validate(result.output_manifest)
                    if result.result_kind == CapabilityResultKind.PYTHON_TEST.value
                    else WorkspaceNodeTestRead.model_validate(result.output_manifest)
                )
            except ValidationError as error:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Workspace coding delivery test evidence changed"
                ) from error
            attempt = attempts[result.attempt_id]
            test_evidence.append(
                {
                    "result_kind": result.result_kind,
                    "attempt": attempt.attempt,
                    "project_path": output.project_path,
                    "test_path": output.test_path,
                    "status": output.status,
                    "exit_code": output.exit_code,
                    "result_ref_digest": result.result_ref_digest,
                    "result_digest": result.output_digest,
                    "verification_digest": result.verification_digest,
                }
            )
            if output.status == "passed":
                passed_count += 1
                if attempt.status != "verified":
                    raise TaskLoopExecutionCoordinatorProofRejectedError(
                        "Passed workspace test is not edge eligible"
                    )
            else:
                if attempt.status != "failed" or attempt.receipt_digest is None:
                    raise TaskLoopExecutionCoordinatorProofRejectedError(
                        "Failed workspace test lost its immutable failure receipt"
                    )
                failure_history.append(
                    {
                        "attempt": attempt.attempt,
                        "status": output.status,
                        "result_ref_digest": result.result_ref_digest,
                        "failure_receipt_digest": attempt.receipt_digest,
                    }
                )
        if passed_count != 1 or len(failure_history) > 1:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Workspace coding delivery has no unique successful fixed test"
            )

        structured_diff = [
            {
                "path": item.path,
                "old_text": item.old_text,
                "new_text": item.new_text,
            }
            for item in sorted(changes, key=lambda item: item.path)
        ]
        rollback_points = [
            {
                "path": path,
                "backup_relative_path": change_receipts[path].backup_relative_path,
                "previous_version_digest": (
                    change_receipts[path].previous_version_digest
                ),
                "version_digest": change_receipts[path].version_digest,
                "receipt_digest": change_receipts[path].receipt_digest,
            }
            for path in sorted(changed_paths)
        ]
        reader_evidence = [
            {
                "path": reader.relative_path,
                "version_digest": reader.version_digest,
                "content_digest": reader.content_digest,
                "result_ref_digest": next(
                    item.result_ref_digest
                    for item in reader_results
                    if item.output_digest == reader.result_digest
                ),
            }
            for reader in sorted(readers, key=lambda item: item.relative_path)
        ]
        delivery_material: dict[str, Any] = {
            "schema_version": "deskpilot.workspace-coding-delivery.v1",
            "task_id": execution.task_id,
            "execution_id": execution.execution_id,
            "run_id": execution.run_id,
            "plan_id": execution.plan_id,
            "plan_generation": execution.plan_generation,
            "plan_manifest_digest": execution.plan_manifest_digest,
            "reader_evidence": reader_evidence,
            "coordinator_evidence": coordinator_evidence,
            "patch_planner_evidence": sorted(
                proposal_evidence,
                key=lambda item: str(item["path"]),
            ),
            "structured_diff": structured_diff,
            "diff_digest": sha256_digest({"changes": structured_diff}),
            "patch_result_ref_digest": patch_result.result_ref_digest,
            "patch_receipt_digest": patch_output.receipt.receipt_digest,
            "changed_files": sorted(changed_paths),
            "test_runs": sorted(test_evidence, key=lambda item: int(item["attempt"])),
            "failure_repair_history": failure_history,
            "remaining_risks": [
                "local_fake_model_quality_unproven",
                "git_commit_not_created",
            ],
            "rollback_points": rollback_points,
            "rollback_available": all(
                item["backup_relative_path"] is not None for item in rollback_points
            ),
            "created_at": now.isoformat(),
        }
        delivery_id = f"wcd_{sha256_digest(delivery_material)}"
        manifest = {**delivery_material, "delivery_id": delivery_id}
        delivery_digest = sha256_digest(manifest)
        existing = await session.scalar(
            select(WorkspaceCodingDeliveryRecord)
            .where(
                WorkspaceCodingDeliveryRecord.execution_id
                == execution.execution_id
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.delivery_id != delivery_id
                or existing.manifest != manifest
                or existing.delivery_digest != delivery_digest
            ):
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Workspace coding delivery changed after persistence"
                )
            return
        session.add(
            WorkspaceCodingDeliveryRecord(
                delivery_id=delivery_id,
                execution_id=execution.execution_id,
                task_id=execution.task_id,
                run_id=execution.run_id,
                plan_id=execution.plan_id,
                plan_manifest_digest=execution.plan_manifest_digest,
                changed_file_count=len(changed_paths),
                test_run_count=len(test_results),
                failure_count=len(failure_history),
                rollback_available=bool(delivery_material["rollback_available"]),
                manifest=manifest,
                delivery_digest=delivery_digest,
                created_at=now,
            )
        )
        await session.flush()

    async def _record_no_progress(
        self,
        read: TaskLoopExecutionRead,
        command: TaskLoopReducerCommand,
    ) -> None:
        """Append one fenced observation for the exact unchanged projection."""

        await self._record_cycle_event(
            read,
            command,
            kind="no_progress_observed",
        )

    async def _repair_failed_node(
        self,
        read: TaskLoopExecutionRead,
        command: TaskLoopReducerCommand,
    ) -> None:
        """Fence one exact retry without changing the sealed Plan or authority."""

        if command.node_id is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Repair command has no exact failed node"
            )
        await self._record_cycle_event(read, command, kind="repair_started")
        await self._apply_bounded_repair(read.task_id, command.node_id)
        await self._complete_repair_cycle(read.task_id, command.node_id)

    async def _reconcile_incomplete_repair(self, task_id: str) -> None:
        """Finish a durable repair marker after a process restart."""

        read = await self._activation.get(task_id)
        if read is None or read.execution is None or read.cycle is None:
            return
        if read.cycle.latest_event_kind != "repair_started":
            return
        async with self._database.session() as session:
            record = await session.scalar(
                select(TaskLoopCycleEventRecord)
                .where(
                    TaskLoopCycleEventRecord.execution_id
                    == read.execution.execution_id,
                    TaskLoopCycleEventRecord.sequence
                    == read.cycle.latest_event_sequence,
                )
            )
        if record is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Repair cycle marker disappeared"
            )
        event = self._cycle_event_from_record(record)
        target = event.evidence_manifest.get("target_node_id")
        if not isinstance(target, str):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Repair cycle marker has no exact failed node"
            )
        node = next((item for item in read.nodes if item.node_id == target), None)
        if node is None:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Repair target disappeared from its sealed Plan"
            )
        if node.status == "failed":
            await self._apply_bounded_repair(task_id, target)
        await self._complete_repair_cycle(task_id, target)

    async def _apply_bounded_repair(self, task_id: str, node_id: str) -> None:
        """Reset only a retry-authorized failed node; prior attempts stay immutable."""

        read = await self._required_read(task_id)
        execution = self._required_execution(read)
        async with self._database.session() as session, session.begin():
            execution_record = await self._locked_execution(session, execution)
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == node_id)
                .with_for_update()
            )
            attempt = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(
                    TaskLoopNodeAttemptRecord.execution_id
                    == execution.execution_id,
                    TaskLoopNodeAttemptRecord.node_id == node_id,
                )
                .order_by(TaskLoopNodeAttemptRecord.attempt.desc())
                .limit(1)
                .with_for_update()
            )
            approval = await session.scalar(
                select(TaskLoopCapabilityApprovalRecord.approval_id)
                .where(
                    TaskLoopCapabilityApprovalRecord.execution_id
                    == execution.execution_id,
                    TaskLoopCapabilityApprovalRecord.node_id == node_id,
                )
                .limit(1)
            )
            if node is None or attempt is None:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Repair target lost its failed attempt"
                )
            try:
                budget = PlanNodeBudget.model_validate(node.budget)
            except ValidationError as error:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Repair target budget Schema was rejected"
                ) from error
            if (
                node.run_id != execution.run_id
                or node.status != "failed"
                or attempt.status != "failed"
                or attempt.attempt != node.attempt_count
                or node.attempt_count >= budget.retries + 1
                or approval is not None
                or execution.status not in {"active", "repairing"}
            ):
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Failed node has no retry-safe repair authority"
                )
            now = utc_now()
            if execution.status == "active":
                await self._append_transition(
                    session,
                    execution_record,
                    status="repairing",
                    kind="repair_started",
                    now=now,
                )
                await session.flush()
            node.status = "ready"
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
            await self._append_transition(
                session,
                execution_record,
                status="active",
                kind="resumed",
                now=now,
            )

    async def _complete_repair_cycle(self, task_id: str, node_id: str) -> None:
        read = await self._required_read(task_id)
        snapshot = await self._snapshot(read)
        command = TaskLoopReducerCommand.build(
            snapshot=snapshot,
            kind="start_repair",
            reason_code="TASK_LOOP_BOUNDED_REPAIR_COMPLETED",
            node_id=node_id,
        )
        await self._record_cycle_event(read, command, kind="repair_completed")

    async def _terminate_with_cycle_event(
        self,
        read: TaskLoopExecutionRead,
        command: TaskLoopReducerCommand,
        *,
        kind: Literal["no_progress_terminated", "budget_exhausted"],
    ) -> None:
        # The marker is content-addressed and replay-idempotent.  If the
        # process exits between these two short transactions, the next reducer
        # pass observes the same proof, reuses the marker, and seals failure.
        await self._record_cycle_event(read, command, kind=kind)
        await self._fail_execution(read, command.reason_code)

    async def _record_cycle_event(
        self,
        read: TaskLoopExecutionRead,
        command: TaskLoopReducerCommand,
        *,
        kind: TaskLoopCycleEventKind,
    ) -> TaskLoopCycleEvent:
        execution = self._required_execution(read)
        current_snapshot = await self._snapshot(read)
        budget_evidence: dict[str, object] | None = None
        if kind == "budget_exhausted":
            exhausted, budget_evidence = await self._budget_state(read)
            if not exhausted:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Budget termination has no exhausted budget proof"
                )
        if (
            command.expected_execution_revision != execution.revision
            or command.source_progress_digest
            != current_snapshot.semantic_progress_digest
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Cycle command changed before persistence"
            )
        async with self._database.session() as session, session.begin():
            await self._locked_execution(session, execution)
            records = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord)
                        .where(TaskExecutionNodeRecord.run_id == execution.run_id)
                        .order_by(TaskExecutionNodeRecord.node_id)
                        .with_for_update()
                    )
                ).all()
            )
            expected_nodes = {
                item.node_id: (
                    item.status,
                    item.attempt_count,
                    item.candidate_present,
                    item.verified_result_present,
                    item.updated_at,
                )
                for item in read.nodes
            }
            actual_nodes = {
                item.node_id: (
                    item.status,
                    item.attempt_count,
                    item.status == "awaiting_verification",
                    item.status == "verified" and item.node_kind != "control",
                    (
                        item.updated_at.replace(tzinfo=UTC)
                        if item.updated_at.tzinfo is None
                        else item.updated_at
                    ),
                )
                for item in records
            }
            if actual_nodes != expected_nodes:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Task-loop nodes changed before cycle persistence"
                )
            latest_record = await session.scalar(
                select(TaskLoopCycleEventRecord)
                .where(TaskLoopCycleEventRecord.execution_id == execution.execution_id)
                .order_by(TaskLoopCycleEventRecord.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            latest = (
                self._cycle_event_from_record(latest_record)
                if latest_record is not None
                else None
            )
            if (
                latest is not None
                and latest.kind == kind
                and latest.source_progress_digest == command.source_progress_digest
                and kind != "no_progress_observed"
            ):
                return latest
            if kind == "repair_started":
                repair_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(TaskLoopCycleEventRecord)
                        .where(
                            TaskLoopCycleEventRecord.execution_id
                            == execution.execution_id,
                            TaskLoopCycleEventRecord.kind == "repair_started",
                        )
                    )
                    or 0
                )
                if command.node_id is None or repair_count >= 2:
                    raise TaskLoopExecutionCoordinatorProofRejectedError(
                        "Repair cycle exceeded its bounded generation count"
                    )
            if kind == "repair_completed":
                latest_target = (
                    latest.evidence_manifest.get("target_node_id")
                    if latest is not None
                    else None
                )
                if (
                    latest is None
                    or latest.kind != "repair_started"
                    or command.node_id is None
                    or latest_target != command.node_id
                ):
                    raise TaskLoopExecutionCoordinatorProofRejectedError(
                        "Repair completion has no exact start marker"
                    )
            observation_count = 0
            if kind == "no_progress_observed":
                prior = tuple(
                    (
                        await session.scalars(
                            select(TaskLoopCycleEventRecord)
                            .where(
                                TaskLoopCycleEventRecord.execution_id
                                == execution.execution_id,
                                TaskLoopCycleEventRecord.kind
                                == "no_progress_observed",
                                TaskLoopCycleEventRecord.source_progress_digest
                                == command.source_progress_digest,
                            )
                            .order_by(TaskLoopCycleEventRecord.sequence)
                        )
                    ).all()
                )
                observation_count = len(prior) + 1
                if observation_count > 3:
                    raise TaskLoopExecutionCoordinatorProofRejectedError(
                        "No-progress observation exceeded its bounded counter"
                    )
            evidence = {
                "schema_version": "deskpilot.task-loop-cycle-evidence.v1",
                "command_digest": command.command_digest,
                "execution_revision": execution.revision,
                "node_state_digest": sha256_digest(
                    {
                        "nodes": [
                            {
                                "node_id": item.node_id,
                                "status": item.status,
                                "revision": item.revision,
                                "attempt_count": item.attempt_count,
                            }
                            for item in records
                        ]
                    }
                ),
                "observation_count": observation_count,
            }
            if budget_evidence is not None:
                evidence["budget"] = budget_evidence
            if command.node_id is not None:
                evidence["target_node_id"] = command.node_id
            event = TaskLoopCycleEvent.build(
                execution_id=execution.execution_id,
                task_id=execution.task_id,
                sequence=(latest.sequence + 1 if latest is not None else 1),
                previous_event_digest=(
                    latest.event_digest if latest is not None else None
                ),
                kind=kind,
                plan_generation=execution.plan_generation,
                source_progress_digest=command.source_progress_digest,
                reason_code=command.reason_code,
                evidence_manifest=evidence,
                created_at=utc_now(),
            )
            session.add(
                TaskLoopCycleEventRecord(
                    event_id=event.event_id,
                    execution_id=event.execution_id,
                    task_id=event.task_id,
                    sequence=event.sequence,
                    previous_event_digest=event.previous_event_digest,
                    kind=event.kind,
                    plan_generation=event.plan_generation,
                    source_progress_digest=event.source_progress_digest,
                    reason_code=event.reason_code,
                    evidence_manifest=event.evidence_manifest,
                    evidence_digest=event.evidence_digest,
                    manifest=event.model_dump(mode="json"),
                    event_digest=event.event_digest,
                    created_at=event.created_at,
                )
            )
            return event

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
        status: TaskLoopExecutionStatus,
        kind: TaskLoopExecutionEventKind,
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

    async def _snapshot(self, read: TaskLoopExecutionRead) -> TaskLoopReducerSnapshot:
        snapshot = self._snapshot_base(read)
        execution = read.execution
        if execution is None:
            return snapshot
        budget_exhausted, _budget_evidence = await self._budget_state(read)
        repair_count = read.cycle.repair_count if read.cycle is not None else 0
        repair_available = await self._repair_available(
            read,
            budget_exhausted=budget_exhausted,
            repair_count=repair_count,
        )
        snapshot = TaskLoopReducerSnapshot.build(
            task_id=snapshot.task_id,
            execution_id=snapshot.execution_id,
            execution_status=snapshot.execution_status,
            execution_revision=snapshot.execution_revision,
            nodes=snapshot.nodes,
            active_claim_count=snapshot.active_claim_count,
            no_progress_count=0,
            repair_count=repair_count,
            repair_available=repair_available,
            budget_exhausted=budget_exhausted,
            deadline_exceeded=snapshot.deadline_exceeded,
            pending_user_revision=snapshot.pending_user_revision,
        )
        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopCycleEventRecord)
                        .where(
                            TaskLoopCycleEventRecord.execution_id
                            == execution.execution_id
                        )
                        .order_by(TaskLoopCycleEventRecord.sequence.desc())
                    )
                ).all()
            )
        count = 0
        expected_sequence = records[0].sequence if records else 0
        for record in records:
            event = self._cycle_event_from_record(record)
            if event.sequence != expected_sequence:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Task-loop cycle event sequence is not contiguous"
                )
            expected_sequence -= 1
            if (
                event.kind == "no_progress_observed"
                and event.source_progress_digest == snapshot.semantic_progress_digest
            ):
                count += 1
                continue
            break
        return TaskLoopReducerSnapshot.build(
            task_id=snapshot.task_id,
            execution_id=snapshot.execution_id,
            execution_status=snapshot.execution_status,
            execution_revision=snapshot.execution_revision,
            nodes=snapshot.nodes,
            active_claim_count=snapshot.active_claim_count,
            no_progress_count=min(count, 3),
            repair_count=snapshot.repair_count,
            repair_available=snapshot.repair_available,
            budget_exhausted=snapshot.budget_exhausted,
            deadline_exceeded=snapshot.deadline_exceeded,
            pending_user_revision=snapshot.pending_user_revision,
        )

    async def _repair_available(
        self,
        read: TaskLoopExecutionRead,
        *,
        budget_exhausted: bool,
        repair_count: int,
    ) -> bool:
        execution = self._required_execution(read)
        failed = tuple(item for item in read.nodes if item.status == "failed")
        if (
            execution.status not in {"active", "repairing"}
            or budget_exhausted
            or repair_count >= 2
            or len(failed) != 1
            or failed[0].attempt_count >= failed[0].max_attempts
        ):
            return False
        node = failed[0]
        async with self._database.session() as session:
            attempt = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(
                    TaskLoopNodeAttemptRecord.execution_id
                    == execution.execution_id,
                    TaskLoopNodeAttemptRecord.node_id == node.node_id,
                )
                .order_by(TaskLoopNodeAttemptRecord.attempt.desc())
                .limit(1)
            )
            approval = await session.scalar(
                select(TaskLoopCapabilityApprovalRecord.approval_id)
                .where(
                    TaskLoopCapabilityApprovalRecord.execution_id
                    == execution.execution_id,
                    TaskLoopCapabilityApprovalRecord.node_id == node.node_id,
                )
                .limit(1)
            )
        return bool(
            attempt is not None
            and attempt.status == "failed"
            and attempt.attempt == node.attempt_count
            and approval is None
        )

    async def _budget_state(
        self,
        read: TaskLoopExecutionRead,
    ) -> tuple[bool, dict[str, object]]:
        """Reconcile bounded generic usage against the sealed node budgets."""

        execution = self._required_execution(read)
        async with self._database.session() as session:
            node_records = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).where(
                            TaskExecutionNodeRecord.run_id == execution.run_id
                        )
                    )
                ).all()
            )
            turn_usage = (
                await session.execute(
                    select(
                        func.count(AgentModelTurnRecord.turn_id),
                        func.coalesce(func.sum(AgentModelTurnRecord.input_tokens), 0),
                        func.coalesce(func.sum(AgentModelTurnRecord.output_tokens), 0),
                        func.coalesce(func.sum(AgentModelTurnRecord.cost_micros), 0),
                    )
                    .join(
                        AgentInvocationRecord,
                        AgentInvocationRecord.invocation_id
                        == AgentModelTurnRecord.invocation_id,
                    )
                    .where(AgentInvocationRecord.run_id == execution.run_id)
                )
            ).one()
            attempts = tuple(
                (
                    await session.scalars(
                        select(TaskLoopNodeAttemptRecord).where(
                            TaskLoopNodeAttemptRecord.execution_id
                            == execution.execution_id
                        )
                    )
                ).all()
            )
        budgets: dict[str, PlanNodeBudget] = {}
        for node in node_records:
            try:
                budgets[node.node_id] = PlanNodeBudget.model_validate(node.budget)
            except ValidationError as error:
                raise TaskLoopExecutionCoordinatorProofRejectedError(
                    "Task-loop node budget Schema was rejected"
                ) from error
        fields = (
            "model_calls",
            "tool_calls",
            "input_tokens",
            "output_tokens",
            "retries",
            "cost_micros",
            "handoffs",
        )
        limits = {
            field: sum(int(getattr(item, field)) for item in budgets.values())
            for field in fields
        }
        nodes_by_id = {item.node_id: item for item in node_records}
        capability_tool_calls = sum(
            nodes_by_id.get(item.node_id) is not None
            and nodes_by_id[item.node_id].node_kind == DraftNodeKind.CAPABILITY.value
            and item.status not in {"prepared", "cancelled"}
            for item in attempts
        )
        retries_used = sum(max(0, item.attempt_count - 1) for item in node_records)
        used = {
            "model_calls": int(turn_usage[0]),
            "tool_calls": capability_tool_calls,
            "input_tokens": int(turn_usage[1]),
            "output_tokens": int(turn_usage[2]),
            "retries": retries_used,
            "cost_micros": int(turn_usage[3]),
            "handoffs": 0,
        }
        if any(used[field] > limits[field] for field in fields):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task-loop runtime usage exceeded its sealed budget"
            )
        unresolved = {
            item.node_id
            for item in node_records
            if item.status in {"pending", "ready"}
            and item.node_kind in {
                DraftNodeKind.AGENT.value,
                DraftNodeKind.CAPABILITY.value,
            }
        }
        prepared_nodes = {
            item.node_id for item in attempts if item.status == "prepared"
        }
        attempts_exhausted = any(
            item.node_id in unresolved
            and item.node_id not in prepared_nodes
            and item.attempt_count >= int(item.budget["retries"]) + 1
            for item in node_records
        )
        resource_exhausted = any(
            used[field] == limits[field]
            and any(
                node_id in unresolved
                and int(getattr(budgets[node_id], field)) > 0
                for node_id in budgets
            )
            for field in ("model_calls", "tool_calls", "input_tokens", "output_tokens")
        )
        evidence: dict[str, object] = {
            "schema_version": "deskpilot.task-loop-budget-evidence.v1",
            "limits": limits,
            "used": used,
            "unresolved_node_count": len(unresolved),
            "attempts_exhausted": attempts_exhausted,
            "resource_exhausted": resource_exhausted,
        }
        return attempts_exhausted or resource_exhausted, evidence

    @staticmethod
    def _snapshot_base(read: TaskLoopExecutionRead) -> TaskLoopReducerSnapshot:
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

    @staticmethod
    def _cycle_event_from_record(
        record: TaskLoopCycleEventRecord,
    ) -> TaskLoopCycleEvent:
        try:
            event = TaskLoopCycleEvent.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task-loop cycle event Schema was rejected"
            ) from error
        if (
            record.event_id != event.event_id
            or record.execution_id != event.execution_id
            or record.task_id != event.task_id
            or record.sequence != event.sequence
            or record.previous_event_digest != event.previous_event_digest
            or record.kind != event.kind
            or record.plan_generation != event.plan_generation
            or record.source_progress_digest != event.source_progress_digest
            or record.reason_code != event.reason_code
            or record.evidence_manifest != event.evidence_manifest
            or record.evidence_digest != event.evidence_digest
            or record.event_digest != event.event_digest
        ):
            raise TaskLoopExecutionCoordinatorProofRejectedError(
                "Task-loop cycle event columns changed"
            )
        return event


__all__ = [
    "TaskLoopAdvanceResult",
    "TaskLoopExecutionCoordinator",
    "TaskLoopExecutionCoordinatorError",
    "TaskLoopExecutionCoordinatorProofRejectedError",
    "TaskLoopExecutionCoordinatorUnavailableError",
    "TaskLoopFinalAcceptanceRejectedError",
]
