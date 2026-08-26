"""Persistent fail-closed bridge from project exploration to a confirmed Reader Plan."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from deskpilot.application.agent_registry import AgentRegistry
from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningError,
)
from deskpilot.application.plan_compiler import (
    workspace_coding_file_set_contract,
    workspace_coding_file_set_draft,
)
from deskpilot.application.workspace_coding_runtime import (
    WorkspaceCodingError,
    WorkspaceCodingRuntime,
)
from deskpilot.application.workspace_file_runtime import WorkspaceFileError
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import AgentOutputResult
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingExplorationDecision,
    WorkspaceCodingExplorationProposal,
    WorkspaceCodingExplorationSnapshot,
    WorkspaceCodingExplorationWorkbenchRead,
    WorkspaceCodingExplorerRunBinding,
    WorkspaceCodingExplorerTurnProof,
    WorkspaceCodingFileSetNodeMapping,
    WorkspaceCodingFileSetPlanBinding,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentResultRecord,
    ConversationMessageRecord,
    TaskContractVersionRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskPlanGenerationRecord,
    TaskRecord,
    WorkspaceCodingExplorationProposalRecord,
    WorkspaceCodingExplorationSnapshotRecord,
    WorkspaceCodingExplorerRunBindingRecord,
    WorkspaceCodingExplorerTurnProofRecord,
    WorkspaceCodingFileSetPlanBindingRecord,
)


class WorkspaceCodingExplorationBindingError(RuntimeError):
    code = "WORKSPACE_CODING_EXPLORATION_BINDING_ERROR"


class WorkspaceCodingExplorationNotFoundError(WorkspaceCodingExplorationBindingError):
    code = "WORKSPACE_CODING_EXPLORATION_NOT_FOUND"


class WorkspaceCodingExplorationConflictError(WorkspaceCodingExplorationBindingError):
    code = "WORKSPACE_CODING_EXPLORATION_CONFLICT"


class WorkspaceCodingExplorationProofRejectedError(WorkspaceCodingExplorationBindingError):
    code = "WORKSPACE_CODING_EXPLORATION_PROOF_REJECTED"


class WorkspaceCodingExplorationBinder:
    """Store immutable snapshot/proposal/confirmation proofs without a second FSM."""

    def __init__(
        self,
        database: Database,
        workspace: WorkspaceCodingRuntime,
        agents: AgentRegistry,
        capabilities: CapabilityCatalog,
        planning: PlanCompilationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._workspace = workspace
        self._agents = agents
        self._capabilities = capabilities
        self._planning = planning
        self._clock = clock or (lambda: datetime.now(UTC))

    async def prepare(
        self,
        *,
        task_id: str,
        user_message_id: str,
        project_path: str,
        ecosystem: Literal["python", "node"],
        test_path: str,
    ) -> WorkspaceCodingExplorationSnapshot:
        """Persist one server-owned catalog before any Explorer dispatch."""

        async with self._database.session() as session:
            task = await session.get(TaskRecord, task_id)
            message = await session.get(ConversationMessageRecord, user_message_id)
            self._assert_source_message(task, message)
            assert task is not None and message is not None and message.content is not None
            existing = await session.scalar(
                select(WorkspaceCodingExplorationSnapshotRecord).where(
                    WorkspaceCodingExplorationSnapshotRecord.source_task_id == task_id
                )
            )
            if existing is not None:
                persisted = self._snapshot_from_record(existing)
                self._assert_current_snapshot(persisted)
                return persisted
            snapshot = self._workspace.exploration_snapshot(
                task_id=task_id,
                user_message_id=user_message_id,
                user_message_digest=message.message_digest,
                project_path=project_path,
                ecosystem=ecosystem,
                test_path=test_path,
                objective_digest=sha256_digest({"objective": task.goal}),
                created_at=self._now(),
            )
        try:
            async with self._database.session() as session, session.begin():
                existing = await session.scalar(
                    select(WorkspaceCodingExplorationSnapshotRecord)
                    .where(WorkspaceCodingExplorationSnapshotRecord.source_task_id == task_id)
                    .with_for_update()
                )
                if existing is not None:
                    persisted = self._snapshot_from_record(existing)
                    if persisted.snapshot_id != snapshot.snapshot_id:
                        raise WorkspaceCodingExplorationConflictError(
                            "Task already has another exploration snapshot"
                        )
                    return persisted
                locked_task = await session.get(TaskRecord, task_id)
                locked_message = await session.get(
                    ConversationMessageRecord,
                    user_message_id,
                )
                self._assert_source_message(locked_task, locked_message)
                if (
                    locked_message is None
                    or locked_message.message_digest != snapshot.user_message_digest
                    or locked_task is None
                    or sha256_digest({"objective": locked_task.goal}) != snapshot.objective_digest
                ):
                    raise WorkspaceCodingExplorationProofRejectedError(
                        "Exploration source changed before snapshot persistence"
                    )
                session.add(self._snapshot_record(snapshot))
        except IntegrityError:
            persisted = await self.get_snapshot(task_id=task_id)
            if persisted.snapshot_id == snapshot.snapshot_id:
                return persisted
            raise WorkspaceCodingExplorationConflictError(
                "Concurrent exploration snapshot did not converge"
            ) from None
        return await self.get_snapshot(task_id=task_id)

    async def submit_proposal(
        self,
        snapshot_id: str,
        decision: WorkspaceCodingExplorationDecision,
    ) -> WorkspaceCodingExplorationProposal:
        """Reject the removed unverified ingress; use the persistent Explorer runtime."""

        del snapshot_id, decision
        raise WorkspaceCodingExplorationProofRejectedError(
            "Explorer proposals require a verified persistent Invocation/Model Turn"
        )

    async def confirm(
        self,
        proposal_id: str,
        *,
        successor_task_id: str,
        confirmation_message_id: str,
    ) -> WorkspaceCodingFileSetPlanBinding:
        """Atomically bind explicit confirmation and successor generation-1 Plan."""

        proposal = await self.get_proposal(proposal_id=proposal_id)
        snapshot = await self.get_snapshot(snapshot_id=proposal.snapshot_id)
        self._assert_current_snapshot(snapshot)
        self._assert_decision(snapshot, proposal.decision)
        file_count = len(proposal.decision.files)
        contract = workspace_coding_file_set_contract(
            successor_task_id,
            self._capabilities,
            file_count=file_count,
        )
        draft = workspace_coding_file_set_draft(
            successor_task_id,
            file_count=file_count,
        )
        expected_plan = self._planning.preview_initial(contract, draft)
        nodes_by_key = {item.local_key: item for item in expected_plan.nodes}
        mappings = tuple(
            WorkspaceCodingFileSetNodeMapping.build(
                ordinal=index,
                relative_path=candidate.relative_path,
                source_file_proof_digest=candidate.source_file_proof_digest,
                plan_node_id=nodes_by_key[f"inspect_candidate_{index:02d}"].node_id,
                plan_local_key=f"inspect_candidate_{index:02d}",
                plan_node_spec_digest=nodes_by_key[
                    f"inspect_candidate_{index:02d}"
                ].node_spec_digest,
            )
            for index, candidate in enumerate(proposal.decision.files, start=1)
        )
        try:
            async with self._database.session() as session, session.begin():
                activated = await self._planning.activate_initial_once_in_session(
                    session,
                    contract,
                    draft,
                )
                if activated.plan != expected_plan:
                    raise WorkspaceCodingExplorationProofRejectedError(
                        "Confirmed file-set Plan changed during activation"
                    )
                snapshot_record = await session.scalar(
                    select(WorkspaceCodingExplorationSnapshotRecord)
                    .where(
                        WorkspaceCodingExplorationSnapshotRecord.snapshot_id == snapshot.snapshot_id
                    )
                    .with_for_update()
                )
                proposal_record = await session.scalar(
                    select(WorkspaceCodingExplorationProposalRecord)
                    .where(
                        WorkspaceCodingExplorationProposalRecord.proposal_id == proposal.proposal_id
                    )
                    .with_for_update()
                )
                existing = await session.scalar(
                    select(WorkspaceCodingFileSetPlanBindingRecord)
                    .where(
                        WorkspaceCodingFileSetPlanBindingRecord.proposal_id == proposal.proposal_id
                    )
                    .with_for_update()
                )
                if existing is not None:
                    persisted = self._binding_from_record(existing)
                    self._assert_binding_proposal(persisted, proposal)
                    if (
                        persisted.task_contract != contract
                        or persisted.draft_plan != draft
                        or persisted.expected_plan != expected_plan
                        or persisted.mappings != mappings
                    ):
                        raise WorkspaceCodingExplorationProofRejectedError(
                            "Existing file-set binding changed across recovery"
                        )
                    return persisted
                source_task = await session.get(TaskRecord, snapshot.task_id)
                successor_task = await session.get(TaskRecord, successor_task_id)
                confirmation = await session.get(
                    ConversationMessageRecord,
                    confirmation_message_id,
                )
                if (
                    snapshot_record is None
                    or proposal_record is None
                    or self._snapshot_from_record(snapshot_record) != snapshot
                    or self._proposal_from_record(proposal_record) != proposal
                ):
                    raise WorkspaceCodingExplorationProofRejectedError(
                        "Exploration proof changed before confirmation"
                    )
                self._assert_confirmation(
                    source_task=source_task,
                    successor_task=successor_task,
                    confirmation=confirmation,
                    proposal=proposal,
                )
                assert confirmation is not None
                binding = WorkspaceCodingFileSetPlanBinding.build(
                    proposal_id=proposal.proposal_id,
                    proposal_digest=proposal.proposal_digest,
                    successor_task_id=successor_task_id,
                    confirmation_message_id=confirmation_message_id,
                    confirmation_message_digest=confirmation.message_digest,
                    task_contract=contract,
                    draft_plan=draft,
                    expected_plan=expected_plan,
                    mappings=mappings,
                    created_at=self._now(),
                )
                session.add(self._binding_record(binding))
        except IntegrityError:
            recovered_binding = await self.get_binding(proposal_id=proposal_id)
            if recovered_binding is not None:
                return recovered_binding
            raise WorkspaceCodingExplorationConflictError(
                "Concurrent file-set confirmation did not converge"
            ) from None
        except PlanningError as error:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Confirmed file-set planning generation was rejected"
            ) from error
        recovered_binding = await self.get_binding(proposal_id=proposal_id)
        if recovered_binding is None:
            raise WorkspaceCodingExplorationConflictError(
                "Confirmed file-set Plan binding was not persisted"
            )
        return recovered_binding

    async def get_snapshot(
        self,
        *,
        task_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> WorkspaceCodingExplorationSnapshot:
        if (task_id is None) == (snapshot_id is None):
            raise ValueError("Exactly one exploration snapshot key is required")
        async with self._database.session() as session:
            statement = select(WorkspaceCodingExplorationSnapshotRecord)
            statement = statement.where(
                WorkspaceCodingExplorationSnapshotRecord.source_task_id == task_id
                if task_id is not None
                else WorkspaceCodingExplorationSnapshotRecord.snapshot_id == snapshot_id
            )
            record = await session.scalar(statement)
            if record is None:
                raise WorkspaceCodingExplorationNotFoundError(
                    "Workspace exploration snapshot does not exist"
                )
            snapshot = self._snapshot_from_record(record)
            task = await session.get(TaskRecord, snapshot.task_id)
            message = await session.get(
                ConversationMessageRecord,
                snapshot.user_message_id,
            )
            self._assert_source_message(task, message)
            if (
                message is None
                or task is None
                or message.message_digest != snapshot.user_message_digest
                or sha256_digest({"objective": task.goal}) != snapshot.objective_digest
            ):
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Exploration snapshot crossed its persisted source"
                )
            return snapshot

    async def get_proposal(
        self,
        *,
        snapshot_id: str | None = None,
        proposal_id: str | None = None,
    ) -> WorkspaceCodingExplorationProposal:
        if (snapshot_id is None) == (proposal_id is None):
            raise ValueError("Exactly one exploration proposal key is required")
        async with self._database.session() as session:
            statement = select(WorkspaceCodingExplorationProposalRecord)
            statement = statement.where(
                WorkspaceCodingExplorationProposalRecord.snapshot_id == snapshot_id
                if snapshot_id is not None
                else WorkspaceCodingExplorationProposalRecord.proposal_id == proposal_id
            )
            record = await session.scalar(statement)
            if record is None:
                raise WorkspaceCodingExplorationNotFoundError(
                    "Workspace Explorer proposal does not exist"
                )
            proposal = self._proposal_from_record(record)
            snapshot_record = await session.get(
                WorkspaceCodingExplorationSnapshotRecord,
                proposal.snapshot_id,
            )
            if snapshot_record is None:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer proposal lost its snapshot"
                )
            snapshot = self._snapshot_from_record(snapshot_record)
            if (
                proposal.snapshot_digest != snapshot.snapshot_digest
                or proposal.decision.snapshot_id != snapshot.snapshot_id
                or proposal.decision.snapshot_digest != snapshot.snapshot_digest
            ):
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer proposal crossed its persisted snapshot"
                )
            turn_proof_record = await session.scalar(
                select(WorkspaceCodingExplorerTurnProofRecord).where(
                    WorkspaceCodingExplorerTurnProofRecord.proposal_id == proposal.proposal_id
                )
            )
            if turn_proof_record is None:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer proposal has no succeeded persistent Model Turn proof"
                )
            try:
                proof = WorkspaceCodingExplorerTurnProof.model_validate(turn_proof_record.manifest)
            except ValidationError as error:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Persisted Explorer Model Turn proof is invalid"
                ) from error
            run_binding_record = await session.get(
                WorkspaceCodingExplorerRunBindingRecord,
                proof.run_binding_id,
            )
            invocation = await session.get(AgentInvocationRecord, proof.invocation_id)
            turn = await session.get(AgentModelTurnRecord, proof.turn_id)
            agent_decision = await session.get(
                AgentDecisionRecord,
                proof.agent_decision_id,
            )
            result = (
                await session.get(AgentResultRecord, invocation.result_id)
                if invocation is not None and invocation.result_id is not None
                else None
            )
            if run_binding_record is None:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer proposal lost its persistent Run binding"
                )
            try:
                run_binding = WorkspaceCodingExplorerRunBinding.model_validate(
                    run_binding_record.manifest
                )
            except ValidationError as error:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Persisted Explorer Run binding is invalid"
                ) from error
            contract_record = await session.get(
                TaskContractVersionRecord,
                (
                    run_binding.task_contract.task_id,
                    run_binding.task_contract.version,
                ),
            )
            plan_record = await session.get(
                TaskPlanGenerationRecord,
                (
                    run_binding.expected_plan.task_id,
                    run_binding.expected_plan.plan_generation,
                ),
            )
            run_record = await session.get(TaskExecutionRunRecord, run_binding.run_id)
            explorer_node = await session.get(
                TaskExecutionNodeRecord,
                run_binding.explorer_node_id,
            )
            try:
                result_envelope = (
                    AgentOutputResult.model_validate(result.manifest)
                    if result is not None
                    else None
                )
            except ValidationError as error:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Persisted Explorer Agent Result is invalid"
                ) from error
            expected_agent_decision_digest = (
                sha256_digest(
                    {
                        "turn_id": turn.turn_id,
                        "invocation_id": turn.invocation_id,
                        "decision": agent_decision.manifest,
                        "response_digest": turn.response_digest,
                    }
                )
                if turn is not None and agent_decision is not None
                else None
            )
            if (
                turn_proof_record.proof_id != proof.proof_id
                or turn_proof_record.proposal_id != proof.proposal_id
                or turn_proof_record.proposal_digest != proof.proposal_digest
                or turn_proof_record.run_binding_id != proof.run_binding_id
                or turn_proof_record.run_binding_digest != proof.run_binding_digest
                or turn_proof_record.invocation_id != proof.invocation_id
                or turn_proof_record.turn_id != proof.turn_id
                or turn_proof_record.agent_decision_id != proof.agent_decision_id
                or turn_proof_record.agent_decision_digest != proof.agent_decision_digest
                or turn_proof_record.model_request_digest != proof.model_request_digest
                or turn_proof_record.model_response_digest != proof.model_response_digest
                or turn_proof_record.proof_digest != proof.proof_digest
                or self._aware(turn_proof_record.created_at) != proof.created_at
                or proof.proposal_id != proposal.proposal_id
                or proof.proposal_digest != proposal.proposal_digest
                or proof.run_binding_digest != run_binding.binding_digest
                or run_binding_record.binding_id != run_binding.binding_id
                or run_binding_record.snapshot_id != run_binding.snapshot_id
                or run_binding_record.snapshot_digest != run_binding.snapshot_digest
                or run_binding_record.source_task_id != run_binding.task_contract.task_id
                or run_binding_record.contract_version != run_binding.task_contract.version
                or run_binding_record.contract_digest != run_binding.task_contract_digest
                or run_binding_record.plan_generation != run_binding.expected_plan.plan_generation
                or run_binding_record.plan_id != run_binding.expected_plan.plan_id
                or run_binding_record.plan_manifest_digest
                != run_binding.expected_plan_manifest_digest
                or run_binding_record.run_id != run_binding.run_id
                or run_binding_record.explorer_node_id != run_binding.explorer_node_id
                or run_binding_record.explorer_node_spec_digest
                != run_binding.explorer_node_spec_digest
                or run_binding_record.explorer_agent_id != run_binding.explorer_agent.agent_id
                or run_binding_record.explorer_agent_version != run_binding.explorer_agent.version
                or run_binding_record.explorer_agent_contract_digest
                != run_binding.explorer_agent.contract_digest
                or run_binding_record.explorer_prompt_package_digest
                != run_binding.explorer_agent.prompt_package_digest
                or run_binding_record.binding_digest != run_binding.binding_digest
                or self._aware(run_binding_record.created_at) != run_binding.created_at
                or run_binding.snapshot_id != snapshot.snapshot_id
                or run_binding.snapshot_digest != snapshot.snapshot_digest
                or run_binding.explorer_agent != proposal.explorer_agent
                or invocation is None
                or invocation.run_id != run_binding.run_id
                or invocation.node_id != run_binding.explorer_node_id
                or invocation.execution_status != "result_submitted"
                or invocation.verification_status != "verified"
                or invocation.agent_id != run_binding.explorer_agent.agent_id
                or invocation.agent_version != run_binding.explorer_agent.version
                or invocation.agent_contract_digest != run_binding.explorer_agent.contract_digest
                or invocation.prompt_package_digest
                != run_binding.explorer_agent.prompt_package_digest
                or turn is None
                or turn.invocation_id != invocation.invocation_id
                or turn.status != "succeeded"
                or turn.request_digest != proof.model_request_digest
                or turn.response_digest != proof.model_response_digest
                or agent_decision is None
                or agent_decision.turn_id != turn.turn_id
                or agent_decision.invocation_id != invocation.invocation_id
                or agent_decision.binding_id != run_binding.binding_id
                or agent_decision.kind != "propose_file_set"
                or agent_decision.manifest != proposal.decision.model_dump(mode="json")
                or agent_decision.decision_digest != proof.agent_decision_digest
                or agent_decision.decision_digest != expected_agent_decision_digest
                or contract_record is None
                or contract_record.contract_digest != run_binding.task_contract_digest
                or contract_record.manifest != run_binding.task_contract.model_dump(mode="json")
                or plan_record is None
                or plan_record.status != "active"
                or plan_record.plan_manifest_digest != run_binding.expected_plan_manifest_digest
                or plan_record.manifest != run_binding.expected_plan.model_dump(mode="json")
                or run_record is None
                or run_record.task_id != run_binding.task_contract.task_id
                or run_record.plan_generation != run_binding.expected_plan.plan_generation
                or run_record.plan_digest != run_binding.expected_plan_manifest_digest
                or run_record.status != "succeeded"
                or explorer_node is None
                or explorer_node.run_id != run_binding.run_id
                or explorer_node.node_spec_digest != run_binding.explorer_node_spec_digest
                or explorer_node.status != "verified"
                or result is None
                or result_envelope is None
                or result.result_digest != result_envelope.result_digest
                or result_envelope.invocation_id != invocation.invocation_id
                or result_envelope.output != proposal.decision.model_dump(mode="json")
                or result_envelope.input_digest != proof.model_request_digest
                or result_envelope.model_response_digest != proof.model_response_digest
                or f"workspace-explorer-turn:{proof.proof_digest}"
                not in result_envelope.evidence_refs
            ):
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer proposal crossed its Invocation/Model Turn proof"
                )
            return proposal

    async def get_binding(
        self,
        *,
        proposal_id: str,
    ) -> WorkspaceCodingFileSetPlanBinding | None:
        async with self._database.session() as session:
            record = await session.scalar(
                select(WorkspaceCodingFileSetPlanBindingRecord).where(
                    WorkspaceCodingFileSetPlanBindingRecord.proposal_id == proposal_id
                )
            )
            if record is None:
                return None
            binding = self._binding_from_record(record)
        proposal = await self.get_proposal(proposal_id=binding.proposal_id)
        self._assert_binding_proposal(binding, proposal)
        try:
            contracts = await self._planning.list_contracts(binding.successor_task_id)
            plan = await self._planning.get_plan(
                binding.successor_task_id,
                binding.expected_plan.plan_generation,
            )
        except PlanningError as error:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Confirmed file-set planning evidence is missing or invalid"
            ) from error
        contract_matches = tuple(
            item.contract
            for item in contracts.contracts
            if item.contract.version == binding.task_contract.version
        )
        if contract_matches != (binding.task_contract,) or plan.plan != binding.expected_plan:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Confirmed file-set planning evidence crossed its binding"
            )
        return binding

    async def get_workbench(
        self,
        task_id: str,
    ) -> WorkspaceCodingExplorationWorkbenchRead | None:
        """Project one proof chain for its source Task or confirmed successor."""

        async with self._database.session() as session:
            snapshot_record = await session.scalar(
                select(WorkspaceCodingExplorationSnapshotRecord).where(
                    WorkspaceCodingExplorationSnapshotRecord.source_task_id == task_id
                )
            )
            proposal_record: WorkspaceCodingExplorationProposalRecord | None = None
            run_binding_record: WorkspaceCodingExplorerRunBindingRecord | None = None
            turn_proof_record: WorkspaceCodingExplorerTurnProofRecord | None = None
            explorer_run_record: TaskExecutionRunRecord | None = None
            explorer_invocation_record: AgentInvocationRecord | None = None
            explorer_turn_record: AgentModelTurnRecord | None = None
            binding_record: WorkspaceCodingFileSetPlanBindingRecord | None = None
            if snapshot_record is not None:
                run_binding_record = await session.scalar(
                    select(WorkspaceCodingExplorerRunBindingRecord).where(
                        WorkspaceCodingExplorerRunBindingRecord.snapshot_id
                        == snapshot_record.snapshot_id
                    )
                )
                proposal_record = await session.scalar(
                    select(WorkspaceCodingExplorationProposalRecord).where(
                        WorkspaceCodingExplorationProposalRecord.snapshot_id
                        == snapshot_record.snapshot_id
                    )
                )
                if proposal_record is not None:
                    turn_proof_record = await session.scalar(
                        select(WorkspaceCodingExplorerTurnProofRecord).where(
                            WorkspaceCodingExplorerTurnProofRecord.proposal_id
                            == proposal_record.proposal_id
                        )
                    )
                    binding_record = await session.scalar(
                        select(WorkspaceCodingFileSetPlanBindingRecord).where(
                            WorkspaceCodingFileSetPlanBindingRecord.proposal_id
                            == proposal_record.proposal_id
                        )
                    )
            else:
                binding_record = await session.scalar(
                    select(WorkspaceCodingFileSetPlanBindingRecord).where(
                        WorkspaceCodingFileSetPlanBindingRecord.successor_task_id == task_id
                    )
                )
                if binding_record is not None:
                    proposal_record = await session.get(
                        WorkspaceCodingExplorationProposalRecord,
                        binding_record.proposal_id,
                    )
                    if proposal_record is not None:
                        snapshot_record = await session.get(
                            WorkspaceCodingExplorationSnapshotRecord,
                            proposal_record.snapshot_id,
                        )
                        run_binding_record = await session.scalar(
                            select(WorkspaceCodingExplorerRunBindingRecord).where(
                                WorkspaceCodingExplorerRunBindingRecord.snapshot_id
                                == proposal_record.snapshot_id
                            )
                        )
                        turn_proof_record = await session.scalar(
                            select(WorkspaceCodingExplorerTurnProofRecord).where(
                                WorkspaceCodingExplorerTurnProofRecord.proposal_id
                                == proposal_record.proposal_id
                            )
                        )
            if run_binding_record is not None:
                explorer_run_record = await session.get(
                    TaskExecutionRunRecord,
                    run_binding_record.run_id,
                )
                explorer_invocation_record = await session.scalar(
                    select(AgentInvocationRecord)
                    .where(
                        AgentInvocationRecord.run_id == run_binding_record.run_id,
                        AgentInvocationRecord.node_id == run_binding_record.explorer_node_id,
                    )
                    .order_by(AgentInvocationRecord.attempt.desc())
                    .limit(1)
                )
                if explorer_invocation_record is not None:
                    explorer_turn_record = await session.scalar(
                        select(AgentModelTurnRecord)
                        .where(
                            AgentModelTurnRecord.invocation_id
                            == explorer_invocation_record.invocation_id
                        )
                        .order_by(AgentModelTurnRecord.turn_no.desc())
                        .limit(1)
                    )
        if snapshot_record is None:
            return None
        snapshot = await self.get_snapshot(snapshot_id=snapshot_record.snapshot_id)
        self._assert_current_snapshot(snapshot)
        run_binding = (
            WorkspaceCodingExplorerRunBinding.model_validate(run_binding_record.manifest)
            if run_binding_record is not None
            else None
        )
        turn_proof = (
            WorkspaceCodingExplorerTurnProof.model_validate(turn_proof_record.manifest)
            if turn_proof_record is not None
            else None
        )
        proposal = (
            await self.get_proposal(proposal_id=proposal_record.proposal_id)
            if proposal_record is not None
            else None
        )
        binding = (
            await self.get_binding(proposal_id=binding_record.proposal_id)
            if binding_record is not None
            else None
        )
        phase: Literal[
            "snapshot_ready",
            "explorer_ready",
            "explorer_blocked",
            "proposal_ready",
            "confirmed_read_only_plan",
        ] = (
            "confirmed_read_only_plan"
            if binding is not None
            else "proposal_ready"
            if proposal is not None
            else "explorer_blocked"
            if explorer_turn_record is not None
            and explorer_turn_record.status in {"failed", "outcome_unknown"}
            else "explorer_ready"
            if run_binding is not None
            else "snapshot_ready"
        )
        material = {
            "schema_version": "deskpilot.workspace-coding-exploration-workbench.v1",
            "phase": phase,
            "source_task_id": snapshot.task_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "project_path": snapshot.project_path,
            "ecosystem": snapshot.ecosystem,
            "test_path": snapshot.test_path,
            "catalog_file_count": len(snapshot.files),
            "catalog_truncated": snapshot.truncated,
            "explorer_run_binding_id": (
                run_binding.binding_id if run_binding is not None else None
            ),
            "explorer_run_binding_digest": (
                run_binding.binding_digest if run_binding is not None else None
            ),
            "explorer_run_id": run_binding.run_id if run_binding is not None else None,
            "explorer_run_status": (
                explorer_run_record.status if explorer_run_record is not None else None
            ),
            "explorer_invocation_id": (
                explorer_invocation_record.invocation_id
                if explorer_turn_record is not None and explorer_invocation_record is not None
                else None
            ),
            "explorer_turn_id": (
                explorer_turn_record.turn_id if explorer_turn_record is not None else None
            ),
            "explorer_turn_status": (
                explorer_turn_record.status if explorer_turn_record is not None else None
            ),
            "explorer_turn_proof_digest": (
                turn_proof.proof_digest if turn_proof is not None else None
            ),
            "proposal_id": proposal.proposal_id if proposal is not None else None,
            "proposal_digest": (proposal.proposal_digest if proposal is not None else None),
            "candidates": proposal.decision.files if proposal is not None else (),
            "confirmation_text": (
                f"确认候选文件集：{proposal.proposal_id}" if proposal is not None else None
            ),
            "binding_id": binding.binding_id if binding is not None else None,
            "binding_digest": binding.binding_digest if binding is not None else None,
            "successor_task_id": (binding.successor_task_id if binding is not None else None),
            "plan_generation": (
                binding.expected_plan.plan_generation if binding is not None else None
            ),
            "plan_id": binding.expected_plan.plan_id if binding is not None else None,
            "plan_manifest_digest": (
                binding.expected_plan_manifest_digest if binding is not None else None
            ),
            "requires_user_confirmation": proposal is not None and binding is None,
        }
        return WorkspaceCodingExplorationWorkbenchRead.model_validate(
            {**material, "projection_digest": sha256_digest(material)}
        )

    def _assert_current_snapshot(
        self,
        snapshot: WorkspaceCodingExplorationSnapshot,
    ) -> None:
        try:
            current = self._workspace.exploration_snapshot(
                task_id=snapshot.task_id,
                user_message_id=snapshot.user_message_id,
                user_message_digest=snapshot.user_message_digest,
                project_path=snapshot.project_path,
                ecosystem=snapshot.ecosystem,
                test_path=snapshot.test_path,
                objective_digest=snapshot.objective_digest,
                created_at=snapshot.created_at,
            )
        except (WorkspaceCodingError, WorkspaceFileError) as error:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Workspace exploration project is no longer eligible"
            ) from error
        if current != snapshot:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Workspace exploration catalog drifted"
            )

    def revalidate_snapshot(
        self,
        snapshot: WorkspaceCodingExplorationSnapshot,
    ) -> None:
        """Fail closed when the persisted snapshot no longer matches disk metadata."""

        self._assert_current_snapshot(snapshot)

    @staticmethod
    def _assert_decision(
        snapshot: WorkspaceCodingExplorationSnapshot,
        decision: WorkspaceCodingExplorationDecision,
    ) -> None:
        if (
            decision.snapshot_id != snapshot.snapshot_id
            or decision.snapshot_digest != snapshot.snapshot_digest
        ):
            raise WorkspaceCodingExplorationProofRejectedError(
                "Explorer decision crossed its snapshot"
            )
        catalog = {item.relative_path: item for item in snapshot.files}
        for candidate in decision.files:
            source = catalog.get(candidate.relative_path)
            if source is None or candidate.source_file_proof_digest != source.proof_digest:
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer candidate path or file proof is not in the snapshot"
                )

    def validate_decision(
        self,
        snapshot: WorkspaceCodingExplorationSnapshot,
        decision: WorkspaceCodingExplorationDecision,
    ) -> None:
        """Validate one untrusted decision against the exact server snapshot."""

        self._assert_decision(snapshot, decision)

    @staticmethod
    def _assert_binding_proposal(
        binding: WorkspaceCodingFileSetPlanBinding,
        proposal: WorkspaceCodingExplorationProposal,
    ) -> None:
        expected = tuple(
            (item.relative_path, item.source_file_proof_digest) for item in proposal.decision.files
        )
        actual = tuple(
            (item.relative_path, item.source_file_proof_digest) for item in binding.mappings
        )
        if (
            binding.proposal_id != proposal.proposal_id
            or binding.proposal_digest != proposal.proposal_digest
            or actual != expected
        ):
            raise WorkspaceCodingExplorationProofRejectedError(
                "Confirmed Reader mappings crossed the Explorer proposal"
            )

    @classmethod
    def _assert_source_message(
        cls,
        task: TaskRecord | None,
        message: ConversationMessageRecord | None,
    ) -> None:
        if (
            task is None
            or message is None
            or task.conversation_id is None
            or message.conversation_id != task.conversation_id
            or message.task_id != task.task_id
            or message.role != "user"
            or message.status != "active"
            or message.content is None
            or message.content_ref is not None
            or message.content != task.goal
            or message.message_digest != cls._message_digest(message)
        ):
            raise WorkspaceCodingExplorationProofRejectedError(
                "Exploration source Task or user message changed"
            )

    @classmethod
    def _assert_confirmation(
        cls,
        *,
        source_task: TaskRecord | None,
        successor_task: TaskRecord | None,
        confirmation: ConversationMessageRecord | None,
        proposal: WorkspaceCodingExplorationProposal,
    ) -> None:
        expected = f"确认候选文件集：{proposal.proposal_id}"
        if (
            source_task is None
            or successor_task is None
            or confirmation is None
            or source_task.task_id == successor_task.task_id
            or source_task.conversation_id is None
            or successor_task.conversation_id != source_task.conversation_id
            or confirmation.conversation_id != source_task.conversation_id
            or confirmation.task_id != successor_task.task_id
            or confirmation.role != "user"
            or confirmation.status != "active"
            or confirmation.content != expected
            or confirmation.content_ref is not None
            or successor_task.goal != expected
            or confirmation.message_digest != cls._message_digest(confirmation)
        ):
            raise WorkspaceCodingExplorationProofRejectedError(
                "File-set confirmation is not one exact same-conversation user turn"
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
    def _snapshot_record(
        snapshot: WorkspaceCodingExplorationSnapshot,
    ) -> WorkspaceCodingExplorationSnapshotRecord:
        return WorkspaceCodingExplorationSnapshotRecord(
            snapshot_id=snapshot.snapshot_id,
            source_task_id=snapshot.task_id,
            source_user_message_id=snapshot.user_message_id,
            source_user_message_digest=snapshot.user_message_digest,
            project_path=snapshot.project_path,
            ecosystem=snapshot.ecosystem,
            test_path=snapshot.test_path,
            file_count=len(snapshot.files),
            catalog_digest=snapshot.catalog_digest,
            scanned_file_count=snapshot.scanned_file_count,
            scanned_byte_count=snapshot.scanned_byte_count,
            truncated=snapshot.truncated,
            manifest=snapshot.model_dump(mode="json"),
            snapshot_digest=snapshot.snapshot_digest,
            created_at=snapshot.created_at,
        )

    @staticmethod
    def _proposal_record(
        proposal: WorkspaceCodingExplorationProposal,
    ) -> WorkspaceCodingExplorationProposalRecord:
        agent = proposal.explorer_agent
        return WorkspaceCodingExplorationProposalRecord(
            proposal_id=proposal.proposal_id,
            snapshot_id=proposal.snapshot_id,
            snapshot_digest=proposal.snapshot_digest,
            explorer_agent_id=agent.agent_id,
            explorer_agent_version=agent.version,
            explorer_agent_contract_digest=agent.contract_digest,
            explorer_prompt_package_digest=agent.prompt_package_digest,
            candidate_count=len(proposal.decision.files),
            manifest=proposal.model_dump(mode="json"),
            proposal_digest=proposal.proposal_digest,
            created_at=proposal.created_at,
        )

    @staticmethod
    def proposal_record(
        proposal: WorkspaceCodingExplorationProposal,
    ) -> WorkspaceCodingExplorationProposalRecord:
        """Build the normalized persistence row used by the verified Turn reducer."""

        return WorkspaceCodingExplorationBinder._proposal_record(proposal)

    @staticmethod
    def _binding_record(
        binding: WorkspaceCodingFileSetPlanBinding,
    ) -> WorkspaceCodingFileSetPlanBindingRecord:
        plan = binding.expected_plan
        return WorkspaceCodingFileSetPlanBindingRecord(
            binding_id=binding.binding_id,
            proposal_id=binding.proposal_id,
            proposal_digest=binding.proposal_digest,
            successor_task_id=binding.successor_task_id,
            confirmation_message_id=binding.confirmation_message_id,
            confirmation_message_digest=binding.confirmation_message_digest,
            contract_version=binding.task_contract.version,
            contract_digest=binding.task_contract_digest,
            plan_generation=plan.plan_generation,
            plan_id=plan.plan_id,
            plan_manifest_digest=binding.expected_plan_manifest_digest,
            file_count=len(binding.mappings),
            mappings_digest=binding.mappings_digest,
            manifest=binding.model_dump(mode="json"),
            binding_digest=binding.binding_digest,
            created_at=binding.created_at,
        )

    @classmethod
    def _snapshot_from_record(
        cls,
        record: WorkspaceCodingExplorationSnapshotRecord,
    ) -> WorkspaceCodingExplorationSnapshot:
        try:
            snapshot = WorkspaceCodingExplorationSnapshot.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Persisted exploration snapshot is invalid"
            ) from error
        expected = cls._snapshot_record(snapshot)
        for field in (
            "snapshot_id",
            "source_task_id",
            "source_user_message_id",
            "source_user_message_digest",
            "project_path",
            "ecosystem",
            "test_path",
            "file_count",
            "catalog_digest",
            "scanned_file_count",
            "scanned_byte_count",
            "truncated",
            "snapshot_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Exploration snapshot columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != snapshot.created_at:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Exploration snapshot timestamp changed"
            )
        return snapshot

    @classmethod
    def _proposal_from_record(
        cls,
        record: WorkspaceCodingExplorationProposalRecord,
    ) -> WorkspaceCodingExplorationProposal:
        try:
            proposal = WorkspaceCodingExplorationProposal.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Persisted Explorer proposal is invalid"
            ) from error
        expected = cls._proposal_record(proposal)
        for field in (
            "proposal_id",
            "snapshot_id",
            "snapshot_digest",
            "explorer_agent_id",
            "explorer_agent_version",
            "explorer_agent_contract_digest",
            "explorer_prompt_package_digest",
            "candidate_count",
            "proposal_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingExplorationProofRejectedError(
                    "Explorer proposal columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != proposal.created_at:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Explorer proposal timestamp changed"
            )
        return proposal

    @classmethod
    def _binding_from_record(
        cls,
        record: WorkspaceCodingFileSetPlanBindingRecord,
    ) -> WorkspaceCodingFileSetPlanBinding:
        try:
            binding = WorkspaceCodingFileSetPlanBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise WorkspaceCodingExplorationProofRejectedError(
                "Persisted file-set Plan binding is invalid"
            ) from error
        expected = cls._binding_record(binding)
        for field in (
            "binding_id",
            "proposal_id",
            "proposal_digest",
            "successor_task_id",
            "confirmation_message_id",
            "confirmation_message_digest",
            "contract_version",
            "contract_digest",
            "plan_generation",
            "plan_id",
            "plan_manifest_digest",
            "file_count",
            "mappings_digest",
            "binding_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise WorkspaceCodingExplorationProofRejectedError(
                    "File-set Plan columns diverged from its manifest"
                )
        if cls._aware(record.created_at) != binding.created_at:
            raise WorkspaceCodingExplorationProofRejectedError(
                "File-set Plan binding timestamp changed"
            )
        return binding

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Workspace exploration clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "WorkspaceCodingExplorationBinder",
    "WorkspaceCodingExplorationBindingError",
    "WorkspaceCodingExplorationConflictError",
    "WorkspaceCodingExplorationNotFoundError",
    "WorkspaceCodingExplorationProofRejectedError",
]
