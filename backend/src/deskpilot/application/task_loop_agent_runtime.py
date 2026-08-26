"""Persistent bridge for source-bound Agent nodes in model-planner Task Loops.

This runtime is deliberately narrower than the legacy Workbench reducers.  It
coordinates an existing :class:`AgentExecutionRuntime` claim with the generic
Task Loop attempt table, recovers the immutable source-step input, and persists
only a fully verified generic ResultRef.  It never reads a ``TurnRouteRecord``.

External research and Workspace reads are invoked only after the database
transaction that acquired/updated the Task Loop attempt has closed.  Existing
Agent result and verification tables remain the source of truth; the generic
ResultRef contains only their result/schema/verification digests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.agent_model_loop import AgentModelLoopRuntime
from deskpilot.application.agent_model_requests import (
    build_bounded_coding_coordinator_model_request,
    build_dynamic_coordinator_model_request,
    build_patch_planner_model_request,
)
from deskpilot.application.agent_verified_result_bridge import (
    AgentVerifiedResultBridge,
    AgentVerifiedResultBridgeError,
    AgentVerifiedResultPlanProof,
    ResearchAgentVerificationProof,
    WorkspaceCodingCoordinatorVerificationProof,
    WorkspacePatchPlannerVerificationProof,
    WorkspaceReaderVerificationProof,
)
from deskpilot.application.research_runtime import ResearchRuntime
from deskpilot.application.task_loop_agent_adapter_registry import (
    TaskLoopAgentAdapterError,
    TaskLoopAgentAdapterRegistry,
)
from deskpilot.application.verified_edges import mark_verified_and_unlock
from deskpilot.application.workspace_coding_graph import (
    WORKSPACE_CODING_MAX_FILES,
    WORKSPACE_CODING_MIN_FILES,
    is_workspace_coding_planner_key,
    is_workspace_coding_reader_key,
    workspace_coding_file_count,
    workspace_coding_graph_keys,
    workspace_coding_parameter_for_key,
    workspace_coding_path_parameter,
    workspace_coding_planner_key,
    workspace_coding_planner_keys,
    workspace_coding_reader_key,
    workspace_coding_reader_keys,
)
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.agent_loop import (
    AgentProposeTaskGraphDecision,
    DynamicCoordinatorLoopDecision,
    WorkspaceBoundedCodingCoordinatorDecision,
    WorkspaceBoundedCodingGraphDecision,
    WorkspacePatchLoopDecision,
    WorkspacePatchSubmitProposalDecision,
)
from deskpilot.domain.agent_runtime import (
    AgentInvocationRead,
    AgentOutputResult,
    AgentResult,
    ClaimedInvocation,
    HandoffEnvelope,
    InvocationExecutionStatus,
    InvocationVerificationStatus,
)
from deskpilot.domain.capability_execution import VerifiedCapabilityResultRef
from deskpilot.domain.model_contracts import PrivacyMode
from deskpilot.domain.research import SearchRequest
from deskpilot.domain.task_loop import ModelPlannerStepBinding
from deskpilot.domain.task_loop_execution import (
    ModelPlannerNodeBinding,
    TaskLoopExecution,
    TaskLoopNodeAttempt,
    TaskLoopVerifiedResult,
)
from deskpilot.domain.task_plans import ExecutablePlan, TaskContract
from deskpilot.domain.workspace_files import WorkspaceFileRead
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentHandoffRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentResultRecord,
    ClaimVerdictRecord,
    ModelPlannerNodeBindingRecord,
    ModelPlannerStepBindingRecord,
    ResearchCitationRecord,
    ResearchClaimRecord,
    ResearchPageSnapshotRecord,
    ResearchSearchCallRecord,
    ResearchSessionRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopExecutionRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskPlanGenerationRecord,
    TaskRecord,
    VerificationEvidenceSnapshotRecord,
    VerificationRunRecord,
    WorkspaceAgentResultRecord,
    utc_now,
)

AgentSourceRoute = Literal[
    "research_to_html",
    "workspace_file_read",
    "workspace_coding_loop",
]
AgentSourceParameter = Literal[
    "goal",
    "path",
    "primary_path",
    "secondary_path",
    "file_03_path",
    "file_04_path",
    "file_05_path",
    "file_06_path",
    "file_07_path",
    "file_08_path",
    "project_path",
]


class TaskLoopAgentRuntimeError(RuntimeError):
    code = "TASK_LOOP_AGENT_RUNTIME_ERROR"


class TaskLoopAgentNotFoundError(TaskLoopAgentRuntimeError):
    code = "TASK_LOOP_AGENT_NOT_FOUND"


class TaskLoopAgentConflictError(TaskLoopAgentRuntimeError):
    code = "TASK_LOOP_AGENT_CONFLICT"


class TaskLoopAgentProofRejectedError(TaskLoopAgentRuntimeError):
    code = "TASK_LOOP_AGENT_PROOF_REJECTED"


class TaskLoopAgentRuntimeUnavailableError(TaskLoopAgentRuntimeError):
    code = "TASK_LOOP_AGENT_RUNTIME_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AgentSourcePlanProof:
    """Trusted deterministic source recipe output used by the proof bridge.

    The step binding itself is loaded from the database.  Callers normally
    obtain these two immutable values from exact Route recipe recompilation or
    deferred-plan revalidation; no Provider call is needed or permitted.
    """

    source_contract: TaskContract
    source_plan: ExecutablePlan


@dataclass(frozen=True, slots=True)
class SourceBoundAgentClaim:
    """Agent claim plus its sealed source-step input and generic attempt."""

    execution_id: str
    binding: ModelPlannerNodeBinding
    attempt: TaskLoopNodeAttempt
    route_id: AgentSourceRoute
    parameter_name: AgentSourceParameter
    parameter_value: str
    claimed: ClaimedInvocation


@dataclass(frozen=True, slots=True)
class _AgentProfile:
    route_id: AgentSourceRoute
    source_local_key: str
    parameter_name: AgentSourceParameter
    agent_id: str
    capability_id: str


_PROFILES: dict[tuple[AgentSourceRoute, str], _AgentProfile] = {
    ("research_to_html", "research"): _AgentProfile(
        route_id="research_to_html",
        source_local_key="research",
        parameter_name="goal",
        agent_id="builtin.web_researcher",
        capability_id="research.read.v1",
    ),
    ("workspace_file_read", "workspace_file_read"): _AgentProfile(
        route_id="workspace_file_read",
        source_local_key="workspace_file_read",
        parameter_name="path",
        agent_id="builtin.workspace_reader",
        capability_id="workspace.file.read.v1",
    ),
    ("workspace_coding_loop", "inspect_primary"): _AgentProfile(
        route_id="workspace_coding_loop",
        source_local_key="inspect_primary",
        parameter_name="primary_path",
        agent_id="builtin.workspace_reader",
        capability_id="workspace.file.read.v1",
    ),
    ("workspace_coding_loop", "coordinate_coding"): _AgentProfile(
        route_id="workspace_coding_loop",
        source_local_key="coordinate_coding",
        parameter_name="project_path",
        agent_id="builtin.workspace_coordinator",
        capability_id="workspace.dynamic.coordinate.v1",
    ),
    ("workspace_coding_loop", "inspect_secondary"): _AgentProfile(
        route_id="workspace_coding_loop",
        source_local_key="inspect_secondary",
        parameter_name="secondary_path",
        agent_id="builtin.workspace_reader",
        capability_id="workspace.file.read.v1",
    ),
    ("workspace_coding_loop", "plan_primary_patch"): _AgentProfile(
        route_id="workspace_coding_loop",
        source_local_key="plan_primary_patch",
        parameter_name="primary_path",
        agent_id="builtin.workspace_patch_planner",
        capability_id="workspace.patch.propose.v1",
    ),
    ("workspace_coding_loop", "plan_secondary_patch"): _AgentProfile(
        route_id="workspace_coding_loop",
        source_local_key="plan_secondary_patch",
        parameter_name="secondary_path",
        agent_id="builtin.workspace_patch_planner",
        capability_id="workspace.patch.propose.v1",
    ),
}


class TaskLoopAgentRuntime:
    """Coordinate exact Agent claims and persist proof-backed ResultRefs."""

    def __init__(
        self,
        database: Database,
        execution: AgentExecutionRuntime,
        adapters: TaskLoopAgentAdapterRegistry,
        *,
        research: ResearchRuntime | None = None,
        workspace: WorkspaceFileRuntime | None = None,
        model_loop: AgentModelLoopRuntime | None = None,
    ) -> None:
        self._database = database
        self._execution = execution
        self._adapters = adapters
        self._research = research
        self._workspace = workspace
        self._model_loop = model_loop

    async def claim_next(
        self,
        execution_id: str,
        owner_id: str,
        *,
        lease_seconds: int = 60,
        node_id: str | None = None,
    ) -> SourceBoundAgentClaim | None:
        """Claim the first exact source-bound Agent and create its shared attempt.

        The read preflight deliberately mirrors ``AgentExecutionRuntime``'s
        local-key ordering.  A concurrent mismatch is rejected after claim;
        authority is never inferred from the claimed Handoff.
        """

        async with self._database.session() as session:
            selected = await self._select_next(session, execution_id, node_id=node_id)
        if selected is None:
            return None
        execution, binding, profile = selected
        claimed = await self._execution.claim_next(
            execution.run_id,
            owner_id,
            lease_seconds=lease_seconds,
            node_id=binding.composite_node_id,
        )
        if claimed is None:
            return None
        self._assert_claim(binding, execution, profile, claimed)
        attempt = await self._persist_claim(execution, binding, profile, claimed)
        return SourceBoundAgentClaim(
            execution_id=execution.execution_id,
            binding=binding,
            attempt=attempt,
            route_id=profile.route_id,
            parameter_name=profile.parameter_name,
            parameter_value=binding.bound_input_manifest[profile.parameter_name],
            claimed=claimed,
        )

    async def recover_pending(
        self,
        execution_id: str,
        node_id: str | None = None,
    ) -> SourceBoundAgentClaim | None:
        """Reconstruct one persisted verification claim after process restart.

        No Provider, filesystem read, Research run, or Turn Route is replayed.
        The claim is rebuilt only from the exact execution, node binding,
        attempt, Handoff, and submitted Invocation records.
        """

        async with self._database.session() as session:
            execution_record = await session.get(TaskLoopExecutionRecord, execution_id)
            if execution_record is None:
                raise TaskLoopAgentNotFoundError("Task Loop execution does not exist")
            execution = self._execution_from_record(execution_record)
            if execution.status != "active":
                return None
            attempt_query = select(TaskLoopNodeAttemptRecord).where(
                TaskLoopNodeAttemptRecord.execution_id == execution_id,
                TaskLoopNodeAttemptRecord.status == "awaiting_verification",
            )
            if node_id is not None:
                attempt_query = attempt_query.where(
                    TaskLoopNodeAttemptRecord.node_id == node_id
                )
            attempts = tuple(
                (
                    await session.scalars(
                        attempt_query.order_by(
                            TaskLoopNodeAttemptRecord.node_id,
                            TaskLoopNodeAttemptRecord.created_at,
                        )
                    )
                ).all()
            )
            if not attempts:
                return None
            if len(attempts) > 1:
                raise TaskLoopAgentProofRejectedError(
                    "Task Loop recovery target is not a unique Agent verification"
                )
            attempt_record = attempts[0]
            attempt = self._attempt_from_record(attempt_record)
            binding_record = await session.get(
                ModelPlannerNodeBindingRecord,
                attempt.node_binding_id,
            )
            node = await session.get(TaskExecutionNodeRecord, attempt.node_id)
            invocation = await session.scalar(
                select(AgentInvocationRecord).where(
                    AgentInvocationRecord.run_id == execution.run_id,
                    AgentInvocationRecord.node_id == attempt.node_id,
                    AgentInvocationRecord.attempt == attempt.attempt,
                )
            )
            if binding_record is None or node is None or invocation is None:
                raise TaskLoopAgentProofRejectedError(
                    "Pending Agent verification lost its binding, node, or Invocation"
                )
            binding = self._binding_from_record(binding_record)
            profile = self._profile(binding)
            self._assert_bound_node(execution, binding, profile, node)
            handoff_record = await session.get(AgentHandoffRecord, invocation.handoff_id)
            if handoff_record is None:
                raise TaskLoopAgentProofRejectedError(
                    "Pending Agent verification lost its Handoff"
                )
            try:
                handoff = HandoffEnvelope.model_validate(handoff_record.manifest)
            except ValidationError as error:
                raise TaskLoopAgentProofRejectedError(
                    "Pending Agent Handoff Schema was rejected"
                ) from error
            if (
                handoff_record.run_id != execution.run_id
                or handoff_record.target_node_id != node.node_id
                or handoff_record.handoff_digest != handoff.handoff_digest
                or invocation.execution_status != "result_submitted"
                or invocation.result_id is None
                or node.status not in {"awaiting_verification", "verified"}
                or attempt.claim_owner_id is None
                or attempt.claim_acquired_at is None
                or attempt.claim_expires_at is None
                or attempt.run_id != execution.run_id
                or attempt.node_id != node.node_id
                or attempt.attempt != invocation.attempt
                or attempt.claim_fencing_token != node.claim_fencing_token
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Pending Agent verification proof changed after restart"
                )
            claimed = ClaimedInvocation(
                handoff=handoff,
                invocation=self._invocation_read(invocation),
                claim_owner_id=attempt.claim_owner_id,
                claim_fencing_token=attempt.claim_fencing_token,
                claim_expires_at=attempt.claim_expires_at,
            )
            self._assert_claim(binding, execution, profile, claimed)
            return SourceBoundAgentClaim(
                execution_id=execution.execution_id,
                binding=binding,
                attempt=attempt,
                route_id=profile.route_id,
                parameter_name=profile.parameter_name,
                parameter_value=binding.bound_input_manifest[profile.parameter_name],
                claimed=claimed,
            )

    async def claim_batch(
        self,
        execution_id: str,
        owner_id: str,
        node_ids: tuple[str, ...],
        *,
        lease_seconds: int = 60,
    ) -> tuple[SourceBoundAgentClaim, ...]:
        """Claim one canonical ready Agent batch without broadening authority.

        ``AgentExecutionRuntime`` enforces its configured parallel ceiling for
        every individual claim.  The caller supplies only node identities that
        came from one proof-checked reducer snapshot; parameters and Agent
        authority are reloaded independently for every claim.
        """

        if not 2 <= len(node_ids) <= 2 or tuple(sorted(node_ids)) != node_ids:
            raise TaskLoopAgentProofRejectedError(
                "Parallel Agent batch must contain two canonical node IDs"
            )
        claims: list[SourceBoundAgentClaim] = []
        for node_id in node_ids:
            claimed = await self.claim_next(
                execution_id,
                owner_id,
                lease_seconds=lease_seconds,
                node_id=node_id,
            )
            if claimed is None:
                raise TaskLoopAgentConflictError(
                    "Parallel Agent batch could not acquire every selected node"
                )
            claims.append(claimed)
        if {item.binding.composite_node_id for item in claims} != set(node_ids):
            raise TaskLoopAgentConflictError(
                "Parallel Agent claims differ from their reducer-selected batch"
            )
        return tuple(sorted(claims, key=lambda item: item.binding.composite_node_id))

    async def run_research(self, source: SourceBoundAgentClaim) -> object:
        """Run existing ResearchRuntime with only the sealed ``goal`` value."""

        if source.route_id != "research_to_html" or source.parameter_name != "goal":
            raise TaskLoopAgentConflictError("Source-bound claim is not a Research node")
        if self._research is None:
            raise TaskLoopAgentRuntimeUnavailableError("Research runtime is unavailable")
        await self._mark_attempt_running(source)
        # No database session owned by this runtime is open across Provider,
        # search, page-read, or model I/O.
        result = await self._research.run(
            source.claimed,
            query=source.parameter_value,
        )
        await self._mark_attempt_awaiting_verification(source)
        return result

    async def read_workspace_file(
        self,
        source: SourceBoundAgentClaim,
    ) -> WorkspaceFileRead:
        """Read the exact sealed Workspace ``path`` outside a DB transaction.

        This returns a deterministic candidate only.  Existing Workspace Agent
        result/verification persistence must complete before
        :meth:`persist_verified_result` can accept it.
        """

        if not self._is_workspace_file_source(source):
            raise TaskLoopAgentConflictError(
                "Source-bound claim is not a Workspace file-read node"
            )
        if self._workspace is None:
            raise TaskLoopAgentRuntimeUnavailableError("Workspace runtime is unavailable")
        await self._mark_attempt_running(source)
        return await asyncio.to_thread(self._workspace.read, source.parameter_value)

    async def run_workspace_file(
        self,
        source: SourceBoundAgentClaim,
        source_plan_proof: AgentSourcePlanProof,
    ) -> VerifiedCapabilityResultRef:
        """Execute and verify one exact source-bound Workspace file read.

        The filesystem read occurs outside every database transaction.  The
        candidate is then submitted through ``AgentExecutionRuntime`` and the
        same deterministic Workspace proof shape used by the existing Reader
        reducer is persisted without consulting a Turn Route.
        """

        await self.run_workspace_file_candidate(source)
        return await self.persist_verified_result(source, source_plan_proof)

    async def run_workspace_file_candidate(
        self,
        source: SourceBoundAgentClaim,
    ) -> AgentOutputResult:
        """Persist a deterministic Workspace candidate without minting ResultRef."""

        if not self._is_workspace_file_source(source):
            raise TaskLoopAgentConflictError(
                "Source-bound claim is not a Workspace file-read node"
            )
        if self._workspace is None:
            raise TaskLoopAgentRuntimeUnavailableError("Workspace runtime is unavailable")
        await self._mark_attempt_running(source)
        await self._execution.start_invocation(
            source.claimed.invocation.invocation_id,
            source.claimed.claim_owner_id,
            source.claimed.claim_fencing_token,
        )
        workspace = await asyncio.to_thread(
            self._workspace.read,
            source.parameter_value,
        )
        result = self._workspace_agent_result(source, workspace)
        await self._execution.submit_result(
            result,
            owner_id=source.claimed.claim_owner_id,
            fencing_token=source.claimed.claim_fencing_token,
        )
        await self._verify_workspace_result(source, workspace, result)
        await self._record_agent_candidate(source, result)
        return result

    async def run_coding_coordinator_candidate(
        self,
        source: SourceBoundAgentClaim,
    ) -> AgentOutputResult:
        """Persist one LOCAL-only confirmation of the server-sealed coding graph."""

        profile = self._profile(source.binding)
        if (
            profile.route_id != "workspace_coding_loop"
            or profile.source_local_key
            not in {"coordinate_coding", "coordinate_bounded_coding"}
            or profile.agent_id
            not in {
                "builtin.workspace_coordinator",
                "builtin.workspace_bounded_coordinator",
            }
        ):
            raise TaskLoopAgentConflictError(
                "Source-bound claim is not the coding Coordinator node"
            )
        if self._model_loop is None:
            raise TaskLoopAgentRuntimeUnavailableError(
                "Coding Coordinator Model Turn runtime is unavailable"
            )
        task, expected = await self._coding_coordinator_context(source)
        graph_material = {
            "nodes": [item.model_dump(mode="json") for item in expected.nodes],
            "output_node_key": expected.output_node_key,
        }
        graph_digest = sha256_digest(graph_material)
        graph_binding_digest = sha256_digest(
            {
                "node_binding_digest": source.binding.binding_digest,
                "graph_digest": graph_digest,
            }
        )
        graph_binding_id = f"tgb_{graph_binding_digest}"
        await self._mark_attempt_running(source)
        await self._execution.start_invocation(
            source.claimed.invocation.invocation_id,
            source.claimed.claim_owner_id,
            source.claimed.claim_fencing_token,
        )
        request_id = (
            "task-loop-coordinate-"
            f"{source.claimed.invocation.invocation_id[-20:]}"
        )
        privacy_mode = cast(PrivacyMode, task.privacy_mode)
        offered_capabilities = [
            cast(dict[str, object], item.model_dump(mode="json"))
            for item in expected.nodes
        ]
        proposal: (
            AgentProposeTaskGraphDecision
            | WorkspaceBoundedCodingGraphDecision
            | object
        )
        if isinstance(expected, WorkspaceBoundedCodingGraphDecision):
            request = build_bounded_coding_coordinator_model_request(
                request_id=request_id,
                task_id=source.binding.task_id,
                privacy_mode=privacy_mode,
                budget=source.claimed.handoff.budget_allocation,
                offered_capabilities=offered_capabilities,
                allowed_context_refs=("task_contract", "conversation_message"),
            )
            dispatched = await self._model_loop.dispatch(
                source.claimed,
                turn_no=1,
                request=request,
                decision_model=WorkspaceBoundedCodingCoordinatorDecision,
            )
            proposal = cast(
                WorkspaceBoundedCodingCoordinatorDecision,
                dispatched.decision,
            ).root
        else:
            request = build_dynamic_coordinator_model_request(
                request_id=request_id,
                task_id=source.binding.task_id,
                privacy_mode=privacy_mode,
                budget=source.claimed.handoff.budget_allocation,
                phase="propose_task_graph",
                offered_capabilities=offered_capabilities,
                allowed_context_refs=("task_contract", "conversation_message"),
                max_nodes=len(expected.nodes),
                repair_advice=None,
                import_sources=[],
            )
            dispatched = await self._model_loop.dispatch(
                source.claimed,
                turn_no=1,
                request=request,
                decision_model=DynamicCoordinatorLoopDecision,
            )
            proposal = cast(DynamicCoordinatorLoopDecision, dispatched.decision).root
        if (
            not isinstance(
                proposal,
                (AgentProposeTaskGraphDecision, WorkspaceBoundedCodingGraphDecision),
            )
            or proposal.nodes != expected.nodes
            or proposal.output_node_key != expected.output_node_key
        ):
            await self._model_loop.fail(
                source.claimed,
                dispatched.turn_id,
                "TASK_LOOP_COORDINATOR_GRAPH_REJECTED",
                sha256_digest(proposal),
            )
            await self._settle_model_rejected(
                source,
                error_code="TASK_LOOP_COORDINATOR_GRAPH_REJECTED",
            )
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator changed the server-sealed graph"
            )
        decision_id = await self._model_loop.accept(
            source.claimed,
            dispatched,
            proposal,
            binding_id=graph_binding_id,
        )
        decision_digest = sha256_digest(
            {
                "turn_id": dispatched.turn_id,
                "invocation_id": source.claimed.invocation.invocation_id,
                "decision": proposal.model_dump(mode="json"),
                "response_digest": sha256_digest(dispatched.response),
            }
        )
        result = self._coding_coordinator_agent_result(
            source,
            proposal,
            graph_digest=graph_digest,
            decision_id=decision_id,
            decision_digest=decision_digest,
            request_digest=dispatched.request_digest,
            response_digest=sha256_digest(dispatched.response),
        )
        await self._execution.submit_result(
            result,
            owner_id=source.claimed.claim_owner_id,
            fencing_token=source.claimed.claim_fencing_token,
        )
        await self._verify_coding_coordinator_result(
            source,
            result,
            expected,
            graph_binding_id=graph_binding_id,
            graph_digest=graph_digest,
        )
        await self._record_agent_candidate(source, result)
        return result

    async def _coding_coordinator_context(
        self,
        source: SourceBoundAgentClaim,
    ) -> tuple[
        TaskRecord,
        AgentProposeTaskGraphDecision | WorkspaceBoundedCodingGraphDecision,
    ]:
        async with self._database.session() as session:
            task = await session.get(TaskRecord, source.binding.task_id)
            record = await session.scalar(
                select(TaskPlanGenerationRecord).where(
                    TaskPlanGenerationRecord.task_id == source.binding.task_id,
                    TaskPlanGenerationRecord.plan_id
                    == source.binding.composite_plan_id,
                    TaskPlanGenerationRecord.plan_manifest_digest
                    == source.binding.composite_plan_manifest_digest,
                    TaskPlanGenerationRecord.status == "active",
                )
            )
        if task is None or record is None:
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator lost its active sealed Plan"
            )
        try:
            plan = ExecutablePlan.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator sealed Plan Schema was rejected"
            ) from error
        return task, self._coding_proposal_from_plan(source.binding, plan)

    @staticmethod
    def _coding_proposal_from_plan(
        binding: ModelPlannerNodeBinding,
        plan: ExecutablePlan,
    ) -> AgentProposeTaskGraphDecision | WorkspaceBoundedCodingGraphDecision:
        try:
            file_count = workspace_coding_file_count(binding.bound_input_manifest)
        except ValueError as error:
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator bounded file count changed"
            ) from error
        keys = workspace_coding_graph_keys(file_count)
        coordinator_key = (
            "coordinate_coding"
            if file_count == WORKSPACE_CODING_MIN_FILES
            else "coordinate_bounded_coding"
        )
        prefix = f"s{binding.step_ordinal:02d}_"
        by_key = {
            item.local_key.removeprefix(prefix): item
            for item in plan.nodes
            if item.local_key.startswith(prefix)
        }
        if set(keys) - set(by_key) or coordinator_key not in by_key:
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator fixed Plan nodes changed"
            )
        node_id_to_key = {item.node_id: key for key, item in by_key.items()}
        input_specs: dict[str, tuple[str, str]] = {
            "apply_patch": ("route_patch_test_spec", "patch_bundle"),
            "run_fixed_test": (
                (
                    "route_python_test_spec"
                    if binding.bound_input_manifest.get("test_kind") == "python"
                    else "route_node_test_spec"
                ),
                "fixed_test",
            ),
            "commit_git": ("route_patch_test_spec", "git_commit"),
        }
        for index in range(1, file_count + 1):
            parameter_name = workspace_coding_path_parameter(index)
            input_specs[workspace_coding_reader_key(index)] = (
                "route_explicit_file_path",
                parameter_name,
            )
            planner_binding_key = (
                "primary_change"
                if index == 1
                else "secondary_change"
                if index == 2
                else f"file_{index:02d}_change"
            )
            input_specs[workspace_coding_planner_key(index)] = (
                "route_patch_test_spec",
                planner_binding_key,
            )
        expected_capabilities = {
            "apply_patch": "workspace.patch.bundle.v1",
            "run_fixed_test": (
                "workspace.python.test.v1"
                if binding.bound_input_manifest.get("test_kind") == "python"
                else "workspace.node.test.v1"
            ),
            "commit_git": "workspace.git.commit.v1",
        }
        expected_capabilities.update(
            {
                key: "workspace.file.read.v1"
                for key in workspace_coding_reader_keys(file_count)
            }
        )
        expected_capabilities.update(
            {
                key: "workspace.patch.propose.v1"
                for key in workspace_coding_planner_keys(file_count)
            }
        )
        expected_dependencies: dict[str, tuple[str, ...]] = {
            **{key: () for key in workspace_coding_reader_keys(file_count)},
            **{
                workspace_coding_planner_key(index): (
                    workspace_coding_reader_key(index),
                )
                for index in range(1, file_count + 1)
            },
            "apply_patch": workspace_coding_planner_keys(file_count),
            "run_fixed_test": ("apply_patch",),
            "commit_git": ("run_fixed_test",),
        }
        if binding.bound_input_manifest.get("test_kind") not in {"python", "node"}:
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator fixed test ecosystem changed"
            )
        proposals: list[dict[str, object]] = []
        known = set(keys)
        for key in keys:
            node = by_key[key]
            if node.capability is None:
                raise TaskLoopAgentProofRejectedError(
                    "Coding Coordinator downstream node lost its Capability"
                )
            input_source, binding_key = input_specs[key]
            dependencies = tuple(
                node_id_to_key[item]
                for item in node.depends_on
                if node_id_to_key.get(item) in known
            )
            if (
                node.capability.capability_id != expected_capabilities[key]
                or set(dependencies) != set(expected_dependencies[key])
                or len(dependencies) != len(expected_dependencies[key])
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Coding Coordinator downstream Capability or dependency changed"
                )
            proposals.append(
                {
                    "local_key": key,
                    "target_capability_id": node.capability.capability_id,
                    "objective": node.objective,
                    "context_refs": ("task_contract", "conversation_message"),
                    "input_source": input_source,
                    "input_binding_key": binding_key,
                    "depends_on": expected_dependencies[key],
                    "budget_slice": node.budget,
                }
            )
        decision_model = (
            AgentProposeTaskGraphDecision
            if file_count == WORKSPACE_CODING_MIN_FILES
            else WorkspaceBoundedCodingGraphDecision
        )
        return decision_model.model_validate(
            {
                "schema_version": (
                    "deskpilot.agent-decision.v1"
                    if file_count == WORKSPACE_CODING_MIN_FILES
                    else "deskpilot.agent-decision.v2"
                ),
                "kind": "propose_task_graph",
                "nodes": proposals,
                "output_node_key": "commit_git",
                "decision_summary": "Confirm the exact server-sealed coding graph.",
            }
        )

    async def _verify_coding_coordinator_result(
        self,
        source: SourceBoundAgentClaim,
        result: AgentOutputResult,
        expected: AgentProposeTaskGraphDecision | WorkspaceBoundedCodingGraphDecision,
        *,
        graph_binding_id: str,
        graph_digest: str,
    ) -> None:
        async with self._database.session() as session, session.begin():
            invocation = await session.get(
                AgentInvocationRecord,
                result.invocation_id,
                with_for_update=True,
            )
            node = await session.get(
                TaskExecutionNodeRecord,
                source.binding.composite_node_id,
                with_for_update=True,
            )
            persisted_result = await session.get(AgentResultRecord, result.result_id)
            turns = tuple(
                (await session.scalars(select(AgentModelTurnRecord).where(
                    AgentModelTurnRecord.invocation_id == result.invocation_id
                ))).all()
            )
            decisions = tuple(
                (await session.scalars(select(AgentDecisionRecord).where(
                    AgentDecisionRecord.invocation_id == result.invocation_id
                ))).all()
            )
            if (
                invocation is None
                or node is None
                or persisted_result is None
                or len(turns) != 1
                or len(decisions) != 1
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Coding Coordinator candidate persistence is incomplete"
                )
            turn, decision = turns[0], decisions[0]
            try:
                proposal = type(expected).model_validate(decision.manifest)
            except ValidationError as error:
                raise TaskLoopAgentProofRejectedError(
                    "Coding Coordinator decision Schema was rejected"
                ) from error
            decision_material = {
                "turn_id": turn.turn_id,
                "invocation_id": invocation.invocation_id,
                "decision": decision.manifest,
                "response_digest": turn.response_digest,
            }
            parsed_result = AgentOutputResult.model_validate(persisted_result.manifest)
            if (
                proposal.nodes != expected.nodes
                or proposal.output_node_key != expected.output_node_key
                or invocation.result_id != result.result_id
                or invocation.execution_status != "result_submitted"
                or invocation.verification_status not in {"pending", "verified"}
                or node.status != "awaiting_verification"
                or persisted_result.result_digest != result.result_digest
                or turn.turn_no != 1
                or turn.status != "succeeded"
                or turn.response_digest is None
                or decision.turn_id != turn.turn_id
                or decision.kind != "propose_task_graph"
                or decision.binding_id != graph_binding_id
                or decision.decision_digest != sha256_digest(decision_material)
                or parsed_result.output.get("graph_digest") != graph_digest
                or result.input_digest != turn.request_digest
                or result.model_response_digest != turn.response_digest
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Coding Coordinator candidate crossed its sealed graph binding"
                )
            if invocation.verification_status == "pending":
                invocation.verification_status = "verified"
                invocation.revision += 1

    @staticmethod
    def _coding_coordinator_agent_result(
        source: SourceBoundAgentClaim,
        proposal: AgentProposeTaskGraphDecision | WorkspaceBoundedCodingGraphDecision,
        *,
        graph_digest: str,
        decision_id: str,
        decision_digest: str,
        request_digest: str,
        response_digest: str,
    ) -> AgentOutputResult:
        output = {
            "nodes": [item.model_dump(mode="json") for item in proposal.nodes],
            "output_node_key": proposal.output_node_key,
            "graph_digest": graph_digest,
            "decision_digest": decision_digest,
        }
        identity = {
            "invocation_id": source.claimed.invocation.invocation_id,
            "attempt": source.claimed.invocation.attempt,
            "decision_digest": decision_digest,
        }
        material = {
            "schema_version": "deskpilot.agent-output-result.v1",
            "result_id": f"res_{sha256_digest(identity)}",
            "invocation_id": source.claimed.invocation.invocation_id,
            "disposition": "candidate",
            "output": output,
            "evidence_refs": (f"agent-decision:{decision_id}",),
            "limitation_codes": (),
            "input_digest": request_digest,
            "model_response_digest": response_digest,
            "output_schema_digest": sha256_digest(
                type(proposal).model_json_schema()
            ),
        }
        return AgentOutputResult.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )

    async def run_patch_planner_candidate(
        self,
        source: SourceBoundAgentClaim,
    ) -> AgentOutputResult:
        """Run one persisted LOCAL-only Patch Planner turn without write authority."""

        profile = self._profile(source.binding)
        if (
            profile.route_id != "workspace_coding_loop"
            or not is_workspace_coding_planner_key(profile.source_local_key)
            or profile.agent_id != "builtin.workspace_patch_planner"
        ):
            raise TaskLoopAgentConflictError(
                "Source-bound claim is not a coding Patch Planner node"
            )
        if self._model_loop is None:
            raise TaskLoopAgentRuntimeUnavailableError(
                "Patch Planner Model Turn runtime is unavailable"
            )
        task, workspace, upstream_result_ref, expected_change = (
            await self._patch_planner_context(source)
        )
        await self._mark_attempt_running(source)
        await self._execution.start_invocation(
            source.claimed.invocation.invocation_id,
            source.claimed.claim_owner_id,
            source.claimed.claim_fencing_token,
        )
        observation_digest = sha256_digest(
            {
                "result_ref_digest": upstream_result_ref["result_ref_digest"],
                "workspace_result_digest": workspace.result_digest,
            }
        )
        route_binding_material = {
            "node_binding_digest": source.binding.binding_digest,
            "path": source.parameter_value,
            "observation_digest": observation_digest,
        }
        route_binding_id = f"rbn_{sha256_digest(route_binding_material)}"
        patch_binding_material = {
            "node_binding_digest": source.binding.binding_digest,
            "bound_input_digest": source.binding.bound_input_digest,
            "path": source.parameter_value,
            "expected_change": expected_change,
        }
        patch_binding_id = f"ptb_{sha256_digest(patch_binding_material)}"
        request = build_patch_planner_model_request(
            request_id=(
                "task-loop-patch-"
                f"{source.claimed.invocation.invocation_id[-20:]}"
            ),
            task_id=source.binding.task_id,
            privacy_mode=cast(PrivacyMode, task.privacy_mode),
            budget=source.claimed.handoff.budget_allocation,
            phase="propose_patch",
            path=source.parameter_value,
            project_path=str(source.binding.bound_input_manifest["project_path"]),
            test_path=str(source.binding.bound_input_manifest["test_path"]),
            test_kind=cast(
                Literal["python", "node"],
                source.binding.bound_input_manifest["test_kind"],
            ),
            objective=(
                "Propose exactly the single server-offered replacement and no other change: "
                f"{json.dumps(expected_change, ensure_ascii=False, separators=(',', ':'))}"
            ),
            route_binding_id=route_binding_id,
            patch_binding_id=patch_binding_id,
            route_id="workspace_coding_loop",
            upstream_data=[
                {
                    "result_ref": upstream_result_ref,
                    "external_untrusted_result": workspace.model_dump(mode="json"),
                }
            ],
            observation_digest=observation_digest,
            source_text=workspace.content,
        )
        dispatched = await self._model_loop.dispatch(
            source.claimed,
            turn_no=1,
            request=request,
            decision_model=WorkspacePatchLoopDecision,
        )
        proposal = cast(WorkspacePatchLoopDecision, dispatched.decision).root
        if (
            not isinstance(proposal, WorkspacePatchSubmitProposalDecision)
            or proposal.patch_binding_id != patch_binding_id
            or proposal.observation_digest != observation_digest
            or len(proposal.changes) != 1
            or proposal.changes[0].path != expected_change["path"]
            or proposal.changes[0].old_text != expected_change["old_text"]
            or proposal.changes[0].new_text != expected_change["new_text"]
        ):
            await self._model_loop.fail(
                source.claimed,
                dispatched.turn_id,
                "TASK_LOOP_PATCH_PROPOSAL_REJECTED",
                sha256_digest(proposal),
            )
            await self._settle_model_rejected(
                source,
                error_code="TASK_LOOP_PATCH_PROPOSAL_REJECTED",
            )
            raise TaskLoopAgentProofRejectedError(
                "Patch Planner changed the exact server-offered replacement"
            )
        decision_id = await self._model_loop.accept(
            source.claimed,
            dispatched,
            proposal,
            binding_id=patch_binding_id,
        )
        result = self._patch_planner_agent_result(
            source,
            proposal,
            decision_id=decision_id,
            decision_digest=sha256_digest(
                {
                    "turn_id": dispatched.turn_id,
                    "invocation_id": source.claimed.invocation.invocation_id,
                    "decision": proposal.model_dump(mode="json"),
                    "response_digest": sha256_digest(dispatched.response),
                }
            ),
            request_digest=dispatched.request_digest,
            response_digest=sha256_digest(dispatched.response),
        )
        await self._execution.submit_result(
            result,
            owner_id=source.claimed.claim_owner_id,
            fencing_token=source.claimed.claim_fencing_token,
        )
        await self._verify_patch_planner_result(source, result, expected_change)
        await self._record_agent_candidate(source, result)
        return result

    async def _patch_planner_context(
        self,
        source: SourceBoundAgentClaim,
    ) -> tuple[
        TaskRecord,
        WorkspaceFileRead,
        dict[str, object],
        dict[str, str],
    ]:
        async with self._database.session() as session:
            task = await session.get(TaskRecord, source.binding.task_id)
            node = await session.get(
                TaskExecutionNodeRecord,
                source.binding.composite_node_id,
            )
            if task is None or node is None or len(node.depends_on) != 1:
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner lost its Task or unique Reader dependency"
                )
            dependency_id = node.depends_on[0]
            records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopVerifiedResultRecord).where(
                            TaskLoopVerifiedResultRecord.execution_id
                            == source.execution_id,
                            TaskLoopVerifiedResultRecord.node_id == dependency_id,
                            TaskLoopVerifiedResultRecord.result_kind == "workspace_file",
                        )
                    )
                ).all()
            )
            if len(records) != 1:
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner has no unique verified Reader ResultRef"
                )
            record = records[0]
            try:
                workspace = WorkspaceFileRead.model_validate(record.output_manifest)
                result_ref = VerifiedCapabilityResultRef.model_validate(
                    record.result_ref_manifest
                )
            except ValidationError as error:
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner Reader proof Schema was rejected"
                ) from error
            if (
                workspace.relative_path != source.parameter_value
                or workspace.result_digest != record.output_digest
                or result_ref.result_ref_digest != record.result_ref_digest
                or result_ref.result_kind.value != "workspace_file"
                or result_ref.producer_node_id != dependency_id
                or result_ref.task_id != source.binding.task_id
                or result_ref.run_id != source.claimed.handoff.run_id
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner Reader ResultRef crossed its exact dependency"
                )
            expected_change = self._expected_patch_change(
                source.binding,
                source.parameter_value,
            )
            return (
                task,
                workspace,
                result_ref.model_dump(mode="json"),
                expected_change,
            )

    async def _verify_patch_planner_result(
        self,
        source: SourceBoundAgentClaim,
        result: AgentOutputResult,
        expected_change: dict[str, str],
    ) -> None:
        """Verify the persisted ModelTurn/Decision without unlocking successors."""

        async with self._database.session() as session, session.begin():
            invocation = await session.scalar(
                select(AgentInvocationRecord)
                .where(AgentInvocationRecord.invocation_id == result.invocation_id)
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.node_id
                    == source.binding.composite_node_id
                )
                .with_for_update()
            )
            persisted_result = await session.get(AgentResultRecord, result.result_id)
            turns = tuple(
                (
                    await session.scalars(
                        select(AgentModelTurnRecord).where(
                            AgentModelTurnRecord.invocation_id == result.invocation_id
                        )
                    )
                ).all()
            )
            decisions = tuple(
                (
                    await session.scalars(
                        select(AgentDecisionRecord).where(
                            AgentDecisionRecord.invocation_id == result.invocation_id
                        )
                    )
                ).all()
            )
            if (
                invocation is None
                or node is None
                or persisted_result is None
                or len(turns) != 1
                or len(decisions) != 1
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner candidate persistence is incomplete"
                )
            turn = turns[0]
            decision = decisions[0]
            try:
                proposal = WorkspacePatchSubmitProposalDecision.model_validate(
                    decision.manifest
                )
            except ValidationError as error:
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner decision Schema was rejected"
                ) from error
            change = proposal.changes[0] if len(proposal.changes) == 1 else None
            decision_material = {
                "turn_id": turn.turn_id,
                "invocation_id": invocation.invocation_id,
                "decision": decision.manifest,
                "response_digest": turn.response_digest,
            }
            if (
                invocation.run_id != source.claimed.handoff.run_id
                or invocation.node_id != source.binding.composite_node_id
                or invocation.attempt != source.claimed.invocation.attempt
                or invocation.result_id != result.result_id
                or invocation.execution_status != "result_submitted"
                or invocation.verification_status not in {"pending", "verified"}
                or node.status != "awaiting_verification"
                or persisted_result.invocation_id != invocation.invocation_id
                or persisted_result.manifest != result.model_dump(mode="json")
                or persisted_result.result_digest != result.result_digest
                or turn.turn_no != 1
                or turn.status != "succeeded"
                or turn.response_digest is None
                or decision.turn_id != turn.turn_id
                or decision.kind != "submit_result"
                or decision.binding_id != proposal.patch_binding_id
                or decision.decision_digest != sha256_digest(decision_material)
                or change is None
                or change.path != expected_change["path"]
                or change.old_text != expected_change["old_text"]
                or change.new_text != expected_change["new_text"]
                or result.input_digest != turn.request_digest
                or result.model_response_digest != turn.response_digest
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Patch Planner candidate crossed its ModelTurn or Offer binding"
                )
            if invocation.verification_status == "pending":
                invocation.verification_status = "verified"
                invocation.revision += 1

    @staticmethod
    def _patch_planner_agent_result(
        source: SourceBoundAgentClaim,
        proposal: WorkspacePatchSubmitProposalDecision,
        *,
        decision_id: str,
        decision_digest: str,
        request_digest: str,
        response_digest: str,
    ) -> AgentOutputResult:
        output = {
            "patch_binding_id": proposal.patch_binding_id,
            "observation_digest": proposal.observation_digest,
            "changes": [item.model_dump(mode="json") for item in proposal.changes],
            "decision_digest": decision_digest,
        }
        identity = {
            "invocation_id": source.claimed.invocation.invocation_id,
            "attempt": source.claimed.invocation.attempt,
            "decision_digest": decision_digest,
        }
        material = {
            "schema_version": "deskpilot.agent-output-result.v1",
            "result_id": f"res_{sha256_digest(identity)}",
            "invocation_id": source.claimed.invocation.invocation_id,
            "disposition": "candidate",
            "output": output,
            "evidence_refs": (f"agent-decision:{decision_id}",),
            "limitation_codes": (),
            "input_digest": request_digest,
            "model_response_digest": response_digest,
            "output_schema_digest": sha256_digest(
                WorkspacePatchSubmitProposalDecision.model_json_schema()
            ),
        }
        return AgentOutputResult.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )

    @staticmethod
    def _expected_patch_change(
        binding: ModelPlannerNodeBinding,
        path: str,
    ) -> dict[str, str]:
        raw = binding.bound_input_manifest.get("changes_json")
        if not isinstance(raw, str):
            raise TaskLoopAgentProofRejectedError(
                "Coding Patch Planner has no sealed change Offer"
            )
        try:
            changes = json.loads(raw)
        except json.JSONDecodeError as error:
            raise TaskLoopAgentProofRejectedError(
                "Coding Patch Planner change Offer is invalid JSON"
            ) from error
        try:
            file_count = workspace_coding_file_count(binding.bound_input_manifest)
        except ValueError as error:
            raise TaskLoopAgentProofRejectedError(
                "Coding Patch Planner file count is invalid"
            ) from error
        if not isinstance(changes, list) or len(changes) != file_count:
            raise TaskLoopAgentProofRejectedError(
                "Coding Patch Planner requires the exact bounded change count"
            )
        matches: list[dict[str, str]] = []
        for item in changes:
            if not isinstance(item, dict) or set(item) != {
                "path",
                "old_text",
                "new_text",
            }:
                raise TaskLoopAgentProofRejectedError(
                    "Coding Patch Planner change Offer shape changed"
                )
            if not all(isinstance(item[name], str) for name in item):
                raise TaskLoopAgentProofRejectedError(
                    "Coding Patch Planner change values must be strings"
                )
            if item["path"] == path:
                matches.append(cast(dict[str, str], item))
        if len(matches) != 1:
            raise TaskLoopAgentProofRejectedError(
                "Coding Patch Planner path has no unique offered change"
            )
        return dict(matches[0])

    async def persist_verified_result(
        self,
        source: SourceBoundAgentClaim,
        source_plan_proof: AgentSourcePlanProof,
    ) -> VerifiedCapabilityResultRef:
        """Atomically persist a bridge ResultRef and the generic verified attempt.

        All Agent and verification evidence is reloaded inside this transaction.
        If the existing verifier left the node awaiting verification, the node
        transition and successor unlock occur in this same transaction; a bridge
        rejection rolls them back.  If the existing verifier already marked the
        node verified, this method accepts only the exact same immutable proof.
        """

        try:
            async with self._database.session() as session, session.begin():
                execution, binding, step, composite_plan = await self._locked_scope(
                    session,
                    source,
                    source_plan_proof,
                )
                run = await session.scalar(
                    select(TaskExecutionRunRecord)
                    .where(TaskExecutionRunRecord.run_id == execution.run_id)
                    .with_for_update()
                )
                node = await session.scalar(
                    select(TaskExecutionNodeRecord)
                    .where(TaskExecutionNodeRecord.node_id == binding.composite_node_id)
                    .with_for_update()
                )
                attempt = await session.scalar(
                    select(TaskLoopNodeAttemptRecord)
                    .where(TaskLoopNodeAttemptRecord.attempt_id == source.attempt.attempt_id)
                    .with_for_update()
                )
                if run is None or node is None or attempt is None:
                    raise TaskLoopAgentNotFoundError(
                        "Agent Run, node, or Task Loop attempt disappeared"
                    )
                self._assert_attempt_scope(source, execution, binding, run, node, attempt)

                invocation = await session.scalar(
                    select(AgentInvocationRecord).where(
                        AgentInvocationRecord.run_id == run.run_id,
                        AgentInvocationRecord.node_id == node.node_id,
                        AgentInvocationRecord.attempt == attempt.attempt,
                    )
                )
                if invocation is None or invocation.result_id is None:
                    raise TaskLoopAgentProofRejectedError(
                        "Agent Invocation has no submitted result"
                    )
                result = await session.get(AgentResultRecord, invocation.result_id)
                if result is None:
                    raise TaskLoopAgentProofRejectedError(
                        "Agent submitted result record is missing"
                    )

                if (
                    node.status not in {"awaiting_verification", "verified"}
                    or invocation.verification_status != "verified"
                ):
                    raise TaskLoopAgentProofRejectedError(
                        "Agent node is not ready for verified-result persistence"
                    )

                plan_proof = AgentVerifiedResultPlanProof(
                    step_binding=step,
                    source_contract=source_plan_proof.source_contract,
                    source_plan=source_plan_proof.source_plan,
                    composite_plan=composite_plan,
                    run=run,
                    node=node,
                    invocation=invocation,
                    result=result,
                )
                profile = self._profile(binding)
                output_manifest: dict[str, object]
                verification_manifest: dict[str, object]
                if profile.route_id == "research_to_html":
                    verification_proof = await self._research_proof(
                        session,
                        plan_proof,
                        binding,
                    )
                    result_ref = AgentVerifiedResultBridge.research(
                        plan_proof,
                        verification_proof,
                        allow_pending_node_transition=True,
                    )
                    output_manifest = dict(result.manifest)
                    verification_manifest = {
                        "schema_version": (
                            "deskpilot.agent-result-verification-reference.v1"
                        ),
                        "verification_run_id": (
                            verification_proof.verification.verification_run_id
                        ),
                        "verification_digest": result_ref.verification_digest,
                        "agent_result_digest": result.result_digest,
                        "evidence_snapshot_digest": (
                            verification_proof.evidence_snapshot.snapshot_digest
                        ),
                    }
                elif profile.agent_id in {
                    "builtin.workspace_coordinator",
                    "builtin.workspace_bounded_coordinator",
                }:
                    expected_proposal = self._coding_proposal_from_plan(
                        binding,
                        composite_plan,
                    )
                    coordinator_proof = await self._coding_coordinator_proof(
                        session,
                        invocation,
                    )
                    graph_digest = sha256_digest(
                        {
                            "nodes": [
                                item.model_dump(mode="json")
                                for item in expected_proposal.nodes
                            ],
                            "output_node_key": expected_proposal.output_node_key,
                        }
                    )
                    graph_binding_digest = sha256_digest(
                        {
                            "node_binding_digest": binding.binding_digest,
                            "graph_digest": graph_digest,
                        }
                    )
                    graph_binding_id = f"tgb_{graph_binding_digest}"
                    result_ref = (
                        AgentVerifiedResultBridge.workspace_coding_coordinator(
                            plan_proof,
                            coordinator_proof,
                            expected_proposal=expected_proposal,
                            expected_graph_binding_id=graph_binding_id,
                            allow_pending_node_transition=True,
                        )
                    )
                    parsed_result = AgentOutputResult.model_validate(result.manifest)
                    output_manifest = {
                        **parsed_result.output,
                        "result_digest": result.result_digest,
                    }
                    verification_manifest = {
                        "schema_version": (
                            "deskpilot.agent-result-verification-reference.v1"
                        ),
                        "model_turn_id": coordinator_proof.model_turn.turn_id,
                        "decision_id": coordinator_proof.decision.decision_id,
                        "decision_digest": coordinator_proof.decision.decision_digest,
                        "graph_digest": graph_digest,
                        "verification_digest": result_ref.verification_digest,
                        "agent_result_digest": result.result_digest,
                    }
                elif profile.agent_id == "builtin.workspace_patch_planner":
                    expected_change = self._expected_patch_change(
                        binding,
                        source.parameter_value,
                    )
                    patch_proof = await self._patch_planner_proof(
                        session,
                        invocation,
                    )
                    result_ref = AgentVerifiedResultBridge.workspace_patch_planner(
                        plan_proof,
                        patch_proof,
                        expected_source_local_key=profile.source_local_key,
                        source_parameter_name=profile.parameter_name,
                        expected_change=expected_change,
                        allow_pending_node_transition=True,
                    )
                    parsed_result = AgentOutputResult.model_validate(result.manifest)
                    output_manifest = {
                        **parsed_result.output,
                        "result_digest": result.result_digest,
                    }
                    verification_manifest = {
                        "schema_version": (
                            "deskpilot.agent-result-verification-reference.v1"
                        ),
                        "model_turn_id": patch_proof.model_turn.turn_id,
                        "decision_id": patch_proof.decision.decision_id,
                        "decision_digest": patch_proof.decision.decision_digest,
                        "verification_digest": result_ref.verification_digest,
                        "agent_result_digest": result.result_digest,
                    }
                else:
                    workspace_proof = await self._workspace_proof(session, invocation)
                    result_ref = AgentVerifiedResultBridge.workspace_reader(
                        plan_proof,
                        workspace_proof,
                        allow_pending_node_transition=True,
                        expected_route_id=profile.route_id,
                        expected_source_local_key=profile.source_local_key,
                        source_parameter_name=profile.parameter_name,
                    )
                    output_manifest = dict(workspace_proof.workspace_result.manifest)
                    verification_manifest = {
                        "schema_version": (
                            "deskpilot.agent-result-verification-reference.v1"
                        ),
                        "workspace_invocation_id": (
                            workspace_proof.workspace_result.invocation_id
                        ),
                        "verification_digest": result_ref.verification_digest,
                        "workspace_result_digest": (
                            workspace_proof.workspace_result.result_digest
                        ),
                        "agent_result_digest": result.result_digest,
                    }

                bound_agent = binding.effective_authority.bound_agent
                if bound_agent is None:
                    raise TaskLoopAgentProofRejectedError(
                        "Agent node lost its exact effective Agent authority"
                    )
                agent_binding_manifest = bound_agent.model_dump(mode="json")
                agent_binding_digest = sha256_digest(agent_binding_manifest)
                agent_result_proof_digest = sha256_digest(
                    {
                        "schema_version": "deskpilot.agent-result-proof.v1",
                        "agent_binding_digest": agent_binding_digest,
                        "agent_result_id": result.result_id,
                        "agent_result_digest": result.result_digest,
                        "output_digest": result_ref.result_digest,
                        "verification_digest": result_ref.verification_digest,
                    }
                )
                capability_manifest = result_ref.capability.model_dump(mode="json")
                capability_digest = sha256_digest(capability_manifest)

                existing = await session.scalar(
                    select(TaskLoopVerifiedResultRecord).where(
                        TaskLoopVerifiedResultRecord.attempt_id == attempt.attempt_id
                    )
                )
                created_at = (
                    self._aware(existing.created_at)
                    if existing is not None
                    else utc_now()
                )
                result_ref_identity = {
                    "attempt_id": attempt.attempt_id,
                    "result_ref_digest": result_ref.result_ref_digest,
                }
                result_ref_id = f"tlr_{sha256_digest(result_ref_identity)}"
                verified = TaskLoopVerifiedResult(
                    result_ref_id=result_ref_id,
                    attempt_id=attempt.attempt_id,
                    execution_id=execution.execution_id,
                    node_binding_id=binding.node_binding_id,
                    node_binding_digest=binding.binding_digest,
                    run_id=run.run_id,
                    node_id=node.node_id,
                    producer_kind="agent_bridge",
                    capability_manifest=capability_manifest,
                    capability_digest=capability_digest,
                    agent_binding_manifest=agent_binding_manifest,
                    agent_binding_digest=agent_binding_digest,
                    executor_manifest_digest=None,
                    agent_result_proof_digest=agent_result_proof_digest,
                    input_binding_digest=attempt.input_digest,
                    context_digest=attempt.context_digest,
                    candidate_digest=None,
                    result_kind=result_ref.result_kind.value,
                    output_manifest=output_manifest,
                    output_schema_digest=result_ref.result_schema_digest,
                    output_digest=result_ref.result_digest,
                    verification_manifest=verification_manifest,
                    verification_digest=result_ref.verification_digest,
                    result_ref_manifest=result_ref.model_dump(mode="json"),
                    result_ref_digest=result_ref.result_ref_digest,
                    created_at=created_at,
                )
                if existing is not None:
                    self._assert_verified_record(existing, verified)
                    self._assert_verified_attempt(attempt, result_ref)
                    if node.status == "awaiting_verification":
                        await mark_verified_and_unlock(session, run, node)
                    return result_ref

                session.add(
                    TaskLoopVerifiedResultRecord(
                        **verified.model_dump(
                            mode="python",
                            exclude={"schema_version"},
                        )
                    )
                )
                receipt_manifest = {
                    "schema_version": "deskpilot.agent-verified-result-receipt.v1",
                    "node_binding_digest": binding.binding_digest,
                    "invocation_id": invocation.invocation_id,
                    "invocation_attempt": invocation.attempt,
                    "result_ref_digest": result_ref.result_ref_digest,
                    "agent_result_proof_digest": agent_result_proof_digest,
                    "verification_digest": result_ref.verification_digest,
                }
                self._settle_attempt_verified(
                    attempt,
                    receipt_manifest,
                    result_ref,
                    candidate_manifest={
                        "schema_version": "deskpilot.agent-result-candidate-reference.v1",
                        "candidate_digest": result.result_digest,
                        "invocation_id": invocation.invocation_id,
                        "agent_result_id": result.result_id,
                        "agent_result_digest": result.result_digest,
                    },
                    verification_manifest=verification_manifest,
                )
                await session.flush()
                if node.status == "awaiting_verification":
                    await mark_verified_and_unlock(session, run, node)
                    await session.flush()
                return result_ref
        except TaskLoopAgentRuntimeError:
            raise
        except AgentVerifiedResultBridgeError as error:
            raise TaskLoopAgentProofRejectedError(
                "Existing Agent verification proof was rejected"
            ) from error
        except (ValidationError, ValueError) as error:
            raise TaskLoopAgentProofRejectedError(
                "Task Loop Agent proof Schema or digest was rejected"
            ) from error

    async def settle_rejected(
        self,
        source: SourceBoundAgentClaim,
        *,
        error_code: str,
    ) -> None:
        """Seal an independently rejected Agent candidate as terminal evidence."""

        if not error_code or len(error_code) > 100:
            raise ValueError("Agent rejection error code is invalid")
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(TaskLoopNodeAttemptRecord.attempt_id == source.attempt.attempt_id)
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.node_id
                    == source.binding.composite_node_id
                )
                .with_for_update()
            )
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == source.attempt.run_id)
                .with_for_update()
            )
            if record is None or node is None or run is None:
                raise TaskLoopAgentNotFoundError(
                    "Rejected Agent attempt, node, or Run disappeared"
                )
            previous = self._attempt_from_record(record)
            if (
                previous.execution_id != source.execution_id
                or previous.node_binding_id != source.binding.node_binding_id
                or previous.status != "awaiting_verification"
                or node.status not in {"awaiting_verification", "failed"}
                or run.run_id != source.attempt.run_id
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Rejected Agent candidate crossed its attempt or node state"
                )
            now = utc_now()
            material = previous.model_dump(mode="python", exclude={"attempt_digest"})
            material.update(
                {
                    "status": "failed",
                    "revision": previous.revision + 1,
                    "claim_owner_id": None,
                    "claim_acquired_at": None,
                    "claim_expires_at": None,
                    "error_code": error_code,
                    "error_digest": sha256_digest(
                        {
                            "attempt_id": previous.attempt_id,
                            "candidate_digest": previous.candidate_digest,
                            "error_code": error_code,
                        }
                    ),
                    "updated_at": now,
                }
            )
            current = self._build_attempt(material)
            self._apply_attempt_record(record, current)
            if node.status != "failed":
                node.status = "failed"
                node.revision += 1
                node.claim_owner_id = None
                node.claim_expires_at = None
                node.updated_at = now
            if run.status != "failed":
                run.status = "failed"
                run.revision += 1
                run.updated_at = now

    async def _settle_model_rejected(
        self,
        source: SourceBoundAgentClaim,
        *,
        error_code: str,
    ) -> None:
        """Fail one bounded model node after a usable but unauthorized decision."""

        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(
                    TaskLoopNodeAttemptRecord.attempt_id
                    == source.attempt.attempt_id
                )
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.node_id
                    == source.binding.composite_node_id
                )
                .with_for_update()
            )
            run = await session.scalar(
                select(TaskExecutionRunRecord)
                .where(TaskExecutionRunRecord.run_id == source.attempt.run_id)
                .with_for_update()
            )
            invocation = await session.scalar(
                select(AgentInvocationRecord)
                .where(
                    AgentInvocationRecord.invocation_id
                    == source.claimed.invocation.invocation_id
                )
                .with_for_update()
            )
            if record is None or node is None or run is None or invocation is None:
                raise TaskLoopAgentNotFoundError(
                    "Rejected model attempt, Invocation, node, or Run disappeared"
                )
            previous = self._attempt_from_record(record)
            if (
                previous.execution_id != source.execution_id
                or previous.node_binding_id != source.binding.node_binding_id
                or previous.status != "running"
                or node.status != "running"
                or invocation.execution_status != "running"
                or invocation.verification_status != "not_requested"
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Rejected model decision crossed its running attempt"
                )
            now = utc_now()
            error_digest = sha256_digest(
                {
                    "attempt_id": previous.attempt_id,
                    "invocation_id": invocation.invocation_id,
                    "error_code": error_code,
                }
            )
            material = previous.model_dump(mode="python", exclude={"attempt_digest"})
            material.update(
                {
                    "status": "failed",
                    "revision": previous.revision + 1,
                    "claim_owner_id": None,
                    "claim_acquired_at": None,
                    "claim_expires_at": None,
                    "error_code": error_code,
                    "error_digest": error_digest,
                    "updated_at": now,
                }
            )
            self._apply_attempt_record(record, self._build_attempt(material))
            invocation.execution_status = "failed_terminal"
            invocation.verification_status = "rejected"
            invocation.finished_at = now
            invocation.revision += 1
            node.status = "failed"
            node.revision += 1
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.updated_at = now
            run.status = "failed"
            run.revision += 1
            run.updated_at = now

    async def _select_next(
        self,
        session: AsyncSession,
        execution_id: str,
        *,
        node_id: str | None = None,
    ) -> tuple[TaskLoopExecution, ModelPlannerNodeBinding, _AgentProfile] | None:
        record = await session.get(TaskLoopExecutionRecord, execution_id)
        if record is None:
            raise TaskLoopAgentNotFoundError("Task Loop execution does not exist")
        execution = self._execution_from_record(record)
        if execution.status != "active":
            return None
        node_statement = (
            select(TaskExecutionNodeRecord)
            .where(
                TaskExecutionNodeRecord.run_id == execution.run_id,
                TaskExecutionNodeRecord.status == "ready",
                TaskExecutionNodeRecord.runtime_enabled.is_(True),
                TaskExecutionNodeRecord.bound_agent.is_not(None),
            )
        )
        if node_id is not None:
            node_statement = node_statement.where(
                TaskExecutionNodeRecord.node_id == node_id
            )
        node = await session.scalar(
            node_statement.order_by(TaskExecutionNodeRecord.local_key)
            .limit(1)
        )
        if node is None:
            return None
        binding_record = await session.scalar(
            select(ModelPlannerNodeBindingRecord).where(
                ModelPlannerNodeBindingRecord.execution_id == execution.execution_id,
                ModelPlannerNodeBindingRecord.composite_node_id == node.node_id,
            )
        )
        if binding_record is None:
            raise TaskLoopAgentProofRejectedError(
                "Ready Agent node has no exact model-planner binding"
            )
        binding = self._binding_from_record(binding_record)
        profile = self._profile(binding)
        self._assert_bound_node(execution, binding, profile, node)
        return execution, binding, profile

    async def _persist_claim(
        self,
        execution: TaskLoopExecution,
        binding: ModelPlannerNodeBinding,
        profile: _AgentProfile,
        claimed: ClaimedInvocation,
    ) -> TaskLoopNodeAttempt:
        async with self._database.session() as session, session.begin():
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == binding.composite_node_id)
                .with_for_update()
            )
            if node is None:
                raise TaskLoopAgentNotFoundError("Claimed Agent node disappeared")
            self._assert_bound_node(execution, binding, profile, node)
            if (
                node.status != "claimed"
                or node.claim_owner_id != claimed.claim_owner_id
                or node.claim_fencing_token != claimed.claim_fencing_token
                or node.attempt_count != claimed.invocation.attempt
            ):
                raise TaskLoopAgentConflictError(
                    "Agent claim changed before Task Loop attempt persistence"
                )
            attempt_identity = {
                "execution_id": execution.execution_id,
                "node_id": node.node_id,
                "attempt": node.attempt_count,
            }
            attempt_id = f"tla_{sha256_digest(attempt_identity)}"
            existing = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(TaskLoopNodeAttemptRecord.attempt_id == attempt_id)
                .with_for_update()
            )
            if existing is not None:
                attempt = self._attempt_from_record(existing)
                if (
                    existing.node_binding_id != binding.node_binding_id
                    or attempt.claim_owner_id != claimed.claim_owner_id
                    or attempt.claim_fencing_token != claimed.claim_fencing_token
                ):
                    raise TaskLoopAgentConflictError(
                        "Task Loop attempt belongs to another claim fence"
                    )
                return attempt
            input_manifest = self._input_manifest(binding, profile)
            context_manifest = {
                "schema_version": "deskpilot.source-bound-agent-context.v1",
                "task_id": execution.task_id,
                "execution_id": execution.execution_id,
                "run_id": execution.run_id,
                "plan_id": execution.plan_id,
                "plan_generation": execution.plan_generation,
                "plan_manifest_digest": execution.plan_manifest_digest,
                "node_id": node.node_id,
                "node_spec_digest": node.node_spec_digest,
                "node_binding_id": binding.node_binding_id,
                "node_binding_digest": binding.binding_digest,
                "effective_authority_digest": (
                    binding.effective_authority.authority_digest
                ),
                "runtime_eligibility_digest": (
                    binding.runtime_eligibility.eligibility_digest
                ),
                "invocation_id": claimed.invocation.invocation_id,
                "invocation_attempt": claimed.invocation.attempt,
            }
            created_at = utc_now()
            attempt = self._build_attempt(
                {
                    "schema_version": "deskpilot.task-loop-node-attempt.v1",
                    "attempt_id": attempt_id,
                    "execution_id": execution.execution_id,
                    "node_binding_id": binding.node_binding_id,
                    "run_id": execution.run_id,
                    "node_id": node.node_id,
                    "attempt": node.attempt_count,
                    "status": "claimed",
                    "revision": 1,
                    "claim_owner_id": claimed.claim_owner_id,
                    "claim_fencing_token": claimed.claim_fencing_token,
                    "claim_acquired_at": created_at,
                    "claim_expires_at": claimed.claim_expires_at,
                    "input_manifest": input_manifest,
                    "input_digest": sha256_digest(input_manifest),
                    "context_manifest": context_manifest,
                    "context_digest": sha256_digest(context_manifest),
                    "candidate_manifest": None,
                    "candidate_digest": None,
                    "candidate_recorded_at": None,
                    "verification_manifest": None,
                    "verification_digest": None,
                    "verified_at": None,
                    "receipt_manifest": None,
                    "receipt_digest": None,
                    "error_code": None,
                    "error_digest": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )
            session.add(
                TaskLoopNodeAttemptRecord(
                    **attempt.model_dump(
                        mode="python",
                        exclude={"schema_version", "attempt_digest"},
                    ),
                    manifest=attempt.model_dump(mode="json"),
                    attempt_digest=attempt.attempt_digest,
                )
            )
            await session.flush()
            return attempt

    async def _mark_attempt_running(self, source: SourceBoundAgentClaim) -> None:
        async with self._database.session() as session, session.begin():
            record = await self._locked_attempt_for_claim(session, source)
            if record.status == "running":
                return
            if record.status != "claimed":
                raise TaskLoopAgentConflictError(
                    "Task Loop attempt is not claimable for external execution"
                )
            self._transition_attempt(record, status="running")

    async def _mark_attempt_awaiting_verification(
        self,
        source: SourceBoundAgentClaim,
    ) -> None:
        async with self._database.session() as session, session.begin():
            record = await self._locked_attempt_for_claim(session, source)
            node = await session.get(TaskExecutionNodeRecord, source.binding.composite_node_id)
            if node is None or node.status != "awaiting_verification":
                raise TaskLoopAgentProofRejectedError(
                    "Research runtime did not persist an awaiting-verification Agent result"
                )
            if record.status == "awaiting_verification":
                return
            if record.status != "running":
                raise TaskLoopAgentConflictError(
                    "Task Loop attempt did not enter running state"
                )
            invocation = await session.get(
                AgentInvocationRecord,
                source.claimed.invocation.invocation_id,
            )
            if invocation is None or invocation.result_id is None:
                raise TaskLoopAgentProofRejectedError(
                    "Research runtime did not persist its Agent Result"
                )
            result_record = await session.get(AgentResultRecord, invocation.result_id)
            if result_record is None:
                raise TaskLoopAgentProofRejectedError(
                    "Research Agent Result record is missing"
                )
            try:
                result = AgentResult.model_validate(result_record.manifest)
            except ValidationError as error:
                raise TaskLoopAgentProofRejectedError(
                    "Research Agent candidate Schema was rejected"
                ) from error
            if (
                result.result_id != result_record.result_id
                or result.invocation_id != invocation.invocation_id
                or result.result_digest != result_record.result_digest
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Research Agent candidate persistence changed"
                )
            previous = self._attempt_from_record(record)
            now = utc_now()
            candidate_manifest = {
                "schema_version": "deskpilot.agent-result-candidate-reference.v1",
                "candidate_digest": result.result_digest,
                "invocation_id": invocation.invocation_id,
                "agent_result_id": result.result_id,
                "agent_result_digest": result.result_digest,
            }
            material = previous.model_dump(mode="python", exclude={"attempt_digest"})
            material.update(
                {
                    "status": "awaiting_verification",
                    "revision": previous.revision + 1,
                    "candidate_manifest": candidate_manifest,
                    "candidate_digest": result.result_digest,
                    "candidate_recorded_at": now,
                    "updated_at": now,
                }
            )
            current = self._build_attempt(material)
            record.status = current.status
            record.revision = current.revision
            record.candidate_manifest = candidate_manifest
            record.candidate_digest = result.result_digest
            record.candidate_recorded_at = now
            record.manifest = current.model_dump(mode="json")
            record.attempt_digest = current.attempt_digest
            record.updated_at = now

    async def _record_agent_candidate(
        self,
        source: SourceBoundAgentClaim,
        result: AgentOutputResult,
    ) -> None:
        async with self._database.session() as session, session.begin():
            record = await self._locked_attempt_for_claim(session, source)
            node = await session.get(TaskExecutionNodeRecord, source.binding.composite_node_id)
            if node is None or node.status != "awaiting_verification":
                raise TaskLoopAgentProofRejectedError(
                    "Workspace Agent candidate did not enter verification"
                )
            candidate_manifest = self._agent_candidate_manifest(result)
            if record.status == "awaiting_verification":
                if (
                    record.candidate_manifest != candidate_manifest
                    or record.candidate_digest != result.result_digest
                ):
                    raise TaskLoopAgentProofRejectedError(
                        "Recovered Workspace Agent candidate changed"
                    )
                return
            if record.status != "running":
                raise TaskLoopAgentConflictError(
                    "Workspace Agent attempt is not running"
                )
            previous = self._attempt_from_record(record)
            now = utc_now()
            material = previous.model_dump(mode="python", exclude={"attempt_digest"})
            material.update(
                {
                    "status": "awaiting_verification",
                    "revision": previous.revision + 1,
                    "candidate_manifest": candidate_manifest,
                    "candidate_digest": result.result_digest,
                    "candidate_recorded_at": now,
                    "updated_at": now,
                }
            )
            current = self._build_attempt(material)
            record.status = current.status
            record.revision = current.revision
            record.candidate_manifest = candidate_manifest
            record.candidate_digest = result.result_digest
            record.candidate_recorded_at = now
            record.manifest = current.model_dump(mode="json")
            record.attempt_digest = current.attempt_digest
            record.updated_at = now

    async def _verify_workspace_result(
        self,
        source: SourceBoundAgentClaim,
        workspace: WorkspaceFileRead,
        result: AgentOutputResult,
    ) -> None:
        """Persist deterministic Workspace proof without unlocking the node."""

        async with self._database.session() as session, session.begin():
            invocation = await session.scalar(
                select(AgentInvocationRecord)
                .where(
                    AgentInvocationRecord.invocation_id == result.invocation_id
                )
                .with_for_update()
            )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == source.binding.composite_node_id)
                .with_for_update()
            )
            persisted_result = await session.get(AgentResultRecord, result.result_id)
            if invocation is None or node is None or persisted_result is None:
                raise TaskLoopAgentProofRejectedError(
                    "Workspace Agent candidate persistence is incomplete"
                )
            if (
                invocation.run_id != source.claimed.handoff.run_id
                or invocation.node_id != source.binding.composite_node_id
                or invocation.attempt != source.claimed.invocation.attempt
                or invocation.result_id != result.result_id
                or invocation.execution_status != "result_submitted"
                or invocation.verification_status not in {"pending", "verified"}
                or node.status != "awaiting_verification"
                or persisted_result.invocation_id != invocation.invocation_id
                or persisted_result.manifest != result.model_dump(mode="json")
                or persisted_result.result_digest != result.result_digest
                or workspace.relative_path != source.parameter_value
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Workspace Agent candidate crossed its source binding"
                )
            existing = await session.get(
                WorkspaceAgentResultRecord,
                invocation.invocation_id,
            )
            if existing is None:
                session.add(
                    WorkspaceAgentResultRecord(
                        invocation_id=invocation.invocation_id,
                        run_id=invocation.run_id,
                        result_kind="file",
                        manifest=workspace.model_dump(mode="json"),
                        result_digest=workspace.result_digest,
                        created_at=utc_now(),
                    )
                )
            elif (
                existing.run_id != invocation.run_id
                or existing.result_kind != "file"
                or existing.manifest != workspace.model_dump(mode="json")
                or existing.result_digest != workspace.result_digest
            ):
                raise TaskLoopAgentProofRejectedError(
                    "Recovered Workspace deterministic proof changed"
                )
            if invocation.verification_status == "pending":
                invocation.verification_status = "verified"
                invocation.revision += 1
            await session.flush()

    @staticmethod
    def _workspace_agent_result(
        source: SourceBoundAgentClaim,
        workspace: WorkspaceFileRead,
    ) -> AgentOutputResult:
        response_digest = sha256_digest(
            {
                "schema_version": "deskpilot.source-bound-workspace-read-decision.v1",
                "node_binding_digest": source.binding.binding_digest,
                "bound_input_digest": source.binding.bound_input_digest,
                "workspace_result_digest": workspace.result_digest,
            }
        )
        identity = {
            "invocation_id": source.claimed.invocation.invocation_id,
            "attempt": source.claimed.invocation.attempt,
            "workspace_result_digest": workspace.result_digest,
        }
        result_id = f"res_{sha256_digest(identity)}"
        material = {
            "schema_version": "deskpilot.agent-output-result.v1",
            "result_id": result_id,
            "invocation_id": source.claimed.invocation.invocation_id,
            "disposition": "candidate",
            "output": {
                "relative_path": workspace.relative_path,
                "result_digest": workspace.result_digest,
            },
            "evidence_refs": (f"workspace-file:{workspace.result_digest}",),
            "limitation_codes": (),
            "input_digest": source.attempt.input_digest,
            "model_response_digest": response_digest,
            "output_schema_digest": sha256_digest(
                WorkspaceFileRead.model_json_schema()
            ),
        }
        return AgentOutputResult.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )

    @staticmethod
    def _agent_candidate_manifest(
        result: AgentOutputResult,
    ) -> dict[str, object]:
        return {
            "schema_version": "deskpilot.agent-result-candidate-reference.v1",
            "candidate_digest": result.result_digest,
            "invocation_id": result.invocation_id,
            "agent_result_id": result.result_id,
            "agent_result_digest": result.result_digest,
        }

    async def _locked_attempt_for_claim(
        self,
        session: AsyncSession,
        source: SourceBoundAgentClaim,
    ) -> TaskLoopNodeAttemptRecord:
        record = await session.scalar(
            select(TaskLoopNodeAttemptRecord)
            .where(TaskLoopNodeAttemptRecord.attempt_id == source.attempt.attempt_id)
            .with_for_update()
        )
        if record is None:
            raise TaskLoopAgentNotFoundError("Task Loop Agent attempt does not exist")
        attempt = self._attempt_from_record(record)
        if (
            attempt.execution_id != source.execution_id
            or record.node_binding_id != source.binding.node_binding_id
            or attempt.run_id != source.claimed.handoff.run_id
            or attempt.node_id != source.claimed.handoff.target_node_id
            or attempt.attempt != source.claimed.invocation.attempt
            or attempt.claim_owner_id != source.claimed.claim_owner_id
            or attempt.claim_fencing_token != source.claimed.claim_fencing_token
        ):
            raise TaskLoopAgentConflictError("Task Loop Agent attempt fence changed")
        return record

    async def _locked_scope(
        self,
        session: AsyncSession,
        source: SourceBoundAgentClaim,
        source_plan_proof: AgentSourcePlanProof,
    ) -> tuple[
        TaskLoopExecution,
        ModelPlannerNodeBinding,
        ModelPlannerStepBinding,
        ExecutablePlan,
    ]:
        execution_record = await session.scalar(
            select(TaskLoopExecutionRecord)
            .where(TaskLoopExecutionRecord.execution_id == source.execution_id)
            .with_for_update()
        )
        binding_record = await session.scalar(
            select(ModelPlannerNodeBindingRecord)
            .where(ModelPlannerNodeBindingRecord.node_binding_id == source.binding.node_binding_id)
            .with_for_update()
        )
        if execution_record is None or binding_record is None:
            raise TaskLoopAgentNotFoundError(
                "Task Loop execution or Agent binding disappeared"
            )
        execution = self._execution_from_record(execution_record)
        binding = self._binding_from_record(binding_record)
        if binding != source.binding or binding_record.execution_id != execution.execution_id:
            raise TaskLoopAgentProofRejectedError(
                "Source-bound Agent binding changed after claim"
            )
        step_record = await session.get(
            ModelPlannerStepBindingRecord,
            binding.step_binding_id,
        )
        plan_record = await session.get(
            TaskPlanGenerationRecord,
            (execution.task_id, execution.plan_generation),
        )
        if step_record is None or plan_record is None:
            raise TaskLoopAgentProofRejectedError(
                "Source step or composite Plan record is missing"
            )
        step = self._step_from_record(step_record)
        composite_plan = self._plan_from_record(plan_record)
        self._assert_source_plan_proof(
            execution,
            binding,
            step,
            composite_plan,
            source_plan_proof,
        )
        return execution, binding, step, composite_plan

    async def _research_proof(
        self,
        session: AsyncSession,
        plan: AgentVerifiedResultPlanProof,
        binding: ModelPlannerNodeBinding,
    ) -> ResearchAgentVerificationProof:
        research_records = tuple(
            (
                await session.scalars(
                    select(ResearchSessionRecord).where(
                        ResearchSessionRecord.invocation_id
                        == plan.invocation.invocation_id
                    )
                )
            ).all()
        )
        if len(research_records) != 1:
            raise TaskLoopAgentProofRejectedError(
                "Research Invocation has no unique session proof"
            )
        research = research_records[0]
        search_calls = tuple(
            (
                await session.scalars(
                    select(ResearchSearchCallRecord)
                    .where(
                        ResearchSearchCallRecord.research_session_id
                        == research.research_session_id
                    )
                    .order_by(ResearchSearchCallRecord.attempt)
                )
            ).all()
        )
        verifications = tuple(
            (
                await session.scalars(
                    select(VerificationRunRecord).where(
                        VerificationRunRecord.run_id == plan.run.run_id,
                        VerificationRunRecord.node_id == plan.node.node_id,
                        VerificationRunRecord.result_id == plan.result.result_id,
                    )
                )
            ).all()
        )
        if len(search_calls) != 1 or len(verifications) != 1:
            raise TaskLoopAgentProofRejectedError(
                "Research search or VerificationRun proof is not unique"
            )
        verification = verifications[0]
        snapshot = await session.get(
            VerificationEvidenceSnapshotRecord,
            verification.evidence_snapshot_id,
        )
        if snapshot is None:
            raise TaskLoopAgentProofRejectedError(
                "Research verification evidence snapshot is missing"
            )
        claims = tuple(
            (
                await session.scalars(
                    select(ResearchClaimRecord)
                    .where(
                        ResearchClaimRecord.research_session_id
                        == research.research_session_id
                    )
                    .order_by(ResearchClaimRecord.claim_id)
                )
            ).all()
        )
        citations = tuple(
            (
                await session.scalars(
                    select(ResearchCitationRecord)
                    .where(
                        ResearchCitationRecord.research_session_id
                        == research.research_session_id
                    )
                    .order_by(ResearchCitationRecord.citation_id)
                )
            ).all()
        )
        pages = tuple(
            (
                await session.scalars(
                    select(ResearchPageSnapshotRecord)
                    .where(
                        ResearchPageSnapshotRecord.research_session_id
                        == research.research_session_id
                    )
                    .order_by(ResearchPageSnapshotRecord.page_snapshot_id)
                )
            ).all()
        )
        verdicts = tuple(
            (
                await session.scalars(
                    select(ClaimVerdictRecord)
                    .where(
                        ClaimVerdictRecord.verification_run_id
                        == verification.verification_run_id
                    )
                    .order_by(ClaimVerdictRecord.claim_id)
                )
            ).all()
        )
        policy = plan.source_contract.research
        if policy is None:
            raise TaskLoopAgentProofRejectedError(
                "Research source Contract has no research policy"
            )
        query = binding.bound_input_manifest.get("goal")
        if query is None:
            raise TaskLoopAgentProofRejectedError(
                "Research source binding lost its goal"
            )
        return ResearchAgentVerificationProof(
            research=research,
            search_call=search_calls[0],
            search_request=SearchRequest(
                query=query[:500],
                max_results=policy.max_results_per_search,
                allowed_domains=policy.allowed_domains,
            ),
            verification=verification,
            evidence_snapshot=snapshot,
            claims=claims,
            citations=citations,
            pages=pages,
            verdicts=verdicts,
        )

    @staticmethod
    async def _workspace_proof(
        session: AsyncSession,
        invocation: AgentInvocationRecord,
    ) -> WorkspaceReaderVerificationProof:
        record = await session.get(
            WorkspaceAgentResultRecord,
            invocation.invocation_id,
        )
        if record is None:
            raise TaskLoopAgentProofRejectedError(
                "Workspace Reader deterministic result proof is missing"
            )
        return WorkspaceReaderVerificationProof(workspace_result=record)

    @staticmethod
    async def _patch_planner_proof(
        session: AsyncSession,
        invocation: AgentInvocationRecord,
    ) -> WorkspacePatchPlannerVerificationProof:
        turns = tuple(
            (
                await session.scalars(
                    select(AgentModelTurnRecord).where(
                        AgentModelTurnRecord.invocation_id
                        == invocation.invocation_id
                    )
                )
            ).all()
        )
        decisions = tuple(
            (
                await session.scalars(
                    select(AgentDecisionRecord).where(
                        AgentDecisionRecord.invocation_id
                        == invocation.invocation_id
                    )
                )
            ).all()
        )
        if len(turns) != 1 or len(decisions) != 1:
            raise TaskLoopAgentProofRejectedError(
                "Patch Planner has no unique persisted ModelTurn decision"
            )
        return WorkspacePatchPlannerVerificationProof(
            model_turn=turns[0],
            decision=decisions[0],
        )

    @staticmethod
    async def _coding_coordinator_proof(
        session: AsyncSession,
        invocation: AgentInvocationRecord,
    ) -> WorkspaceCodingCoordinatorVerificationProof:
        turns = tuple(
            (
                await session.scalars(
                    select(AgentModelTurnRecord).where(
                        AgentModelTurnRecord.invocation_id
                        == invocation.invocation_id
                    )
                )
            ).all()
        )
        decisions = tuple(
            (
                await session.scalars(
                    select(AgentDecisionRecord).where(
                        AgentDecisionRecord.invocation_id
                        == invocation.invocation_id
                    )
                )
            ).all()
        )
        if len(turns) != 1 or len(decisions) != 1:
            raise TaskLoopAgentProofRejectedError(
                "Coding Coordinator has no unique persisted ModelTurn decision"
            )
        return WorkspaceCodingCoordinatorVerificationProof(
            model_turn=turns[0],
            decision=decisions[0],
        )

    @classmethod
    def _assert_source_plan_proof(
        cls,
        execution: TaskLoopExecution,
        binding: ModelPlannerNodeBinding,
        step: ModelPlannerStepBinding,
        composite_plan: ExecutablePlan,
        proof: AgentSourcePlanProof,
    ) -> None:
        profile = cls._profile(binding)
        source_node = next(
            (
                item
                for item in proof.source_plan.nodes
                if item.node_id == binding.source_node_id
            ),
            None,
        )
        composite_node = next(
            (
                item
                for item in composite_plan.nodes
                if item.node_id == binding.composite_node_id
            ),
            None,
        )
        if (
            step.step_binding_id != binding.step_binding_id
            or step.step_binding_digest != binding.step_binding_digest
            or step.ordinal != binding.step_ordinal
            or step.recipe != binding.recipe
            or step.parameter_bindings != binding.parameter_bindings
            or step.source_plan_id != proof.source_plan.plan_id
            or step.source_plan_manifest_digest
            != proof.source_plan.plan_manifest_digest
            or proof.source_contract.digest != binding.source_contract_digest
            or proof.source_plan.task_contract.digest != proof.source_contract.digest
            or proof.source_plan.plan_id != binding.source_plan_id
            or proof.source_plan.plan_manifest_digest
            != binding.source_plan_manifest_digest
            or composite_plan.plan_id != execution.plan_id
            or composite_plan.plan_manifest_digest
            != execution.plan_manifest_digest
            or composite_plan.plan_id != binding.composite_plan_id
            or composite_plan.plan_manifest_digest
            != binding.composite_plan_manifest_digest
            or source_node is None
            or composite_node is None
            or source_node.local_key != profile.source_local_key
            or source_node.node_spec_digest != binding.source_node_spec_digest
            or composite_node.node_spec_digest
            != binding.composite_node_spec_digest
        ):
            raise TaskLoopAgentProofRejectedError(
                "Agent source recipe, step, Contract, or Plan proof changed"
            )

    def _assert_bound_node(
        self,
        execution: TaskLoopExecution,
        binding: ModelPlannerNodeBinding,
        profile: _AgentProfile,
        node: TaskExecutionNodeRecord,
    ) -> None:
        authority = binding.effective_authority
        eligibility = binding.runtime_eligibility
        bound_agent = authority.bound_agent
        capability = authority.capability
        try:
            if bound_agent is None or capability is None:
                raise TaskLoopAgentAdapterError(
                    "Agent binding has no exact Agent or Capability"
                )
            adapter = self._adapters.resolve(
                route_id=binding.recipe.route_id,
                source_local_key=binding.mapping.source_local_key,
                bound_agent=bound_agent,
                capability=capability,
            )
        except TaskLoopAgentAdapterError as error:
            raise TaskLoopAgentProofRejectedError(
                "Task Loop Agent adapter is no longer eligible"
            ) from error
        if (
            binding.task_id != execution.task_id
            or binding.composite_plan_id != execution.plan_id
            or binding.composite_plan_manifest_digest
            != execution.plan_manifest_digest
            or binding.composite_node_id != node.node_id
            or binding.composite_node_spec_digest != node.node_spec_digest
            or binding.mapping.source_local_key != profile.source_local_key
            or authority.authority_rule != "composite_intersection_source_step"
            or authority.node_kind.value != "agent"
            or bound_agent is None
            or bound_agent.agent_id != profile.agent_id
            or capability is None
            or capability.capability_id != profile.capability_id
            or eligibility.runtime_kind != "agent"
            or not eligibility.runtime_enabled
            or eligibility.bound_agent != bound_agent
            or eligibility.agent_adapter_id != adapter.adapter_id
            or eligibility.agent_adapter_manifest_digest != adapter.manifest_digest
            or node.run_id != execution.run_id
            or node.node_kind != "agent"
            or node.bound_agent != bound_agent.model_dump(mode="json")
            or node.capability != capability.model_dump(mode="json")
            or not node.runtime_enabled
        ):
            raise TaskLoopAgentProofRejectedError(
                "Agent node authority or runtime eligibility changed"
            )
        values = binding.bound_input_manifest
        if not values.get(profile.parameter_name):
            raise TaskLoopAgentProofRejectedError(
                "Agent source-step input shape changed"
            )
        parameter_values = {
            item.parameter_name: item.value for item in binding.parameter_bindings
        }
        fixed_names: set[str] = set()
        if profile.route_id == "workspace_coding_loop":
            fixed_names.add("test_kind")
            if "file_count" in values:
                fixed_names.add("file_count")
        if (
            set(values) != set(parameter_values) | fixed_names
            or any(values.get(name) != value for name, value in parameter_values.items())
            or (
                fixed_names
                and values.get("test_kind") not in {"python", "node"}
            )
            or (
                "file_count" in fixed_names
                and values.get("file_count")
                not in {str(item) for item in range(3, WORKSPACE_CODING_MAX_FILES + 1)}
            )
        ):
            raise TaskLoopAgentProofRejectedError(
                "Agent source-step parameter binding changed"
            )

    @classmethod
    def _assert_claim(
        cls,
        binding: ModelPlannerNodeBinding,
        execution: TaskLoopExecution,
        profile: _AgentProfile,
        claimed: ClaimedInvocation,
    ) -> None:
        authority = binding.effective_authority
        if (
            claimed.handoff.task_id != execution.task_id
            or claimed.handoff.run_id != execution.run_id
            or claimed.handoff.target_node_id != binding.composite_node_id
            or claimed.invocation.node_id != binding.composite_node_id
            or claimed.invocation.attempt < 1
            or claimed.handoff.target_agent != authority.bound_agent
            or claimed.handoff.capability != authority.capability
            or claimed.handoff.budget_allocation != authority.budget
            or claimed.handoff.target_agent.agent_id != profile.agent_id
        ):
            raise TaskLoopAgentProofRejectedError(
                "Agent runtime claim differs from exact source-step authority"
            )

    @classmethod
    def _assert_attempt_scope(
        cls,
        source: SourceBoundAgentClaim,
        execution: TaskLoopExecution,
        binding: ModelPlannerNodeBinding,
        run: TaskExecutionRunRecord,
        node: TaskExecutionNodeRecord,
        attempt: TaskLoopNodeAttemptRecord,
    ) -> None:
        persisted = cls._attempt_from_record(attempt)
        if (
            persisted.attempt_id != source.attempt.attempt_id
            or persisted.execution_id != execution.execution_id
            or attempt.node_binding_id != binding.node_binding_id
            or persisted.run_id != run.run_id
            or persisted.node_id != node.node_id
            or persisted.attempt != source.claimed.invocation.attempt
            or (
                persisted.status != "verified"
                and persisted.claim_owner_id != source.claimed.claim_owner_id
            )
            or (
                persisted.status == "verified"
                and persisted.claim_owner_id is not None
            )
            or persisted.claim_fencing_token
            != source.claimed.claim_fencing_token
            or run.task_id != execution.task_id
            or run.plan_generation != execution.plan_generation
            or run.plan_digest != execution.plan_manifest_digest
            or node.run_id != run.run_id
            or node.attempt_count != persisted.attempt
            or node.claim_fencing_token != persisted.claim_fencing_token
        ):
            raise TaskLoopAgentConflictError(
                "Task Loop Agent attempt crossed its claim or execution scope"
            )

    @staticmethod
    def _input_manifest(
        binding: ModelPlannerNodeBinding,
        profile: _AgentProfile,
    ) -> dict[str, object]:
        return {
            "schema_version": "deskpilot.source-bound-agent-input.v1",
            "route_id": profile.route_id,
            "parameter_name": profile.parameter_name,
            "value": binding.bound_input_manifest[profile.parameter_name],
            "bound_input_digest": binding.bound_input_digest,
            "node_binding_digest": binding.binding_digest,
        }

    @staticmethod
    def _profile(binding: ModelPlannerNodeBinding) -> _AgentProfile:
        profile = _PROFILES.get(
            (
                cast(AgentSourceRoute, binding.recipe.route_id),
                binding.mapping.source_local_key,
            )
        )
        if profile is None and binding.recipe.route_id == "workspace_coding_loop":
            local_key = binding.mapping.source_local_key
            if local_key == "coordinate_bounded_coding":
                profile = _AgentProfile(
                    route_id="workspace_coding_loop",
                    source_local_key=local_key,
                    parameter_name="project_path",
                    agent_id="builtin.workspace_bounded_coordinator",
                    capability_id="workspace.dynamic.coordinate.v1",
                )
            else:
                parameter_name = workspace_coding_parameter_for_key(local_key)
                if parameter_name is not None and parameter_name not in {
                    "primary_path",
                    "secondary_path",
                }:
                    profile = _AgentProfile(
                        route_id="workspace_coding_loop",
                        source_local_key=local_key,
                        parameter_name=cast(AgentSourceParameter, parameter_name),
                        agent_id=(
                            "builtin.workspace_reader"
                            if is_workspace_coding_reader_key(local_key)
                            else "builtin.workspace_patch_planner"
                        ),
                        capability_id=(
                            "workspace.file.read.v1"
                            if is_workspace_coding_reader_key(local_key)
                            else "workspace.patch.propose.v1"
                        ),
                    )
        if profile is None:
            raise TaskLoopAgentProofRejectedError(
                "Agent source Route is not activated by stage 112B"
            )
        return profile

    @staticmethod
    def _is_workspace_file_source(source: SourceBoundAgentClaim) -> bool:
        return (
            source.route_id == "workspace_file_read"
            and source.binding.mapping.source_local_key == "workspace_file_read"
        ) or (
            source.route_id == "workspace_coding_loop"
            and is_workspace_coding_reader_key(
                source.binding.mapping.source_local_key
            )
        )

    @staticmethod
    def _execution_from_record(record: TaskLoopExecutionRecord) -> TaskLoopExecution:
        execution = TaskLoopExecution.model_validate(record.manifest)
        for field in (
            "execution_id",
            "loop_id",
            "draft_id",
            "task_id",
            "plan_id",
            "plan_generation",
            "plan_manifest_digest",
            "run_id",
            "status",
            "revision",
            "event_count",
            "latest_event_id",
            "latest_event_digest",
            "node_binding_count",
            "binding_set_digest",
            "execution_digest",
        ):
            if getattr(record, field) != getattr(execution, field):
                raise TaskLoopAgentProofRejectedError(
                    "Task Loop execution columns changed from its manifest"
                )
        return execution

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
            verification_status=InvocationVerificationStatus(
                record.verification_status
            ),
            result_id=record.result_id,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )

    @staticmethod
    def _binding_from_record(
        record: ModelPlannerNodeBindingRecord,
    ) -> ModelPlannerNodeBinding:
        binding = ModelPlannerNodeBinding.model_validate(record.manifest)
        direct_fields = (
            "node_binding_id",
            "task_id",
            "user_message_id",
            "draft_id",
            "step_binding_id",
            "step_binding_digest",
            "step_ordinal",
            "offer_id",
            "offer_key",
            "offer_digest",
            "policy_snapshot_digest",
            "source_contract_digest",
            "source_plan_id",
            "source_plan_manifest_digest",
            "source_node_id",
            "source_node_spec_digest",
            "composite_contract_digest",
            "composite_plan_id",
            "composite_plan_manifest_digest",
            "composite_node_id",
            "composite_node_spec_digest",
            "parameter_bindings_digest",
            "bound_input_digest",
            "binding_digest",
        )
        if any(getattr(record, field) != getattr(binding, field) for field in direct_fields):
            raise TaskLoopAgentProofRejectedError(
                "Model Planner node-binding columns changed from its manifest"
            )
        if (
            record.recipe_manifest != binding.recipe.model_dump(mode="json")
            or record.recipe_digest != binding.recipe.route_manifest_digest
            or record.mapping_manifest != binding.mapping.model_dump(mode="json")
            or record.mapping_digest != binding.mapping.mapping_digest
            or record.parameter_bindings_manifest
            != [item.model_dump(mode="json") for item in binding.parameter_bindings]
            or record.bound_input_manifest != binding.bound_input_manifest
            or record.effective_authority_manifest
            != binding.effective_authority.model_dump(mode="json")
            or record.effective_authority_digest
            != binding.effective_authority.authority_digest
            or record.runtime_eligibility_manifest
            != binding.runtime_eligibility.model_dump(mode="json")
            or record.runtime_eligibility_digest
            != binding.runtime_eligibility.eligibility_digest
        ):
            raise TaskLoopAgentProofRejectedError(
                "Model Planner node-binding proof manifests changed"
            )
        return binding

    @staticmethod
    def _step_from_record(record: ModelPlannerStepBindingRecord) -> ModelPlannerStepBinding:
        step = ModelPlannerStepBinding.model_validate(record.manifest)
        if (
            record.step_binding_id != step.step_binding_id
            or record.step_binding_digest != step.step_binding_digest
            or record.task_id != step.source.task_id
            or record.user_message_id != step.source.user_message_id
            or record.ordinal != step.ordinal
            or record.offer_id != step.offer.offer_id
            or record.offer_key != step.offer.offer_key
            or record.offer_digest != step.offer.offer_digest
            or record.recipe_id != step.recipe.route_id
            or record.recipe_version != step.recipe.route_version
            or record.recipe_digest != step.recipe.route_manifest_digest
            or record.policy_snapshot_digest != step.policy_snapshot_digest
            or record.source_plan_id != step.source_plan_id
            or record.source_plan_manifest_digest
            != step.source_plan_manifest_digest
            or record.source_plan_binding_snapshot_digest
            != step.source_plan_binding_snapshot_digest
            or record.parameter_bindings_manifest
            != [item.model_dump(mode="json") for item in step.parameter_bindings]
            or record.parameter_bindings_digest
            != step.parameter_bindings_digest
            or record.node_mappings_manifest
            != [item.model_dump(mode="json") for item in step.node_mappings]
            or record.node_mappings_digest != step.node_mappings_digest
        ):
            raise TaskLoopAgentProofRejectedError(
                "Model Planner source-step columns changed from its manifest"
            )
        return step

    @staticmethod
    def _plan_from_record(record: TaskPlanGenerationRecord) -> ExecutablePlan:
        plan = ExecutablePlan.model_validate(record.manifest)
        if (
            record.task_id != plan.task_id
            or record.generation != plan.plan_generation
            or record.plan_id != plan.plan_id
            or record.contract_version != plan.task_contract.version
            or record.contract_digest != plan.task_contract.digest
            or record.plan_manifest_digest != plan.plan_manifest_digest
        ):
            raise TaskLoopAgentProofRejectedError(
                "Composite Plan columns changed from its manifest"
            )
        return plan

    @staticmethod
    def _build_attempt(material: dict[str, object]) -> TaskLoopNodeAttempt:
        return TaskLoopNodeAttempt.model_validate(
            {**material, "attempt_digest": sha256_digest(material)}
        )

    @staticmethod
    def _apply_attempt_record(
        record: TaskLoopNodeAttemptRecord,
        attempt: TaskLoopNodeAttempt,
    ) -> None:
        for field in (
            "status",
            "revision",
            "claim_owner_id",
            "claim_fencing_token",
            "claim_acquired_at",
            "claim_expires_at",
            "input_manifest",
            "input_digest",
            "context_manifest",
            "context_digest",
            "candidate_manifest",
            "candidate_digest",
            "candidate_recorded_at",
            "verification_manifest",
            "verification_digest",
            "verified_at",
            "receipt_manifest",
            "receipt_digest",
            "error_code",
            "error_digest",
            "created_at",
            "updated_at",
            "attempt_digest",
        ):
            setattr(record, field, getattr(attempt, field))
        record.manifest = attempt.model_dump(mode="json")

    @classmethod
    def _attempt_from_record(
        cls,
        record: TaskLoopNodeAttemptRecord,
    ) -> TaskLoopNodeAttempt:
        attempt = TaskLoopNodeAttempt.model_validate(record.manifest)
        for field in (
            "attempt_id",
            "execution_id",
            "node_binding_id",
            "run_id",
            "node_id",
            "attempt",
            "status",
            "revision",
            "claim_owner_id",
            "claim_fencing_token",
            "claim_acquired_at",
            "claim_expires_at",
            "input_manifest",
            "input_digest",
            "context_manifest",
            "context_digest",
            "candidate_manifest",
            "candidate_digest",
            "candidate_recorded_at",
            "verification_manifest",
            "verification_digest",
            "verified_at",
            "receipt_manifest",
            "receipt_digest",
            "error_code",
            "error_digest",
            "created_at",
            "updated_at",
            "attempt_digest",
        ):
            record_value = getattr(record, field)
            attempt_value = getattr(attempt, field)
            if isinstance(record_value, datetime):
                record_value = cls._aware(record_value)
            if record_value != attempt_value:
                raise TaskLoopAgentProofRejectedError(
                    "Task Loop attempt columns changed from its manifest"
                )
        expected = cls._build_attempt(
            attempt.model_dump(mode="python", exclude={"attempt_digest"})
        )
        if expected != attempt:
            raise TaskLoopAgentProofRejectedError(
                "Task Loop attempt digest changed"
            )
        return attempt

    @classmethod
    def _transition_attempt(
        cls,
        record: TaskLoopNodeAttemptRecord,
        *,
        status: Literal["running", "awaiting_verification"],
    ) -> None:
        previous = cls._attempt_from_record(record)
        now = utc_now()
        material = previous.model_dump(mode="python", exclude={"attempt_digest"})
        material.update(
            {
                "status": status,
                "revision": previous.revision + 1,
                "updated_at": now,
            }
        )
        current = cls._build_attempt(material)
        record.status = status
        record.revision = current.revision
        record.manifest = current.model_dump(mode="json")
        record.attempt_digest = current.attempt_digest
        record.updated_at = now

    @classmethod
    def _settle_attempt_verified(
        cls,
        record: TaskLoopNodeAttemptRecord,
        receipt_manifest: dict[str, object],
        result_ref: VerifiedCapabilityResultRef,
        *,
        candidate_manifest: dict[str, object],
        verification_manifest: dict[str, object],
    ) -> None:
        previous = cls._attempt_from_record(record)
        if previous.status not in {
            "claimed",
            "running",
            "awaiting_verification",
        }:
            raise TaskLoopAgentConflictError(
                "Task Loop attempt cannot transition to verified"
            )
        receipt_digest = sha256_digest(receipt_manifest)
        if receipt_manifest["result_ref_digest"] != result_ref.result_ref_digest:
            raise TaskLoopAgentProofRejectedError(
                "Agent verification receipt ResultRef changed"
            )
        now = utc_now()
        candidate_digest = cast(str, candidate_manifest["candidate_digest"])
        verification_digest = cast(
            str,
            verification_manifest["verification_digest"],
        )
        material = previous.model_dump(mode="python", exclude={"attempt_digest"})
        material.update(
            {
                "status": "verified",
                "revision": previous.revision + 1,
                "claim_owner_id": None,
                "claim_acquired_at": None,
                "claim_expires_at": None,
                "candidate_manifest": candidate_manifest,
                "candidate_digest": candidate_digest,
                "candidate_recorded_at": (
                    previous.candidate_recorded_at or now
                ),
                "verification_manifest": verification_manifest,
                "verification_digest": verification_digest,
                "verified_at": now,
                "receipt_manifest": receipt_manifest,
                "receipt_digest": receipt_digest,
                "error_code": None,
                "error_digest": None,
                "updated_at": now,
            }
        )
        current = cls._build_attempt(material)
        record.status = "verified"
        record.revision = current.revision
        record.claim_owner_id = None
        record.claim_acquired_at = None
        record.claim_expires_at = None
        record.candidate_manifest = candidate_manifest
        record.candidate_digest = candidate_digest
        record.candidate_recorded_at = current.candidate_recorded_at
        record.verification_manifest = verification_manifest
        record.verification_digest = verification_digest
        record.verified_at = now
        record.receipt_manifest = receipt_manifest
        record.receipt_digest = receipt_digest
        record.error_code = None
        record.error_digest = None
        record.manifest = current.model_dump(mode="json")
        record.attempt_digest = current.attempt_digest
        record.updated_at = now

    @classmethod
    def _assert_verified_attempt(
        cls,
        record: TaskLoopNodeAttemptRecord,
        result_ref: VerifiedCapabilityResultRef,
    ) -> None:
        attempt = cls._attempt_from_record(record)
        if (
            attempt.status != "verified"
            or attempt.verification_digest != result_ref.verification_digest
            or record.receipt_manifest is None
            or record.receipt_digest != sha256_digest(record.receipt_manifest)
            or record.receipt_manifest.get("result_ref_digest")
            != result_ref.result_ref_digest
        ):
            raise TaskLoopAgentProofRejectedError(
                "Recovered Task Loop verified attempt changed"
            )

    @staticmethod
    def _assert_verified_record(
        record: TaskLoopVerifiedResultRecord,
        expected: TaskLoopVerifiedResult,
    ) -> None:
        actual = TaskLoopVerifiedResult(
            result_ref_id=record.result_ref_id,
            attempt_id=record.attempt_id,
            execution_id=record.execution_id,
            node_binding_id=record.node_binding_id,
            node_binding_digest=record.node_binding_digest,
            run_id=record.run_id,
            node_id=record.node_id,
            producer_kind=cast(
                Literal["capability_executor", "agent_bridge"],
                record.producer_kind,
            ),
            capability_manifest=record.capability_manifest,
            capability_digest=record.capability_digest,
            agent_binding_manifest=record.agent_binding_manifest,
            agent_binding_digest=record.agent_binding_digest,
            executor_manifest_digest=record.executor_manifest_digest,
            agent_result_proof_digest=record.agent_result_proof_digest,
            input_binding_digest=record.input_binding_digest,
            context_digest=record.context_digest,
            candidate_digest=record.candidate_digest,
            result_kind=record.result_kind,
            output_manifest=record.output_manifest,
            output_schema_digest=record.output_schema_digest,
            output_digest=record.output_digest,
            verification_manifest=record.verification_manifest,
            verification_digest=record.verification_digest,
            result_ref_manifest=record.result_ref_manifest,
            result_ref_digest=record.result_ref_digest,
            created_at=(
                record.created_at.replace(tzinfo=UTC)
                if record.created_at.tzinfo is None
                else record.created_at
            ),
        )
        if actual != expected:
            raise TaskLoopAgentProofRejectedError(
                "Recovered Task Loop ResultRef changed"
            )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


__all__ = [
    "AgentSourcePlanProof",
    "SourceBoundAgentClaim",
    "TaskLoopAgentConflictError",
    "TaskLoopAgentNotFoundError",
    "TaskLoopAgentProofRejectedError",
    "TaskLoopAgentRuntime",
    "TaskLoopAgentRuntimeError",
    "TaskLoopAgentRuntimeUnavailableError",
]
