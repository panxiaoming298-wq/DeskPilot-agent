"""Persistent zero-tool Explorer Invocation over one immutable workspace snapshot."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_execution_runtime import (
    AgentExecutionRuntime,
    AgentRuntimeConflictError,
    AgentRuntimeError,
)
from deskpilot.application.agent_model_loop import (
    AgentModelLoopError,
    AgentModelLoopOutcomeUnknownError,
    AgentModelLoopRuntime,
    DecisionReducer,
    DispatchedAgentDecision,
)
from deskpilot.application.agent_registry import AgentRegistry, AgentRegistryError
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningError,
)
from deskpilot.application.plan_compiler import (
    workspace_coding_explorer_contract,
    workspace_coding_explorer_draft,
)
from deskpilot.application.verified_edges import mark_verified_and_unlock
from deskpilot.application.workspace_coding_exploration_binder import (
    WorkspaceCodingExplorationBinder,
    WorkspaceCodingExplorationNotFoundError,
    WorkspaceCodingExplorationProofRejectedError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import (
    AgentOutputResult,
    ClaimedInvocation,
    ExecutionNodeStatus,
    ExecutionRunStatus,
    InvocationExecutionStatus,
    InvocationVerificationStatus,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelExecutionBudget,
    ModelMessage,
    ModelRequest,
    ModelRole,
    PrivacyMode,
    StructuredOutputDefinition,
)
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingExplorationDecision,
    WorkspaceCodingExplorationProposal,
    WorkspaceCodingExplorationSnapshot,
    WorkspaceCodingExplorerRunBinding,
    WorkspaceCodingExplorerTurnProof,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentResultRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskRecord,
    WorkspaceCodingExplorationProposalRecord,
    WorkspaceCodingExplorationSnapshotRecord,
    WorkspaceCodingExplorerRunBindingRecord,
    WorkspaceCodingExplorerTurnProofRecord,
    utc_now,
)


class WorkspaceCodingExplorerRuntimeError(RuntimeError):
    code = "WORKSPACE_CODING_EXPLORER_RUNTIME_ERROR"


class WorkspaceCodingExplorerConflictError(WorkspaceCodingExplorerRuntimeError):
    code = "WORKSPACE_CODING_EXPLORER_CONFLICT"


class WorkspaceCodingExplorerProofRejectedError(WorkspaceCodingExplorerRuntimeError):
    code = "WORKSPACE_CODING_EXPLORER_PROOF_REJECTED"


class WorkspaceCodingExplorerRuntime:
    """Activate and run one exact Explorer without adding an execution FSM."""

    def __init__(
        self,
        database: Database,
        binder: WorkspaceCodingExplorationBinder,
        agents: AgentRegistry,
        planning: PlanCompilationService,
        execution: AgentExecutionRuntime,
        model_loop: AgentModelLoopRuntime,
    ) -> None:
        self._database = database
        self._binder = binder
        self._agents = agents
        self._planning = planning
        self._execution = execution
        self._model_loop = model_loop

    async def activate(
        self,
        snapshot_id: str,
    ) -> WorkspaceCodingExplorerRunBinding:
        """Atomically seal the Explorer Contract, Plan, Run and node mapping."""

        snapshot = await self._binder.get_snapshot(snapshot_id=snapshot_id)
        self._binder.revalidate_snapshot(snapshot)
        contract = workspace_coding_explorer_contract(snapshot.task_id)
        draft = workspace_coding_explorer_draft(snapshot.task_id)
        expected_plan = self._planning.preview_initial(contract, draft)
        try:
            async with self._database.session() as session, session.begin():
                snapshot_record = await session.scalar(
                    select(WorkspaceCodingExplorationSnapshotRecord)
                    .where(
                        WorkspaceCodingExplorationSnapshotRecord.snapshot_id == snapshot.snapshot_id
                    )
                    .with_for_update()
                )
                self._assert_locked_snapshot(snapshot_record, snapshot)
                existing = await session.scalar(
                    select(WorkspaceCodingExplorerRunBindingRecord)
                    .where(
                        WorkspaceCodingExplorerRunBindingRecord.snapshot_id == snapshot.snapshot_id
                    )
                    .with_for_update()
                )
                if existing is not None:
                    persisted = self._run_binding_from_record(existing)
                    if (
                        persisted.task_contract != contract
                        or persisted.draft_plan != draft
                        or persisted.expected_plan != expected_plan
                    ):
                        raise WorkspaceCodingExplorerProofRejectedError(
                            "Existing Explorer Run binding changed across recovery"
                        )
                    return persisted
                activated = await self._planning.activate_initial_once_in_session(
                    session,
                    contract,
                    draft,
                )
                if activated.plan != expected_plan:
                    raise WorkspaceCodingExplorerProofRejectedError(
                        "Explorer Plan changed during activation"
                    )
                run = await self._execution.start_exact_in_session(
                    session,
                    expected_plan,
                )
                nodes = {item.local_key: item for item in run.nodes}
                explorer_node = nodes.get("propose_file_set")
                if (
                    set(nodes) != {"propose_file_set", "final_acceptance", "delivery"}
                    or explorer_node is None
                    or explorer_node.bound_agent is None
                ):
                    raise WorkspaceCodingExplorerProofRejectedError(
                        "Explorer execution Run crossed its exact Plan"
                    )
                binding = WorkspaceCodingExplorerRunBinding.build(
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_digest=snapshot.snapshot_digest,
                    task_contract=contract,
                    draft_plan=draft,
                    expected_plan=expected_plan,
                    run_id=run.run_id,
                    explorer_node_id=explorer_node.node_id,
                    explorer_node_spec_digest=next(
                        item.node_spec_digest
                        for item in expected_plan.nodes
                        if item.node_id == explorer_node.node_id
                    ),
                    explorer_agent=explorer_node.bound_agent,
                    created_at=utc_now(),
                )
                session.add(self._run_binding_record(binding))
        except IntegrityError:
            recovered = await self.get_binding(snapshot_id=snapshot.snapshot_id)
            if recovered is not None:
                return recovered
            raise WorkspaceCodingExplorerConflictError(
                "Concurrent Explorer activation did not converge"
            ) from None
        except PlanningError as error:
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer generation-1 planning was rejected"
            ) from error
        final_binding = await self.get_binding(snapshot_id=snapshot.snapshot_id)
        if final_binding is None:
            raise WorkspaceCodingExplorerConflictError("Explorer Run binding was not persisted")
        return final_binding

    async def run(
        self,
        snapshot_id: str,
        *,
        owner_id: str = "workspace-coding-explorer",
        lease_seconds: int = 60,
    ) -> WorkspaceCodingExplorationProposal:
        """Run or recover one exact persistent Explorer Invocation and Model Turn."""

        binding = await self.activate(snapshot_id)
        snapshot = await self._binder.get_snapshot(snapshot_id=snapshot_id)
        self._binder.revalidate_snapshot(snapshot)
        try:
            proposal = await self._binder.get_proposal(snapshot_id=snapshot_id)
        except WorkspaceCodingExplorationNotFoundError:
            proposal = None
        if proposal is not None:
            await self.get_turn_proof(proposal_id=proposal.proposal_id)
            return proposal
        await self._assert_current_binding(binding, snapshot)
        claimed = await self._execution.claim_next(
            binding.run_id,
            owner_id,
            lease_seconds=lease_seconds,
            node_id=binding.explorer_node_id,
        )
        if claimed is None:
            raise WorkspaceCodingExplorerConflictError(
                "Explorer Run is not claimable; active or unknown work is never replayed"
            )
        self._assert_claim(binding, claimed)
        request = await self._request(binding, snapshot, claimed)
        await self._execution.start_invocation(
            claimed.invocation.invocation_id,
            claimed.claim_owner_id,
            claimed.claim_fencing_token,
        )
        try:
            dispatched = await self._model_loop.dispatch(
                claimed,
                turn_no=1,
                request=request,
                decision_model=WorkspaceCodingExplorationDecision,
            )
        except AgentModelLoopOutcomeUnknownError:
            raise
        except AgentModelLoopError as error:
            await self._terminalize(claimed)
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer Model Turn route was rejected"
            ) from error
        decision = cast(WorkspaceCodingExplorationDecision, dispatched.decision)
        try:
            self._binder.revalidate_snapshot(snapshot)
            self._binder.validate_decision(snapshot, decision)
            self._assert_response_budget(claimed, dispatched)
        except (WorkspaceCodingExplorationProofRejectedError, ValueError) as error:
            await self._reject_dispatched(claimed, dispatched, type(error).__name__)
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer decision or snapshot proof was rejected"
            ) from error
        try:
            await self._model_loop.accept(
                claimed,
                dispatched,
                decision,
                binding_id=binding.binding_id,
                reducer=self._proposal_reducer(
                    binding,
                    snapshot,
                    claimed,
                    decision,
                ),
            )
        except (
            AgentRuntimeConflictError,
            IntegrityError,
            ValidationError,
            WorkspaceCodingExplorerRuntimeError,
            WorkspaceCodingExplorationProofRejectedError,
        ) as error:
            await self._reject_dispatched(claimed, dispatched, type(error).__name__)
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer proposal persistence proof was rejected"
            ) from error
        proposal = await self._binder.get_proposal(snapshot_id=snapshot_id)
        await self.get_turn_proof(proposal_id=proposal.proposal_id)
        return proposal

    async def get_binding(
        self,
        *,
        snapshot_id: str | None = None,
        task_id: str | None = None,
        binding_id: str | None = None,
    ) -> WorkspaceCodingExplorerRunBinding | None:
        if sum(value is not None for value in (snapshot_id, task_id, binding_id)) != 1:
            raise ValueError("Exactly one Explorer Run binding key is required")
        async with self._database.session() as session:
            statement = select(WorkspaceCodingExplorerRunBindingRecord)
            if snapshot_id is not None:
                statement = statement.where(
                    WorkspaceCodingExplorerRunBindingRecord.snapshot_id == snapshot_id
                )
            elif task_id is not None:
                statement = statement.where(
                    WorkspaceCodingExplorerRunBindingRecord.source_task_id == task_id
                )
            else:
                statement = statement.where(
                    WorkspaceCodingExplorerRunBindingRecord.binding_id == binding_id
                )
            record = await session.scalar(statement)
            if record is None:
                return None
            binding = self._run_binding_from_record(record)
        snapshot = await self._binder.get_snapshot(snapshot_id=binding.snapshot_id)
        await self._assert_current_binding(binding, snapshot)
        return binding

    async def get_turn_proof(
        self,
        *,
        proposal_id: str,
    ) -> WorkspaceCodingExplorerTurnProof:
        async with self._database.session() as session:
            record = await session.scalar(
                select(WorkspaceCodingExplorerTurnProofRecord).where(
                    WorkspaceCodingExplorerTurnProofRecord.proposal_id == proposal_id
                )
            )
            if record is None:
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer proposal has no persisted Model Turn proof"
                )
            proof = self._turn_proof_from_record(record)
            proposal_record = await session.get(
                WorkspaceCodingExplorationProposalRecord,
                proof.proposal_id,
            )
            binding_record = await session.get(
                WorkspaceCodingExplorerRunBindingRecord,
                proof.run_binding_id,
            )
            invocation = await session.get(AgentInvocationRecord, proof.invocation_id)
            turn = await session.get(AgentModelTurnRecord, proof.turn_id)
            decision = await session.get(AgentDecisionRecord, proof.agent_decision_id)
            result = (
                await session.get(AgentResultRecord, invocation.result_id)
                if invocation is not None and invocation.result_id is not None
                else None
            )
            if proposal_record is None or binding_record is None:
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer Turn proof lost its proposal or Run binding"
                )
            proposal = WorkspaceCodingExplorationProposal.model_validate(proposal_record.manifest)
            binding = self._run_binding_from_record(binding_record)
            if (
                proof.proposal_digest != proposal.proposal_digest
                or proof.run_binding_digest != binding.binding_digest
                or invocation is None
                or invocation.run_id != binding.run_id
                or invocation.node_id != binding.explorer_node_id
                or invocation.execution_status != InvocationExecutionStatus.RESULT_SUBMITTED.value
                or invocation.verification_status != InvocationVerificationStatus.VERIFIED.value
                or turn is None
                or turn.invocation_id != invocation.invocation_id
                or turn.status != "succeeded"
                or turn.request_digest != proof.model_request_digest
                or turn.response_digest != proof.model_response_digest
                or decision is None
                or decision.turn_id != turn.turn_id
                or decision.invocation_id != invocation.invocation_id
                or decision.binding_id != binding.binding_id
                or decision.kind != "propose_file_set"
                or decision.manifest != proposal.decision.model_dump(mode="json")
                or decision.decision_digest != proof.agent_decision_digest
                or result is None
            ):
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer persisted Invocation/Turn/Decision proof crossed its proposal"
                )
            try:
                envelope = AgentOutputResult.model_validate(result.manifest)
            except ValidationError as error:
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer Agent Result envelope is invalid"
                ) from error
            if (
                envelope.result_digest != result.result_digest
                or envelope.invocation_id != invocation.invocation_id
                or envelope.input_digest != proof.model_request_digest
                or envelope.model_response_digest != proof.model_response_digest
                or envelope.output != proposal.decision.model_dump(mode="json")
                or f"workspace-explorer-turn:{proof.proof_digest}" not in envelope.evidence_refs
            ):
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer Agent Result crossed its Turn proof"
                )
            return proof

    async def _request(
        self,
        binding: WorkspaceCodingExplorerRunBinding,
        snapshot: WorkspaceCodingExplorationSnapshot,
        claimed: ClaimedInvocation,
    ) -> ModelRequest:
        async with self._database.session() as session:
            task = await session.get(TaskRecord, snapshot.task_id)
        if task is None:
            raise WorkspaceCodingExplorerProofRejectedError("Explorer source Task is missing")
        files = [
            {
                "relative_path": item.relative_path,
                "proof_digest": item.proof_digest,
            }
            for item in snapshot.files
        ]
        payload = {
            "external_untrusted_objective": task.goal,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "catalog_digest": snapshot.catalog_digest,
            "ecosystem": snapshot.ecosystem,
            "test_path": snapshot.test_path,
            "files": files,
            "authority": "proposal_only_no_file_read_or_write",
        }
        budget = claimed.handoff.budget_allocation
        return ModelRequest(
            request_id=f"workspace-explorer-{claimed.invocation.invocation_id[-24:]}",
            task_id=task.task_id,
            role=ModelRole.PLANNER,
            messages=(
                ModelMessage(
                    role="system",
                    content="Return one strict snapshot-bound Explorer decision.",
                ),
                ModelMessage(
                    role="user",
                    content=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:200_000],
                ),
            ),
            privacy_mode=cast(PrivacyMode, task.privacy_mode),
            requirements=ModelCapabilityRequirements(
                structured_output=True,
                strict_json_schema=True,
                min_context_tokens=8_192,
            ),
            output_schema=StructuredOutputDefinition.from_model(
                name="workspace_coding_exploration_decision",
                description="One unprivileged exact file-set proposal",
                model=WorkspaceCodingExplorationDecision,
                strict=True,
            ),
            max_output_tokens=budget.output_tokens,
            timeout_seconds=float(budget.wall_seconds),
            execution_budget=ModelExecutionBudget(
                max_attempts=1,
                max_retry_delay_seconds=0,
                max_task_cost_micros=budget.cost_micros,
            ),
            metadata={
                "workspace_explorer_run_binding_id": binding.binding_id,
                "workspace_explorer_run_binding_digest": binding.binding_digest,
                "workspace_exploration_snapshot_id": snapshot.snapshot_id,
                "workspace_exploration_snapshot_digest": snapshot.snapshot_digest,
                "workspace_exploration_catalog_digest": snapshot.catalog_digest,
                "workspace_exploration_files": cast(JsonValue, files),
            },
        )

    def _proposal_reducer(
        self,
        binding: WorkspaceCodingExplorerRunBinding,
        snapshot: WorkspaceCodingExplorationSnapshot,
        claimed: ClaimedInvocation,
        decision: WorkspaceCodingExplorationDecision,
    ) -> DecisionReducer:
        async def reduce(
            session: AsyncSession,
            record: AgentDecisionRecord,
            turn: AgentModelTurnRecord,
            now: datetime,
        ) -> None:
            run = await session.get(TaskExecutionRunRecord, binding.run_id)
            node = await session.get(TaskExecutionNodeRecord, binding.explorer_node_id)
            invocation = await session.get(
                AgentInvocationRecord,
                claimed.invocation.invocation_id,
            )
            binding_record = await session.get(
                WorkspaceCodingExplorerRunBindingRecord,
                binding.binding_id,
            )
            snapshot_record = await session.get(
                WorkspaceCodingExplorationSnapshotRecord,
                snapshot.snapshot_id,
            )
            existing = await session.scalar(
                select(WorkspaceCodingExplorationProposalRecord).where(
                    WorkspaceCodingExplorationProposalRecord.snapshot_id == snapshot.snapshot_id
                )
            )
            if (
                run is None
                or node is None
                or invocation is None
                or binding_record is None
                or self._run_binding_from_record(binding_record) != binding
                or snapshot_record is None
                or snapshot_record.manifest != snapshot.model_dump(mode="json")
                or snapshot_record.snapshot_digest != snapshot.snapshot_digest
                or existing is not None
                or run.task_id != snapshot.task_id
                or run.plan_generation != 1
                or run.plan_digest != binding.expected_plan_manifest_digest
                or run.status != ExecutionRunStatus.ACTIVE.value
                or node.run_id != run.run_id
                or node.status != ExecutionNodeStatus.RUNNING.value
                or invocation.run_id != run.run_id
                or invocation.node_id != node.node_id
                or invocation.execution_status != InvocationExecutionStatus.RUNNING.value
                or record.invocation_id != invocation.invocation_id
                or record.turn_id != turn.turn_id
                or record.binding_id != binding.binding_id
                or record.kind != "propose_file_set"
                or record.manifest != decision.model_dump(mode="json")
                or turn.request_digest == ""
                or turn.response_digest is None
            ):
                raise AgentRuntimeConflictError(
                    "Explorer proposal reducer lost its exact persistent proof"
                )
            proposal = WorkspaceCodingExplorationProposal.build(
                snapshot_id=snapshot.snapshot_id,
                snapshot_digest=snapshot.snapshot_digest,
                explorer_agent=binding.explorer_agent,
                decision=decision,
                created_at=now,
            )
            proof = WorkspaceCodingExplorerTurnProof.build(
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
                run_binding_id=binding.binding_id,
                run_binding_digest=binding.binding_digest,
                invocation_id=invocation.invocation_id,
                turn_id=turn.turn_id,
                agent_decision_id=record.decision_id,
                agent_decision_digest=record.decision_digest,
                model_request_digest=turn.request_digest,
                model_response_digest=turn.response_digest,
                created_at=now,
            )
            session.add(self._binder.proposal_record(proposal))
            session.add(self._turn_proof_record(proof))
            result_id = f"res_{sha256_digest({'invocation_id': invocation.invocation_id})}"
            material = {
                "schema_version": "deskpilot.agent-output-result.v1",
                "result_id": result_id,
                "invocation_id": invocation.invocation_id,
                "disposition": "candidate",
                "output": decision.model_dump(mode="json"),
                "evidence_refs": [
                    f"workspace-explorer-turn:{proof.proof_digest}",
                    f"workspace-snapshot:{snapshot.snapshot_digest}",
                ],
                "limitation_codes": ["proposal_has_no_execution_authority"],
                "input_digest": turn.request_digest,
                "model_response_digest": turn.response_digest,
                "output_schema_digest": claimed.handoff.output_schema_digest,
            }
            envelope = AgentOutputResult.model_validate(
                {**material, "result_digest": sha256_digest(material)}
            )
            session.add(
                AgentResultRecord(
                    result_id=result_id,
                    invocation_id=invocation.invocation_id,
                    manifest=envelope.model_dump(mode="json"),
                    result_digest=envelope.result_digest,
                    created_at=now,
                )
            )
            invocation.result_id = result_id
            invocation.execution_status = InvocationExecutionStatus.RESULT_SUBMITTED.value
            invocation.verification_status = InvocationVerificationStatus.VERIFIED.value
            invocation.finished_at = now
            invocation.revision += 1
            self._clear_claim(node)
            await mark_verified_and_unlock(session, run, node)
            for local_key in ("final_acceptance", "delivery"):
                control = await session.scalar(
                    select(TaskExecutionNodeRecord).where(
                        TaskExecutionNodeRecord.run_id == run.run_id,
                        TaskExecutionNodeRecord.local_key == local_key,
                    )
                )
                if control is None or control.status != ExecutionNodeStatus.READY.value:
                    raise AgentRuntimeConflictError("Explorer verified control edge is incomplete")
                await mark_verified_and_unlock(session, run, control)
            run.status = ExecutionRunStatus.SUCCEEDED.value
            run.revision += 1
            run.updated_at = now

        return reduce

    async def _assert_current_binding(
        self,
        binding: WorkspaceCodingExplorerRunBinding,
        snapshot: WorkspaceCodingExplorationSnapshot,
    ) -> None:
        if (
            binding.snapshot_id != snapshot.snapshot_id
            or binding.snapshot_digest != snapshot.snapshot_digest
            or binding.task_contract.task_id != snapshot.task_id
        ):
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer Run binding crossed its snapshot"
            )
        try:
            registration = self._agents.resolve_exact(
                binding.explorer_agent.agent_id,
                binding.explorer_agent.version,
                contract_digest=binding.explorer_agent.contract_digest,
                prompt_package_digest=binding.explorer_agent.prompt_package_digest,
            )
            contracts = await self._planning.list_contracts(snapshot.task_id)
            plan = await self._planning.get_plan(snapshot.task_id, 1)
            run = await self._execution.get(binding.run_id)
        except (AgentRegistryError, PlanningError, AgentRuntimeError) as error:
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer Plan, Agent or Run evidence is unavailable"
            ) from error
        contract_matches = tuple(
            item.contract
            for item in contracts.contracts
            if item.contract.version == binding.task_contract.version
        )
        nodes = {item.node_id: item for item in run.nodes}
        explorer = nodes.get(binding.explorer_node_id)
        if (
            registration.contract.digest != binding.explorer_agent.contract_digest
            or registration.prompt_package.digest != binding.explorer_agent.prompt_package_digest
            or contract_matches != (binding.task_contract,)
            or plan.plan != binding.expected_plan
            or run.task_id != snapshot.task_id
            or run.plan_generation != 1
            or run.plan_digest != binding.expected_plan_manifest_digest
            or explorer is None
            or explorer.bound_agent != binding.explorer_agent
            or explorer.budget.tool_calls != 0
        ):
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer persistent Plan or Run proof drifted"
            )

    @staticmethod
    def _assert_claim(
        binding: WorkspaceCodingExplorerRunBinding,
        claimed: ClaimedInvocation,
    ) -> None:
        if (
            claimed.handoff.run_id != binding.run_id
            or claimed.handoff.target_node_id != binding.explorer_node_id
            or claimed.invocation.node_id != binding.explorer_node_id
            or claimed.handoff.target_agent != binding.explorer_agent
            or claimed.handoff.capability is not None
            or claimed.handoff.capability_input is not None
            or claimed.handoff.upstream_result_refs
            or claimed.handoff.budget_allocation.tool_calls != 0
        ):
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer claim crossed its zero-tool Run binding"
            )

    def _assert_response_budget(
        self,
        claimed: ClaimedInvocation,
        dispatched: DispatchedAgentDecision,
    ) -> None:
        budget = claimed.handoff.budget_allocation
        if (
            dispatched.response.usage.input_tokens > budget.input_tokens
            or dispatched.response.usage.output_tokens > budget.output_tokens
            or self._model_loop.response_cost_micros(dispatched.response) > budget.cost_micros
        ):
            raise ValueError("Explorer Model Turn exceeded its bound budget")

    async def _reject_dispatched(
        self,
        claimed: ClaimedInvocation,
        dispatched: DispatchedAgentDecision,
        error_code: str,
    ) -> None:
        try:
            await self._model_loop.fail(
                claimed,
                dispatched.turn_id,
                error_code[:100],
                sha256_digest(dispatched.response),
            )
        except AgentRuntimeConflictError:
            pass
        await self._terminalize(claimed)

    async def _terminalize(self, claimed: ClaimedInvocation) -> None:
        async with self._database.session() as session, session.begin():
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
                .where(AgentInvocationRecord.invocation_id == claimed.invocation.invocation_id)
                .with_for_update()
            )
            if run is None or node is None or invocation is None:
                raise WorkspaceCodingExplorerConflictError("Explorer failure state is missing")
            if run.status == ExecutionRunStatus.FAILED.value:
                return
            AgentExecutionRuntime._assert_lease(  # noqa: SLF001 - shared fencing rule
                node,
                claimed.claim_owner_id,
                claimed.claim_fencing_token,
            )
            if (
                run.status != ExecutionRunStatus.ACTIVE.value
                or node.status != ExecutionNodeStatus.RUNNING.value
                or invocation.execution_status != InvocationExecutionStatus.RUNNING.value
            ):
                raise WorkspaceCodingExplorerConflictError(
                    "Explorer failure was fenced by newer state"
                )
            now = utc_now()
            invocation.execution_status = InvocationExecutionStatus.FAILED_TERMINAL.value
            invocation.verification_status = InvocationVerificationStatus.REJECTED.value
            invocation.finished_at = now
            invocation.revision += 1
            node.status = ExecutionNodeStatus.FAILED.value
            self._clear_claim(node)
            node.claim_fencing_token += 1
            node.revision += 1
            node.updated_at = now
            run.status = ExecutionRunStatus.FAILED.value
            run.revision += 1
            run.updated_at = now

    @staticmethod
    def _clear_claim(node: TaskExecutionNodeRecord) -> None:
        node.claim_owner_id = None
        node.claim_acquired_at = None
        node.claim_heartbeat_at = None
        node.claim_expires_at = None

    @staticmethod
    def _assert_locked_snapshot(
        record: WorkspaceCodingExplorationSnapshotRecord | None,
        snapshot: WorkspaceCodingExplorationSnapshot,
    ) -> None:
        if (
            record is None
            or record.source_task_id != snapshot.task_id
            or record.snapshot_digest != snapshot.snapshot_digest
            or record.manifest != snapshot.model_dump(mode="json")
        ):
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer snapshot changed before activation"
            )

    @staticmethod
    def _run_binding_record(
        binding: WorkspaceCodingExplorerRunBinding,
    ) -> WorkspaceCodingExplorerRunBindingRecord:
        agent = binding.explorer_agent
        plan = binding.expected_plan
        return WorkspaceCodingExplorerRunBindingRecord(
            binding_id=binding.binding_id,
            snapshot_id=binding.snapshot_id,
            snapshot_digest=binding.snapshot_digest,
            source_task_id=binding.task_contract.task_id,
            contract_version=binding.task_contract.version,
            contract_digest=binding.task_contract_digest,
            plan_generation=plan.plan_generation,
            plan_id=plan.plan_id,
            plan_manifest_digest=binding.expected_plan_manifest_digest,
            run_id=binding.run_id,
            explorer_node_id=binding.explorer_node_id,
            explorer_node_spec_digest=binding.explorer_node_spec_digest,
            explorer_agent_id=agent.agent_id,
            explorer_agent_version=agent.version,
            explorer_agent_contract_digest=agent.contract_digest,
            explorer_prompt_package_digest=agent.prompt_package_digest,
            manifest=binding.model_dump(mode="json"),
            binding_digest=binding.binding_digest,
            created_at=binding.created_at,
        )

    @classmethod
    def _run_binding_from_record(
        cls,
        record: WorkspaceCodingExplorerRunBindingRecord,
    ) -> WorkspaceCodingExplorerRunBinding:
        try:
            binding = WorkspaceCodingExplorerRunBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingExplorerProofRejectedError(
                "Persisted Explorer Run binding is invalid"
            ) from error
        expected = cls._run_binding_record(binding)
        for field in (
            "binding_id",
            "snapshot_id",
            "snapshot_digest",
            "source_task_id",
            "contract_version",
            "contract_digest",
            "plan_generation",
            "plan_id",
            "plan_manifest_digest",
            "run_id",
            "explorer_node_id",
            "explorer_node_spec_digest",
            "explorer_agent_id",
            "explorer_agent_version",
            "explorer_agent_contract_digest",
            "explorer_prompt_package_digest",
            "binding_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer Run binding columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != binding.created_at:
            raise WorkspaceCodingExplorerProofRejectedError(
                "Explorer Run binding timestamp changed"
            )
        return binding

    @staticmethod
    def _turn_proof_record(
        proof: WorkspaceCodingExplorerTurnProof,
    ) -> WorkspaceCodingExplorerTurnProofRecord:
        return WorkspaceCodingExplorerTurnProofRecord(
            proof_id=proof.proof_id,
            proposal_id=proof.proposal_id,
            proposal_digest=proof.proposal_digest,
            run_binding_id=proof.run_binding_id,
            run_binding_digest=proof.run_binding_digest,
            invocation_id=proof.invocation_id,
            turn_id=proof.turn_id,
            agent_decision_id=proof.agent_decision_id,
            agent_decision_digest=proof.agent_decision_digest,
            model_request_digest=proof.model_request_digest,
            model_response_digest=proof.model_response_digest,
            manifest=proof.model_dump(mode="json"),
            proof_digest=proof.proof_digest,
            created_at=proof.created_at,
        )

    @classmethod
    def _turn_proof_from_record(
        cls,
        record: WorkspaceCodingExplorerTurnProofRecord,
    ) -> WorkspaceCodingExplorerTurnProof:
        try:
            proof = WorkspaceCodingExplorerTurnProof.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingExplorerProofRejectedError(
                "Persisted Explorer Turn proof is invalid"
            ) from error
        expected = cls._turn_proof_record(proof)
        for field in (
            "proof_id",
            "proposal_id",
            "proposal_digest",
            "run_binding_id",
            "run_binding_digest",
            "invocation_id",
            "turn_id",
            "agent_decision_id",
            "agent_decision_digest",
            "model_request_digest",
            "model_response_digest",
            "proof_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingExplorerProofRejectedError(
                    "Explorer Turn proof columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != proof.created_at:
            raise WorkspaceCodingExplorerProofRejectedError("Explorer Turn proof timestamp changed")
        return proof

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "WorkspaceCodingExplorerConflictError",
    "WorkspaceCodingExplorerProofRejectedError",
    "WorkspaceCodingExplorerRuntime",
    "WorkspaceCodingExplorerRuntimeError",
]
