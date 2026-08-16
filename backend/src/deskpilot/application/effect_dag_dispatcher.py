"""Database-driven v2 DAG dispatcher with bounded parallel execution and leases."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from deskpilot.application.effect_dag_admission import (
    EffectDagAdmissionCancelledError,
    EffectDagAdmissionController,
    EffectDagAdmissionControllerPort,
    EffectDagAdmissionPermitPort,
    EffectDagAdmissionRequest,
)
from deskpilot.application.effect_dag_cluster_admission import (
    EffectDagAdmissionPermitLostError,
)
from deskpilot.application.runner_client import ProgressCallback
from deskpilot.application.task_service import (
    EffectDagAdmissionProofRejectedError,
    EffectGraphFenceRejectedError,
    EffectNodeFenceRejectedError,
    EffectReadySetProofRejectedError,
    TaskService,
)
from deskpilot.domain.effect_graph import (
    EffectCompensationPlanRead,
    EffectEdgeKind,
    EffectGraphRead,
    EffectGraphStatus,
    EffectNodeClaimRead,
    EffectNodeRead,
    EffectNodeStatus,
)
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.runner.ipc_protocol import ToolCallResult


@dataclass(frozen=True, slots=True)
class EffectNodeExecutionResult:
    """A Runner adapter's durable interpretation of one node outcome."""

    status: EffectNodeStatus
    error_code: str | None = None
    transition_committed: bool = False

    def __post_init__(self) -> None:
        if self.status not in {
            EffectNodeStatus.SUCCEEDED,
            EffectNodeStatus.FAILED,
            EffectNodeStatus.UNKNOWN,
            EffectNodeStatus.CANCELLED,
            EffectNodeStatus.COMPENSATED,
            EffectNodeStatus.COMPENSATION_FAILED,
            EffectNodeStatus.COMPENSATION_UNKNOWN,
        }:
            raise ValueError("DAG executor returned a non-terminal node status")


