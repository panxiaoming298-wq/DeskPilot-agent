"""Server adjudication and proof checks for bounded dynamic Agent child DAGs."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_model_loop import DecisionReducer
from deskpilot.application.agent_registry import AgentRegistration, AgentRegistry
from deskpilot.application.capability_catalog import CapabilityCatalog, CapabilityCatalogError
from deskpilot.application.verified_edges import unlock_if_ready
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.agent_loop import AgentProposeTaskGraphDecision
from deskpilot.domain.agent_replanning import (
    CROSS_GENERATION_BUDGET_CONSTRAINT,
    AgentReplanRead,
    AgentReplanRepairAdvice,
    AgentReplanResultSource,
)
from deskpilot.domain.agent_runtime import (
    AgentOutputResult,
    AgentTaskGraphApprovalBinding,
    AgentTaskGraphCapabilityInput,
    AgentTaskGraphConditionDecision,
    AgentTaskGraphManifest,
    AgentTaskGraphNodeRead,
    AgentTaskGraphRead,
    AgentTaskGraphResultRef,
    BoundAgentTaskGraphCondition,
    BoundAgentTaskGraphNode,
    ClaimedInvocation,
    ExecutionNodeStatus,
    ExecutionRunStatus,
    InvocationExecutionStatus,
    InvocationVerificationStatus,
)
from deskpilot.domain.task_plans import CapabilityRef, PlanNodeBudget, TaskContract
from deskpilot.domain.task_workbench import TurnRouteStatus
from deskpilot.domain.workspace_files import (
    WorkspaceDirectoryRead,
    WorkspaceFileRead,
    WorkspaceNodeTestRead,
    WorkspacePatchPreview,
    WorkspacePatchTestRead,
    WorkspacePythonTestRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInputRequestRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentObservationRecord,
    AgentReplanRecord,
    AgentResultRecord,
    AgentTaskGraphNodeRecord,
    AgentTaskGraphRecord,
    TaskContractVersionRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskPlanGenerationRecord,
    TurnRouteRecord,
    WorkspaceAgentResultRecord,
)


class AgentSupervisorError(RuntimeError):
    code = "AGENT_SUPERVISOR_ERROR"


class AgentTaskGraphRejectedError(AgentSupervisorError):
    code = "AGENT_TASK_GRAPH_REJECTED"


class AgentTaskGraphProofRejectedError(AgentSupervisorError):
    code = "AGENT_TASK_GRAPH_PROOF_REJECTED"


@dataclass(frozen=True)
class TaskGraphCapabilityOffer:
    capability_id: str
    budget: PlanNodeBudget
    input_sources: tuple[str, ...]
    input_bindings: tuple[AgentTaskGraphCapabilityInput, ...] = ()


@dataclass(frozen=True)
class TaskGraphOffer:
    context_refs: tuple[str, ...]
    capabilities: tuple[TaskGraphCapabilityOffer, ...]
    max_nodes: int
    repair_advice: AgentReplanRepairAdvice | None = None


WorkspaceAgentResult = (
    WorkspaceFileRead
    | WorkspaceDirectoryRead
    | WorkspacePythonTestRead
    | WorkspaceNodeTestRead
    | WorkspacePatchTestRead
)


class AgentSupervisorRuntime:
    """Bind one complete untrusted graph proposal to exact Registry and Plan facts."""

    def __init__(
        self,
        database: Database,
        agents: AgentRegistry,
        capabilities: CapabilityCatalog,
    ) -> None:
        self._database = database
        self._agents = agents
        self._capabilities = capabilities

    async def offer(self, claimed: ClaimedInvocation, privacy_mode: str) -> TaskGraphOffer:
        async with self._database.session() as session:
            run, parent, _invocation, contract = await self._context(session, claimed)
            route = await self._route(session, claimed.handoff.task_id)
            repair_advice = await self._verified_replan_advice(session, run)
            parent_registration = self._parent_registration(claimed)
            targets = self._allowed_targets(parent_registration, contract, privacy_mode)
            offered_targets = tuple(
                (
                    capability,
                    registration,
                    self._input_sources(capability, route),
                    self._input_bindings(capability, route),
                )
                for capability, registration in targets
                if self._input_sources(capability, route)
            )
            existing_nodes = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskExecutionNodeRecord)
                    .where(TaskExecutionNodeRecord.run_id == run.run_id)
                )
                or 0
            )
            max_nodes = min(
                parent_registration.contract.handoff_policy.max_outgoing_handoffs,
                claimed.handoff.budget_allocation.handoffs,
                contract.budget.max_handoffs,
                contract.budget.max_plan_nodes - existing_nodes,
            )
            if parent.handoff_parent_node_id is not None:
                max_nodes = 0
            if max_nodes < 1 or not offered_targets:
                raise AgentTaskGraphRejectedError(
                    "No bounded dynamic child graph is authorized for this invocation"
                )
            return TaskGraphOffer(
                context_refs=(
                    f"turn-route://{claimed.handoff.task_id}/parameters",
                    f"task-contract://{contract.contract_id}/constraints",
                )
                + (
                    (f"agent-replan://{run.run_id}/repair-advice",)
                    if repair_advice is not None
                    else ()
                ),
                capabilities=tuple(
                    TaskGraphCapabilityOffer(
                        capability_id=capability.capability_id,
                        budget=self._agent_budget(registration),
                        input_sources=input_sources,
                        input_bindings=input_bindings,
                    )
                    for capability, registration, input_sources, input_bindings in offered_targets
                ),
                max_nodes=max_nodes,
                repair_advice=repair_advice,
            )

    async def validate(
        self,
        claimed: ClaimedInvocation,
        decision: AgentProposeTaskGraphDecision,
        privacy_mode: str,
    ) -> None:
        async with self._database.session() as session:
            await self._bound_graph(
                session,
                claimed,
                decision,
                privacy_mode,
                decision_id=f"agd_{sha256_digest({'turn_id': 'validation-only'})}",
                binding_id=f"tgb_{'0' * 64}",
                persist=False,
            )

    def seal_graph_reducer(
        self,
        claimed: ClaimedInvocation,
        decision: AgentProposeTaskGraphDecision,
        privacy_mode: str,
        binding_id: str,
    ) -> DecisionReducer:
        async def reduce(
            session: AsyncSession,
            record: AgentDecisionRecord,
            _turn: AgentModelTurnRecord,
            now: datetime,
        ) -> None:
            if record.binding_id != binding_id or record.manifest != decision.model_dump(
                mode="json"
            ):
                raise AgentTaskGraphRejectedError("Task graph decision binding changed")
            manifest = await self._bound_graph(
                session,
                claimed,
                decision,
                privacy_mode,
                decision_id=record.decision_id,
                binding_id=binding_id,
                persist=True,
                now=now,
            )
            if not manifest.nodes:
                raise AgentTaskGraphRejectedError("Task graph has no bound nodes")

        return reduce

    async def verified_observation(
        self, claimed: ClaimedInvocation
    ) -> tuple[AgentTaskGraphRecord, AgentObservationRecord]:
        async with self._database.session() as session:
            graph = await session.scalar(
                select(AgentTaskGraphRecord).where(
                    AgentTaskGraphRecord.parent_invocation_id == claimed.invocation.invocation_id,
                    AgentTaskGraphRecord.status == "verified",
                )
            )
            if graph is None or graph.observation_id is None:
                raise AgentTaskGraphProofRejectedError("Verified task graph is missing")
            observation = await session.get(AgentObservationRecord, graph.observation_id)
            if observation is None:
                raise AgentTaskGraphProofRejectedError("Verified task graph observation is missing")
            await self._assert_verified_observation(session, graph, observation)
            return graph, observation

    @classmethod
    async def collect_verified_replan_sources(
        cls,
        session: AsyncSession,
        source_run: TaskExecutionRunRecord,
        *,
        exclude_result_kinds: frozenset[str] = frozenset(),
    ) -> tuple[AgentReplanResultSource, ...]:
        """Recompute reusable verified child results from one terminal source Run."""

        graphs = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphRecord)
                    .where(AgentTaskGraphRecord.run_id == source_run.run_id)
                    .order_by(AgentTaskGraphRecord.created_at, AgentTaskGraphRecord.graph_id)
                )
            ).all()
        )
        sources: list[AgentReplanResultSource] = []
        for graph in graphs:
            manifest = cls._manifest(graph)
            persisted_nodes = {
                item.local_key: item
                for item in (
                    await session.scalars(
                        select(AgentTaskGraphNodeRecord)
                        .where(AgentTaskGraphNodeRecord.graph_id == graph.graph_id)
                        .order_by(AgentTaskGraphNodeRecord.local_key)
                    )
                ).all()
            }
            for bound in manifest.nodes:
                graph_node = persisted_nodes.get(bound.local_key)
                if graph_node is None or graph_node.status not in {
                    "child_verified",
                    "consumed",
                }:
                    continue
                result_ref, _workspace_result = await cls._verified_result_ref(
                    session,
                    graph,
                    bound,
                    graph_node,
                    require_persisted=True,
                )
                if result_ref.result_kind in exclude_result_kinds:
                    continue
                source_key = (
                    "replan_result_"
                    + sha256_digest(
                        {
                            "source_run_id": source_run.run_id,
                            "result_ref_digest": result_ref.result_ref_digest,
                        }
                    )[:32]
                )
                material = {
                    "schema_version": "deskpilot.agent-replan-result-source.v1",
                    "source_key": source_key,
                    "source_run_id": source_run.run_id,
                    "source_plan_generation": source_run.plan_generation,
                    "source_plan_digest": source_run.plan_digest,
                    "source_graph_digest": graph.graph_digest,
                    "result_ref": result_ref,
                }
                sources.append(
                    AgentReplanResultSource.model_validate(
                        {**material, "source_digest": sha256_digest(material)}
                    )
                )
        return tuple(sources[:7])

    @classmethod
    async def collect_failed_condition_decisions(
        cls,
        session: AsyncSession,
        source_run: TaskExecutionRunRecord,
    ) -> tuple[AgentTaskGraphConditionDecision, ...]:
        """Recompute every false server condition that terminally failed one Run."""

        graphs = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphRecord)
                    .where(AgentTaskGraphRecord.run_id == source_run.run_id)
                    .order_by(AgentTaskGraphRecord.created_at, AgentTaskGraphRecord.graph_id)
                )
            ).all()
        )
        failed: list[AgentTaskGraphConditionDecision] = []
        for graph in graphs:
            manifest = cls._manifest(graph)
            if manifest.schema_version not in {
                "deskpilot.agent-task-graph.v7",
                "deskpilot.agent-task-graph.v8",
            }:
                continue
            graph_nodes = {
                item.local_key: item
                for item in (
                    await session.scalars(
                        select(AgentTaskGraphNodeRecord)
                        .where(AgentTaskGraphNodeRecord.graph_id == graph.graph_id)
                        .order_by(AgentTaskGraphNodeRecord.local_key)
                    )
                ).all()
            }
            by_node_id = {item.runtime_node_id: item for item in manifest.nodes}
            for target in manifest.nodes:
                decisions = await cls._condition_decisions(
                    session,
                    graph,
                    target,
                    require_matched=False,
                )
                for decision in decisions:
                    if decision.matched:
                        continue
                    source = by_node_id.get(decision.source_node_id)
                    source_record = graph_nodes.get(decision.source_local_key)
                    if source is None or source_record is None:
                        raise AgentTaskGraphProofRejectedError(
                            "Failed graph condition source is missing"
                        )
                    result_ref, _workspace_result = await cls._verified_result_ref(
                        session,
                        graph,
                        source,
                        source_record,
                        require_persisted=True,
                    )
                    if result_ref.result_ref_digest != decision.result_ref_digest:
                        raise AgentTaskGraphProofRejectedError(
                            "Failed graph condition ResultRef changed"
                        )
                    failed.append(decision)
        return tuple(failed)

    @classmethod
    async def _verified_replan_advice(
        cls,
        session: AsyncSession,
        target_run: TaskExecutionRunRecord,
    ) -> AgentReplanRepairAdvice | None:
        record = await session.scalar(
            select(AgentReplanRecord).where(AgentReplanRecord.target_run_id == target_run.run_id)
        )
        if record is None:
            return None
        try:
            replan = AgentReplanRead.model_validate(record.manifest)
        except ValidationError as error:
            raise AgentTaskGraphProofRejectedError(
                "Agent replan import proof is invalid"
            ) from error
        source_run = await session.get(TaskExecutionRunRecord, replan.source_run_id)
        source_plan = await session.get(
            TaskPlanGenerationRecord,
            (replan.task_id, replan.source_plan_generation),
        )
        target_plan = await session.get(
            TaskPlanGenerationRecord,
            (replan.task_id, replan.target_plan_generation),
        )
        contract = await session.get(
            TaskContractVersionRecord,
            (replan.task_id, replan.contract_version),
        )
        route = await session.get(TurnRouteRecord, replan.task_id)
        if (
            replan.schema_version
            not in {
                "deskpilot.agent-replan.v2",
                "deskpilot.agent-replan.v3",
                "deskpilot.agent-replan.v4",
                "deskpilot.agent-replan.v5",
            }
            or replan.repair_advice is None
            or record.replan_id != replan.replan_id
            or record.task_id != replan.task_id
            or record.source_run_id != replan.source_run_id
            or record.target_run_id != replan.target_run_id
            or record.replan_digest != replan.replan_digest
            or source_run is None
            or source_run.status != ExecutionRunStatus.FAILED.value
            or source_run.plan_generation != replan.source_plan_generation
            or source_run.plan_digest != replan.source_plan_digest
            or target_run.task_id != replan.task_id
            or target_run.plan_generation != replan.target_plan_generation
            or target_run.plan_digest != replan.target_plan_digest
            or source_plan is None
            or source_plan.status != "superseded"
            or source_plan.plan_manifest_digest != replan.source_plan_digest
            or target_plan is None
            or target_plan.plan_manifest_digest != replan.target_plan_digest
            or contract is None
            or contract.contract_digest != replan.contract_digest
            or route is None
            or route.parameter_digest != replan.failure_snapshot.route_parameter_digest
        ):
            raise AgentTaskGraphProofRejectedError("Agent replan import lineage changed")
        verified_sources = await cls.collect_verified_replan_sources(
            session,
            source_run,
            exclude_result_kinds=(
                frozenset({"patch_test"})
                if replan.failure_snapshot.stable_error_code
                == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                else frozenset()
            ),
        )
        if replan.repair_advice.result_sources != verified_sources:
            raise AgentTaskGraphProofRejectedError("Agent replan ResultRef offer changed")
        return replan.repair_advice

    @classmethod
    async def verified_upstream_result_refs(
        cls,
        session: AsyncSession,
        graph: AgentTaskGraphRecord,
        graph_node: AgentTaskGraphNodeRecord,
    ) -> tuple[AgentTaskGraphResultRef, ...]:
        """Recompute exact dependency ResultRefs before a child Handoff is sealed."""

        manifest = cls._manifest(graph)
        if manifest.schema_version == "deskpilot.agent-task-graph.v1":
            return ()
        bound = next(
            (item for item in manifest.nodes if item.local_key == graph_node.local_key),
            None,
        )
        if (
            bound is None
            or graph_node.child_node_id != bound.runtime_node_id
            or graph_node.binding_id != bound.binding_id
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic graph target child binding changed")
        refs: list[AgentTaskGraphResultRef] = []
        if bound.import_sources or bound.imported_result_refs:
            target_run = await session.get(TaskExecutionRunRecord, graph.run_id)
            if target_run is None:
                raise AgentTaskGraphProofRejectedError("Imported ResultRef target Run is missing")
            advice = await cls._verified_replan_advice(session, target_run)
            offered = (
                {item.source_key: item.result_ref for item in advice.result_sources}
                if advice is not None
                else {}
            )
            for source_key, supplied in zip(
                bound.import_sources,
                bound.imported_result_refs,
                strict=True,
            ):
                if offered.get(source_key) != supplied:
                    raise AgentTaskGraphProofRejectedError(
                        "Dynamic graph imported an unauthorized ResultRef source"
                    )
                refs.append(supplied)
        for dependency_key in bound.depends_on:
            dependency_bound = next(
                item for item in manifest.nodes if item.local_key == dependency_key
            )
            dependency = await session.get(
                AgentTaskGraphNodeRecord, (graph.graph_id, dependency_key)
            )
            if dependency is None or dependency.status not in {
                "child_verified",
                "consumed",
            }:
                raise AgentTaskGraphProofRejectedError("Dynamic graph dependency is not verified")
            result_ref, _workspace_result = await cls._verified_result_ref(
                session,
                graph,
                dependency_bound,
                dependency,
                require_persisted=True,
            )
            refs.append(result_ref)
        if len(refs) != len({item.result_ref_digest for item in refs}):
            raise AgentTaskGraphProofRejectedError("Dynamic graph ResultRef inputs are duplicated")
        return tuple(refs)

    async def resolve_result_inputs(
        self,
        refs: tuple[AgentTaskGraphResultRef, ...],
        *,
        target_run_id: str,
    ) -> tuple[tuple[AgentTaskGraphResultRef, WorkspaceAgentResult], ...]:
        """Resolve Handoff ResultRefs only after rechecking their persisted proofs."""

        resolved: list[tuple[AgentTaskGraphResultRef, WorkspaceAgentResult]] = []
        async with self._database.session() as session:
            target_run = await session.get(TaskExecutionRunRecord, target_run_id)
            if target_run is None:
                raise AgentTaskGraphProofRejectedError("ResultRef target Run is missing")
            advice = await self._verified_replan_advice(session, target_run)
            imported = (
                {item.result_ref.result_ref_digest for item in advice.result_sources}
                if advice is not None
                else set()
            )
            for supplied in refs:
                graph = await session.get(AgentTaskGraphRecord, supplied.graph_id)
                same_generation = bool(
                    graph is not None
                    and graph.run_id == target_run_id
                    and graph.status in {"running", "verified"}
                )
                imported_from_replan = bool(
                    graph is not None
                    and graph.status == "failed"
                    and supplied.result_ref_digest in imported
                )
                if not same_generation and not imported_from_replan:
                    raise AgentTaskGraphProofRejectedError(
                        "Dynamic graph ResultRef source is unavailable"
                    )
                assert graph is not None
                manifest = self._manifest(graph)
                bound = next(
                    (
                        item
                        for item in manifest.nodes
                        if item.local_key == supplied.producer_local_key
                    ),
                    None,
                )
                graph_node = await session.get(
                    AgentTaskGraphNodeRecord,
                    (graph.graph_id, supplied.producer_local_key),
                )
                if bound is None or graph_node is None:
                    raise AgentTaskGraphProofRejectedError(
                        "Dynamic graph ResultRef source binding is missing"
                    )
                verified, workspace_result = await self._verified_result_ref(
                    session,
                    graph,
                    bound,
                    graph_node,
                    require_persisted=True,
                )
                if supplied != verified:
                    raise AgentTaskGraphProofRejectedError(
                        "Dynamic graph Handoff ResultRef changed"
                    )
                resolved.append((verified, workspace_result))
        return tuple(resolved)

    @classmethod
    def verified_capability_input(
        cls,
        graph: AgentTaskGraphRecord,
        graph_node: AgentTaskGraphNodeRecord,
    ) -> AgentTaskGraphCapabilityInput | None:
        """Return a node's immutable server-bound input after persistence checks."""

        manifest = cls._manifest(graph)
        if manifest.schema_version in {
            "deskpilot.agent-task-graph.v1",
            "deskpilot.agent-task-graph.v2",
        }:
            return None
        bound = next(
            (item for item in manifest.nodes if item.local_key == graph_node.local_key),
            None,
        )
        try:
            persisted = AgentTaskGraphCapabilityInput.model_validate(graph_node.input_manifest)
        except ValidationError as error:
            raise AgentTaskGraphProofRejectedError(
                "Dynamic graph capability input is invalid"
            ) from error
        if (
            bound is None
            or bound.capability_input is None
            or persisted != bound.capability_input
            or graph_node.input_digest != persisted.input_digest
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic graph capability input binding changed")
        return persisted

    @staticmethod
    def verified_patch_approval(
        graph: AgentTaskGraphRecord,
        bound: BoundAgentTaskGraphNode,
        graph_node: AgentTaskGraphNodeRecord,
    ) -> WorkspacePatchPreview | None:
        """Recheck the optional per-node approval proof without granting authority."""

        is_patch = bound.capability.capability_id == "workspace.patch.propose.v1"
        manifest = AgentSupervisorRuntime._manifest(graph)
        if not is_patch:
            if graph_node.approval_manifest is not None or graph_node.approval_digest is not None:
                raise AgentTaskGraphProofRejectedError(
                    "Non-Patch graph node contains an approval proof"
            )
            return None
        if manifest.schema_version == "deskpilot.agent-task-graph.v8" and (
            bound.approval_binding is None
            or bound.capability_input is None
            or bound.approval_binding.graph_id != graph.graph_id
            or bound.approval_binding.local_key != bound.local_key
            or bound.approval_binding.node_id != bound.runtime_node_id
            or bound.approval_binding.capability_input_digest
            != bound.capability_input.input_digest
        ):
            raise AgentTaskGraphProofRejectedError(
                "Composable Patch approval slot binding changed"
            )
        if graph_node.approval_manifest is None and graph_node.approval_digest is None:
            return None
        if graph_node.approval_manifest is None or graph_node.approval_digest is None:
            raise AgentTaskGraphProofRejectedError("Patch approval proof is incomplete")
        try:
            approval = WorkspacePatchPreview.model_validate(graph_node.approval_manifest)
        except ValidationError as error:
            raise AgentTaskGraphProofRejectedError("Patch approval proof is invalid") from error
        capability_input = bound.capability_input
        task_id = manifest.task_id
        if (
            capability_input is None
            or capability_input.read_kind != "patch_test"
            or capability_input.target_path is None
            or approval.task_id != task_id
            or approval.confirmation_digest != graph_node.approval_digest
            or len(approval.changes) != 1
            or approval.changes[0].relative_path != capability_input.target_path
        ):
            raise AgentTaskGraphProofRejectedError("Patch approval binding changed")
        return approval

    async def record_verified_child(
        self,
        session: AsyncSession,
        *,
        child_invocation: AgentInvocationRecord,
        child_result_id: str,
        now: datetime,
    ) -> bool:
        graph_node = await session.scalar(
            select(AgentTaskGraphNodeRecord)
            .where(AgentTaskGraphNodeRecord.child_invocation_id == child_invocation.invocation_id)
            .with_for_update()
        )
        if graph_node is None:
            return False
        graph = await session.scalar(
            select(AgentTaskGraphRecord)
            .where(AgentTaskGraphRecord.graph_id == graph_node.graph_id)
            .with_for_update()
        )
        if (
            graph is None
            or graph.status != "running"
            or graph_node.status != "waiting_child"
            or graph_node.child_result_id is not None
            or graph_node.result_ref_manifest is not None
            or graph_node.result_ref_digest is not None
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic child graph state changed")
        manifest = self._manifest(graph)
        bound = next(
            (item for item in manifest.nodes if item.local_key == graph_node.local_key),
            None,
        )
        if bound is None:
            raise AgentTaskGraphProofRejectedError("Dynamic child graph binding is missing")
        self.verified_capability_input(graph, graph_node)
        if (
            bound.capability.capability_id == "workspace.patch.propose.v1"
            and self.verified_patch_approval(graph, bound, graph_node) is None
        ):
            raise AgentTaskGraphProofRejectedError("Verified Patch child has no approval proof")
        result_ref, workspace_result = await self._verified_result_ref(
            session,
            graph,
            bound,
            graph_node,
            child_invocation=child_invocation,
            child_result_id=child_result_id,
            require_persisted=False,
        )
        graph_node.status = "child_verified"
        graph_node.child_result_id = child_result_id
        graph_node.result_ref_manifest = result_ref.model_dump(mode="json")
        graph_node.result_ref_digest = result_ref.result_ref_digest
        graph_node.updated_at = now
        await session.flush()
        if not await self._adjudicate_outgoing_conditions(
            session,
            graph=graph,
            manifest=manifest,
            source_bound=bound,
            result_ref=result_ref,
            workspace_result=workspace_result,
            now=now,
        ):
            await self._fail_condition_graph(session, graph=graph, now=now)
            return True
        children = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphNodeRecord)
                    .where(AgentTaskGraphNodeRecord.graph_id == graph.graph_id)
                    .order_by(AgentTaskGraphNodeRecord.local_key)
                )
            ).all()
        )
        if any(item.status != "child_verified" for item in children):
            return True
        by_key = {item.local_key: item for item in children}
        child_projection: list[dict[str, object]] = []
        for bound in manifest.nodes:
            child = by_key.get(bound.local_key)
            if child is None or child.child_invocation_id is None or child.child_result_id is None:
                raise AgentTaskGraphProofRejectedError(
                    "Verified dynamic graph child proof is incomplete"
                )
            verified_ref, _workspace_result = await self._verified_result_ref(
                session, graph, bound, child, require_persisted=True
            )
            child_projection.append(
                {
                    "local_key": bound.local_key,
                    "result_ref": verified_ref.model_dump(mode="json"),
                }
            )
        observation_id = f"obs_{sha256_digest({'decision_id': graph.decision_id})}"
        material = {
            "observation_id": observation_id,
            "invocation_id": graph.parent_invocation_id,
            "decision_id": graph.decision_id,
            "source_kind": "handoff",
            "binding_id": graph.binding_id,
            "status": "succeeded",
            "result_ref": f"agent-task-graph:{graph.graph_id}",
            "projection": {
                "graph_id": graph.graph_id,
                "graph_digest": graph.graph_digest,
                "children": child_projection,
            },
        }
        session.add(
            AgentObservationRecord(
                **material,
                observation_digest=sha256_digest(material),
                created_at=now,
            )
        )
        graph.status = "verified"
        graph.observation_id = observation_id
        graph.updated_at = now
        parent_invocation = await session.get(AgentInvocationRecord, graph.parent_invocation_id)
        parent_node = await session.get(TaskExecutionNodeRecord, graph.parent_node_id)
        run = await session.get(TaskExecutionRunRecord, graph.run_id)
        if (
            parent_invocation is None
            or parent_invocation.execution_status
            != InvocationExecutionStatus.WAITING_CHILDREN.value
            or parent_node is None
            or parent_node.status != ExecutionNodeStatus.WAITING_CHILDREN.value
            or run is None
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic graph has no waiting parent invocation")
        parent_node.status = ExecutionNodeStatus.READY.value
        parent_node.revision += 1
        parent_node.updated_at = now
        run.status = ExecutionRunStatus.ACTIVE.value
        run.revision += 1
        run.updated_at = now
        return True

    @staticmethod
    async def _adjudicate_outgoing_conditions(
        session: AsyncSession,
        *,
        graph: AgentTaskGraphRecord,
        manifest: AgentTaskGraphManifest,
        source_bound: BoundAgentTaskGraphNode,
        result_ref: AgentTaskGraphResultRef,
        workspace_result: WorkspaceAgentResult,
        now: datetime,
    ) -> bool:
        edges = tuple(
            (
                await session.scalars(
                    select(TaskExecutionEdgeRecord)
                    .where(
                        TaskExecutionEdgeRecord.run_id == graph.run_id,
                        TaskExecutionEdgeRecord.from_node_id
                        == source_bound.runtime_node_id,
                        TaskExecutionEdgeRecord.requirement == "server_condition",
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not edges:
            return True
        if not isinstance(
            workspace_result,
            (WorkspacePythonTestRead, WorkspaceNodeTestRead, WorkspacePatchTestRead),
        ):
            raise AgentTaskGraphProofRejectedError(
                "Conditional graph edge source is not a fixed test result"
            )
        run = await session.get(TaskExecutionRunRecord, graph.run_id)
        if run is None:
            raise AgentTaskGraphProofRejectedError("Conditional graph run is missing")
        all_matched = True
        for edge in edges:
            target_bound = next(
                (item for item in manifest.nodes if item.runtime_node_id == edge.to_node_id),
                None,
            )
            condition = (
                next(
                    (
                        item
                        for item in target_bound.conditions
                        if item.source_node_id == source_bound.runtime_node_id
                    ),
                    None,
                )
                if target_bound is not None
                else None
            )
            try:
                persisted_condition = BoundAgentTaskGraphCondition.model_validate(
                    edge.condition_manifest
                )
            except ValidationError as error:
                raise AgentTaskGraphProofRejectedError(
                    "Conditional graph edge binding is invalid"
                ) from error
            if (
                target_bound is None
                or condition is None
                or persisted_condition != condition
                or edge.condition_digest != condition.condition_digest
                or edge.decision_manifest is not None
                or edge.decision_digest is not None
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Conditional graph edge binding changed"
                )
            actual_status = workspace_result.status
            decision_material = {
                "schema_version": "deskpilot.agent-task-graph-condition-decision.v1",
                "graph_id": graph.graph_id,
                "source_local_key": source_bound.local_key,
                "source_node_id": source_bound.runtime_node_id,
                "target_local_key": target_bound.local_key,
                "target_node_id": target_bound.runtime_node_id,
                "predicate": condition.predicate,
                "actual_status": actual_status,
                "result_ref_digest": result_ref.result_ref_digest,
                "matched": actual_status in {"passed", "verified"},
            }
            decision = AgentTaskGraphConditionDecision.model_validate(
                {
                    **decision_material,
                    "decision_digest": sha256_digest(decision_material),
                }
            )
            edge.decision_manifest = decision.model_dump(mode="json")
            edge.decision_digest = decision.decision_digest
            if decision.matched:
                target = await session.get(TaskExecutionNodeRecord, edge.to_node_id)
                if target is None:
                    raise AgentTaskGraphProofRejectedError(
                        "Conditional graph target node is missing"
                    )
                await unlock_if_ready(session, run, target)
            else:
                all_matched = False
        return all_matched

    @staticmethod
    async def _fail_condition_graph(
        session: AsyncSession,
        *,
        graph: AgentTaskGraphRecord,
        now: datetime,
    ) -> None:
        graph.status = "failed"
        graph.updated_at = now
        graph_nodes = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphNodeRecord).where(
                        AgentTaskGraphNodeRecord.graph_id == graph.graph_id,
                        AgentTaskGraphNodeRecord.status == "waiting_child",
                    )
                )
            ).all()
        )
        for graph_node in graph_nodes:
            graph_node.status = "cancelled"
            graph_node.updated_at = now
            node = await session.get(TaskExecutionNodeRecord, graph_node.child_node_id)
            if node is not None and node.status not in {
                ExecutionNodeStatus.VERIFIED.value,
                ExecutionNodeStatus.FAILED.value,
                ExecutionNodeStatus.CANCELLED.value,
            }:
                node.status = ExecutionNodeStatus.CANCELLED.value
                node.claim_fencing_token += 1
                AgentSupervisorRuntime._clear_claim(node)
                node.revision += 1
                node.updated_at = now
        parent = await session.get(TaskExecutionNodeRecord, graph.parent_node_id)
        parent_invocation = await session.get(
            AgentInvocationRecord, graph.parent_invocation_id
        )
        run = await session.get(TaskExecutionRunRecord, graph.run_id)
        if parent is None or parent_invocation is None or run is None:
            raise AgentTaskGraphProofRejectedError(
                "Conditional graph parent state is missing"
            )
        route = await session.get(TurnRouteRecord, run.task_id)
        parent.status = ExecutionNodeStatus.FAILED.value
        AgentSupervisorRuntime._clear_claim(parent)
        parent.revision += 1
        parent.updated_at = now
        parent_invocation.execution_status = InvocationExecutionStatus.FAILED_TERMINAL.value
        parent_invocation.finished_at = now
        parent_invocation.revision += 1
        run.status = ExecutionRunStatus.FAILED.value
        run.revision += 1
        run.updated_at = now
        if route is not None:
            route.status = TurnRouteStatus.FAILED.value
            route.error_code = "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
            route.revision += 1
            route.updated_at = now

    async def consume_graph(
        self,
        session: AsyncSession,
        *,
        graph_id: str,
        parent_invocation_id: str,
        now: datetime,
    ) -> tuple[AgentTaskGraphManifest, WorkspaceAgentResult]:
        graph = await session.scalar(
            select(AgentTaskGraphRecord)
            .where(AgentTaskGraphRecord.graph_id == graph_id)
            .with_for_update()
        )
        if (
            graph is None
            or graph.parent_invocation_id != parent_invocation_id
            or graph.status != "verified"
            or graph.observation_id is None
        ):
            raise AgentTaskGraphProofRejectedError("Verified dynamic graph cannot be consumed")
        observation = await session.get(AgentObservationRecord, graph.observation_id)
        if observation is None:
            raise AgentTaskGraphProofRejectedError("Verified dynamic graph observation is missing")
        await self._assert_verified_observation(session, graph, observation)
        children = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphNodeRecord)
                    .where(AgentTaskGraphNodeRecord.graph_id == graph_id)
                    .with_for_update()
                )
            ).all()
        )
        if any(item.status != "child_verified" for item in children):
            raise AgentTaskGraphProofRejectedError("Dynamic graph children are not verified")
        graph.status = "consumed"
        graph.updated_at = now
        for child in children:
            child.status = "consumed"
            child.updated_at = now
        manifest = self._manifest(graph)
        if manifest.output_local_key is None:
            raise AgentTaskGraphProofRejectedError("Dynamic graph has no server-bound output node")
        output_node = next(
            (item for item in children if item.local_key == manifest.output_local_key),
            None,
        )
        output_bound = next(
            (item for item in manifest.nodes if item.local_key == manifest.output_local_key),
            None,
        )
        if output_node is None or output_bound is None:
            raise AgentTaskGraphProofRejectedError("Dynamic graph output node proof is missing")
        _result_ref, output = await self._verified_result_ref(
            session, graph, output_bound, output_node, require_persisted=True
        )
        return manifest, output

    async def fail_child(
        self,
        session: AsyncSession,
        *,
        run: TaskExecutionRunRecord,
        failed_node: TaskExecutionNodeRecord,
        failed_invocation: AgentInvocationRecord,
        now: datetime,
    ) -> bool:
        if (
            failed_node.run_id != run.run_id
            or failed_invocation.run_id != run.run_id
            or failed_invocation.node_id != failed_node.node_id
            or failed_invocation.attempt != failed_node.attempt_count
        ):
            raise AgentTaskGraphProofRejectedError(
                "Failed dynamic child execution lineage changed"
            )
        graph_node = await session.scalar(
            select(AgentTaskGraphNodeRecord)
            .where(
                AgentTaskGraphNodeRecord.child_invocation_id
                == failed_invocation.invocation_id
            )
            .with_for_update()
        )
        if graph_node is None:
            return False
        graph = await session.scalar(
            select(AgentTaskGraphRecord)
            .where(AgentTaskGraphRecord.graph_id == graph_node.graph_id)
            .with_for_update()
        )
        if graph is None or graph.run_id != run.run_id:
            raise AgentTaskGraphProofRejectedError("Dynamic graph is missing")
        graph.status = "failed"
        graph.updated_at = now
        graph_children = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphNodeRecord)
                    .where(AgentTaskGraphNodeRecord.graph_id == graph.graph_id)
                    .order_by(AgentTaskGraphNodeRecord.child_node_id)
                    .with_for_update()
                )
            ).all()
        )
        for child in graph_children:
            if child.child_node_id == failed_node.node_id:
                child.status = "failed"
                child.updated_at = now
            elif child.status == "waiting_child":
                child.status = "cancelled"
                child.updated_at = now

        failed_node_ids = {failed_node.node_id, graph.parent_node_id}
        unfinished_nodes = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .where(
                        TaskExecutionNodeRecord.run_id == run.run_id,
                        TaskExecutionNodeRecord.status.in_(
                            (
                                ExecutionNodeStatus.PENDING.value,
                                ExecutionNodeStatus.READY.value,
                                ExecutionNodeStatus.CLAIMED.value,
                                ExecutionNodeStatus.RUNNING.value,
                                ExecutionNodeStatus.WAITING_USER.value,
                                ExecutionNodeStatus.WAITING_CHILDREN.value,
                                ExecutionNodeStatus.AWAITING_VERIFICATION.value,
                            )
                        ),
                    )
                    .order_by(TaskExecutionNodeRecord.node_id)
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

        active_invocations = tuple(
            (
                await session.scalars(
                    select(AgentInvocationRecord)
                    .where(
                        AgentInvocationRecord.run_id == run.run_id,
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
                    .order_by(AgentInvocationRecord.invocation_id)
                    .with_for_update()
                )
            ).all()
        )
        for invocation in active_invocations:
            invocation.execution_status = (
                InvocationExecutionStatus.FAILED_TERMINAL.value
                if invocation.node_id in failed_node_ids
                else InvocationExecutionStatus.CANCELLED.value
            )
            if invocation.finished_at is None:
                invocation.finished_at = now
            invocation.revision += 1

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
                        .order_by(AgentInputRequestRecord.input_request_id)
                        .with_for_update()
                    )
                ).all()
            )
            for request in pending_inputs:
                request.status = "cancelled"
                request.resolved_at = now
        run.status = ExecutionRunStatus.FAILED.value
        run.updated_at = now
        return True

    async def _bound_graph(
        self,
        session: AsyncSession,
        claimed: ClaimedInvocation,
        decision: AgentProposeTaskGraphDecision,
        privacy_mode: str,
        *,
        decision_id: str,
        binding_id: str,
        persist: bool,
        now: datetime | None = None,
    ) -> AgentTaskGraphManifest:
        run, parent, invocation, contract = await self._context(session, claimed, lock=persist)
        parent_registration = self._parent_registration(claimed)
        allowed_targets = {
            capability.capability_id: (capability, registration)
            for capability, registration in self._allowed_targets(
                parent_registration, contract, privacy_mode
            )
        }
        route = await self._route(session, claimed.handoff.task_id)
        repair_advice = await self._verified_replan_advice(session, run)
        import_sources = (
            {item.source_key: item.result_ref for item in repair_advice.result_sources}
            if repair_advice is not None
            else {}
        )
        if (
            "server_adjudicated_dynamic_graph_v1" not in contract.constraints
            or parent.handoff_parent_node_id is not None
            or len(decision.nodes)
            > parent_registration.contract.handoff_policy.max_outgoing_handoffs
            or len(decision.nodes) > claimed.handoff.budget_allocation.handoffs
            or len(decision.nodes) > contract.budget.max_handoffs
        ):
            raise AgentTaskGraphRejectedError(
                "Task Contract, depth, or handoff limits rejected the graph"
            )
        proposal_by_key = {item.local_key: item for item in decision.nodes}
        composable_patch_approvals = (
            "composable_patch_approval_nodes_v1" in contract.constraints
        )
        patch_proposals = tuple(
            item
            for item in decision.nodes
            if item.target_capability_id == "workspace.patch.propose.v1"
        )
        if composable_patch_approvals:
            expected_patch_bindings = {
                item[0] for item in self._patch_binding_specs(route)
            }
            proposed_patch_bindings = {
                item.input_binding_key
                for item in patch_proposals
                if item.input_binding_key is not None
            }
            if (
                not expected_patch_bindings
                or proposed_patch_bindings != expected_patch_bindings
                or len(proposed_patch_bindings) != len(patch_proposals)
                or any(
                    item.input_binding_key is not None
                    for item in decision.nodes
                    if item.target_capability_id != "workspace.patch.propose.v1"
                )
            ):
                raise AgentTaskGraphRejectedError(
                    "Dynamic graph must consume every Patch approval binding exactly once"
                )
        elif any(item.input_binding_key is not None for item in decision.nodes):
            raise AgentTaskGraphRejectedError(
                "Legacy dynamic graph cannot select node input bindings"
            )
        condition_capabilities = {
            "workspace.python.test.v1",
            "workspace.node.test.v1",
            "workspace.patch.propose.v1",
        }
        for proposal in decision.nodes:
            condition_sources = {item.source_local_key for item in proposal.conditions}
            required_sources = {
                source
                for source in proposal.depends_on
                if proposal_by_key[source].target_capability_id in condition_capabilities
            }
            if condition_sources != required_sources or (
                condition_sources
                and "server_adjudicated_test_conditions_v1" not in contract.constraints
            ):
                raise AgentTaskGraphRejectedError(
                    "Dynamic graph test dependencies require server conditions"
                )
        existing_graph = await session.scalar(
            select(AgentTaskGraphRecord).where(
                AgentTaskGraphRecord.parent_invocation_id == invocation.invocation_id
            )
        )
        if existing_graph is not None:
            raise AgentTaskGraphRejectedError("Parent invocation already sealed a dynamic graph")
        node_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.run_id == run.run_id)
            )
            or 0
        )
        if node_count + len(decision.nodes) > contract.budget.max_plan_nodes:
            raise AgentTaskGraphRejectedError("Dynamic graph exceeds the Task node budget")
        offer_refs = {
            f"turn-route://{claimed.handoff.task_id}/parameters",
            f"task-contract://{contract.contract_id}/constraints",
        }
        if repair_advice is not None:
            offer_refs.add(f"agent-replan://{run.run_id}/repair-advice")
        for proposal in decision.nodes:
            target = allowed_targets.get(proposal.target_capability_id)
            if target is None:
                raise AgentTaskGraphRejectedError("Dynamic graph capability is not authorized")
            _capability, registration = target
            self._assert_child_budget(proposal.budget_slice, registration)
            if not set(proposal.context_refs).issubset(offer_refs):
                raise AgentTaskGraphRejectedError(
                    "Dynamic graph requested an unauthorized context reference"
                )
            if not set(proposal.import_sources).issubset(import_sources):
                raise AgentTaskGraphRejectedError(
                    "Dynamic graph requested an unauthorized replan ResultRef source"
                )
            self._capability_input(
                proposal.input_source,
                target[0],
                route,
                input_binding_key=proposal.input_binding_key,
            )
        output_proposal = next(
            item for item in decision.nodes if item.local_key == decision.output_node_key
        )
        if output_proposal.target_capability_id != "workspace.directory.read.v1":
            raise AgentTaskGraphRejectedError(
                "Workspace directory graph output must remain a directory result"
            )
        await self._assert_task_budget(session, run, contract, decision)
        depth = self._graph_depth(decision)
        graph_id = f"atg_{sha256_digest({'decision_id': decision_id})}"
        node_ids = {
            proposal.local_key: (
                f"pnd_{sha256_digest({'graph_id': graph_id, 'local_key': proposal.local_key})}"
            )
            for proposal in decision.nodes
        }
        bound_nodes: list[BoundAgentTaskGraphNode] = []
        for proposal in sorted(decision.nodes, key=lambda item: item.local_key):
            capability, registration = allowed_targets[proposal.target_capability_id]
            capability_input = self._capability_input(
                proposal.input_source,
                capability,
                route,
                input_binding_key=proposal.input_binding_key,
            )
            runtime_local_key = (
                "dynamic_"
                + sha256_digest({"graph_id": graph_id, "local_key": proposal.local_key})[:56]
            )
            conditions: list[BoundAgentTaskGraphCondition] = []
            for condition in proposal.conditions:
                condition_material = {
                    "schema_version": "deskpilot.agent-task-graph-condition.v1",
                    "source_local_key": condition.source_local_key,
                    "source_node_id": node_ids[condition.source_local_key],
                    "predicate": condition.predicate,
                }
                conditions.append(
                    BoundAgentTaskGraphCondition.model_validate(
                        {
                            **condition_material,
                            "condition_digest": sha256_digest(condition_material),
                        }
                    )
                )
            approval_binding: AgentTaskGraphApprovalBinding | None = None
            if composable_patch_approvals and capability.capability_id == (
                "workspace.patch.propose.v1"
            ):
                approval_material = {
                    "schema_version": "deskpilot.agent-task-graph-approval-binding.v1",
                    "approval_binding_id": (
                        "apb_"
                        + sha256_digest(
                            {
                                "graph_id": graph_id,
                                "local_key": proposal.local_key,
                                "capability_input_digest": capability_input.input_digest,
                            }
                        )
                    ),
                    "approval_kind": "workspace_patch",
                    "graph_id": graph_id,
                    "local_key": proposal.local_key,
                    "node_id": node_ids[proposal.local_key],
                    "capability_input_digest": capability_input.input_digest,
                    "confirmation_policy": "fresh_user_confirmation_per_node_v1",
                    "manifest_policy": "content_addressed_workspace_manifest_v1",
                }
                approval_binding = AgentTaskGraphApprovalBinding.model_validate(
                    {
                        **approval_material,
                        "approval_binding_digest": sha256_digest(approval_material),
                    }
                )
            node_material = {
                "local_key": proposal.local_key,
                "runtime_node_id": node_ids[proposal.local_key],
                "runtime_local_key": runtime_local_key,
                "binding_id": (
                    "hbn_" + sha256_digest({"graph_id": graph_id, "local_key": proposal.local_key})
                ),
                "target_agent": BoundAgentRef(
                    agent_id=registration.contract.agent_id,
                    version=registration.contract.version,
                    contract_digest=registration.contract.digest,
                    prompt_package_digest=registration.prompt_package.digest,
                ),
                "capability": capability,
                "objective": proposal.objective,
                "context_refs": proposal.context_refs,
                "capability_input": capability_input,
                "depends_on": proposal.depends_on,
                "depends_on_node_ids": tuple(node_ids[key] for key in proposal.depends_on),
                "conditions": tuple(conditions),
                "import_sources": proposal.import_sources,
                "imported_result_refs": tuple(
                    import_sources[key] for key in proposal.import_sources
                ),
                "approval_binding": approval_binding,
                "budget_allocation": proposal.budget_slice,
            }
            bound_nodes.append(
                BoundAgentTaskGraphNode.model_validate(
                    {
                        **node_material,
                        "node_spec_digest": sha256_digest(node_material),
                    }
                )
            )
        proposal_manifest = decision.model_dump(mode="json")
        graph_material = {
            "schema_version": (
                "deskpilot.agent-task-graph.v8"
                if composable_patch_approvals
                else "deskpilot.agent-task-graph.v7"
            ),
            "graph_id": graph_id,
            "binding_id": binding_id,
            "task_id": run.task_id,
            "run_id": run.run_id,
            "plan_generation": run.plan_generation,
            "plan_digest": run.plan_digest,
            "parent_invocation_id": invocation.invocation_id,
            "parent_node_id": parent.node_id,
            "decision_id": decision_id,
            "proposal_digest": sha256_digest(proposal_manifest),
            "output_local_key": decision.output_node_key,
            "output_node_id": node_ids[decision.output_node_key],
            "nodes": tuple(bound_nodes),
        }
        manifest = AgentTaskGraphManifest.model_validate(
            {**graph_material, "graph_digest": sha256_digest(graph_material)}
        )
        if not persist:
            return manifest
        assert now is not None
        session.add(
            AgentTaskGraphRecord(
                graph_id=manifest.graph_id,
                run_id=run.run_id,
                parent_invocation_id=invocation.invocation_id,
                parent_node_id=parent.node_id,
                decision_id=decision_id,
                binding_id=binding_id,
                status="running",
                manifest=manifest.model_dump(mode="json"),
                graph_digest=manifest.graph_digest,
                node_count=len(manifest.nodes),
                max_depth=depth,
                output_local_key=manifest.output_local_key,
                output_node_id=manifest.output_node_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        for bound in manifest.nodes:
            session.add(
                TaskExecutionNodeRecord(
                    node_id=bound.runtime_node_id,
                    run_id=run.run_id,
                    local_key=bound.runtime_local_key,
                    node_kind="agent",
                    node_spec_digest=bound.node_spec_digest,
                    depends_on=list(bound.depends_on_node_ids),
                    handoff_parent_node_id=parent.node_id,
                    bound_agent=bound.target_agent.model_dump(mode="json"),
                    capability=bound.capability.model_dump(mode="json"),
                    acceptance_refs=[],
                    budget=bound.budget_allocation.model_dump(mode="json"),
                    runtime_enabled=True,
                    status=(
                        ExecutionNodeStatus.READY.value
                        if not bound.depends_on
                        else ExecutionNodeStatus.PENDING.value
                    ),
                    revision=1,
                    attempt_count=0,
                    claim_fencing_token=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        # The graph-node rows reference runtime nodes.  Flush the runtime DAG
        # first so SQLite can enforce the FK even though the ORM models do not
        # carry relationship() objects that would otherwise establish ordering.
        await session.flush()
        for bound in manifest.nodes:
            session.add(
                AgentTaskGraphNodeRecord(
                    graph_id=manifest.graph_id,
                    local_key=bound.local_key,
                    child_node_id=bound.runtime_node_id,
                    binding_id=bound.binding_id,
                    status="waiting_child",
                    budget_allocation=bound.budget_allocation.model_dump(mode="json"),
                    input_manifest=(
                        bound.capability_input.model_dump(mode="json")
                        if bound.capability_input is not None
                        else None
                    ),
                    input_digest=(
                        bound.capability_input.input_digest
                        if bound.capability_input is not None
                        else None
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        for bound in manifest.nodes:
            conditions_by_node_id = {
                item.source_node_id: item for item in bound.conditions
            }
            for source in bound.depends_on_node_ids:
                edge_condition = conditions_by_node_id.get(source)
                session.add(
                    TaskExecutionEdgeRecord(
                        run_id=run.run_id,
                        from_node_id=source,
                        to_node_id=bound.runtime_node_id,
                        requirement=(
                            "server_condition"
                            if edge_condition is not None
                            else "verified"
                        ),
                        condition_manifest=(
                            edge_condition.model_dump(mode="json")
                            if edge_condition is not None
                            else None
                        ),
                        condition_digest=(
                            edge_condition.condition_digest
                            if edge_condition is not None
                            else None
                        ),
                    )
                )
        invocation.execution_status = InvocationExecutionStatus.WAITING_CHILDREN.value
        invocation.revision += 1
        parent.status = ExecutionNodeStatus.WAITING_CHILDREN.value
        self._clear_claim(parent)
        parent.revision += 1
        parent.updated_at = now
        run.status = ExecutionRunStatus.ACTIVE.value
        run.revision += 1
        run.updated_at = now
        return manifest

    async def _context(
        self,
        session: AsyncSession,
        claimed: ClaimedInvocation,
        *,
        lock: bool = False,
    ) -> tuple[
        TaskExecutionRunRecord,
        TaskExecutionNodeRecord,
        AgentInvocationRecord,
        TaskContract,
    ]:
        run_query = select(TaskExecutionRunRecord).where(
            TaskExecutionRunRecord.run_id == claimed.handoff.run_id
        )
        parent_query = select(TaskExecutionNodeRecord).where(
            TaskExecutionNodeRecord.node_id == claimed.invocation.node_id
        )
        invocation_query = select(AgentInvocationRecord).where(
            AgentInvocationRecord.invocation_id == claimed.invocation.invocation_id
        )
        if lock:
            run_query = run_query.with_for_update()
            parent_query = parent_query.with_for_update()
            invocation_query = invocation_query.with_for_update()
        run = await session.scalar(run_query)
        parent = await session.scalar(parent_query)
        invocation = await session.scalar(invocation_query)
        if run is None or parent is None or invocation is None:
            raise AgentTaskGraphRejectedError("Dynamic graph parent state is missing")
        plan = await session.get(TaskPlanGenerationRecord, (run.task_id, run.plan_generation))
        contract_record = (
            await session.get(TaskContractVersionRecord, (run.task_id, plan.contract_version))
            if plan is not None
            else None
        )
        if plan is None or contract_record is None or plan.plan_manifest_digest != run.plan_digest:
            raise AgentTaskGraphProofRejectedError(
                "Dynamic graph Plan or Contract proof is missing"
            )
        try:
            contract = TaskContract.model_validate(contract_record.manifest)
        except ValidationError as error:
            raise AgentTaskGraphProofRejectedError(
                "Dynamic graph Task Contract is invalid"
            ) from error
        if (
            contract.digest != contract_record.contract_digest
            or plan.contract_digest != contract.digest
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic graph Task Contract digest changed")
        return run, parent, invocation, contract

    def _allowed_targets(
        self,
        parent: AgentRegistration,
        contract: TaskContract,
        privacy_mode: str,
    ) -> tuple[tuple[CapabilityRef, AgentRegistration], ...]:
        parent_key = parent.contract.key
        capabilities = {item.capability_id: item for item in contract.capabilities}
        result: list[tuple[CapabilityRef, AgentRegistration]] = []
        for target_ref in parent.contract.handoff_policy.may_delegate_to:
            target = self._agents.resolve_exact(target_ref.agent_id, target_ref.version)
            incoming = {item.key for item in target.contract.handoff_policy.may_receive_from}
            if (
                parent_key not in incoming
                or target.contract.key == parent_key
                or privacy_mode not in target.contract.model_policy.allowed_privacy_modes
                or target.contract.tool_policy.grants
            ):
                continue
            for capability_id in target.contract.provides:
                capability = capabilities.get(capability_id)
                if capability is None:
                    continue
                try:
                    pack = self._capabilities.resolve(
                        capability.capability_id,
                        capability.version,
                        capability.digest,
                    )
                except CapabilityCatalogError:
                    continue
                if not pack.runtime_enabled or pack.workspace_write:
                    continue
                result.append((capability, target))
        by_capability: dict[str, tuple[CapabilityRef, AgentRegistration]] = {}
        duplicates: set[str] = set()
        for item in result:
            capability_id = item[0].capability_id
            if capability_id in by_capability:
                duplicates.add(capability_id)
            by_capability[capability_id] = item
        return tuple(by_capability[key] for key in sorted(by_capability) if key not in duplicates)

    @staticmethod
    async def _route(session: AsyncSession, task_id: str) -> TurnRouteRecord:
        route = await session.get(TurnRouteRecord, task_id)
        if (
            route is None
            or route.route_id
            not in {
                "workspace_directory_list",
                "workspace_directory_analyze",
                "workspace_dynamic_patch_test",
            }
            or route.parameter_digest != sha256_digest(route.parameters)
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic graph Route parameters are invalid")
        return route

    @staticmethod
    def _input_sources(capability: CapabilityRef, route: TurnRouteRecord) -> tuple[str, ...]:
        if capability.capability_id == "workspace.directory.read.v1":
            return ("route_directory_path",) if route.parameters.get("path") else ()
        if (
            capability.capability_id == "workspace.file.read.v1"
            and route.route_id == "workspace_directory_analyze"
            and route.parameters.get("file_path")
        ):
            return ("route_explicit_file_path",)
        if (
            capability.capability_id == "workspace.python.test.v1"
            and (
                (
                    route.route_id == "workspace_directory_analyze"
                    and route.parameters.get("python_project_path")
                    and route.parameters.get("python_test_path")
                )
                or (
                    route.route_id == "workspace_dynamic_patch_test"
                    and route.parameters.get("test_kind") == "python"
                    and route.parameters.get("project_path")
                    and route.parameters.get("test_path")
                )
            )
        ):
            return ("route_python_test_spec",)
        if (
            capability.capability_id == "workspace.node.test.v1"
            and (
                (
                    route.route_id == "workspace_directory_analyze"
                    and route.parameters.get("node_project_path")
                    and route.parameters.get("node_test_path")
                )
                or (
                    route.route_id == "workspace_dynamic_patch_test"
                    and route.parameters.get("test_kind") == "node"
                    and route.parameters.get("project_path")
                    and route.parameters.get("test_path")
                )
            )
        ):
            return ("route_node_test_spec",)
        if (
            capability.capability_id == "workspace.patch.propose.v1"
            and route.route_id == "workspace_dynamic_patch_test"
            and all(
                route.parameters.get(key)
                for key in ("patch_path", "project_path", "test_path", "test_kind", "objective")
            )
        ):
            return ("route_patch_test_spec",)
        return ()

    @staticmethod
    def _patch_binding_specs(route: TurnRouteRecord) -> tuple[tuple[str, str, str], ...]:
        raw_paths_json = route.parameters.get("patch_paths_json")
        objective = route.parameters.get("objective")
        if not isinstance(raw_paths_json, str) or not isinstance(objective, str):
            return ()
        try:
            parsed = json.loads(raw_paths_json)
        except json.JSONDecodeError:
            return ()
        raw_paths = parsed.get("paths") if isinstance(parsed, dict) else None
        if not isinstance(raw_paths, list):
            return ()
        paths = tuple(item.strip() for item in raw_paths if isinstance(item, str) and item.strip())
        if not paths or len(paths) != len(raw_paths) or len(paths) != len(set(paths)):
            return ()
        return tuple(
            (
                f"patch_slot_{index}",
                path,
                f"{objective.strip()}；仅处理服务器绑定文件 {path}",
            )
            for index, path in enumerate(paths, start=1)
        )

    @classmethod
    def _input_bindings(
        cls,
        capability: CapabilityRef,
        route: TurnRouteRecord,
    ) -> tuple[AgentTaskGraphCapabilityInput, ...]:
        if capability.capability_id != "workspace.patch.propose.v1":
            return ()
        return tuple(
            cls._capability_input(
                "route_patch_test_spec",
                capability,
                route,
                input_binding_key=binding_key,
            )
            for binding_key, _path, _objective in cls._patch_binding_specs(route)
        )

    @classmethod
    def _capability_input(
        cls,
        source_key: str,
        capability: CapabilityRef,
        route: TurnRouteRecord,
        *,
        input_binding_key: str | None = None,
    ) -> AgentTaskGraphCapabilityInput:
        allowed = cls._input_sources(capability, route)
        if capability.capability_id == "workspace.patch.propose.v1":
            if source_key != "route_patch_test_spec" or source_key not in allowed:
                raise AgentTaskGraphRejectedError(
                    "Dynamic graph requested an unauthorized Patch approval input"
                )
            values = {
                key: route.parameters.get(key)
                for key in ("project_path", "test_path", "patch_path", "test_kind", "objective")
            }
            binding_spec = next(
                (
                    item
                    for item in cls._patch_binding_specs(route)
                    if item[0] == input_binding_key
                ),
                None,
            )
            if input_binding_key is not None and binding_spec is None:
                raise AgentTaskGraphRejectedError(
                    "Dynamic graph requested an unknown Patch approval binding"
                )
            if binding_spec is not None:
                values["patch_path"] = binding_spec[1]
                values["objective"] = binding_spec[2]
            if any(not isinstance(value, str) or not value.strip() for value in values.values()):
                raise AgentTaskGraphRejectedError("Dynamic graph Patch approval input is missing")
            test_kind = cast(str, values["test_kind"]).strip()
            if test_kind not in {"python", "node"}:
                raise AgentTaskGraphRejectedError("Dynamic graph Patch test kind is invalid")
            material = {
                "schema_version": (
                    "deskpilot.agent-task-graph-capability-input.v4"
                    if binding_spec is not None
                    else "deskpilot.agent-task-graph-capability-input.v3"
                ),
                "source_key": source_key,
                "source_ref": (
                    f"turn-route://{route.task_id}/parameters/"
                    + (
                        f"patch_paths_json/{input_binding_key}+project_path+test_path+test_kind+objective"
                        if binding_spec is not None
                        else "patch_path+project_path+test_path+test_kind+objective"
                    )
                ),
                "read_kind": "patch_test",
                "path": cast(str, values["project_path"]).strip(),
                "test_path": cast(str, values["test_path"]).strip(),
                "target_path": cast(str, values["patch_path"]).strip(),
                "test_kind": test_kind,
                "objective": cast(str, values["objective"]).strip(),
                "route_parameter_digest": route.parameter_digest,
            }
            if binding_spec is not None:
                material["binding_key"] = binding_spec[0]
            return AgentTaskGraphCapabilityInput.model_validate(
                {**material, "input_digest": sha256_digest(material)}
            )
        expected_source = {
            "workspace.directory.read.v1": (
                "route_directory_path",
                "path",
                "directory",
                None,
            ),
            "workspace.file.read.v1": (
                "route_explicit_file_path",
                "file_path",
                "file",
                None,
            ),
            "workspace.python.test.v1": (
                "route_python_test_spec",
                (
                    "project_path"
                    if route.route_id == "workspace_dynamic_patch_test"
                    else "python_project_path"
                ),
                "python_test",
                (
                    "test_path"
                    if route.route_id == "workspace_dynamic_patch_test"
                    else "python_test_path"
                ),
            ),
            "workspace.node.test.v1": (
                "route_node_test_spec",
                (
                    "project_path"
                    if route.route_id == "workspace_dynamic_patch_test"
                    else "node_project_path"
                ),
                "node_test",
                (
                    "test_path"
                    if route.route_id == "workspace_dynamic_patch_test"
                    else "node_test_path"
                ),
            ),
        }.get(capability.capability_id)
        if expected_source is None or source_key not in allowed or source_key != expected_source[0]:
            raise AgentTaskGraphRejectedError(
                "Dynamic graph requested an unauthorized capability input"
            )
        parameter_name = expected_source[1]
        path = route.parameters.get(parameter_name)
        if not isinstance(path, str) or not path.strip():
            raise AgentTaskGraphRejectedError("Dynamic graph capability input path is missing")
        test_parameter_name = expected_source[3]
        test_path = (
            route.parameters.get(test_parameter_name) if test_parameter_name is not None else None
        )
        if test_parameter_name is not None and (
            not isinstance(test_path, str) or not test_path.strip()
        ):
            raise AgentTaskGraphRejectedError("Dynamic graph capability test path is missing")
        schema_version = (
            "deskpilot.agent-task-graph-capability-input.v2"
            if test_parameter_name is not None
            else "deskpilot.agent-task-graph-capability-input.v1"
        )
        material = {
            "schema_version": schema_version,
            "source_key": source_key,
            "source_ref": (
                f"turn-route://{route.task_id}/parameters/"
                + (
                    f"{parameter_name}+{test_parameter_name}"
                    if test_parameter_name is not None
                    else parameter_name
                )
            ),
            "read_kind": expected_source[2],
            "path": path.strip(),
            "route_parameter_digest": route.parameter_digest,
        }
        if test_parameter_name is not None:
            assert isinstance(test_path, str)
            material["test_path"] = test_path.strip()
        return AgentTaskGraphCapabilityInput.model_validate(
            {**material, "input_digest": sha256_digest(material)}
        )

    def _parent_registration(self, claimed: ClaimedInvocation) -> AgentRegistration:
        return self._agents.resolve_exact(
            claimed.handoff.target_agent.agent_id,
            claimed.handoff.target_agent.version,
            contract_digest=claimed.handoff.target_agent.contract_digest,
            prompt_package_digest=claimed.handoff.target_agent.prompt_package_digest,
        )

    @staticmethod
    def _agent_budget(registration: AgentRegistration) -> PlanNodeBudget:
        budget = registration.contract.budget_policy
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

    @classmethod
    def _assert_child_budget(
        cls, allocation: PlanNodeBudget, registration: AgentRegistration
    ) -> None:
        maximum = cls._agent_budget(registration)
        if any(
            getattr(allocation, field) > getattr(maximum, field)
            for field in PlanNodeBudget.model_fields
        ):
            raise AgentTaskGraphRejectedError(
                "Dynamic graph child budget exceeds its Agent Contract"
            )

    @staticmethod
    async def _assert_task_budget(
        session: AsyncSession,
        run: TaskExecutionRunRecord,
        contract: TaskContract,
        decision: AgentProposeTaskGraphDecision,
    ) -> None:
        node_query = select(TaskExecutionNodeRecord)
        if CROSS_GENERATION_BUDGET_CONSTRAINT in contract.constraints:
            node_query = node_query.join(
                TaskExecutionRunRecord,
                TaskExecutionRunRecord.run_id == TaskExecutionNodeRecord.run_id,
            ).where(
                TaskExecutionRunRecord.task_id == run.task_id,
                TaskExecutionRunRecord.plan_generation <= run.plan_generation,
            )
        else:
            node_query = node_query.where(
                TaskExecutionNodeRecord.run_id == run.run_id
            )
        existing = tuple(
            (await session.scalars(node_query)).all()
        )
        budgets = [PlanNodeBudget.model_validate(item.budget) for item in existing]
        budgets.extend(item.budget_slice for item in decision.nodes)
        totals = {
            "max_model_calls": sum(item.model_calls for item in budgets),
            "max_tool_calls": sum(item.tool_calls for item in budgets),
            "max_input_tokens": sum(item.input_tokens for item in budgets),
            "max_output_tokens": sum(item.output_tokens for item in budgets),
            "max_wall_seconds": sum(item.wall_seconds for item in budgets),
            "max_retries": sum(item.retries for item in budgets),
            "max_cost_micros": sum(item.cost_micros for item in budgets),
            "max_handoffs": sum(item.handoffs for item in budgets),
        }
        limits = contract.budget.model_dump(mode="python")
        if any(value > int(limits[key]) for key, value in totals.items()):
            raise AgentTaskGraphRejectedError(
                "Dynamic graph allocations exceed the Task Contract budget"
            )

    @staticmethod
    def _graph_depth(decision: AgentProposeTaskGraphDecision) -> int:
        graph = {item.local_key: item.depends_on for item in decision.nodes}
        memo: dict[str, int] = {}

        def depth(key: str) -> int:
            if key not in memo:
                memo[key] = 1 + max((depth(source) for source in graph[key]), default=0)
            return memo[key]

        return max(depth(key) for key in graph)

    @classmethod
    async def _verified_result_ref(
        cls,
        session: AsyncSession,
        graph: AgentTaskGraphRecord,
        bound: BoundAgentTaskGraphNode,
        graph_node: AgentTaskGraphNodeRecord,
        *,
        child_invocation: AgentInvocationRecord | None = None,
        child_result_id: str | None = None,
        require_persisted: bool,
    ) -> tuple[AgentTaskGraphResultRef, WorkspaceAgentResult]:
        invocation = child_invocation
        if invocation is None and graph_node.child_invocation_id is not None:
            invocation = await session.get(AgentInvocationRecord, graph_node.child_invocation_id)
        result_id = child_result_id or graph_node.child_result_id
        result = await session.get(AgentResultRecord, result_id) if result_id is not None else None
        workspace_record = (
            await session.get(WorkspaceAgentResultRecord, invocation.invocation_id)
            if invocation is not None
            else None
        )
        try:
            if result is None or workspace_record is None:
                raise ValueError
            result_manifest = AgentOutputResult.model_validate(result.manifest)
            if workspace_record.result_kind == "file":
                workspace_result: WorkspaceAgentResult = WorkspaceFileRead.model_validate(
                    workspace_record.manifest
                )
            elif workspace_record.result_kind == "directory":
                workspace_result = WorkspaceDirectoryRead.model_validate(workspace_record.manifest)
            elif workspace_record.result_kind == "python_test":
                workspace_result = WorkspacePythonTestRead.model_validate(workspace_record.manifest)
            elif workspace_record.result_kind == "node_test":
                workspace_result = WorkspaceNodeTestRead.model_validate(workspace_record.manifest)
            elif workspace_record.result_kind == "patch_test":
                workspace_result = WorkspacePatchTestRead.model_validate(workspace_record.manifest)
            else:
                raise ValueError
        except (ValidationError, ValueError) as error:
            raise AgentTaskGraphProofRejectedError(
                "Dynamic graph child result proof is invalid"
            ) from error
        if isinstance(workspace_result, WorkspacePatchTestRead):
            approval = cls.verified_patch_approval(graph, bound, graph_node)
            manifest = cls._manifest(graph)
            is_conditioned_failure = bool(
                workspace_result.status != "verified"
                and manifest.schema_version
                in {"deskpilot.agent-task-graph.v7", "deskpilot.agent-task-graph.v8"}
                and any(
                    condition.source_node_id == bound.runtime_node_id
                    for target in manifest.nodes
                    for condition in target.conditions
                )
            )
            if approval is None or (
                workspace_result.status != "verified" and not is_conditioned_failure
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Dynamic Patch result has no approval or server condition proof"
                )
        if (
            invocation is None
            or graph_node.graph_id != graph.graph_id
            or graph_node.child_node_id != bound.runtime_node_id
            or graph_node.binding_id != bound.binding_id
            or graph_node.child_invocation_id != invocation.invocation_id
            or invocation.parent_invocation_id != graph.parent_invocation_id
            or invocation.node_id != bound.runtime_node_id
            or invocation.agent_id != bound.target_agent.agent_id
            or invocation.agent_version != bound.target_agent.version
            or invocation.verification_status != InvocationVerificationStatus.VERIFIED.value
            or invocation.result_id != result.result_id
            or result.result_id != result_id
            or result.invocation_id != invocation.invocation_id
            or result_manifest.result_id != result.result_id
            or result_manifest.invocation_id != invocation.invocation_id
            or result_manifest.result_digest != result.result_digest
            or result_manifest.output != cls._result_output(workspace_result)
            or workspace_record.invocation_id != invocation.invocation_id
            or workspace_record.run_id != graph.run_id
            or workspace_record.result_digest != workspace_result.result_digest
        ):
            raise AgentTaskGraphProofRejectedError("Dynamic graph child result binding changed")
        material = {
            "schema_version": "deskpilot.agent-task-graph-result-ref.v1",
            "graph_id": graph.graph_id,
            "producer_local_key": bound.local_key,
            "producer_node_id": bound.runtime_node_id,
            "producer_invocation_id": invocation.invocation_id,
            "producer_result_id": result.result_id,
            "capability": bound.capability,
            "result_kind": workspace_record.result_kind,
            "agent_result_digest": result.result_digest,
            "workspace_result_digest": workspace_result.result_digest,
        }
        result_ref = AgentTaskGraphResultRef.model_validate(
            {**material, "result_ref_digest": sha256_digest(material)}
        )
        if require_persisted:
            try:
                persisted_ref = AgentTaskGraphResultRef.model_validate(
                    graph_node.result_ref_manifest
                )
            except ValidationError as error:
                raise AgentTaskGraphProofRejectedError(
                    "Dynamic graph persisted ResultRef is invalid"
                ) from error
            if (
                persisted_ref != result_ref
                or graph_node.result_ref_digest != result_ref.result_ref_digest
            ):
                raise AgentTaskGraphProofRejectedError("Dynamic graph persisted ResultRef changed")
        return result_ref, workspace_result

    @staticmethod
    def _result_output(result: WorkspaceAgentResult) -> dict[str, object]:
        if isinstance(result, (WorkspaceFileRead, WorkspaceDirectoryRead)):
            return {
                "relative_path": result.relative_path,
                "result_digest": result.result_digest,
            }
        if isinstance(result, WorkspacePatchTestRead):
            test_result = result.python_test or result.node_test
            return {
                "status": result.status,
                "patch_receipt_digest": result.patch_receipt.receipt_digest,
                "test_result_digest": (
                    test_result.result_digest if test_result is not None else None
                ),
                "workspace_patch_test_result_digest": result.result_digest,
            }
        return {
            "project_path": result.project_path,
            "test_path": result.test_path,
            "status": result.status,
            "result_digest": result.result_digest,
        }

    @staticmethod
    def _manifest(record: AgentTaskGraphRecord) -> AgentTaskGraphManifest:
        try:
            manifest = AgentTaskGraphManifest.model_validate(record.manifest)
        except ValidationError as error:
            raise AgentTaskGraphProofRejectedError(
                "Agent task graph manifest is invalid"
            ) from error
        if (
            manifest.graph_id != record.graph_id
            or manifest.run_id != record.run_id
            or manifest.parent_invocation_id != record.parent_invocation_id
            or manifest.parent_node_id != record.parent_node_id
            or manifest.decision_id != record.decision_id
            or manifest.binding_id != record.binding_id
            or manifest.graph_digest != record.graph_digest
            or len(manifest.nodes) != record.node_count
            or AgentSupervisorRuntime._manifest_depth(manifest) != record.max_depth
            or manifest.output_local_key != record.output_local_key
            or manifest.output_node_id != record.output_node_id
        ):
            raise AgentTaskGraphProofRejectedError("Agent task graph persistence binding changed")
        return manifest

    @staticmethod
    def _manifest_depth(manifest: AgentTaskGraphManifest) -> int:
        graph = {item.local_key: item.depends_on for item in manifest.nodes}
        memo: dict[str, int] = {}

        def depth(key: str) -> int:
            if key not in memo:
                memo[key] = 1 + max((depth(source) for source in graph[key]), default=0)
            return memo[key]

        return max(depth(key) for key in graph)

    @staticmethod
    async def _condition_decisions(
        session: AsyncSession,
        graph: AgentTaskGraphRecord,
        bound: BoundAgentTaskGraphNode,
        *,
        require_matched: bool,
    ) -> tuple[AgentTaskGraphConditionDecision, ...]:
        edges = tuple(
            (
                await session.scalars(
                    select(TaskExecutionEdgeRecord)
                    .where(
                        TaskExecutionEdgeRecord.run_id == graph.run_id,
                        TaskExecutionEdgeRecord.to_node_id == bound.runtime_node_id,
                    )
                    .order_by(TaskExecutionEdgeRecord.from_node_id)
                )
            ).all()
        )
        if {item.from_node_id for item in edges} != set(bound.depends_on_node_ids):
            raise AgentTaskGraphProofRejectedError(
                "Agent task graph dependency edge set changed"
            )
        conditions = {item.source_node_id: item for item in bound.conditions}
        decisions: list[AgentTaskGraphConditionDecision] = []
        for edge in edges:
            condition = conditions.get(edge.from_node_id)
            condition_fields = (
                edge.condition_manifest,
                edge.condition_digest,
                edge.decision_manifest,
                edge.decision_digest,
            )
            if condition is None:
                if edge.requirement != "verified" or any(
                    item is not None for item in condition_fields
                ):
                    raise AgentTaskGraphProofRejectedError(
                        "Unconditional graph edge proof changed"
                    )
                continue
            try:
                persisted_condition = BoundAgentTaskGraphCondition.model_validate(
                    edge.condition_manifest
                )
            except ValidationError as error:
                raise AgentTaskGraphProofRejectedError(
                    "Conditional graph edge proof is invalid"
                ) from error
            if (
                edge.requirement != "server_condition"
                or persisted_condition != condition
                or edge.condition_digest != condition.condition_digest
                or (edge.decision_manifest is None) != (edge.decision_digest is None)
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Conditional graph edge proof changed"
                )
            if edge.decision_manifest is None:
                if require_matched:
                    raise AgentTaskGraphProofRejectedError(
                        "Verified graph condition was not adjudicated"
                    )
                continue
            try:
                decision = AgentTaskGraphConditionDecision.model_validate(
                    edge.decision_manifest
                )
            except ValidationError as error:
                raise AgentTaskGraphProofRejectedError(
                    "Conditional graph decision proof is invalid"
                ) from error
            source_node = await session.scalar(
                select(AgentTaskGraphNodeRecord).where(
                    AgentTaskGraphNodeRecord.graph_id == graph.graph_id,
                    AgentTaskGraphNodeRecord.child_node_id == edge.from_node_id,
                )
            )
            if (
                edge.decision_digest != decision.decision_digest
                or decision.graph_id != graph.graph_id
                or decision.source_local_key != condition.source_local_key
                or decision.source_node_id != condition.source_node_id
                or decision.target_local_key != bound.local_key
                or decision.target_node_id != bound.runtime_node_id
                or decision.predicate != condition.predicate
                or source_node is None
                or source_node.result_ref_digest != decision.result_ref_digest
                or (require_matched and not decision.matched)
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Conditional graph decision binding changed"
                )
            decisions.append(decision)
        return tuple(decisions)

    @staticmethod
    def _validate_observation(
        graph: AgentTaskGraphRecord, observation: AgentObservationRecord
    ) -> None:
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
        if (
            observation.observation_digest != sha256_digest(material)
            or observation.invocation_id != graph.parent_invocation_id
            or observation.decision_id != graph.decision_id
            or observation.binding_id != graph.binding_id
            or observation.source_kind != "handoff"
            or observation.status != "succeeded"
            or observation.result_ref != f"agent-task-graph:{graph.graph_id}"
            or set(observation.projection) != {"graph_id", "graph_digest", "children"}
            or observation.projection.get("graph_id") != graph.graph_id
            or observation.projection.get("graph_digest") != graph.graph_digest
        ):
            raise AgentTaskGraphProofRejectedError("Agent task graph observation proof changed")

    @classmethod
    async def _assert_verified_observation(
        cls,
        session: AsyncSession,
        graph: AgentTaskGraphRecord,
        observation: AgentObservationRecord,
    ) -> None:
        cls._validate_observation(graph, observation)
        manifest = cls._manifest(graph)
        graph_nodes = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphNodeRecord)
                    .where(AgentTaskGraphNodeRecord.graph_id == graph.graph_id)
                    .order_by(AgentTaskGraphNodeRecord.local_key)
                )
            ).all()
        )
        raw_children = observation.projection.get("children")
        if not isinstance(raw_children, list) or len(raw_children) != len(manifest.nodes):
            raise AgentTaskGraphProofRejectedError("Agent task graph observation child set changed")
        projected: dict[str, dict[str, object]] = {}
        for item in raw_children:
            if not isinstance(item, dict) or not isinstance(item.get("local_key"), str):
                raise AgentTaskGraphProofRejectedError(
                    "Agent task graph observation child proof is invalid"
                )
            local_key = str(item["local_key"])
            if local_key in projected:
                raise AgentTaskGraphProofRejectedError(
                    "Agent task graph observation contains a duplicate child"
                )
            projected[local_key] = item
        persisted = {item.local_key: item for item in graph_nodes}
        for bound in manifest.nodes:
            if manifest.schema_version in {
                "deskpilot.agent-task-graph.v7",
                "deskpilot.agent-task-graph.v8",
            }:
                await cls._condition_decisions(
                    session, graph, bound, require_matched=True
                )
            graph_node = persisted.get(bound.local_key)
            child_projection = projected.get(bound.local_key)
            if (
                graph_node is None
                or graph_node.status not in {"child_verified", "consumed"}
                or graph_node.child_invocation_id is None
                or graph_node.child_result_id is None
                or child_projection is None
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Agent task graph verified child proof is incomplete"
                )
            if manifest.schema_version in {
                "deskpilot.agent-task-graph.v2",
                "deskpilot.agent-task-graph.v3",
                "deskpilot.agent-task-graph.v4",
                "deskpilot.agent-task-graph.v5",
                "deskpilot.agent-task-graph.v6",
                "deskpilot.agent-task-graph.v7",
                "deskpilot.agent-task-graph.v8",
            }:
                result_ref, _workspace_result = await cls._verified_result_ref(
                    session, graph, bound, graph_node, require_persisted=True
                )
                expected_projection = {
                    "local_key": bound.local_key,
                    "result_ref": result_ref.model_dump(mode="json"),
                }
                if child_projection != expected_projection:
                    raise AgentTaskGraphProofRejectedError(
                        "Agent task graph ResultRef projection changed"
                    )
                continue
            invocation = await session.get(AgentInvocationRecord, graph_node.child_invocation_id)
            result = await session.get(AgentResultRecord, graph_node.child_result_id)
            workspace_result = await session.get(
                WorkspaceAgentResultRecord, graph_node.child_invocation_id
            )
            try:
                if result is None or workspace_result is None:
                    raise ValueError
                result_manifest = AgentOutputResult.model_validate(result.manifest)
                workspace_manifest = (
                    WorkspaceFileRead.model_validate(workspace_result.manifest)
                    if workspace_result.result_kind == "file"
                    else WorkspaceDirectoryRead.model_validate(workspace_result.manifest)
                )
            except (ValidationError, ValueError) as error:
                raise AgentTaskGraphProofRejectedError(
                    "Legacy Agent task graph child result proof is invalid"
                ) from error
            legacy_projection: dict[str, object] = {
                "local_key": bound.local_key,
                "child_invocation_id": graph_node.child_invocation_id,
                "child_result_id": graph_node.child_result_id,
                "result_digest": result.result_digest if result is not None else None,
                "workspace_result_digest": (
                    workspace_result.result_digest if workspace_result is not None else None
                ),
            }
            if (
                invocation is None
                or result is None
                or workspace_result is None
                or invocation.parent_invocation_id != graph.parent_invocation_id
                or invocation.node_id != bound.runtime_node_id
                or invocation.result_id != graph_node.child_result_id
                or invocation.verification_status != InvocationVerificationStatus.VERIFIED.value
                or result_manifest.result_digest != result.result_digest
                or workspace_manifest.result_digest != workspace_result.result_digest
                or child_projection != legacy_projection
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Legacy Agent task graph child result binding changed"
                )

    @staticmethod
    def _clear_claim(node: TaskExecutionNodeRecord) -> None:
        node.claim_owner_id = None
        node.claim_acquired_at = None
        node.claim_heartbeat_at = None
        node.claim_expires_at = None


async def read_agent_task_graphs(
    session: AsyncSession,
    run: TaskExecutionRunRecord,
    *,
    nodes: Mapping[str, TaskExecutionNodeRecord],
    invocations: Mapping[str, AgentInvocationRecord],
    decisions: Mapping[str, AgentDecisionRecord],
    observations: Mapping[str, AgentObservationRecord],
) -> tuple[AgentTaskGraphRead, ...]:
    """Recompute every dynamic graph proof before exposing it to the Workbench."""

    records = tuple(
        (
            await session.scalars(
                select(AgentTaskGraphRecord)
                .where(AgentTaskGraphRecord.run_id == run.run_id)
                .order_by(AgentTaskGraphRecord.created_at)
            )
        ).all()
    )
    result: list[AgentTaskGraphRead] = []
    for record in records:
        manifest = AgentSupervisorRuntime._manifest(record)
        if (
            manifest.task_id != run.task_id
            or manifest.plan_generation != run.plan_generation
            or manifest.plan_digest != run.plan_digest
        ):
            raise AgentTaskGraphProofRejectedError("Agent task graph run lineage changed")
        decision = decisions.get(record.decision_id) or await session.get(
            AgentDecisionRecord, record.decision_id
        )
        parent = invocations.get(record.parent_invocation_id) or await session.get(
            AgentInvocationRecord, record.parent_invocation_id
        )
        parent_node = nodes.get(record.parent_node_id) or await session.get(
            TaskExecutionNodeRecord, record.parent_node_id
        )
        if (
            decision is None
            or decision.kind != "propose_task_graph"
            or decision.invocation_id != record.parent_invocation_id
            or decision.binding_id != record.binding_id
            or sha256_digest(decision.manifest) != manifest.proposal_digest
            or parent is None
            or parent.node_id != record.parent_node_id
            or parent_node is None
        ):
            raise AgentTaskGraphProofRejectedError("Agent task graph proposal lineage changed")
        graph_nodes = tuple(
            (
                await session.scalars(
                    select(AgentTaskGraphNodeRecord)
                    .where(AgentTaskGraphNodeRecord.graph_id == record.graph_id)
                    .order_by(AgentTaskGraphNodeRecord.local_key)
                )
            ).all()
        )
        by_key = {item.local_key: item for item in graph_nodes}
        if len(by_key) != len(manifest.nodes):
            raise AgentTaskGraphProofRejectedError("Agent task graph child set changed")
        reads: list[AgentTaskGraphNodeRead] = []
        for bound in manifest.nodes:
            graph_node = by_key.get(bound.local_key)
            runtime_node = nodes.get(bound.runtime_node_id) or await session.get(
                TaskExecutionNodeRecord, bound.runtime_node_id
            )
            if (
                graph_node is None
                or graph_node.child_node_id != bound.runtime_node_id
                or graph_node.binding_id != bound.binding_id
                or graph_node.budget_allocation != bound.budget_allocation.model_dump(mode="json")
                or runtime_node is None
                or runtime_node.run_id != run.run_id
                or runtime_node.local_key != bound.runtime_local_key
                or runtime_node.node_spec_digest != bound.node_spec_digest
                or tuple(runtime_node.depends_on) != bound.depends_on_node_ids
                or runtime_node.handoff_parent_node_id != record.parent_node_id
                or runtime_node.bound_agent != bound.target_agent.model_dump(mode="json")
                or runtime_node.capability != bound.capability.model_dump(mode="json")
                or runtime_node.budget != bound.budget_allocation.model_dump(mode="json")
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Agent task graph runtime node proof changed"
                )
            capability_input = AgentSupervisorRuntime.verified_capability_input(record, graph_node)
            approval = AgentSupervisorRuntime.verified_patch_approval(
                record, bound, graph_node
            )
            condition_decisions = await AgentSupervisorRuntime._condition_decisions(
                session,
                record,
                bound,
                require_matched=record.status in {"verified", "consumed"},
            )
            child_invocation = (
                invocations.get(graph_node.child_invocation_id)
                or await session.get(AgentInvocationRecord, graph_node.child_invocation_id)
                if graph_node.child_invocation_id is not None
                else None
            )
            if child_invocation is not None and (
                child_invocation.node_id != bound.runtime_node_id
                or child_invocation.parent_invocation_id != parent.invocation_id
                or child_invocation.agent_id != bound.target_agent.agent_id
                or child_invocation.agent_version != bound.target_agent.version
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Agent task graph child Invocation lineage changed"
                )
            verified = graph_node.status in {"child_verified", "consumed"}
            result_ref: AgentTaskGraphResultRef | None = None
            if verified and (
                child_invocation is None
                or graph_node.child_result_id is None
                or child_invocation.result_id != graph_node.child_result_id
                or child_invocation.verification_status
                != InvocationVerificationStatus.VERIFIED.value
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Agent task graph verified child proof is missing"
                )
            workspace_result: WorkspaceAgentResult | None = None
            if verified and manifest.schema_version in {
                "deskpilot.agent-task-graph.v2",
                "deskpilot.agent-task-graph.v3",
                "deskpilot.agent-task-graph.v4",
                "deskpilot.agent-task-graph.v5",
                "deskpilot.agent-task-graph.v6",
                "deskpilot.agent-task-graph.v7",
                "deskpilot.agent-task-graph.v8",
            }:
                result_ref, workspace_result = await AgentSupervisorRuntime._verified_result_ref(
                    session,
                    record,
                    bound,
                    graph_node,
                    require_persisted=True,
                )
            if verified and bound.capability.capability_id == "workspace.patch.propose.v1":
                if approval is None or not isinstance(workspace_result, WorkspacePatchTestRead):
                    raise AgentTaskGraphProofRejectedError(
                        "Verified Patch graph node proof is incomplete"
                    )
            if approval is not None and not verified:
                if (
                    child_invocation is None
                    or child_invocation.execution_status
                    != InvocationExecutionStatus.WAITING_USER.value
                    or runtime_node.status != ExecutionNodeStatus.WAITING_USER.value
                ):
                    raise AgentTaskGraphProofRejectedError(
                        "Pending Patch approval is not waiting for the user"
                    )
            if graph_node.status == "waiting_child" and any(
                item is not None
                for item in (
                    graph_node.child_result_id,
                    graph_node.result_ref_manifest,
                    graph_node.result_ref_digest,
                )
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Waiting Agent task graph child contains a result"
                )
            reads.append(
                AgentTaskGraphNodeRead(
                    local_key=bound.local_key,
                    node_id=bound.runtime_node_id,
                    binding_id=bound.binding_id,
                    status=cast(
                        Literal[
                            "waiting_child",
                            "child_verified",
                            "consumed",
                            "cancelled",
                            "failed",
                        ],
                        graph_node.status,
                    ),
                    depends_on=bound.depends_on,
                    target_agent=bound.target_agent,
                    capability=bound.capability,
                    capability_input=capability_input,
                    conditions=bound.conditions,
                    condition_decisions=condition_decisions,
                    import_sources=bound.import_sources,
                    imported_result_refs=bound.imported_result_refs,
                    approval_binding=bound.approval_binding,
                    budget_allocation=bound.budget_allocation,
                    child_invocation_id=graph_node.child_invocation_id,
                    child_result_id=graph_node.child_result_id,
                    result_ref=result_ref,
                    test_result=(
                        (
                            workspace_result.python_test or workspace_result.node_test
                            if isinstance(workspace_result, WorkspacePatchTestRead)
                            else workspace_result
                        )
                        if isinstance(
                            workspace_result,
                            (
                                WorkspacePythonTestRead,
                                WorkspaceNodeTestRead,
                                WorkspacePatchTestRead,
                            ),
                        )
                        else None
                    ),
                    approval=approval,
                    patch_result=(
                        workspace_result
                        if isinstance(workspace_result, WorkspacePatchTestRead)
                        else None
                    ),
                )
            )
        observation = (
            observations.get(record.observation_id)
            or await session.get(AgentObservationRecord, record.observation_id)
            if record.observation_id is not None
            else None
        )
        verified_graph = record.status in {"verified", "consumed"}
        if verified_graph:
            if observation is None or any(
                item.status not in {"child_verified", "consumed"} for item in graph_nodes
            ):
                raise AgentTaskGraphProofRejectedError(
                    "Verified Agent task graph has incomplete children"
                )
            await AgentSupervisorRuntime._assert_verified_observation(session, record, observation)
        elif record.observation_id is not None:
            raise AgentTaskGraphProofRejectedError(
                "Unverified Agent task graph contains an observation"
            )
        result.append(
            AgentTaskGraphRead(
                schema_version=manifest.schema_version,
                graph_id=record.graph_id,
                binding_id=record.binding_id,
                parent_invocation_id=record.parent_invocation_id,
                parent_node_id=record.parent_node_id,
                decision_id=record.decision_id,
                status=cast(
                    Literal["running", "verified", "consumed", "cancelled", "failed"],
                    record.status,
                ),
                node_count=record.node_count,
                max_depth=record.max_depth,
                graph_digest=record.graph_digest,
                output_local_key=manifest.output_local_key,
                output_node_id=manifest.output_node_id,
                observation_id=record.observation_id,
                nodes=tuple(reads),
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )
    return tuple(result)
