"""Persistent no-write Change Proposal and fresh-confirmed successor Plan runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from deskpilot.application.capability_catalog import CapabilityCatalog, CapabilityCatalogError
from deskpilot.application.plan_compilation_service import PlanCompilationService, PlanningError
from deskpilot.application.plan_compiler import (
    workspace_coding_change_proposer_contract,
    workspace_coding_change_proposer_draft,
)
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog, RouteRecipeError
from deskpilot.application.verified_edges import mark_verified_and_unlock
from deskpilot.application.workspace_coding_exploration_binder import (
    WorkspaceCodingExplorationBinder,
    WorkspaceCodingExplorationNotFoundError,
    WorkspaceCodingExplorationProofRejectedError,
)
from deskpilot.application.workspace_coding_graph import (
    WORKSPACE_CODING_MIN_FILES,
    workspace_coding_path_parameter,
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
from deskpilot.domain.task_loop_execution import TaskLoopExecution, TaskLoopVerifiedResult
from deskpilot.domain.workspace_coding_changes import (
    WorkspaceCodingChangeDecision,
    WorkspaceCodingChangeProposal,
    WorkspaceCodingChangeRunBinding,
    WorkspaceCodingChangeTurnProof,
    WorkspaceCodingChangeWorkbenchRead,
    WorkspaceCodingWritePlanBinding,
)
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingFileSetPlanBinding,
)
from deskpilot.domain.workspace_files import WorkspaceFileRead
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentResultRecord,
    ConversationMessageRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopExecutionRecord,
    TaskLoopVerifiedResultRecord,
    TaskRecord,
    WorkspaceCodingChangeProposalRecord,
    WorkspaceCodingChangeRunBindingRecord,
    WorkspaceCodingChangeTurnProofRecord,
    WorkspaceCodingWritePlanBindingRecord,
    utc_now,
)


class WorkspaceCodingChangeRuntimeError(RuntimeError):
    code = "WORKSPACE_CODING_CHANGE_RUNTIME_ERROR"


class WorkspaceCodingChangeConflictError(WorkspaceCodingChangeRuntimeError):
    code = "WORKSPACE_CODING_CHANGE_CONFLICT"


class WorkspaceCodingChangeProofRejectedError(WorkspaceCodingChangeRuntimeError):
    code = "WORKSPACE_CODING_CHANGE_PROOF_REJECTED"


@dataclass(frozen=True, slots=True)
class _ReaderEvidence:
    binding: WorkspaceCodingFileSetPlanBinding
    execution: TaskLoopExecution
    results: tuple[TaskLoopVerifiedResult, ...]
    files: tuple[WorkspaceFileRead, ...]
    result_set_digest: str


class WorkspaceCodingChangeRuntime:
    """Use existing Plan/Run/Turn truth for an unprivileged change proposal."""

    def __init__(
        self,
        database: Database,
        explorations: WorkspaceCodingExplorationBinder,
        agents: AgentRegistry,
        capabilities: CapabilityCatalog,
        planning: PlanCompilationService,
        execution: AgentExecutionRuntime,
        model_loop: AgentModelLoopRuntime,
    ) -> None:
        self._database = database
        self._explorations = explorations
        self._agents = agents
        self._capabilities = capabilities
        self._planning = planning
        self._execution = execution
        self._model_loop = model_loop

    async def activate(self, reader_task_id: str) -> WorkspaceCodingChangeRunBinding:
        """Atomically advance to generation 2 and bind one exact proposer Run."""

        source = await self._reader_evidence(reader_task_id)
        existing = await self.get_run_binding(reader_task_id=reader_task_id)
        if existing is not None:
            self._assert_binding_source(existing, source)
            return existing
        contract = workspace_coding_change_proposer_contract(
            reader_task_id,
            previous_contract_digest=source.binding.task_contract_digest,
            file_count=len(source.files),
        )
        draft = workspace_coding_change_proposer_draft(reader_task_id)
        try:
            async with self._database.session() as session, session.begin():
                execution_record = await session.scalar(
                    select(TaskLoopExecutionRecord)
                    .where(TaskLoopExecutionRecord.execution_id == source.execution.execution_id)
                    .with_for_update()
                )
                if (
                    execution_record is None
                    or execution_record.status != "succeeded"
                    or execution_record.execution_digest != source.execution.execution_digest
                    or execution_record.latest_event_digest
                    != source.execution.latest_event_digest
                ):
                    raise WorkspaceCodingChangeProofRejectedError(
                        "Reader terminal execution changed before proposer activation"
                    )
                persisted = await session.scalar(
                    select(WorkspaceCodingChangeRunBindingRecord)
                    .where(
                        WorkspaceCodingChangeRunBindingRecord.reader_task_id == reader_task_id
                    )
                    .with_for_update()
                )
                if persisted is not None:
                    return self._run_binding_from_record(persisted)
                activated = await self._planning.activate_in_session(session, contract, draft)
                if activated.plan.plan_generation != 2:
                    raise WorkspaceCodingChangeProofRejectedError(
                        "Change proposer did not receive generation 2"
                    )
                run = await self._execution.start_exact_in_session(session, activated.plan)
                nodes = {item.local_key: item for item in run.nodes}
                proposer = nodes.get("propose_change_set")
                if (
                    set(nodes) != {"propose_change_set", "final_acceptance", "delivery"}
                    or proposer is None
                    or proposer.bound_agent is None
                ):
                    raise WorkspaceCodingChangeProofRejectedError(
                        "Change proposer Run crossed its exact Plan"
                    )
                binding = WorkspaceCodingChangeRunBinding.build(
                    file_set_binding_id=source.binding.binding_id,
                    file_set_binding_digest=source.binding.binding_digest,
                    reader_execution_id=source.execution.execution_id,
                    reader_execution_digest=source.execution.execution_digest,
                    reader_terminal_event_digest=source.execution.latest_event_digest,
                    reader_result_ref_digests=tuple(
                        item.result_ref_digest for item in source.results
                    ),
                    task_contract=contract,
                    draft_plan=draft,
                    expected_plan=activated.plan,
                    run_id=run.run_id,
                    proposer_node_id=proposer.node_id,
                    proposer_node_spec_digest=next(
                        item.node_spec_digest
                        for item in activated.plan.nodes
                        if item.node_id == proposer.node_id
                    ),
                    proposer_agent=proposer.bound_agent,
                    created_at=utc_now(),
                )
                session.add(self._run_binding_record(binding))
        except IntegrityError:
            recovered = await self.get_run_binding(reader_task_id=reader_task_id)
            if recovered is not None:
                self._assert_binding_source(recovered, source)
                return recovered
            raise WorkspaceCodingChangeConflictError(
                "Concurrent Change Proposal activation did not converge"
            ) from None
        except PlanningError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer planning generation was rejected"
            ) from error
        final_binding = await self.get_run_binding(reader_task_id=reader_task_id)
        if final_binding is None:
            raise WorkspaceCodingChangeConflictError(
                "Change proposer Run binding was not persisted"
            )
        return final_binding

    async def run(
        self,
        reader_task_id: str,
        *,
        owner_id: str = "workspace-coding-change-proposer",
        lease_seconds: int = 60,
    ) -> WorkspaceCodingChangeProposal:
        """Run or recover one exact no-write proposal Model Turn."""

        binding = await self.activate(reader_task_id)
        source = await self._reader_evidence(reader_task_id)
        proposal = await self.get_proposal(reader_task_id=reader_task_id)
        if proposal is not None:
            await self.get_turn_proof(proposal.proposal_id)
            return proposal
        await self._assert_current_binding(binding, source)
        claimed = await self._execution.claim_next(
            binding.run_id,
            owner_id,
            lease_seconds=lease_seconds,
            node_id=binding.proposer_node_id,
        )
        if claimed is None:
            raise WorkspaceCodingChangeConflictError(
                "Change proposer is active or unknown and is never automatically replayed"
            )
        self._assert_claim(binding, claimed)
        request = await self._request(binding, source, claimed)
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
                decision_model=WorkspaceCodingChangeDecision,
            )
        except AgentModelLoopOutcomeUnknownError:
            raise
        except AgentModelLoopError as error:
            await self._terminalize(claimed)
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer Model Turn route was rejected"
            ) from error
        decision = cast(WorkspaceCodingChangeDecision, dispatched.decision)
        try:
            current = await self._reader_evidence(reader_task_id)
            self._assert_binding_source(binding, current)
            self._validate_decision(current, decision)
            self._assert_response_budget(claimed, dispatched)
        except (WorkspaceCodingChangeRuntimeError, ValueError) as error:
            await self._reject_dispatched(claimed, dispatched, type(error).__name__)
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposal decision or Reader proof was rejected"
            ) from error
        try:
            await self._model_loop.accept(
                claimed,
                dispatched,
                decision,
                binding_id=binding.binding_id,
                reducer=self._proposal_reducer(binding, source, claimed, decision),
            )
        except (
            AgentRuntimeConflictError,
            IntegrityError,
            ValidationError,
            WorkspaceCodingChangeRuntimeError,
        ) as error:
            await self._reject_dispatched(claimed, dispatched, type(error).__name__)
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposal persistence proof was rejected"
            ) from error
        proposal = await self.get_proposal(reader_task_id=reader_task_id)
        if proposal is None:
            raise WorkspaceCodingChangeConflictError("Change proposal was not persisted")
        await self.get_turn_proof(proposal.proposal_id)
        return proposal

    async def confirm(
        self,
        proposal_id: str,
        *,
        successor_task_id: str,
        confirmation_message_id: str,
    ) -> WorkspaceCodingWritePlanBinding:
        """Atomically compile a successor write Plan after a fresh exact confirmation."""

        proposal = await self.get_proposal(proposal_id=proposal_id)
        if proposal is None:
            raise WorkspaceCodingChangeProofRejectedError("Change proposal does not exist")
        run_binding = await self.get_run_binding(binding_id=proposal.run_binding_id)
        if run_binding is None:
            raise WorkspaceCodingChangeProofRejectedError("Change proposal lost its Run binding")
        source = await self._reader_evidence(run_binding.task_contract.task_id)
        await self._assert_current_binding(run_binding, source)
        self._validate_decision(source, proposal.decision)
        snapshot = await self._explorations.get_snapshot(
            snapshot_id=(await self._explorations.get_proposal(
                proposal_id=source.binding.proposal_id
            )).snapshot_id
        )
        fixed = self._write_parameters(
            snapshot.project_path,
            snapshot.test_path,
            snapshot.ecosystem,
            source,
            proposal,
        )
        expected_text = f"确认变更提案：{proposal.proposal_id}"
        try:
            recipe = RouteRecipeCatalog.precompile(
                task_id=successor_task_id,
                route_id="workspace_coding_loop",
                message=expected_text,
                proposed={},
                fixed_parameters=fixed,
                capabilities=self._capabilities,
            )
            expected_plan = self._planning.preview_initial(recipe.contract, recipe.draft)
            async with self._database.session() as session, session.begin():
                message = await session.get(ConversationMessageRecord, confirmation_message_id)
                successor = await session.get(TaskRecord, successor_task_id)
                reader_task = await session.get(TaskRecord, run_binding.task_contract.task_id)
                self._assert_confirmation(
                    reader_task=reader_task,
                    successor_task=successor,
                    message=message,
                    expected_text=expected_text,
                    proposal_created_at=proposal.created_at,
                )
                assert message is not None
                binding = WorkspaceCodingWritePlanBinding.build(
                    proposal_id=proposal.proposal_id,
                    proposal_digest=proposal.proposal_digest,
                    successor_task_id=successor_task_id,
                    confirmation_message_id=confirmation_message_id,
                    confirmation_message_digest=message.message_digest,
                    recipe_manifest=recipe.recipe_manifest,
                    parameter_binding_manifest=recipe.parameter_binding_manifest,
                    parameters=recipe.parameters,
                    task_contract=recipe.contract,
                    draft_plan=recipe.draft,
                    expected_plan=expected_plan,
                    created_at=utc_now(),
                )
                existing = await session.scalar(
                    select(WorkspaceCodingWritePlanBindingRecord)
                    .where(WorkspaceCodingWritePlanBindingRecord.proposal_id == proposal_id)
                    .with_for_update()
                )
                if existing is not None:
                    existing_binding = self._write_binding_from_record(existing)
                    self._assert_write_binding_request(existing_binding, binding)
                    return existing_binding
                activated = await self._planning.activate_initial_once_in_session(
                    session,
                    recipe.contract,
                    recipe.draft,
                )
                if activated.plan != expected_plan:
                    raise WorkspaceCodingChangeProofRejectedError(
                        "Confirmed coding write Plan changed during activation"
                    )
                session.add(self._write_binding_record(binding))
        except IntegrityError:
            collision_binding = await self.get_write_plan_binding(proposal_id=proposal_id)
            if collision_binding is not None:
                if (
                    collision_binding.successor_task_id != successor_task_id
                    or collision_binding.confirmation_message_id != confirmation_message_id
                ):
                    raise WorkspaceCodingChangeConflictError(
                        "Change proposal is already bound to a different successor Task"
                    ) from None
                return collision_binding
            raise WorkspaceCodingChangeConflictError(
                "Concurrent write Plan confirmation did not converge"
            ) from None
        except (PlanningError, RouteRecipeError) as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Confirmed coding write Plan was rejected"
            ) from error
        persisted_binding = await self.get_write_plan_binding(proposal_id=proposal_id)
        if persisted_binding is None:
            raise WorkspaceCodingChangeConflictError("Write Plan binding was not persisted")
        return persisted_binding

    async def get_run_binding(
        self,
        *,
        reader_task_id: str | None = None,
        binding_id: str | None = None,
    ) -> WorkspaceCodingChangeRunBinding | None:
        if (reader_task_id is None) == (binding_id is None):
            raise ValueError("Exactly one Change Proposal Run binding key is required")
        async with self._database.session() as session:
            statement = select(WorkspaceCodingChangeRunBindingRecord).where(
                WorkspaceCodingChangeRunBindingRecord.reader_task_id == reader_task_id
                if reader_task_id is not None
                else WorkspaceCodingChangeRunBindingRecord.binding_id == binding_id
            )
            record = await session.scalar(statement)
        if record is None:
            return None
        return self._run_binding_from_record(record)

    async def get_proposal(
        self,
        *,
        reader_task_id: str | None = None,
        proposal_id: str | None = None,
    ) -> WorkspaceCodingChangeProposal | None:
        if (reader_task_id is None) == (proposal_id is None):
            raise ValueError("Exactly one Change Proposal key is required")
        async with self._database.session() as session:
            statement = select(WorkspaceCodingChangeProposalRecord)
            if reader_task_id is not None:
                statement = statement.where(
                    WorkspaceCodingChangeProposalRecord.reader_task_id == reader_task_id
                )
            else:
                statement = statement.where(
                    WorkspaceCodingChangeProposalRecord.proposal_id == proposal_id
                )
            record = await session.scalar(statement)
        if record is None:
            return None
        proposal = self._proposal_from_record(record)
        binding = await self.get_run_binding(binding_id=proposal.run_binding_id)
        if binding is None or binding.binding_digest != proposal.run_binding_digest:
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposal crossed its Run binding"
            )
        await self.get_turn_proof(proposal.proposal_id)
        return proposal

    async def get_turn_proof(self, proposal_id: str) -> WorkspaceCodingChangeTurnProof:
        async with self._database.session() as session:
            record = await session.scalar(
                select(WorkspaceCodingChangeTurnProofRecord).where(
                    WorkspaceCodingChangeTurnProofRecord.proposal_id == proposal_id
                )
            )
            if record is None:
                raise WorkspaceCodingChangeProofRejectedError(
                    "Change proposal has no persisted Model Turn proof"
                )
            proof = self._turn_proof_from_record(record)
            proposal_record = await session.get(WorkspaceCodingChangeProposalRecord, proposal_id)
            binding_record = await session.get(
                WorkspaceCodingChangeRunBindingRecord,
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
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposal Turn proof lost its proposal or Run binding"
            )
        proposal = self._proposal_from_record(proposal_record)
        binding = self._run_binding_from_record(binding_record)
        expected_decision_digest = (
            sha256_digest(
                {
                    "turn_id": turn.turn_id,
                    "invocation_id": turn.invocation_id,
                    "decision": decision.manifest,
                    "response_digest": turn.response_digest,
                }
            )
            if turn is not None and decision is not None
            else None
        )
        try:
            envelope = AgentOutputResult.model_validate(result.manifest) if result else None
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer Agent Result envelope is invalid"
            ) from error
        if (
            proof.proposal_digest != proposal.proposal_digest
            or proof.run_binding_digest != binding.binding_digest
            or invocation is None
            or invocation.run_id != binding.run_id
            or invocation.node_id != binding.proposer_node_id
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
            or decision.kind != "submit_result"
            or decision.manifest != proposal.decision.model_dump(mode="json")
            or decision.decision_digest != proof.agent_decision_digest
            or decision.decision_digest != expected_decision_digest
            or envelope is None
            or result is None
            or envelope.result_digest != result.result_digest
            or envelope.invocation_id != invocation.invocation_id
            or envelope.input_digest != proof.model_request_digest
            or envelope.model_response_digest != proof.model_response_digest
            or envelope.output != proposal.decision.model_dump(mode="json")
            or f"workspace-change-turn:{proof.proof_digest}" not in envelope.evidence_refs
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposal crossed its Invocation/Turn/Decision proof"
            )
        return proof

    async def get_write_plan_binding(
        self,
        *,
        proposal_id: str | None = None,
        successor_task_id: str | None = None,
    ) -> WorkspaceCodingWritePlanBinding | None:
        if (proposal_id is None) == (successor_task_id is None):
            raise ValueError("Exactly one write Plan binding key is required")
        async with self._database.session() as session:
            record = await session.scalar(
                select(WorkspaceCodingWritePlanBindingRecord).where(
                    WorkspaceCodingWritePlanBindingRecord.proposal_id == proposal_id
                    if proposal_id is not None
                    else WorkspaceCodingWritePlanBindingRecord.successor_task_id
                    == successor_task_id
                )
            )
        if record is None:
            return None
        binding = self._write_binding_from_record(record)
        proposal = await self.get_proposal(proposal_id=binding.proposal_id)
        if proposal is None or proposal.proposal_digest != binding.proposal_digest:
            raise WorkspaceCodingChangeProofRejectedError(
                "Write Plan binding crossed its Change Proposal"
            )
        try:
            plan = await self._planning.get_plan(binding.successor_task_id, 1)
            recipe = RouteRecipeCatalog.precompile(
                task_id=binding.successor_task_id,
                route_id=binding.route_id,
                message=f"确认变更提案：{proposal.proposal_id}",
                proposed={},
                fixed_parameters=binding.parameters,
                capabilities=self._capabilities,
            )
            expected_plan = self._planning.preview_initial(recipe.contract, recipe.draft)
        except (
            CapabilityCatalogError,
            PlanningError,
            RouteRecipeError,
            ValueError,
        ) as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Write Plan binding cannot be rebuilt from the current Catalog"
            ) from error
        drifted = tuple(
            name
            for name, changed in (
                (
                    "persisted_plan",
                    plan.plan.plan_manifest_digest
                    != binding.expected_plan_manifest_digest,
                ),
                ("recipe", recipe.recipe_digest != binding.recipe_digest),
                (
                    "parameter_binding",
                    recipe.parameter_binding_digest != binding.parameter_binding_digest,
                ),
                ("parameters", recipe.parameters != binding.parameters),
                ("contract", recipe.contract.digest != binding.task_contract_digest),
                ("draft", sha256_digest(recipe.draft) != binding.draft_plan_digest),
                (
                    "compiled_plan",
                    expected_plan.plan_manifest_digest
                    != binding.expected_plan_manifest_digest,
                ),
            )
            if changed
        )
        if drifted:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted write Plan differs from its current confirmation recipe: "
                + ",".join(drifted)
            )
        run_binding = await self.get_run_binding(binding_id=proposal.run_binding_id)
        if run_binding is None:
            raise WorkspaceCodingChangeProofRejectedError(
                "Write Plan binding lost its Change Proposal Run"
            )
        async with self._database.session() as session:
            reader_task = await session.get(TaskRecord, run_binding.task_contract.task_id)
            successor_task = await session.get(TaskRecord, binding.successor_task_id)
            message = await session.get(
                ConversationMessageRecord,
                binding.confirmation_message_id,
            )
        self._assert_confirmation(
            reader_task=reader_task,
            successor_task=successor_task,
            message=message,
            expected_text=f"确认变更提案：{proposal.proposal_id}",
            proposal_created_at=proposal.created_at,
        )
        if (
            message is None
            or message.message_digest != binding.confirmation_message_digest
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Write Plan confirmation digest changed"
            )
        return binding

    async def get_workbench(
        self,
        task_id: str,
    ) -> WorkspaceCodingChangeWorkbenchRead | None:
        write = await self.get_write_plan_binding(successor_task_id=task_id)
        reader_task_id = task_id
        if write is not None:
            proposal = await self.get_proposal(proposal_id=write.proposal_id)
            assert proposal is not None
            run_binding = await self.get_run_binding(binding_id=proposal.run_binding_id)
            assert run_binding is not None
            reader_task_id = run_binding.task_contract.task_id
        else:
            proposal = await self.get_proposal(reader_task_id=task_id)
            run_binding = await self.get_run_binding(reader_task_id=task_id)
        try:
            source = await self._reader_evidence(reader_task_id)
        except WorkspaceCodingExplorationNotFoundError:
            return None
        if run_binding is None:
            return self._workbench(
                phase="reader_succeeded",
                source=source,
                run_binding=None,
                proposal=None,
                write=None,
                invocation=None,
                turn=None,
            )
        async with self._database.session() as session:
            run = await session.get(TaskExecutionRunRecord, run_binding.run_id)
            invocation = await session.scalar(
                select(AgentInvocationRecord).where(
                    AgentInvocationRecord.run_id == run_binding.run_id,
                    AgentInvocationRecord.node_id == run_binding.proposer_node_id,
                )
            )
            turn = (
                await session.scalar(
                    select(AgentModelTurnRecord).where(
                        AgentModelTurnRecord.invocation_id == invocation.invocation_id
                    )
                )
                if invocation is not None
                else None
            )
        if proposal is not None:
            phase = "confirmed_write_plan" if write is not None else "proposal_ready"
        elif turn is not None and turn.status in {"failed", "outcome_unknown"}:
            phase = "proposal_blocked"
        else:
            phase = "proposal_turn_ready"
        return self._workbench(
            phase=phase,
            source=source,
            run_binding=run_binding,
            proposal=proposal,
            write=write,
            invocation=invocation,
            turn=turn,
            run_status=run.status if run is not None else None,
        )

    async def recoverable_task_ids(self, *, limit: int = 1_000) -> tuple[str, ...]:
        """Return only Reader tasks whose proposer has no dispatched Invocation."""

        async with self._database.session() as session:
            task_ids = tuple(
                (
                    await session.scalars(
                        select(TaskLoopExecutionRecord.task_id)
                        .where(
                            TaskLoopExecutionRecord.source_kind == "confirmed_file_set",
                            TaskLoopExecutionRecord.status == "succeeded",
                        )
                        .order_by(TaskLoopExecutionRecord.updated_at)
                        .limit(limit)
                    )
                ).all()
            )
        recoverable: list[str] = []
        for task_id in task_ids:
            projection = await self.get_workbench(task_id)
            if projection is not None and (
                projection.phase == "reader_succeeded"
                or (
                    projection.phase == "proposal_turn_ready"
                    and projection.invocation_id is None
                )
            ):
                recoverable.append(task_id)
        return tuple(recoverable)

    async def _reader_evidence(self, reader_task_id: str) -> _ReaderEvidence:
        try:
            bundle = await self._explorations.get_reader_activation_bundle(reader_task_id)
        except WorkspaceCodingExplorationProofRejectedError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Confirmed Reader source proof was rejected"
            ) from error
        async with self._database.session() as session:
            record = await session.scalar(
                select(TaskLoopExecutionRecord).where(
                    TaskLoopExecutionRecord.task_id == reader_task_id,
                    TaskLoopExecutionRecord.source_kind == "confirmed_file_set",
                )
            )
            if record is None:
                raise WorkspaceCodingChangeProofRejectedError(
                    "Confirmed Reader TaskLoop execution does not exist"
                )
            execution = self._execution_from_record(record)
            result_records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopVerifiedResultRecord).where(
                            TaskLoopVerifiedResultRecord.execution_id
                            == execution.execution_id
                        )
                    )
                ).all()
            )
        if (
            execution.source_kind != "confirmed_file_set"
            or execution.source_binding_id != bundle.binding.binding_id
            or execution.source_binding_digest != bundle.binding.binding_digest
            or execution.status != "succeeded"
            or execution.plan_id != bundle.binding.expected_plan.plan_id
            or execution.plan_manifest_digest
            != bundle.binding.expected_plan_manifest_digest
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Reader execution is not the exact succeeded confirmed file-set Plan"
            )
        by_node = {item.node_id: self._result_from_record(item) for item in result_records}
        if len(by_node) != len(result_records) or set(by_node) != {
            item.plan_node_id for item in bundle.binding.mappings
        }:
            raise WorkspaceCodingChangeProofRejectedError(
                "Reader ResultRef set does not exactly match confirmed mappings"
            )
        results: list[TaskLoopVerifiedResult] = []
        files: list[WorkspaceFileRead] = []
        for mapping in bundle.binding.mappings:
            result = by_node[mapping.plan_node_id]
            try:
                file_read = WorkspaceFileRead.model_validate(result.output_manifest)
            except ValidationError as error:
                raise WorkspaceCodingChangeProofRejectedError(
                    "Reader ResultRef output is not a workspace file proof"
                ) from error
            expected_path = (
                mapping.relative_path
                if bundle.snapshot.project_path == "."
                else f"{bundle.snapshot.project_path.rstrip('/')}/{mapping.relative_path}"
            )
            if (
                result.result_kind != "workspace_file"
                or result.output_digest != file_read.result_digest
                or file_read.relative_path != expected_path
                or file_read.content_digest
                != next(
                    item.content_digest
                    for item in bundle.snapshot.files
                    if item.relative_path == mapping.relative_path
                )
                or file_read.version_digest
                != next(
                    item.version_digest
                    for item in bundle.snapshot.files
                    if item.relative_path == mapping.relative_path
                )
            ):
                raise WorkspaceCodingChangeProofRejectedError(
                    "Reader ResultRef crossed its snapshot path or content proof"
                )
            results.append(result)
            files.append(file_read)
        digests = tuple(item.result_ref_digest for item in results)
        return _ReaderEvidence(
            binding=bundle.binding,
            execution=execution,
            results=tuple(results),
            files=tuple(files),
            result_set_digest=sha256_digest({"result_ref_digests": list(digests)}),
        )

    async def _request(
        self,
        binding: WorkspaceCodingChangeRunBinding,
        source: _ReaderEvidence,
        claimed: ClaimedInvocation,
    ) -> ModelRequest:
        async with self._database.session() as session:
            task = await session.get(TaskRecord, binding.task_contract.task_id)
        if task is None:
            raise WorkspaceCodingChangeProofRejectedError("Reader Task is missing")
        exploration = await self._explorations.get_proposal(
            proposal_id=source.binding.proposal_id
        )
        snapshot = await self._explorations.get_snapshot(snapshot_id=exploration.snapshot_id)
        readers = [
            {
                "relative_path": mapping.relative_path,
                "workspace_relative_path": file_read.relative_path,
                "content": file_read.content,
                "content_digest": file_read.content_digest,
                "version_digest": file_read.version_digest,
                "result_digest": file_read.result_digest,
                "result_ref_digest": result.result_ref_digest,
            }
            for mapping, result, file_read in zip(
                source.binding.mappings,
                source.results,
                source.files,
                strict=True,
            )
        ]
        if sum(len(item["content"].encode("utf-8")) for item in readers) > 220_000:
            raise WorkspaceCodingChangeProofRejectedError(
                "Verified Reader content exceeds the bounded proposer context"
            )
        payload = {
            "external_untrusted_objective": task.goal,
            "file_set_binding_id": source.binding.binding_id,
            "reader_execution_id": source.execution.execution_id,
            "reader_execution_digest": source.execution.execution_digest,
            "reader_result_set_digest": source.result_set_digest,
            "ecosystem": snapshot.ecosystem,
            "readers": readers,
            "authority": "proposal_only_no_file_write_test_git_or_shell",
        }
        budget = claimed.handoff.budget_allocation
        return ModelRequest(
            request_id=f"workspace-change-{claimed.invocation.invocation_id[-24:]}",
            task_id=task.task_id,
            role=ModelRole.PLANNER,
            messages=(
                ModelMessage(
                    role="system",
                    content="Return one strict verified-Reader-bound no-write change decision.",
                ),
                ModelMessage(
                    role="user",
                    content=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ),
            privacy_mode=cast(PrivacyMode, task.privacy_mode),
            requirements=ModelCapabilityRequirements(
                structured_output=True,
                strict_json_schema=True,
                min_context_tokens=32_768,
            ),
            output_schema=StructuredOutputDefinition.from_model(
                name="workspace_coding_change_decision",
                description="One unprivileged exact change-set proposal",
                model=WorkspaceCodingChangeDecision,
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
                "workspace_change_run_binding_id": binding.binding_id,
                "workspace_change_run_binding_digest": binding.binding_digest,
                "workspace_change_file_set_binding_id": source.binding.binding_id,
                "workspace_change_reader_execution_id": source.execution.execution_id,
                "workspace_change_reader_execution_digest": source.execution.execution_digest,
                "workspace_change_reader_result_set_digest": source.result_set_digest,
                "workspace_change_ecosystem": snapshot.ecosystem,
                "workspace_change_readers": cast(JsonValue, readers),
            },
        )

    def _proposal_reducer(
        self,
        binding: WorkspaceCodingChangeRunBinding,
        source: _ReaderEvidence,
        claimed: ClaimedInvocation,
        decision: WorkspaceCodingChangeDecision,
    ) -> DecisionReducer:
        async def reduce(
            session: AsyncSession,
            record: AgentDecisionRecord,
            turn: AgentModelTurnRecord,
            now: datetime,
        ) -> None:
            run = await session.get(TaskExecutionRunRecord, binding.run_id)
            node = await session.get(TaskExecutionNodeRecord, binding.proposer_node_id)
            invocation = await session.get(AgentInvocationRecord, claimed.invocation.invocation_id)
            binding_record = await session.get(
                WorkspaceCodingChangeRunBindingRecord,
                binding.binding_id,
            )
            existing = await session.scalar(
                select(WorkspaceCodingChangeProposalRecord).where(
                    WorkspaceCodingChangeProposalRecord.run_binding_id == binding.binding_id
                )
            )
            reader_execution = await session.get(
                TaskLoopExecutionRecord,
                source.execution.execution_id,
            )
            if (
                run is None
                or node is None
                or invocation is None
                or binding_record is None
                or self._run_binding_from_record(binding_record) != binding
                or reader_execution is None
                or reader_execution.status != "succeeded"
                or reader_execution.execution_digest != source.execution.execution_digest
                or existing is not None
                or run.task_id != binding.task_contract.task_id
                or run.plan_generation != 2
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
                or record.kind != "submit_result"
                or record.manifest != decision.model_dump(mode="json")
                or turn.request_digest == ""
                or turn.response_digest is None
            ):
                raise AgentRuntimeConflictError(
                    "Change proposal reducer lost its exact persistent proof"
                )
            proposal = WorkspaceCodingChangeProposal.build(
                run_binding_id=binding.binding_id,
                run_binding_digest=binding.binding_digest,
                proposer_agent=binding.proposer_agent,
                decision=decision,
                created_at=now,
            )
            proof = WorkspaceCodingChangeTurnProof.build(
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
            session.add(self._proposal_record(proposal, binding.task_contract.task_id))
            session.add(self._turn_proof_record(proof))
            result_id = f"res_{sha256_digest({'invocation_id': invocation.invocation_id})}"
            material = {
                "schema_version": "deskpilot.agent-output-result.v1",
                "result_id": result_id,
                "invocation_id": invocation.invocation_id,
                "disposition": "candidate",
                "output": decision.model_dump(mode="json"),
                "evidence_refs": [
                    f"workspace-change-turn:{proof.proof_digest}",
                    f"reader-result-set:{source.result_set_digest}",
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
                    raise AgentRuntimeConflictError(
                        "Change proposer verified control edge is incomplete"
                    )
                await mark_verified_and_unlock(session, run, control)
            run.status = ExecutionRunStatus.SUCCEEDED.value
            run.revision += 1
            run.updated_at = now

        return reduce

    def _validate_decision(
        self,
        source: _ReaderEvidence,
        decision: WorkspaceCodingChangeDecision,
    ) -> None:
        expected_paths = tuple(item.relative_path for item in source.binding.mappings)
        if (
            decision.file_set_binding_id != source.binding.binding_id
            or decision.reader_execution_id != source.execution.execution_id
            or decision.reader_execution_digest != source.execution.execution_digest
            or decision.reader_result_set_digest != source.result_set_digest
            or tuple(item.relative_path for item in decision.changes) != expected_paths
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposal crossed its exact Reader result set"
            )
        for change, result, file_read in zip(
            decision.changes,
            source.results,
            source.files,
            strict=True,
        ):
            if (
                change.source_result_ref_digest != result.result_ref_digest
                or change.source_result_digest != file_read.result_digest
                or change.source_version_digest != file_read.version_digest
                or file_read.content.count(change.old_text) != 1
            ):
                raise WorkspaceCodingChangeProofRejectedError(
                    "Change proposal replacement is not uniquely bound to Reader content"
                )

    async def _assert_current_binding(
        self,
        binding: WorkspaceCodingChangeRunBinding,
        source: _ReaderEvidence,
    ) -> None:
        self._assert_binding_source(binding, source)
        try:
            registration = self._agents.resolve_exact(
                binding.proposer_agent.agent_id,
                binding.proposer_agent.version,
                contract_digest=binding.proposer_agent.contract_digest,
                prompt_package_digest=binding.proposer_agent.prompt_package_digest,
            )
            plan = await self._planning.get_plan(binding.task_contract.task_id, 2)
            run = await self._execution.get(binding.run_id)
        except (AgentRegistryError, PlanningError, AgentRuntimeError) as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer Plan, Agent or Run proof is unavailable"
            ) from error
        proposer = next(
            (item for item in run.nodes if item.node_id == binding.proposer_node_id),
            None,
        )
        if (
            registration.contract.digest != binding.proposer_agent.contract_digest
            or registration.prompt_package.digest
            != binding.proposer_agent.prompt_package_digest
            or plan.plan != binding.expected_plan
            or run.task_id != binding.task_contract.task_id
            or run.plan_generation != 2
            or run.plan_digest != binding.expected_plan_manifest_digest
            or proposer is None
            or proposer.bound_agent != binding.proposer_agent
            or proposer.budget.tool_calls != 0
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer persistent Plan or Run proof drifted"
            )

    @staticmethod
    def _assert_binding_source(
        binding: WorkspaceCodingChangeRunBinding,
        source: _ReaderEvidence,
    ) -> None:
        if (
            binding.file_set_binding_id != source.binding.binding_id
            or binding.file_set_binding_digest != source.binding.binding_digest
            or binding.reader_execution_id != source.execution.execution_id
            or binding.reader_execution_digest != source.execution.execution_digest
            or binding.reader_terminal_event_digest != source.execution.latest_event_digest
            or binding.reader_result_set_digest != source.result_set_digest
            or binding.reader_result_ref_digests
            != tuple(item.result_ref_digest for item in source.results)
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer binding crossed its Reader evidence"
            )

    @staticmethod
    def _assert_claim(
        binding: WorkspaceCodingChangeRunBinding,
        claimed: ClaimedInvocation,
    ) -> None:
        if (
            claimed.handoff.run_id != binding.run_id
            or claimed.handoff.target_node_id != binding.proposer_node_id
            or claimed.invocation.node_id != binding.proposer_node_id
            or claimed.handoff.target_agent != binding.proposer_agent
            or claimed.handoff.capability is not None
            or claimed.handoff.capability_input is not None
            or claimed.handoff.upstream_result_refs
            or claimed.handoff.budget_allocation.tool_calls != 0
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Change proposer claim crossed its zero-tool Run binding"
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
            raise ValueError("Change proposer Model Turn exceeded its bound budget")

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
            run = await session.get(TaskExecutionRunRecord, claimed.handoff.run_id)
            node = await session.get(TaskExecutionNodeRecord, claimed.invocation.node_id)
            invocation = await session.get(
                AgentInvocationRecord,
                claimed.invocation.invocation_id,
            )
            if run is None or node is None or invocation is None:
                raise WorkspaceCodingChangeConflictError(
                    "Change proposer failure state is missing"
                )
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
                raise WorkspaceCodingChangeConflictError(
                    "Change proposer failure was fenced by newer state"
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
    def _write_parameters(
        project_path: str,
        test_path: str,
        ecosystem: str,
        source: _ReaderEvidence,
        proposal: WorkspaceCodingChangeProposal,
    ) -> dict[str, str]:
        parameters = {
            "project_path": project_path,
            "test_path": test_path,
            "test_kind": ecosystem,
            "changes_json": json.dumps(
                [
                    {
                        "path": file_read.relative_path,
                        "old_text": change.old_text,
                        "new_text": change.new_text,
                    }
                    for change, file_read in zip(
                        proposal.decision.changes,
                        source.files,
                        strict=True,
                    )
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for index, file_read in enumerate(source.files, start=1):
            parameters[workspace_coding_path_parameter(index)] = file_read.relative_path
        if len(source.files) > WORKSPACE_CODING_MIN_FILES:
            parameters["file_count"] = str(len(source.files))
        return parameters

    @classmethod
    def _assert_confirmation(
        cls,
        *,
        reader_task: TaskRecord | None,
        successor_task: TaskRecord | None,
        message: ConversationMessageRecord | None,
        expected_text: str,
        proposal_created_at: datetime,
    ) -> None:
        if (
            reader_task is None
            or successor_task is None
            or message is None
            or reader_task.task_id == successor_task.task_id
            or reader_task.conversation_id is None
            or successor_task.conversation_id != reader_task.conversation_id
            or message.conversation_id != reader_task.conversation_id
            or message.task_id != successor_task.task_id
            or message.role != "user"
            or message.status != "active"
            or message.content != expected_text
            or message.content_ref is not None
            or successor_task.goal != expected_text
            or message.message_digest != cls._message_digest(message)
            or cls._aware(message.created_at) <= proposal_created_at
        ):
            raise WorkspaceCodingChangeProofRejectedError(
                "Change confirmation is not one fresh exact same-conversation user turn"
            )

    @staticmethod
    def _assert_write_binding_request(
        persisted: WorkspaceCodingWritePlanBinding,
        requested: WorkspaceCodingWritePlanBinding,
    ) -> None:
        if (
            persisted.proposal_id != requested.proposal_id
            or persisted.proposal_digest != requested.proposal_digest
            or persisted.successor_task_id != requested.successor_task_id
            or persisted.confirmation_message_id != requested.confirmation_message_id
            or persisted.confirmation_message_digest != requested.confirmation_message_digest
            or persisted.recipe_digest != requested.recipe_digest
            or persisted.parameter_binding_digest != requested.parameter_binding_digest
            or persisted.parameters_digest != requested.parameters_digest
            or persisted.task_contract_digest != requested.task_contract_digest
            or persisted.draft_plan_digest != requested.draft_plan_digest
            or persisted.expected_plan_manifest_digest
            != requested.expected_plan_manifest_digest
        ):
            raise WorkspaceCodingChangeConflictError(
                "Change proposal is already bound to a different successor Plan"
            )

    @classmethod
    def _message_digest(cls, record: ConversationMessageRecord) -> str:
        return sha256_digest(
            {
                "message_id": record.message_id,
                "conversation_id": record.conversation_id,
                "task_id": record.task_id,
                "role": record.role,
                "content": record.content,
                "content_ref": record.content_ref,
                "classification": record.classification,
                "created_at": cls._aware(record.created_at),
            }
        )

    @staticmethod
    def _execution_from_record(record: TaskLoopExecutionRecord) -> TaskLoopExecution:
        try:
            execution = TaskLoopExecution.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted Reader execution manifest is invalid"
            ) from error
        for field in (
            "execution_id",
            "source_kind",
            "loop_id",
            "draft_id",
            "source_binding_id",
            "source_binding_digest",
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
                raise WorkspaceCodingChangeProofRejectedError(
                    "Reader execution columns diverged from its manifest"
                )
        return execution

    @staticmethod
    def _result_from_record(record: TaskLoopVerifiedResultRecord) -> TaskLoopVerifiedResult:
        values = {
            column.name: getattr(record, column.name)
            for column in TaskLoopVerifiedResultRecord.__table__.columns
            if column.name != "result_ref_id"
        }
        values["result_ref_id"] = record.result_ref_id
        values["created_at"] = WorkspaceCodingChangeRuntime._aware(record.created_at)
        try:
            return TaskLoopVerifiedResult.model_validate(values)
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted Reader ResultRef is invalid"
            ) from error

    @staticmethod
    def _clear_claim(node: TaskExecutionNodeRecord) -> None:
        node.claim_owner_id = None
        node.claim_acquired_at = None
        node.claim_heartbeat_at = None
        node.claim_expires_at = None

    @staticmethod
    def _run_binding_record(
        binding: WorkspaceCodingChangeRunBinding,
    ) -> WorkspaceCodingChangeRunBindingRecord:
        agent = binding.proposer_agent
        return WorkspaceCodingChangeRunBindingRecord(
            binding_id=binding.binding_id,
            file_set_binding_id=binding.file_set_binding_id,
            file_set_binding_digest=binding.file_set_binding_digest,
            reader_execution_id=binding.reader_execution_id,
            reader_execution_digest=binding.reader_execution_digest,
            reader_terminal_event_digest=binding.reader_terminal_event_digest,
            reader_result_set_digest=binding.reader_result_set_digest,
            result_count=len(binding.reader_result_ref_digests),
            reader_task_id=binding.task_contract.task_id,
            contract_version=binding.task_contract.version,
            contract_digest=binding.task_contract_digest,
            plan_generation=binding.expected_plan.plan_generation,
            plan_id=binding.expected_plan.plan_id,
            plan_manifest_digest=binding.expected_plan_manifest_digest,
            run_id=binding.run_id,
            proposer_node_id=binding.proposer_node_id,
            proposer_node_spec_digest=binding.proposer_node_spec_digest,
            proposer_agent_id=agent.agent_id,
            proposer_agent_version=agent.version,
            proposer_agent_contract_digest=agent.contract_digest,
            proposer_prompt_package_digest=agent.prompt_package_digest,
            manifest=binding.model_dump(mode="json"),
            binding_digest=binding.binding_digest,
            created_at=binding.created_at,
        )

    @classmethod
    def _run_binding_from_record(
        cls,
        record: WorkspaceCodingChangeRunBindingRecord,
    ) -> WorkspaceCodingChangeRunBinding:
        try:
            binding = WorkspaceCodingChangeRunBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted Change Proposal Run binding is invalid"
            ) from error
        expected = cls._run_binding_record(binding)
        for field in (
            "binding_id",
            "file_set_binding_id",
            "file_set_binding_digest",
            "reader_execution_id",
            "reader_execution_digest",
            "reader_terminal_event_digest",
            "reader_result_set_digest",
            "result_count",
            "reader_task_id",
            "contract_version",
            "contract_digest",
            "plan_generation",
            "plan_id",
            "plan_manifest_digest",
            "run_id",
            "proposer_node_id",
            "proposer_node_spec_digest",
            "proposer_agent_id",
            "proposer_agent_version",
            "proposer_agent_contract_digest",
            "proposer_prompt_package_digest",
            "binding_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingChangeProofRejectedError(
                    "Change Proposal Run columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != binding.created_at:
            raise WorkspaceCodingChangeProofRejectedError(
                "Change Proposal Run timestamp changed"
            )
        return binding

    @staticmethod
    def _proposal_record(
        proposal: WorkspaceCodingChangeProposal,
        reader_task_id: str,
    ) -> WorkspaceCodingChangeProposalRecord:
        agent = proposal.proposer_agent
        return WorkspaceCodingChangeProposalRecord(
            proposal_id=proposal.proposal_id,
            run_binding_id=proposal.run_binding_id,
            run_binding_digest=proposal.run_binding_digest,
            reader_task_id=reader_task_id,
            reader_execution_id=proposal.decision.reader_execution_id,
            reader_result_set_digest=proposal.decision.reader_result_set_digest,
            proposer_agent_id=agent.agent_id,
            proposer_agent_version=agent.version,
            proposer_agent_contract_digest=agent.contract_digest,
            proposer_prompt_package_digest=agent.prompt_package_digest,
            change_count=len(proposal.decision.changes),
            manifest=proposal.model_dump(mode="json"),
            proposal_digest=proposal.proposal_digest,
            created_at=proposal.created_at,
        )

    @classmethod
    def _proposal_from_record(
        cls,
        record: WorkspaceCodingChangeProposalRecord,
    ) -> WorkspaceCodingChangeProposal:
        try:
            proposal = WorkspaceCodingChangeProposal.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted Change Proposal is invalid"
            ) from error
        expected = cls._proposal_record(proposal, record.reader_task_id)
        for field in (
            "proposal_id",
            "run_binding_id",
            "run_binding_digest",
            "reader_task_id",
            "reader_execution_id",
            "reader_result_set_digest",
            "proposer_agent_id",
            "proposer_agent_version",
            "proposer_agent_contract_digest",
            "proposer_prompt_package_digest",
            "change_count",
            "proposal_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingChangeProofRejectedError(
                    "Change Proposal columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != proposal.created_at:
            raise WorkspaceCodingChangeProofRejectedError("Change Proposal timestamp changed")
        return proposal

    @staticmethod
    def _turn_proof_record(
        proof: WorkspaceCodingChangeTurnProof,
    ) -> WorkspaceCodingChangeTurnProofRecord:
        return WorkspaceCodingChangeTurnProofRecord(
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
        record: WorkspaceCodingChangeTurnProofRecord,
    ) -> WorkspaceCodingChangeTurnProof:
        try:
            proof = WorkspaceCodingChangeTurnProof.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted Change Proposal Turn proof is invalid"
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
                raise WorkspaceCodingChangeProofRejectedError(
                    "Change Proposal Turn columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != proof.created_at:
            raise WorkspaceCodingChangeProofRejectedError(
                "Change Proposal Turn timestamp changed"
            )
        return proof

    @staticmethod
    def _write_binding_record(
        binding: WorkspaceCodingWritePlanBinding,
    ) -> WorkspaceCodingWritePlanBindingRecord:
        return WorkspaceCodingWritePlanBindingRecord(
            binding_id=binding.binding_id,
            proposal_id=binding.proposal_id,
            proposal_digest=binding.proposal_digest,
            successor_task_id=binding.successor_task_id,
            confirmation_message_id=binding.confirmation_message_id,
            confirmation_message_digest=binding.confirmation_message_digest,
            route_id=binding.route_id,
            route_version=binding.route_version,
            recipe_digest=binding.recipe_digest,
            parameter_binding_digest=binding.parameter_binding_digest,
            parameters_digest=binding.parameters_digest,
            contract_version=binding.task_contract.version,
            contract_digest=binding.task_contract_digest,
            plan_generation=binding.expected_plan.plan_generation,
            plan_id=binding.expected_plan.plan_id,
            plan_manifest_digest=binding.expected_plan_manifest_digest,
            change_count=len(json.loads(binding.parameters["changes_json"])),
            manifest=binding.model_dump(mode="json"),
            binding_digest=binding.binding_digest,
            created_at=binding.created_at,
        )

    @classmethod
    def _write_binding_from_record(
        cls,
        record: WorkspaceCodingWritePlanBindingRecord,
    ) -> WorkspaceCodingWritePlanBinding:
        try:
            binding = WorkspaceCodingWritePlanBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingChangeProofRejectedError(
                "Persisted write Plan binding is invalid"
            ) from error
        expected = cls._write_binding_record(binding)
        for field in (
            "binding_id",
            "proposal_id",
            "proposal_digest",
            "successor_task_id",
            "confirmation_message_id",
            "confirmation_message_digest",
            "route_id",
            "route_version",
            "recipe_digest",
            "parameter_binding_digest",
            "parameters_digest",
            "contract_version",
            "contract_digest",
            "plan_generation",
            "plan_id",
            "plan_manifest_digest",
            "change_count",
            "binding_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingChangeProofRejectedError(
                    "Write Plan columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != binding.created_at:
            raise WorkspaceCodingChangeProofRejectedError("Write Plan timestamp changed")
        return binding

    @staticmethod
    def _workbench(
        *,
        phase: str,
        source: _ReaderEvidence,
        run_binding: WorkspaceCodingChangeRunBinding | None,
        proposal: WorkspaceCodingChangeProposal | None,
        write: WorkspaceCodingWritePlanBinding | None,
        invocation: AgentInvocationRecord | None,
        turn: AgentModelTurnRecord | None,
        run_status: str | None = None,
    ) -> WorkspaceCodingChangeWorkbenchRead:
        material = {
            "schema_version": "deskpilot.workspace-coding-change-workbench.v1",
            "phase": phase,
            "reader_task_id": source.binding.successor_task_id,
            "file_set_binding_id": source.binding.binding_id,
            "reader_execution_id": source.execution.execution_id,
            "reader_result_set_digest": source.result_set_digest,
            "run_binding_id": run_binding.binding_id if run_binding else None,
            "run_id": run_binding.run_id if run_binding else None,
            "run_status": run_status,
            "invocation_id": invocation.invocation_id if invocation else None,
            "turn_id": turn.turn_id if turn else None,
            "turn_status": turn.status if turn else None,
            "proposal_id": proposal.proposal_id if proposal else None,
            "proposal_digest": proposal.proposal_digest if proposal else None,
            "changes": proposal.decision.changes if proposal else (),
            "confirmation_text": (
                f"确认变更提案：{proposal.proposal_id}" if proposal else None
            ),
            "write_plan_binding_id": write.binding_id if write else None,
            "successor_task_id": write.successor_task_id if write else None,
            "plan_id": write.expected_plan.plan_id if write else None,
            "plan_manifest_digest": (
                write.expected_plan_manifest_digest if write else None
            ),
            "requires_user_confirmation": phase == "proposal_ready",
        }
        return WorkspaceCodingChangeWorkbenchRead.model_validate(
            {**material, "projection_digest": sha256_digest(material)}
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "WorkspaceCodingChangeConflictError",
    "WorkspaceCodingChangeProofRejectedError",
    "WorkspaceCodingChangeRuntime",
    "WorkspaceCodingChangeRuntimeError",
]