class EffectNodeExecutor(Protocol):
    """Adapter boundary implemented by a trusted parallel Runner integration."""

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult: ...

    async def cancel(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        *,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EffectBranchDecisionSelection:
    """Trusted resolver output; raw evaluation evidence stays outside the graph."""

    outcome: str
    evidence_digest: str


class EffectBranchDecisionResolver(Protocol):
    """Trusted application boundary for unresolved succeeded branch sources."""

    async def resolve(
        self,
        task_id: str,
        source: EffectNodeRead,
        decision_key: str,
        declared_outcomes: tuple[str, ...],
    ) -> EffectBranchDecisionSelection | None: ...


@dataclass(frozen=True, slots=True)
class RunnerEffectNodeRequest:
    """Sensitive dispatch material supplied just-in-time by trusted application code."""

    call_id: str
    arguments: dict[str, object]
    actor: str
    authorization: ToolAuthorizationGrant
    idempotency_key: str | None = None
    expected_resource_versions: dict[str, str] | None = None
    expected_runner_id: str | None = None


class EffectNodeRequestResolver(Protocol):
    async def resolve(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> RunnerEffectNodeRequest: ...


class ParallelRunnerPort(Protocol):
    async def call_tool(
        self,
        *,
        task_id: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        arguments: dict[str, object],
        actor: str,
        expected_runner_id: str | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        expected_resource_versions: dict[str, str] | None = None,
        authorization: ToolAuthorizationGrant,
        progress_callback: ProgressCallback | None = None,
    ) -> ToolCallResult: ...

    async def cancel_call(
        self,
        call_id: str,
        reason: str,
        *,
        expected_runner_id: str | None = None,
    ) -> None: ...


class RunnerEffectNodeExecutor:
    """Concrete adapter from claimed DAG nodes to the isolated Runner supervisor."""

    def __init__(
        self,
        runner: ParallelRunnerPort,
        resolver: EffectNodeRequestResolver,
    ) -> None:
        self._runner = runner
        self._resolver = resolver
        self._active_requests: dict[tuple[str, int], RunnerEffectNodeRequest] = {}
        self._cancelled_claims: set[tuple[str, int]] = set()
        self._calls_lock = asyncio.Lock()

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult:
        request = await self._resolver.resolve(task_id, node, claim)
        claim_key = (claim.node_id, claim.fencing_token)
        async with self._calls_lock:
            self._active_requests[claim_key] = request
            cancelled = claim_key in self._cancelled_claims
        try:
            if cancelled:
                return EffectNodeExecutionResult(
                    status=EffectNodeStatus.CANCELLED,
                    error_code="DAG_CANCELLED_BEFORE_RUNNER_DISPATCH",
                )
            result = await self._runner.call_tool(
                task_id=task_id,
                step_id=node.step_id,
                tool_name=node.tool_name,
                tool_version=node.tool_version,
                arguments=request.arguments,
                actor=request.actor,
                expected_runner_id=request.expected_runner_id,
                call_id=request.call_id,
                idempotency_key=request.idempotency_key,
                expected_resource_versions=request.expected_resource_versions,
                authorization=request.authorization,
            )
        finally:
            async with self._calls_lock:
                self._active_requests.pop(claim_key, None)
                self._cancelled_claims.discard(claim_key)
        return EffectNodeExecutionResult(
            status=EffectNodeStatus(result.status),
            error_code=result.error.code if result.error is not None else None,
        )

    async def cancel(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        *,
        reason: str,
    ) -> None:
        del task_id, node
        claim_key = (claim.node_id, claim.fencing_token)
        async with self._calls_lock:
            self._cancelled_claims.add(claim_key)
            request = self._active_requests.get(claim_key)
        if request is None:
            return
        try:
            await self._runner.cancel_call(
                request.call_id,
                reason,
                expected_runner_id=request.expected_runner_id,
            )
        except Exception:
            # The terminal Runner result (or generation failure) remains authoritative.
            return


@dataclass(frozen=True, slots=True)
class EffectDagDispatchResult:
    task_id: str
    rounds: int
    claimed: int
    completed: int
    fenced: int
    graph_status: EffectGraphStatus
    compensation_plan: EffectCompensationPlanRead | None = None


class EffectDagDispatcher:
    """Turns persisted ready-set proofs into parallel, fenced Runner work."""

    def __init__(
        self,
        task_service: TaskService,
        executor: EffectNodeExecutor,
        *,
        instance_id: str,
        max_concurrency: int = 4,
        graph_lease_ttl_seconds: float = 15,
        node_claim_ttl_seconds: float = 15,
        branch_decision_resolver: EffectBranchDecisionResolver | None = None,
        admission_controller: EffectDagAdmissionControllerPort | None = None,
        ready_page_size: int = 64,
        stop_after_branch_decision: bool = False,
    ) -> None:
        if not 1 <= max_concurrency <= 32:
            raise ValueError("DAG dispatcher concurrency must be between 1 and 32")
        if not 1 <= graph_lease_ttl_seconds <= 3_600:
            raise ValueError("DAG graph lease TTL must be between 1 and 3600 seconds")
        if not 1 <= node_claim_ttl_seconds <= 3_600:
            raise ValueError("DAG node claim TTL must be between 1 and 3600 seconds")
        if not 1 <= len(instance_id) <= 80:
            raise ValueError("DAG dispatcher instance ID is invalid")
        if not 1 <= ready_page_size <= 1_000:
            raise ValueError("DAG dispatcher ready page size is invalid")
        self._task_service = task_service
        self._executor = executor
        self._instance_id = instance_id
        self._max_concurrency = max_concurrency
        self._graph_lease_ttl_seconds = graph_lease_ttl_seconds
        self._node_claim_ttl_seconds = node_claim_ttl_seconds
        self._branch_decision_resolver = branch_decision_resolver
        self._admission = admission_controller or EffectDagAdmissionController(
            global_limit=max_concurrency,
            per_graph_limit=max_concurrency,
            default_tool_limit=max_concurrency,
        )
        self._ready_page_size = ready_page_size
        self._stop_after_branch_decision = stop_after_branch_decision
        self._cancel_lock = asyncio.Lock()
        self._cancel_reason: str | None = None
        self._cancel_persisted = False
        self._cancelled_claims: set[tuple[str, int]] = set()
        self._active_task_id: str | None = None
        self._active_graph_id: str | None = None
        self._active_graph_fencing_token: int | None = None
        self._active_claims: dict[str, tuple[EffectNodeRead, EffectNodeClaimRead]] = {}

    async def request_cancel(
        self,
        task_id: str,
        *,
        reason: str,
        expected_graph_fencing_token: int | None = None,
    ) -> None:
        """Persist graph intent, then cancel only calls owned by current node fences."""
        if self._active_task_id not in {None, task_id}:
            raise ValueError("DAG dispatcher cancellation belongs to another task")
        if (
            expected_graph_fencing_token is not None
            and self._active_graph_fencing_token != expected_graph_fencing_token
        ):
            raise EffectGraphFenceRejectedError(self._active_graph_id or task_id)
        self._cancel_reason = reason
        if self._active_graph_id is not None:
            await self._admission.cancel_waiters(self._active_graph_id)
        await self._apply_pending_cancel()

    async def run_until_idle(
        self,
        task_id: str,
        *,
        max_rounds: int = 100,
    ) -> EffectDagDispatchResult:
        """Dispatch ready waves until terminal, compensating, or externally busy."""
        if not 1 <= max_rounds <= 10_000:
            raise ValueError("DAG dispatcher max rounds is invalid")
        lease = await self._task_service.acquire_effect_graph_lease(
            task_id,
            owner_id=self._instance_id,
            ttl_seconds=self._graph_lease_ttl_seconds,
        )
        self._active_task_id = task_id
        self._active_graph_id = lease.graph_id
        self._active_graph_fencing_token = lease.fencing_token
        bind_graph_lease = getattr(self._executor, "bind_graph_lease", None)
        if bind_graph_lease is not None:
            bind_graph_lease(self._instance_id, lease.fencing_token)
        stop_graph_heartbeat = asyncio.Event()
        graph_heartbeat = asyncio.create_task(
            self._renew_graph_lease(task_id, lease.fencing_token, stop_graph_heartbeat),
            name=f"dag-graph-heartbeat:{task_id}",
        )
        rounds = 0
        claimed_count = 0
        completed_count = 0
        fenced_count = 0
        plan: EffectCompensationPlanRead | None = None
        try:
            await self._apply_pending_cancel()
            graph = await self._task_service.reduce_effect_dag(
                task_id,
                lease_owner_id=self._instance_id,
                fencing_token=lease.fencing_token,
            )
            while rounds < max_rounds and graph.status is EffectGraphStatus.ACTIVE:
                if graph_heartbeat.done():
                    graph_heartbeat.result()
                checkpoint = await self._task_service.checkpoint_effect_dag_ready_set(
                    task_id,
                    lease_owner_id=self._instance_id,
                    fencing_token=lease.fencing_token,
                    page_size=self._ready_page_size,
                )
                selected = checkpoint.ready_nodes[: self._max_concurrency]
                if not selected:
                    graph = await self._task_service.reduce_effect_dag(
                        task_id,
                        lease_owner_id=self._instance_id,
                        fencing_token=lease.fencing_token,
                    )
                    if graph.status is not EffectGraphStatus.ACTIVE:
                        break
                    if await self._resolve_pending_branch_decisions(
                        task_id,
                        graph,
                        lease.fencing_token,
                    ):
                        graph = await self._task_service.reduce_effect_dag(
                            task_id,
                            lease_owner_id=self._instance_id,
                            fencing_token=lease.fencing_token,
                        )
                        if self._stop_after_branch_decision:
                            break
                        continue
                    break
                nodes_by_id = {node.node_id: node for node in graph.nodes}
                try:
                    permits = await self._admission.acquire_batch(
                        graph.graph_id,
                        tuple(
                            EffectDagAdmissionRequest(
                                node_id=proof.node_id,
                                tool_name=nodes_by_id[proof.node_id].tool_name,
                            )
                            for proof in selected
                        ),
                    )
                except EffectDagAdmissionCancelledError:
                    graph = await self._task_service.reduce_effect_dag(
                        task_id,
                        lease_owner_id=self._instance_id,
                        fencing_token=lease.fencing_token,
                    )
                    continue
                try:
                    try:
                        admission_proofs = {
                            permit.request.node_id: permit.proof
                            for permit in permits
                            if permit.proof is not None
                        }
                        claims = await self._task_service.claim_effect_dag_nodes(
                            task_id,
                            tuple(permit.request.node_id for permit in permits),
                            ready_proof_digest=checkpoint.proof_digest,
                            claim_owner_id=self._instance_id,
                            claim_ttl_seconds=self._node_claim_ttl_seconds,
                            lease_owner_id=self._instance_id,
                            fencing_token=lease.fencing_token,
                            admission_proofs=admission_proofs or None,
                        )
                    except (
                        EffectDagAdmissionProofRejectedError,
                        EffectReadySetProofRejectedError,
                    ):
                        graph = await self._task_service.reduce_effect_dag(
                            task_id,
                            lease_owner_id=self._instance_id,
                            fencing_token=lease.fencing_token,
                        )
                        continue
                    claimed_count += len(claims)
                    rounds += 1
                    graph = await self._task_service.get_effect_graph(task_id)
                    nodes_by_id = {node.node_id: node for node in graph.nodes}
                    self._active_claims = {
                        claim.node_id: (nodes_by_id[claim.node_id], claim) for claim in claims
                    }
                    await self._apply_pending_cancel()
                    permits_by_node = {permit.request.node_id: permit for permit in permits}
                    outcomes = await asyncio.gather(
                        *(
                            self._execute_admitted_claim(
                                task_id,
                                nodes_by_id[claim.node_id],
                                claim,
                                lease.fencing_token,
                                permits_by_node[claim.node_id],
                            )
                            for claim in claims
                        )
                    )
                    completed_count += sum(outcome for outcome in outcomes)
                    fenced_count += len(outcomes) - sum(outcome for outcome in outcomes)
                finally:
                    self._active_claims = {}
                    await asyncio.gather(*(permit.release() for permit in permits))
                graph = await self._task_service.reduce_effect_dag(
                    task_id,
                    lease_owner_id=self._instance_id,
                    fencing_token=lease.fencing_token,
                )
            if graph.status is EffectGraphStatus.COMPENSATING:
                plan = await self._task_service.plan_effect_dag_compensation(
                    task_id,
                    lease_owner_id=self._instance_id,
                    fencing_token=lease.fencing_token,
                )
            return EffectDagDispatchResult(
                task_id=task_id,
                rounds=rounds,
                claimed=claimed_count,
                completed=completed_count,
                fenced=fenced_count,
                graph_status=graph.status,
                compensation_plan=plan,
            )
        finally:
            self._active_claims = {}
            stop_graph_heartbeat.set()
            await asyncio.gather(graph_heartbeat, return_exceptions=True)
            await self._task_service.release_effect_graph_lease(
                task_id,
                owner_id=self._instance_id,
                fencing_token=lease.fencing_token,
            )
            self._active_graph_fencing_token = None
            self._active_graph_id = None
            self._active_task_id = None

    async def _execute_admitted_claim(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        graph_fencing_token: int,
        permit: EffectDagAdmissionPermitPort,
    ) -> int:
        try:
            return await permit.run(
                self._execute_claim(
                    task_id,
                    node,
                    claim,
                    graph_fencing_token,
                )
            )
        except EffectDagAdmissionPermitLostError:
            return 0

    async def _apply_pending_cancel(self) -> None:
        async with self._cancel_lock:
            reason = self._cancel_reason
            task_id = self._active_task_id
            graph_fencing_token = self._active_graph_fencing_token
            if reason is None or task_id is None or graph_fencing_token is None:
                return
            if not self._cancel_persisted:
                await self._task_service.request_effect_dag_cancel(
                    task_id,
                    lease_owner_id=self._instance_id,
                    fencing_token=graph_fencing_token,
                )
                self._cancel_persisted = True
            cancel = getattr(self._executor, "cancel", None)
            if cancel is None:
                return
            pending = tuple(
                (node, claim)
                for node, claim in self._active_claims.values()
                if (claim.node_id, claim.fencing_token) not in self._cancelled_claims
            )
            for node, claim in pending:
                await cancel(task_id, node, claim, reason=reason)
                self._cancelled_claims.add((claim.node_id, claim.fencing_token))

    async def _resolve_pending_branch_decisions(
        self,
        task_id: str,
        graph: EffectGraphRead,
        graph_fencing_token: int,
    ) -> bool:
        resolver = self._branch_decision_resolver
        if resolver is None:
            return False
        decided = {
            (decision.source_node_id, decision.decision_key) for decision in graph.branch_decisions
        }
        nodes_by_id = {node.node_id: node for node in graph.nodes}
        outcomes_by_key: dict[tuple[str, str], set[str]] = {}
        for edge in graph.edges:
            if (
                edge.kind is not EffectEdgeKind.CONDITIONAL
                or edge.decision_key is None
                or edge.expected_outcome is None
            ):
                continue
            source = nodes_by_id[edge.from_node_id]
            identity = (source.node_id, edge.decision_key)
            if source.status is EffectNodeStatus.SUCCEEDED and identity not in decided:
                outcomes_by_key.setdefault(identity, set()).add(edge.expected_outcome)
        resolved = False
        for (source_node_id, decision_key), outcomes in sorted(
            outcomes_by_key.items(),
            key=lambda item: (
                nodes_by_id[item[0][0]].ordinal,
                item[0][1],
            ),
        ):
            selection = await resolver.resolve(
                task_id,
                nodes_by_id[source_node_id],
                decision_key,
                tuple(sorted(outcomes)),
            )
            if selection is None:
                continue
            await self._task_service.record_effect_dag_branch_decision(
                task_id,
                source_node_id,
                decision_key=decision_key,
                outcome=selection.outcome,
                evidence_digest=selection.evidence_digest,
                lease_owner_id=self._instance_id,
                fencing_token=graph_fencing_token,
            )
            resolved = True
        return resolved

    async def _execute_claim(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        graph_fencing_token: int,
    ) -> int:
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_node_claim(
                task_id,
                claim,
                graph_fencing_token,
                stop_heartbeat,
            ),
            name=f"dag-node-heartbeat:{claim.node_id}",
        )
        try:
            try:
                outcome = await self._executor.execute(task_id, node, claim)
            except Exception:
                outcome = EffectNodeExecutionResult(
                    status=EffectNodeStatus.UNKNOWN,
                    error_code="DAG_RUNNER_OUTCOME_UNKNOWN",
                )
            if heartbeat.done():
                heartbeat.result()
            if not outcome.transition_committed:
                await self._task_service.transition_claimed_effect_node(
                    task_id,
                    claim.node_id,
                    expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
                    target_status=outcome.status,
                    transition_kind=f"runner_{outcome.status.value}",
                    event_type=f"effect.node.{outcome.status.value}",
                    claim_owner_id=claim.owner_id,
                    node_fencing_token=claim.fencing_token,
                    lease_owner_id=self._instance_id,
                    fencing_token=graph_fencing_token,
                )
            return 1
        except (EffectGraphFenceRejectedError, EffectNodeFenceRejectedError):
            return 0
        finally:
            stop_heartbeat.set()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _renew_node_claim(
        self,
        task_id: str,
        claim: EffectNodeClaimRead,
        graph_fencing_token: int,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._node_claim_ttl_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                await self._task_service.renew_effect_dag_node_claim(
                    task_id,
                    claim.node_id,
                    claim_owner_id=claim.owner_id,
                    node_fencing_token=claim.fencing_token,
                    claim_ttl_seconds=self._node_claim_ttl_seconds,
                    lease_owner_id=self._instance_id,
                    fencing_token=graph_fencing_token,
                )

    async def _renew_graph_lease(
        self,
        task_id: str,
        graph_fencing_token: int,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._graph_lease_ttl_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                await self._task_service.renew_effect_graph_lease(
                    task_id,
                    owner_id=self._instance_id,
                    fencing_token=graph_fencing_token,
                    ttl_seconds=self._graph_lease_ttl_seconds,
                )
