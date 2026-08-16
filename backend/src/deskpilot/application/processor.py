"""Cooperatively controllable task processor using Model Gateway and Runner ports."""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypedDict, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from deskpilot.application.effect_dag_admission import (
    EffectDagAdmissionController,
    EffectDagAdmissionControllerPort,
)
from deskpilot.application.effect_dag_dispatcher import EffectDagDispatcher
from deskpilot.application.effect_graph_control_router import (
    EffectGraphControlOwnerUnavailableError,
)
from deskpilot.application.model_gateway import ModelGateway, ModelGatewayError
from deskpilot.application.policy_engine import PolicyEngine
from deskpilot.application.runner_client import RunnerClientError
from deskpilot.application.runner_supervisor import RunnerSupervisor
from deskpilot.application.task_service import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    EffectGraphFenceRejectedError,
    EffectGraphLeaseUnavailableError,
    EffectGraphNotFoundError,
    InvalidEffectTransitionError,
    InvalidTaskTransitionError,
    InvalidToolCallTransitionError,
    TaskNotFoundError,
    TaskService,
    ToolAuthorizationError,
    ToolCallNotFoundError,
    ToolCallStatus,
)
from deskpilot.application.trusted_effect_dag import (
    DiskPressureBranchDecisionResolver,
    DiskPressureGuardedMaterialResolver,
    EffectDagCompensationDispatcher,
    EffectDagLedgerPreparer,
    EffectNodeMaterialResolver,
    FileMoveDagMaterialResolver,
    LedgerBoundEffectNodeExecutor,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import ApprovalRead, ApprovalStatus, DataEgress
from deskpilot.domain.effect_graph import (
    EFFECT_DAG_SCHEMA_VERSION,
    CompensationStrategy,
    EffectAttemptKind,
    EffectAttemptStatus,
    EffectDagBranchCondition,
    EffectDagNodeDefinition,
    EffectExecutionMode,
    EffectGraphStatus,
    EffectNodeDefinition,
    EffectNodeStatus,
    effect_attempt_id,
    effect_call_id,
)
from deskpilot.domain.effect_graph_control import EffectGraphControlClaimRead
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    PrivacyMode,
    StructuredOutputDefinition,
)
from deskpilot.domain.planning import (
    PlanStep,
    TaskClassification,
    TaskComplexity,
    TaskIntent,
    TaskPlan,
)
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.reconciliations import (
    GraphRecoveryAction,
    ReconciliationCompensationRead,
    ReconciliationEvidenceKind,
    ReconciliationEvidenceRefreshRead,
    ReconciliationGraphRecoveryRead,
    ReconciliationOutcome,
)
from deskpilot.domain.schemas import (
    DiskPressureGuardedFileMoveRequest,
    FileMoveCompensationRequest,
    FileMoveDagRequest,
    FileMoveSagaRequest,
    FileMoveTaskRequest,
    TaskRead,
    TaskStatus,
)
from deskpilot.domain.task_checkpoints import (
    DurableTaskCheckpoint,
    TaskCheckpointPayload,
    initial_tool_call_id,
)
from deskpilot.model_providers.fake import (
    TASK_CLASSIFICATION_SCHEMA,
    TASK_PLAN_SCHEMA,
)
from deskpilot.runner.executor import ToolExecutorError
from deskpilot.tools.computer import (
    DISK_USAGE_CONTRACT,
    DiskUsageInput,
    project_disk_usage_resources,
)
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FILE_MOVE_SOURCE_CAPABILITY,
    FileMoveInput,
    normalize_file_move_input,
    project_file_move_resources,
    read_file_version,
)

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)
logger = logging.getLogger(__name__)
FileMoveRuntimeRequest = (
    FileMoveTaskRequest
    | FileMoveSagaRequest
    | FileMoveDagRequest
    | DiskPressureGuardedFileMoveRequest
    | FileMoveCompensationRequest
)


class ReconciliationCompensationResourceConflictError(ValueError):
    code = "RECONCILIATION_COMPENSATION_RESOURCE_CONFLICT"

    def __init__(self, reconciliation_id: str) -> None:
        super().__init__(f"Reconciliation {reconciliation_id} reverse resources no longer match")
        self.reconciliation_id = reconciliation_id


@dataclass(slots=True)
class _TaskRuntime:
    task_id: str
    goal: str
    privacy_mode: PrivacyMode
    constraints: tuple[str, ...]
    tool_request: FileMoveRuntimeRequest | None = None
    next_stage: int = 0
    stop_requested: bool = False
    stop_signal: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    tool_call_id: str = ""
    classification: TaskClassification | None = None
    plan: TaskPlan | None = None
    planner_provider_id: str | None = None
    tool_arguments: dict[str, object] | None = None
    tool_resources: tuple[PolicyResource, ...] = ()
    expected_resource_versions: dict[str, str] = field(default_factory=dict)
    tool_idempotency_key: str | None = None
    policy_request: ToolAuthorizationRequest | None = None
    policy_decision: PolicyDecision | None = None
    approval_id: str | None = None
    dag_approval_ids: tuple[str, ...] = ()
    approval_expiry_worker: asyncio.Task[None] | None = None
    dag_approval_expiry_workers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    graph_id: str | None = None
    graph_schema_version: str | None = None
    graph_fencing_token: int | None = None
    graph_lease_worker: asyncio.Task[None] | None = None
    graph_lease_lost: bool = False
    current_node_id: str | None = None
    current_node_index: int = 0
    execution_mode: EffectExecutionMode = EffectExecutionMode.FORWARD
    failure_node_id: str | None = None
    current_attempt_id: str | None = None
    reconciled_call_id: str | None = None
    reconciled_outcome: ReconciliationOutcome | None = None
    dag_dispatcher: EffectDagDispatcher | None = None


class _StageDisposition(StrEnum):
    CONTINUE = "continue"
    SUSPEND = "suspend"
    TERMINAL = "terminal"


class _EffectFence(TypedDict):
    lease_owner_id: str
    fencing_token: int


class EffectGraphControlRoutePort(Protocol):
    async def request_cancel(
        self,
        task_id: str,
        *,
        reason: str | None,
    ) -> bool: ...


class ApprovalContinuationState(StrEnum):
    READY = "ready"
    IN_PROGRESS = "in_progress"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TaskRuntimeRecoveryResult:
    restored_task_ids: frozenset[str] = frozenset()
    recoverable_requested_call_ids: frozenset[str] = frozenset()
    failed_task_ids: frozenset[str] = frozenset()
    contended_task_ids: frozenset[str] = frozenset()


class TaskProcessor:
    """Runs Provider-generated stages and stops between durable event checkpoints."""

    _DELAY_AFTER_STAGES = frozenset({0, 2, 5})

    def __init__(
        self,
        task_service: TaskService,
        model_gateway: ModelGateway,
        policy_engine: PolicyEngine,
        runner_client: RunnerSupervisor,
        step_delay_seconds: float,
        disk_usage_path: str,
        model_timeout_seconds: float,
        instance_id: str,
        graph_lease_ttl_seconds: float,
        effect_dag_admission: EffectDagAdmissionControllerPort | None = None,
        effect_dag_max_concurrency: int = 4,
        effect_dag_ready_page_size: int = 64,
    ) -> None:
        if not 1 <= effect_dag_max_concurrency <= 32:
            raise ValueError("Effect DAG processor concurrency is invalid")
        if not 1 <= effect_dag_ready_page_size <= 1_000:
            raise ValueError("Effect DAG processor ready page size is invalid")
        self._task_service = task_service
        self._model_gateway = model_gateway
        self._policy_engine = policy_engine
        self._runner_client = runner_client
        self._step_delay_seconds = step_delay_seconds
        self._disk_usage_path = str(Path(disk_usage_path).expanduser().resolve(strict=True))
        self._model_timeout_seconds = model_timeout_seconds
        self._instance_id = instance_id
        self._dag_owner_id = f"{instance_id[:58]}:dag"
        self._graph_lease_ttl_seconds = graph_lease_ttl_seconds
        self._effect_dag_max_concurrency = effect_dag_max_concurrency
        self._effect_dag_ready_page_size = effect_dag_ready_page_size
        self._effect_dag_admission = effect_dag_admission or EffectDagAdmissionController(
            global_limit=effect_dag_max_concurrency,
            per_graph_limit=effect_dag_max_concurrency,
            default_tool_limit=effect_dag_max_concurrency,
        )
        self._runtimes: dict[str, _TaskRuntime] = {}
        self._recovered_autostart: set[str] = set()
        self._effect_graph_control_router: EffectGraphControlRoutePort | None = None

    @property
    def dag_owner_id(self) -> str:
        return self._dag_owner_id

    def bind_effect_graph_control_router(
        self,
        router: EffectGraphControlRoutePort,
    ) -> None:
        if self._effect_graph_control_router is not None:
            raise RuntimeError("Effect graph control router is already bound")
        self._effect_graph_control_router = router

    def start(
        self,
        task_id: str,
        goal: str,
        *,
        privacy_mode: PrivacyMode,
        constraints: tuple[str, ...],
        tool_request: FileMoveRuntimeRequest | None = None,
    ) -> None:
        if task_id in self._runtimes:
            raise RuntimeError(f"Task processor already knows {task_id}")
        runtime = _TaskRuntime(
            task_id=task_id,
            goal=goal,
            privacy_mode=privacy_mode,
            constraints=constraints,
            tool_request=tool_request,
            tool_call_id=initial_tool_call_id(task_id),
        )
        self._runtimes[task_id] = runtime
        self._start_worker(runtime)

    async def prepare_durable_recovery(self) -> TaskRuntimeRecoveryResult:
        """Rebuild only checkpoints whose event/task/call bindings still agree."""
        loaded = await self._task_service.load_task_checkpoints()
        restored: set[str] = set()
        recoverable_calls: set[str] = set()
        failed: set[str] = set()
        contended: set[str] = set()

        for task_id in loaded.invalid_task_ids:
            try:
                await self._task_service.acquire_effect_graph_lease(
                    task_id,
                    owner_id=self._instance_id,
                    ttl_seconds=self._graph_lease_ttl_seconds,
                )
            except EffectGraphNotFoundError:
                pass
            except EffectGraphLeaseUnavailableError:
                contended.add(task_id)
                continue
            await self._task_service.fail_task_checkpoint(
                task_id,
                code="TASK_CHECKPOINT_INVALID",
            )
            failed.add(task_id)

        for durable in loaded.checkpoints:
            task_id = durable.payload.task_id
            try:
                task = await self._task_service.get_task(task_id)
                if task.status is TaskStatus.WAITING_RECONCILIATION:
                    continue
                lease = None
                if (
                    durable.payload.graph_id is not None
                    and durable.payload.graph_schema_version != EFFECT_DAG_SCHEMA_VERSION
                ):
                    try:
                        lease = await self._task_service.acquire_effect_graph_lease(
                            task_id,
                            owner_id=self._instance_id,
                            ttl_seconds=self._graph_lease_ttl_seconds,
                        )
                    except EffectGraphLeaseUnavailableError:
                        contended.add(task_id)
                        continue
                runtime = await self._runtime_from_checkpoint(durable, task)
                if lease is not None:
                    runtime.graph_fencing_token = lease.fencing_token
            except Exception:
                logger.exception(
                    "Rejected durable task checkpoint for %s during recovery",
                    task_id,
                )
                await self._task_service.fail_task_checkpoint(
                    task_id,
                    code="TASK_CHECKPOINT_BINDING_INVALID",
                )
                failed.add(task_id)
                continue
            if task_id in self._runtimes:
                raise RuntimeError(f"Task processor already knows {task_id}")
            self._runtimes[task_id] = runtime
            if runtime.graph_fencing_token is not None:
                self._start_graph_lease_worker(runtime)
            restored.add(task_id)
            if runtime.next_stage in {5, 6}:
                recoverable_calls.add(runtime.tool_call_id)
            if runtime.dag_approval_ids:
                approvals = await asyncio.gather(
                    *(
                        self._task_service.get_approval(approval_id)
                        for approval_id in runtime.dag_approval_ids
                    )
                )
                recoverable_calls.update(approval.call_id for approval in approvals)
            if task.status in {
                TaskStatus.CREATED,
                TaskStatus.CLASSIFYING,
                TaskStatus.RUNNING,
            }:
                self._recovered_autostart.add(task_id)
            elif task.status is TaskStatus.WAITING_APPROVAL:
                if runtime.approval_id is None and not runtime.dag_approval_ids:
                    raise RuntimeError("Recovered approval task has no approval identity")
                if runtime.approval_id is not None:
                    approval = await self._task_service.get_approval(runtime.approval_id)
                    if approval.status is ApprovalStatus.PENDING:
                        self._schedule_approval_expiry(runtime, approval)
                elif runtime.dag_approval_ids:
                    await self._schedule_dag_approval_expiries(runtime)

        return TaskRuntimeRecoveryResult(
            restored_task_ids=frozenset(restored),
            recoverable_requested_call_ids=frozenset(recoverable_calls),
            failed_task_ids=frozenset(failed),
            contended_task_ids=frozenset(contended),
        )

    def activate_durable_recovery(self) -> None:
        """Start proven non-paused checkpoints after startup reconciliation."""
        task_ids = tuple(sorted(self._recovered_autostart))
        self._recovered_autostart.clear()
        for task_id in task_ids:
            runtime = self._runtimes.get(task_id)
            if runtime is not None and (runtime.worker is None or runtime.worker.done()):
                self._start_worker(runtime)

    async def pause(self, task_id: str) -> None:
        await self._request_stop(task_id)

    async def cancel(self, task_id: str, *, reason: str | None = None) -> None:
        runtime = self._runtimes.get(task_id)
        if runtime is None:
            router = self._effect_graph_control_router
            if router is not None:
                try:
                    await router.request_cancel(task_id, reason=reason)
                except EffectGraphNotFoundError:
                    # A task can be cancelled before it has materialized an effect
                    # graph. There is no graph owner or Runner call to route to in
                    # that case; the API layer still performs the durable task
                    # transition below.
                    pass
            return
        runtime.stop_requested = True
        runtime.stop_signal.set()
        worker = runtime.worker
        dispatcher = runtime.dag_dispatcher
        if dispatcher is not None:
            await dispatcher.request_cancel(
                task_id,
                reason=reason or "Task cancellation requested",
            )
        if worker is not None and not worker.done():
            await worker
        elif runtime.graph_schema_version == EFFECT_DAG_SCHEMA_VERSION:
            await self._cancel_inactive_effect_dag(runtime)
        if runtime.approval_id is not None and runtime.next_stage == 6:
            return
        try:
            await self._task_service.finish_tool_call(
                task_id,
                runtime.tool_call_id,
                status=ToolCallStatus.CANCELLED,
                error_code="TOOL_CALL_CANCELLED_BEFORE_DISPATCH",
                resolution_source="control_plane",
                fail_task=False,
            )
        except (ToolCallNotFoundError, InvalidToolCallTransitionError):
            pass

    async def apply_effect_graph_control(
        self,
        control: EffectGraphControlClaimRead,
    ) -> None:
        """Apply a routed cancel only under its exact live owner generation."""
        if control.target_owner_id != self._dag_owner_id:
            raise EffectGraphControlOwnerUnavailableError(control.control_id)
        graph_fencing_token = control.target_fencing_token
        if graph_fencing_token is None:
            raise EffectGraphControlOwnerUnavailableError(control.control_id)
        runtime = self._runtimes.get(control.task_id)
        if runtime is None:
            raise EffectGraphControlOwnerUnavailableError(control.control_id)
        dispatcher = runtime.dag_dispatcher
        if dispatcher is not None:
            runtime.stop_requested = True
            runtime.stop_signal.set()
            await dispatcher.request_cancel(
                control.task_id,
                reason=control.reason or "Task cancellation requested",
                expected_graph_fencing_token=graph_fencing_token,
            )
        else:
            await self._task_service.request_effect_dag_cancel(
                control.task_id,
                lease_owner_id=self._dag_owner_id,
                fencing_token=graph_fencing_token,
            )
            await self._task_service.reduce_effect_dag(
                control.task_id,
                lease_owner_id=self._dag_owner_id,
                fencing_token=graph_fencing_token,
            )
            runtime.stop_requested = True
            runtime.stop_signal.set()
        worker = runtime.worker
        if worker is not None and not worker.done():
            await worker

    def can_resume(self, task_id: str) -> bool:
        runtime = self._runtimes.get(task_id)
        return (
            runtime is not None
            and runtime.approval_id is None
            and not runtime.dag_approval_ids
            and runtime.next_stage < self._stage_count
            and (runtime.worker is None or runtime.worker.done())
        )

    def has_runtime(self, task_id: str) -> bool:
        """Return whether this process already owns the task's runtime checkpoint."""
        return task_id in self._runtimes

    def accepts_trusted_dag_approval(self, task_id: str) -> bool:
        runtime = self._runtimes.get(task_id)
        return (
            runtime is not None
            and isinstance(
                runtime.tool_request,
                (FileMoveDagRequest, DiskPressureGuardedFileMoveRequest),
            )
            and runtime.next_stage == 3
        )

    async def refresh_reconciliation_evidence(
        self,
        reconciliation_id: str,
    ) -> ReconciliationEvidenceRefreshRead:
        """Query signed Runner state and persist evidence without replaying the call."""
        reconciliation = await self._task_service.get_reconciliation(reconciliation_id)
        queried_runner_id: str | None = None
        try:
            lease = self._runner_client.ensure_ready()
            queried_runner_id = lease.runner_id
            receipt = await lease.client.get_commit_receipt(reconciliation.call_id)
        except Exception as error:
            return await self._task_service.record_reconciliation_evidence(
                reconciliation_id,
                kind=ReconciliationEvidenceKind.QUERY_FAILED,
                queried_runner_id=queried_runner_id,
                error_code=self._receipt_query_error_code(error),
            )

        if receipt is None:
            return await self._task_service.record_reconciliation_evidence(
                reconciliation_id,
                kind=ReconciliationEvidenceKind.NO_RECEIPT,
                queried_runner_id=queried_runner_id,
            )
        try:
            return await self._task_service.record_reconciliation_evidence(
                reconciliation_id,
                kind=ReconciliationEvidenceKind.COMMIT_RECEIPT,
                queried_runner_id=queried_runner_id,
                commit_receipt=receipt,
            )
        except ToolAuthorizationError:
            return await self._task_service.record_reconciliation_evidence(
                reconciliation_id,
                kind=ReconciliationEvidenceKind.QUERY_FAILED,
                queried_runner_id=queried_runner_id,
                error_code="RUNNER_COMMIT_RECEIPT_BINDING_INVALID",
            )

    async def create_reconciliation_compensation(
        self,
        reconciliation_id: str,
        *,
        idempotency_key: str,
    ) -> ReconciliationCompensationRead:
        """Preflight and start one server-derived reverse file.move task."""
        replay = await self._task_service.replay_reconciliation_compensation(
            reconciliation_id,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            if replay.task.status is TaskStatus.CREATED and not self.has_runtime(
                replay.task.task_id
            ):
                request = await self._task_service.get_reconciliation_compensation_request(
                    reconciliation_id
                )
                self.start(
                    replay.task.task_id,
                    replay.task.goal,
                    privacy_mode=replay.task.privacy_mode,
                    constraints=tuple(replay.task.constraints),
                    tool_request=request,
                )
            return replay

        request = await self._task_service.get_reconciliation_compensation_request(
            reconciliation_id
        )
        try:
            normalized = await asyncio.to_thread(
                normalize_file_move_input,
                FileMoveInput(
                    source=request.source,
                    destination=request.destination,
                ),
            )
            current_version = await asyncio.to_thread(
                read_file_version,
                normalized.source,
            )
        except (OSError, ToolExecutorError) as error:
            raise ReconciliationCompensationResourceConflictError(reconciliation_id) from error
        if current_version != request.expected_source_version:
            raise ReconciliationCompensationResourceConflictError(reconciliation_id)
        normalized_request = request.model_copy(
            update={
                "source": normalized.source,
                "destination": normalized.destination,
            }
        )
        result = await self._task_service.create_reconciliation_compensation(
            reconciliation_id,
            request=normalized_request,
            idempotency_key=idempotency_key,
        )
        if result.task.status is TaskStatus.CREATED and not self.has_runtime(result.task.task_id):
            self.start(
                result.task.task_id,
                result.task.goal,
                privacy_mode=result.task.privacy_mode,
                constraints=tuple(result.task.constraints),
                tool_request=normalized_request,
            )
        return result

    async def recover_reconciliation_graph(
        self,
        reconciliation_id: str,
        *,
        action: GraphRecoveryAction,
        idempotency_key: str,
    ) -> ReconciliationGraphRecoveryRead:
        """Apply the verdict and reactivate only the checkpoint proven by that commit."""
        result = await self._task_service.recover_reconciliation_graph(
            reconciliation_id,
            action=action,
            idempotency_key=idempotency_key,
            lease_owner_id=self._instance_id,
            lease_ttl_seconds=self._graph_lease_ttl_seconds,
        )
        if not result.resumed:
            await self._task_service.release_effect_graph_lease(
                result.task.task_id,
                owner_id=self._instance_id,
                fencing_token=result.graph.fencing_token,
            )
            return result
        if result.task.task_id in self._runtimes:
            return result
        loaded = await self._task_service.load_task_checkpoints()
        durable = next(
            (
                checkpoint
                for checkpoint in loaded.checkpoints
                if checkpoint.payload.task_id == result.task.task_id
            ),
            None,
        )
        if durable is None:
            raise TaskRuntimeUnavailableError(result.task.task_id)
        runtime = await self._runtime_from_checkpoint(durable, result.task)
        runtime.graph_fencing_token = result.graph.fencing_token
        self._runtimes[result.task.task_id] = runtime
        self._start_graph_lease_worker(runtime)
        self._start_worker(runtime)
        return result

    @staticmethod
    def _receipt_query_error_code(error: Exception) -> str:
        code = getattr(error, "code", None)
        if (
            isinstance(code, str)
            and 1 <= len(code) <= 100
            and code == code.upper()
            and all(character.isalnum() or character == "_" for character in code)
        ):
            return code
        return "RUNNER_COMMIT_RECEIPT_QUERY_FAILED"

    def resume(self, task_id: str) -> None:
        runtime = self._runtimes.get(task_id)
        if runtime is None or not self.can_resume(task_id):
            raise TaskRuntimeUnavailableError(task_id)
        runtime.stop_requested = False
        runtime.stop_signal.clear()
        self._start_worker(runtime)

    def can_continue_after_approval(self, task_id: str, approval_id: str) -> bool:
        return (
            self.approval_continuation_state(task_id, approval_id)
            is ApprovalContinuationState.READY
        )

    def approval_continuation_state(
        self,
        task_id: str,
        approval_id: str,
    ) -> ApprovalContinuationState:
        runtime = self._runtimes.get(task_id)
        if runtime is None:
            return ApprovalContinuationState.UNAVAILABLE
        legacy_match = runtime.approval_id == approval_id and runtime.next_stage == 6
        dag_match = (
            isinstance(
                runtime.tool_request,
                (FileMoveDagRequest, DiskPressureGuardedFileMoveRequest),
            )
            and approval_id in runtime.dag_approval_ids
            and runtime.next_stage == 3
        )
        if not legacy_match and not dag_match:
            return ApprovalContinuationState.UNAVAILABLE
        if runtime.worker is None or runtime.worker.done():
            return ApprovalContinuationState.READY
        return ApprovalContinuationState.IN_PROGRESS

    def continue_after_approval(self, task_id: str, approval_id: str) -> None:
        runtime = self._runtimes.get(task_id)
        if runtime is None or not self.can_continue_after_approval(task_id, approval_id):
            raise TaskRuntimeUnavailableError(task_id)
        self._cancel_approval_expiry(runtime)
        if approval_id in runtime.dag_approval_ids:
            runtime.dag_approval_ids = ()
        runtime.stop_requested = False
        runtime.stop_signal.clear()
        self._start_worker(runtime)

    def forget(self, task_id: str) -> None:
        runtime = self._runtimes.pop(task_id, None)
        if runtime is not None:
            self._cancel_approval_expiry(runtime)
            self._cancel_graph_lease_worker(runtime)
            if runtime.graph_fencing_token is not None:
                asyncio.create_task(
                    self._release_graph_lease(runtime),
                    name=f"graph-lease-release:{task_id}",
                )
        self._model_gateway.forget_task_budget(task_id)

    async def shutdown(self) -> None:
        owned_runtimes = list(self._runtimes.values())
        active = [
            runtime
            for runtime in owned_runtimes
            if runtime.worker is not None and not runtime.worker.done()
        ]
        for runtime in active:
            runtime.stop_requested = True
            runtime.stop_signal.set()
        for runtime in owned_runtimes:
            self._cancel_approval_expiry(runtime)
            self._cancel_graph_lease_worker(runtime)
        if active:
            await asyncio.gather(
                *(runtime.worker for runtime in active if runtime.worker is not None),
                return_exceptions=True,
            )
        for runtime in active:
            try:
                await self._task_service.transition_task(
                    runtime.task_id,
                    TaskStatus.PAUSED,
                    command="pause",
                    reason="API shutdown",
                    requested_by="system",
                )
            except (InvalidTaskTransitionError, TaskNotFoundError):
                pass
        await asyncio.gather(
            *(
                self._release_graph_lease(runtime)
                for runtime in owned_runtimes
                if runtime.graph_fencing_token is not None
            ),
            return_exceptions=True,
        )

    async def _runtime_from_checkpoint(
        self,
        durable: DurableTaskCheckpoint,
        task: TaskRead,
    ) -> _TaskRuntime:
        payload = durable.payload
        if durable.event_seq != task.last_event_seq:
            raise RuntimeError("Task checkpoint event binding is stale")
        graph = None
        if payload.graph_id is None:
            if payload.tool_call_id != initial_tool_call_id(task.task_id):
                raise RuntimeError("Task checkpoint call identity is invalid")
        else:
            graph = await self._task_service.get_effect_graph(task.task_id)
            if payload.graph_schema_version == EFFECT_DAG_SCHEMA_VERSION:
                if (
                    graph.graph_id != payload.graph_id
                    or graph.schema_version != EFFECT_DAG_SCHEMA_VERSION
                    or payload.current_node_id is not None
                    or payload.graph_fencing_token is not None
                    or payload.tool_call_id != "dag-unbound"
                ):
                    raise RuntimeError("Task checkpoint DAG binding is invalid")
            elif (
                graph.graph_id != payload.graph_id
                or graph.schema_version != payload.graph_schema_version
                or payload.current_node_id is None
                or payload.current_node_index >= len(graph.nodes)
                or graph.nodes[payload.current_node_index].node_id != payload.current_node_id
                or graph.execution_mode is not payload.execution_mode
            ):
                raise RuntimeError("Task checkpoint effect graph binding is invalid")
            if (
                payload.next_stage >= 3
                and payload.graph_schema_version != EFFECT_DAG_SCHEMA_VERSION
            ):
                if payload.current_node_id is None:
                    raise RuntimeError("Task checkpoint effect node identity is missing")
                expected_call_id = effect_call_id(
                    payload.current_node_id,
                    (
                        EffectAttemptKind.COMPENSATION
                        if payload.execution_mode is EffectExecutionMode.COMPENSATING
                        else EffectAttemptKind.FORWARD
                    ),
                )
                if payload.tool_call_id != expected_call_id:
                    raise RuntimeError("Task checkpoint effect call identity is invalid")
        if payload.next_stage == 0:
            allowed_statuses = {TaskStatus.CREATED}
        elif payload.next_stage in {1, 2}:
            allowed_statuses = {TaskStatus.CLASSIFYING}
        else:
            allowed_statuses = {
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
                TaskStatus.WAITING_APPROVAL,
            }
        if task.status not in allowed_statuses:
            raise RuntimeError("Task checkpoint stage does not match task status")

        if (
            graph is not None
            and payload.next_stage >= 3
            and payload.graph_schema_version != EFFECT_DAG_SCHEMA_VERSION
        ):
            node = graph.nodes[payload.current_node_index]
            allowed_node_statuses: dict[int, frozenset[EffectNodeStatus]] = {
                3: frozenset(
                    {
                        EffectNodeStatus.PENDING,
                        EffectNodeStatus.SUCCEEDED,
                    }
                ),
                4: frozenset(
                    {
                        EffectNodeStatus.ACTIVE,
                        EffectNodeStatus.COMPENSATING,
                    }
                ),
                5: frozenset(
                    {
                        EffectNodeStatus.ACTIVE,
                        EffectNodeStatus.COMPENSATING,
                    }
                ),
                6: frozenset(
                    {
                        EffectNodeStatus.ACTIVE,
                        EffectNodeStatus.COMPENSATING,
                        EffectNodeStatus.WAITING_APPROVAL,
                    }
                ),
                7: frozenset(
                    {
                        EffectNodeStatus.SUCCEEDED,
                        EffectNodeStatus.COMPENSATED,
                    }
                ),
                8: frozenset({EffectNodeStatus.SUCCEEDED}),
            }
            if node.status not in allowed_node_statuses[payload.next_stage]:
                raise RuntimeError("Task checkpoint node transition is invalid")

        if payload.next_stage >= 5:
            call_status = await self._task_service.get_tool_call_status(
                task.task_id,
                payload.tool_call_id,
            )
            expected_call_status = (
                ToolCallStatus.REQUESTED
                if payload.next_stage in {5, 6}
                else ToolCallStatus.SUCCEEDED
            )
            reconciled_success = (
                call_status is ToolCallStatus.UNKNOWN
                and payload.reconciled_call_id == payload.tool_call_id
                and payload.reconciled_outcome is ReconciliationOutcome.CONFIRMED_SUCCEEDED
                and payload.next_stage >= 7
            )
            if call_status is not expected_call_status and not reconciled_success:
                raise RuntimeError("Task checkpoint does not match Tool call status")

        if payload.next_stage >= 6:
            request = payload.policy_request
            decision = payload.policy_decision
            if request is None or decision is None:
                raise RuntimeError("Task checkpoint omitted policy facts")
            if (
                request.task_id != task.task_id
                or request.call_id != payload.tool_call_id
                or decision.request_digest != request.request_digest
            ):
                raise RuntimeError("Task checkpoint policy binding is invalid")
            if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                if payload.approval_id is None:
                    raise RuntimeError("Task checkpoint omitted approval identity")
                approval = await self._task_service.get_approval(payload.approval_id)
                if approval.task_id != task.task_id or approval.call_id != payload.tool_call_id:
                    raise RuntimeError("Task checkpoint approval binding is invalid")
                if payload.next_stage == 6:
                    if approval.status is ApprovalStatus.PENDING:
                        if task.status is not TaskStatus.WAITING_APPROVAL:
                            raise RuntimeError("Pending approval task status is invalid")
                    elif approval.status is ApprovalStatus.APPROVED:
                        if approval.consumed_at is not None:
                            raise RuntimeError("Consumed approval cannot resume a requested call")
                        if task.status is not TaskStatus.RUNNING:
                            raise RuntimeError("Approved checkpoint task status is invalid")
                    else:
                        raise RuntimeError("Resolved approval cannot resume")
                elif (
                    approval.status is not ApprovalStatus.APPROVED
                    or approval.consumed_at is None
                    or task.status not in {TaskStatus.RUNNING, TaskStatus.PAUSED}
                ):
                    raise RuntimeError("Completed Tool checkpoint approval binding is invalid")
            elif payload.approval_id is not None or task.status is TaskStatus.WAITING_APPROVAL:
                raise RuntimeError("Non-approval checkpoint contains approval state")

        return _TaskRuntime(
            task_id=task.task_id,
            goal=task.goal,
            privacy_mode=task.privacy_mode,
            constraints=tuple(task.constraints),
            tool_request=payload.tool_request,
            next_stage=payload.next_stage,
            tool_call_id=payload.tool_call_id,
            classification=payload.classification,
            plan=payload.plan,
            planner_provider_id=payload.planner_provider_id,
            tool_arguments=(
                dict(payload.tool_arguments) if payload.tool_arguments is not None else None
            ),
            tool_resources=payload.tool_resources,
            expected_resource_versions=dict(payload.expected_resource_versions),
            tool_idempotency_key=payload.tool_idempotency_key,
            policy_request=payload.policy_request,
            policy_decision=payload.policy_decision,
            approval_id=payload.approval_id,
            dag_approval_ids=payload.dag_approval_ids,
            graph_id=payload.graph_id,
            graph_schema_version=payload.graph_schema_version,
            graph_fencing_token=payload.graph_fencing_token,
            current_node_id=payload.current_node_id,
            current_node_index=payload.current_node_index,
            execution_mode=payload.execution_mode,
            failure_node_id=payload.failure_node_id,
            current_attempt_id=(
                effect_attempt_id(
                    payload.current_node_id,
                    (
                        EffectAttemptKind.COMPENSATION
                        if payload.execution_mode is EffectExecutionMode.COMPENSATING
                        else EffectAttemptKind.FORWARD
                    ),
                )
                if payload.current_node_id is not None
                else None
            ),
            reconciled_call_id=payload.reconciled_call_id,
            reconciled_outcome=payload.reconciled_outcome,
        )

    @staticmethod
    def _checkpoint(runtime: _TaskRuntime) -> TaskCheckpointPayload:
        arguments = None
        if runtime.tool_arguments is not None:
            if not all(isinstance(value, str) for value in runtime.tool_arguments.values()):
                raise RuntimeError("Current checkpoint version only supports string arguments")
            arguments = {
                key: value
                for key, value in runtime.tool_arguments.items()
                if isinstance(value, str)
            }
        return TaskCheckpointPayload(
            task_id=runtime.task_id,
            next_stage=runtime.next_stage,
            tool_call_id=runtime.tool_call_id,
            tool_request=runtime.tool_request,
            classification=runtime.classification,
            plan=runtime.plan,
            planner_provider_id=runtime.planner_provider_id,
            tool_arguments=arguments,
            tool_resources=runtime.tool_resources,
            expected_resource_versions=dict(runtime.expected_resource_versions),
            tool_idempotency_key=runtime.tool_idempotency_key,
            policy_request=runtime.policy_request,
            policy_decision=runtime.policy_decision,
            approval_id=runtime.approval_id,
            dag_approval_ids=runtime.dag_approval_ids,
            graph_id=runtime.graph_id,
            graph_schema_version=runtime.graph_schema_version,
            graph_fencing_token=runtime.graph_fencing_token,
            current_node_id=runtime.current_node_id,
            current_node_index=runtime.current_node_index,
            execution_mode=runtime.execution_mode,
            failure_node_id=runtime.failure_node_id,
            reconciled_call_id=runtime.reconciled_call_id,
            reconciled_outcome=runtime.reconciled_outcome,
        )

    @property
    def _stage_count(self) -> int:
        return 9

    def _start_worker(self, runtime: _TaskRuntime) -> None:
        worker = asyncio.create_task(
            self._run(runtime),
            name=f"fake-task:{runtime.task_id}",
        )
        runtime.worker = worker

    async def _request_stop(self, task_id: str) -> None:
        runtime = self._runtimes.get(task_id)
        if runtime is None or runtime.worker is None or runtime.worker.done():
            return
        runtime.stop_requested = True
        runtime.stop_signal.set()
        await runtime.worker

    async def _run(self, runtime: _TaskRuntime) -> None:
        try:
            while runtime.next_stage < self._stage_count:
                if runtime.stop_requested:
                    return
                current_stage = runtime.next_stage
                disposition = await self._run_stage(runtime, current_stage)
                if (
                    runtime.next_stage == current_stage
                    and disposition is not _StageDisposition.SUSPEND
                ):
                    runtime.next_stage += 1
                if (
                    disposition is not _StageDisposition.TERMINAL
                    and runtime.next_stage < self._stage_count
                ):
                    await self._task_service.save_task_checkpoint(self._checkpoint(runtime))
                if disposition is _StageDisposition.SUSPEND:
                    if runtime.dag_approval_ids:
                        dag_approvals = await asyncio.gather(
                            *(
                                self._task_service.get_approval(approval_id)
                                for approval_id in runtime.dag_approval_ids
                            )
                        )
                        if all(
                            approval.status is ApprovalStatus.APPROVED for approval in dag_approvals
                        ):
                            self._cancel_approval_expiry(runtime)
                            runtime.dag_approval_ids = ()
                            continue
                    if runtime.approval_id is not None:
                        approval = await self._task_service.get_approval(runtime.approval_id)
                        if approval.status is ApprovalStatus.APPROVED:
                            self._cancel_approval_expiry(runtime)
                            continue
                    return
                if disposition is _StageDisposition.TERMINAL:
                    self.forget(runtime.task_id)
                    return
                if runtime.next_stage >= self._stage_count:
                    self.forget(runtime.task_id)
                    return
                if current_stage in self._DELAY_AFTER_STAGES:
                    await self._interruptible_delay(runtime)
        except _ToolCallAlreadyFinalizedError:
            self.forget(runtime.task_id)
        except (EffectGraphFenceRejectedError, EffectGraphLeaseUnavailableError):
            self.forget(runtime.task_id)
        except Exception:
            logger.exception("Task processor failed for %s", runtime.task_id)
            self.forget(runtime.task_id)
            try:
                await self._task_service.append_event(
                    runtime.task_id,
                    "task.failed",
                    {
                        "error_type": "TaskProcessingError",
                        "code": "TASK_PROCESSING_FAILED",
                        "message": "Task processing failed before completion.",
                    },
                    new_status=TaskStatus.FAILED,
                )
            except InvalidTaskTransitionError:
                pass

    async def _run_stage(
        self,
        runtime: _TaskRuntime,
        stage: int,
    ) -> _StageDisposition:
        task_id = runtime.task_id
        if stage == 0:
            await self._task_service.transition_task(
                task_id,
                TaskStatus.CLASSIFYING,
                command="processor",
                requested_by="system",
            )
        elif stage == 1:
            if runtime.tool_request is not None:
                is_compensation = isinstance(
                    runtime.tool_request,
                    FileMoveCompensationRequest,
                )
                if isinstance(runtime.tool_request, DiskPressureGuardedFileMoveRequest):
                    classification, plan = self._disk_pressure_guarded_file_move_plan(
                        runtime.tool_request
                    )
                elif isinstance(runtime.tool_request, FileMoveDagRequest):
                    classification, plan = self._explicit_file_move_dag_plan(runtime.tool_request)
                elif isinstance(runtime.tool_request, FileMoveSagaRequest):
                    classification, plan = self._explicit_file_move_saga_plan(runtime.tool_request)
                else:
                    classification, plan = self._explicit_file_move_plan(
                        compensation=is_compensation
                    )
                runtime.classification = classification
                runtime.plan = plan
                await self._task_service.append_event(
                    task_id,
                    "task.classified",
                    {
                        "source": (
                            "explicit_compensation_request"
                            if is_compensation
                            else "explicit_user_request"
                        ),
                        "classification": classification.model_dump(mode="json"),
                    },
                )
                await self._task_service.append_event(
                    task_id,
                    "plan.proposed",
                    {
                        "source": (
                            "trusted_compensation_template"
                            if is_compensation
                            else "trusted_application_template"
                        ),
                        **plan.model_dump(mode="json"),
                    },
                )
            else:
                classification, classification_response = await self._classify(runtime)
                runtime.classification = classification
                await self._task_service.append_event(
                    task_id,
                    "task.classified",
                    {
                        "request_id": classification_response.request_id,
                        "provider_id": classification_response.provider_id,
                        "model": classification_response.model,
                        "classification": classification.model_dump(mode="json"),
                    },
                )
                plan, plan_response = await self._plan(runtime, classification)
                runtime.plan = plan
                runtime.planner_provider_id = plan_response.provider_id
                await self._task_service.append_event(
                    task_id,
                    "plan.proposed",
                    {
                        "request_id": plan_response.request_id,
                        "provider_id": plan_response.provider_id,
                        "model": plan_response.model,
                        **plan.model_dump(mode="json"),
                    },
                )
            await self._ensure_effect_graph(runtime)
        elif stage == 2:
            await self._task_service.transition_task(
                task_id,
                TaskStatus.RUNNING,
                command="processor",
                requested_by="system",
            )
        elif stage == 3 and isinstance(
            runtime.tool_request,
            (FileMoveDagRequest, DiskPressureGuardedFileMoveRequest),
        ):
            return await self._run_trusted_effect_dag(runtime)
        elif stage == 3:
            tool_step = self._tool_step(runtime)
            node_id = self._current_node_id(runtime)
            attempt_kind = self._current_attempt_kind(runtime)
            runtime.current_attempt_id = effect_attempt_id(node_id, attempt_kind)
            runtime.tool_call_id = effect_call_id(node_id, attempt_kind)
            runtime.approval_id = None
            runtime.policy_request = None
            runtime.policy_decision = None
            runtime.tool_arguments = None
            runtime.tool_resources = ()
            runtime.expected_resource_versions = {}
            runtime.tool_idempotency_key = None
            is_compensating = attempt_kind is EffectAttemptKind.COMPENSATION
            await self._task_service.transition_effect_node(
                task_id,
                node_id,
                expected_statuses=frozenset(
                    {EffectNodeStatus.SUCCEEDED if is_compensating else EffectNodeStatus.PENDING}
                ),
                target_status=(
                    EffectNodeStatus.COMPENSATING if is_compensating else EffectNodeStatus.ACTIVE
                ),
                transition_kind=("compensation_started" if is_compensating else "node_started"),
                event_type=(
                    "effect.compensation.started" if is_compensating else "effect.node.started"
                ),
                graph_status=(
                    EffectGraphStatus.COMPENSATING if is_compensating else EffectGraphStatus.ACTIVE
                ),
                execution_mode=runtime.execution_mode,
                failure_node_id=runtime.failure_node_id,
                **self._effect_fence(runtime),
            )
            await self._task_service.append_event(
                task_id,
                "step.started",
                {
                    "step_id": tool_step.step_id,
                    "agent": tool_step.agent,
                    "title": tool_step.title,
                    "graph_id": runtime.graph_id,
                    "node_id": node_id,
                    "attempt_id": runtime.current_attempt_id,
                    "attempt_kind": attempt_kind.value,
                },
            )
        elif stage == 4:
            tool_step = self._tool_step(runtime)
            if runtime.tool_request is None:
                contract = DISK_USAGE_CONTRACT
                runtime.tool_arguments = {"path": self._disk_usage_path}
                runtime.tool_resources = project_disk_usage_resources(
                    DiskUsageInput.model_validate(runtime.tool_arguments)
                )
            else:
                contract = FILE_MOVE_CONTRACT
                is_compensation = (
                    runtime.execution_mode is EffectExecutionMode.COMPENSATING
                    or isinstance(
                        runtime.tool_request,
                        FileMoveCompensationRequest,
                    )
                )
                normalized_arguments, required_source_version = await self._current_file_move_input(
                    runtime
                )
                runtime.tool_arguments = normalized_arguments.model_dump(mode="python")
                try:
                    runtime.tool_resources = await asyncio.to_thread(
                        project_file_move_resources,
                        normalized_arguments,
                    )
                except (OSError, ToolExecutorError):
                    if isinstance(runtime.tool_request, FileMoveSagaRequest):
                        return await self._handle_saga_pre_dispatch_failure(runtime)
                    if not is_compensation:
                        raise
                    await self._task_service.append_event(
                        task_id,
                        "task.failed",
                        {
                            "error_type": "CompensationResourceConflictError",
                            "code": "COMPENSATION_RESOURCE_VERSION_MISMATCH",
                            "message": (
                                "The receipt-bound reverse resources changed before "
                                "approval and no Tool call was created."
                            ),
                        },
                        new_status=TaskStatus.FAILED,
                    )
                    return _StageDisposition.TERMINAL
                source_resource = next(
                    resource
                    for resource in runtime.tool_resources
                    if resource.operations == (FILE_MOVE_SOURCE_CAPABILITY,)
                )
                if source_resource.version_digest is None:
                    raise UnsupportedModelPlanError(
                        "file.move source projection omitted its version"
                    )
                if is_compensation:
                    if source_resource.version_digest != required_source_version:
                        if isinstance(runtime.tool_request, FileMoveSagaRequest):
                            return await self._handle_saga_pre_dispatch_failure(runtime)
                        await self._task_service.append_event(
                            task_id,
                            "task.failed",
                            {
                                "error_type": "CompensationResourceConflictError",
                                "code": "COMPENSATION_RESOURCE_VERSION_MISMATCH",
                                "message": (
                                    "The receipt-bound reverse source version changed "
                                    "before approval and no Tool call was created."
                                ),
                            },
                            new_status=TaskStatus.FAILED,
                        )
                        return _StageDisposition.TERMINAL
                runtime.expected_resource_versions = {
                    "destination": "absent",
                    "source": source_resource.version_digest,
                }
                runtime.tool_idempotency_key = f"tmk_{secrets.token_urlsafe(32)}"
            if runtime.current_attempt_id is None:
                raise RuntimeError("Effect attempt identity was not initialized")
            await self._task_service.request_effect_tool_call(
                task_id,
                self._current_node_id(runtime),
                call_id=runtime.tool_call_id,
                attempt_id=runtime.current_attempt_id,
                attempt_kind=self._current_attempt_kind(runtime),
                step_id=tool_step.step_id,
                tool_name=contract.name,
                tool_version=contract.version,
                contract_digest=contract.digest,
                arguments=runtime.tool_arguments,
                idempotency=contract.execution.idempotency,
                idempotency_key=runtime.tool_idempotency_key,
                risk=contract.risk_level.value,
                tool_attempt=(
                    2 if runtime.execution_mode is EffectExecutionMode.COMPENSATING else 1
                ),
                checkpoint=self._checkpoint(runtime).model_copy(update={"next_stage": 5}),
                **self._effect_fence(runtime),
            )
        elif stage == 5:
            tool_step = self._tool_step(runtime)
            arguments = self._tool_arguments(runtime)
            contract = (
                FILE_MOVE_CONTRACT if runtime.tool_request is not None else DISK_USAGE_CONTRACT
            )
            resources = runtime.tool_resources
            if not resources:
                raise UnsupportedModelPlanError("Tool resources were not normalized")
            actor = self._tool_actor(runtime)
            request = ToolAuthorizationRequest(
                task_id=task_id,
                step_id=tool_step.step_id,
                call_id=runtime.tool_call_id,
                actor=actor,
                origin="builtin",
                tool_name=contract.name,
                tool_version=contract.version,
                contract_digest=contract.digest,
                arguments_digest=sha256_digest(arguments),
                risk_level=contract.risk_level,
                side_effects=contract.side_effects,
                reversible=contract.reversible,
                capabilities=contract.security.capabilities,
                network_access=contract.security.network_access,
                data_egress=False,
                resources=resources,
                expected_resource_versions_digest=sha256_digest(runtime.expected_resource_versions),
                interactive=True,
                batch_count=1,
            )
            decision = self._policy_engine.evaluate(request)
            runtime.policy_request = request
            runtime.policy_decision = decision
            consequences: tuple[str, ...]
            if runtime.tool_request is None:
                approval_title = "读取磁盘容量信息"
                approval_purpose = "读取所选路径所在磁盘的容量、已用与可用空间。"
                consequences = ("仅读取磁盘容量元数据，不修改文件。",)
            elif runtime.execution_mode is EffectExecutionMode.COMPENSATING or isinstance(
                runtime.tool_request, FileMoveCompensationRequest
            ):
                approval_title = "撤销先前的单文件移动"
                approval_purpose = (
                    "作为多步 saga 的独立补偿，按已验证提交回执反向移动同一版本的文件；"
                    "精确路径与版本见本次新审批卡。"
                )
                consequences = (
                    "这是一次全新的反向 file.move 提交，不会改写原调用账本。",
                    "反向源文件版本变化或原源路径已被占用时不会执行。",
                    "本次授权只允许使用一次，审批内容变化后必须重新审批。",
                )
            else:
                approval_title = "移动单个文件"
                approval_purpose = "将审批卡中的源文件移动到精确目标路径。"
                consequences = (
                    "源路径将不再存在，目标路径将出现同一版本的文件。",
                    "目标已存在、源文件版本变化或跨磁盘时不会执行。",
                    "撤销需要新的反向移动请求、版本检查和一次性审批。",
                )
            approval = await self._task_service.apply_policy_decision(
                task_id,
                runtime.tool_call_id,
                request=request,
                decision=decision,
                title=approval_title,
                purpose=approval_purpose,
                consequences=consequences,
                data_egress=DataEgress(enabled=False),
                expected_resource_versions=runtime.expected_resource_versions,
                fail_task_on_deny=False,
            )
            if decision.effect is PolicyEffect.DENY:
                return await self._handle_effect_call_failure(
                    runtime,
                    terminal_status=ToolCallStatus.FAILED,
                    task_failure_code="POLICY_DENIED",
                    task_failure_message="Policy denied the Tool call before dispatch.",
                    task_failure_error_type="PolicyDeniedError",
                )
            if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                if approval is None:
                    raise RuntimeError("Policy required approval but none was persisted")
                runtime.approval_id = approval.approval_id
                runtime.next_stage = 6
            if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                assert approval is not None
                self._schedule_approval_expiry(runtime, approval)
                return _StageDisposition.SUSPEND
        elif stage == 6:
            tool_step = self._tool_step(runtime)
            if tool_step.tool_name is None or tool_step.tool_version is None:
                raise UnsupportedModelPlanError("Selected plan step has no tool reference")
            try:
                lease = self._runner_client.ensure_ready()
            except RunnerClientError as error:
                return await self._finish_tool_without_dispatch(runtime, error)

            authorization = await self._authorization_grant(runtime)
            arguments = self._tool_arguments(runtime)
            if runtime.current_attempt_id is None:
                raise RuntimeError("Effect attempt identity was not initialized")
            try:
                await self._task_service.start_tool_call(
                    task_id,
                    runtime.tool_call_id,
                    runner_id=lease.runner_id,
                    authorization=authorization,
                    arguments=arguments,
                    expected_resource_versions=runtime.expected_resource_versions,
                    effect_node_id=self._current_node_id(runtime),
                    effect_attempt_id=runtime.current_attempt_id,
                    effect_graph_status=(
                        EffectGraphStatus.COMPENSATING
                        if runtime.execution_mode is EffectExecutionMode.COMPENSATING
                        else EffectGraphStatus.ACTIVE
                    ),
                    effect_execution_mode=runtime.execution_mode,
                    effect_failure_node_id=runtime.failure_node_id,
                    checkpoint=self._checkpoint(runtime),
                    **self._effect_fence(runtime),
                )
            except ApprovalExpiredError as error:
                raise _ToolCallAlreadyFinalizedError from error
            except ToolAuthorizationError as error:
                await self._finish_effect_failure(
                    runtime,
                    terminal_status=ToolCallStatus.FAILED,
                    error_code=error.code,
                    retryable=False,
                    resolution_source="policy",
                )
                return await self._handle_effect_call_failure(
                    runtime,
                    terminal_status=ToolCallStatus.FAILED,
                    transition_committed=True,
                )
            try:
                result = await self._runner_client.call_tool(
                    task_id=task_id,
                    step_id=tool_step.step_id,
                    tool_name=tool_step.tool_name,
                    tool_version=tool_step.tool_version,
                    arguments=arguments,
                    actor=self._tool_actor(runtime),
                    expected_runner_id=lease.runner_id,
                    call_id=runtime.tool_call_id,
                    idempotency_key=runtime.tool_idempotency_key,
                    expected_resource_versions=runtime.expected_resource_versions,
                    authorization=authorization,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._finish_effect_unknown(
                    runtime,
                    error_code=getattr(
                        error,
                        "code",
                        "RUNNER_CALL_OUTCOME_UNKNOWN",
                    ),
                )
                raise _ToolCallAlreadyFinalizedError from error

            if result.status == "succeeded" and result.output is not None:
                if runtime.current_attempt_id is None:
                    raise RuntimeError("Effect attempt identity was not initialized")
                await self._task_service.finish_effect_tool_call(
                    task_id,
                    self._current_node_id(runtime),
                    call_id=runtime.tool_call_id,
                    attempt_id=runtime.current_attempt_id,
                    status=ToolCallStatus.SUCCEEDED,
                    result=result.output,
                    target_status=(
                        EffectNodeStatus.COMPENSATED
                        if runtime.execution_mode is EffectExecutionMode.COMPENSATING
                        else EffectNodeStatus.SUCCEEDED
                    ),
                    transition_kind=(
                        "compensation_succeeded"
                        if runtime.execution_mode is EffectExecutionMode.COMPENSATING
                        else "forward_succeeded"
                    ),
                    event_type=(
                        "effect.compensation.completed"
                        if runtime.execution_mode is EffectExecutionMode.COMPENSATING
                        else "effect.node.completed"
                    ),
                    attempt_status=EffectAttemptStatus.SUCCEEDED,
                    graph_status=(
                        EffectGraphStatus.COMPENSATING
                        if runtime.execution_mode is EffectExecutionMode.COMPENSATING
                        else EffectGraphStatus.ACTIVE
                    ),
                    execution_mode=runtime.execution_mode,
                    failure_node_id=runtime.failure_node_id,
                    create_effect=True,
                    checkpoint=self._checkpoint(runtime).model_copy(update={"next_stage": 7}),
                    **self._effect_fence(runtime),
                )
            else:
                error_code = (
                    result.error.code if result.error is not None else "RUNNER_RESULT_INVALID"
                )
                terminal_status = {
                    "failed": ToolCallStatus.FAILED,
                    "cancelled": ToolCallStatus.CANCELLED,
                    "unknown": ToolCallStatus.UNKNOWN,
                }.get(result.status, ToolCallStatus.UNKNOWN)
                if terminal_status is ToolCallStatus.UNKNOWN:
                    await self._finish_effect_unknown(
                        runtime,
                        error_code=error_code,
                        retryable=(result.error.retryable if result.error is not None else False),
                    )
                    raise _ToolCallAlreadyFinalizedError
                await self._finish_effect_failure(
                    runtime,
                    terminal_status=terminal_status,
                    error_code=error_code,
                    retryable=(result.error.retryable if result.error is not None else False),
                )
                return await self._handle_effect_call_failure(
                    runtime,
                    terminal_status=terminal_status,
                    transition_committed=True,
                )
        elif stage == 7:
            tool_step = self._tool_step(runtime)
            await self._task_service.append_event(
                task_id,
                "step.completed",
                {
                    "step_id": tool_step.step_id,
                    "verified": True,
                    "graph_id": runtime.graph_id,
                    "node_id": runtime.current_node_id,
                    "attempt_id": runtime.current_attempt_id,
                    "attempt_kind": self._current_attempt_kind(runtime).value,
                },
            )
            if isinstance(runtime.tool_request, FileMoveSagaRequest):
                if runtime.execution_mode is EffectExecutionMode.COMPENSATING:
                    if runtime.current_node_index > 0:
                        runtime.current_node_index -= 1
                        graph = await self._task_service.get_effect_graph(task_id)
                        runtime.current_node_id = graph.nodes[runtime.current_node_index].node_id
                        runtime.next_stage = 3
                        self._clear_current_attempt(runtime)
                        return _StageDisposition.CONTINUE
                    await self._task_service.transition_effect_node(
                        task_id,
                        self._current_node_id(runtime),
                        expected_statuses=frozenset({EffectNodeStatus.COMPENSATED}),
                        target_status=EffectNodeStatus.COMPENSATED,
                        transition_kind="saga_compensated",
                        event_type="effect_graph.compensated",
                        graph_status=EffectGraphStatus.COMPENSATED,
                        execution_mode=EffectExecutionMode.COMPENSATING,
                        failure_node_id=runtime.failure_node_id,
                        **self._effect_fence(runtime),
                    )
                    await self._fail_saga_task(
                        runtime,
                        code="SAGA_COMPENSATED",
                        message=(
                            "The forward saga failed, and every committed predecessor "
                            "was compensated in reverse order with fresh authorization."
                        ),
                    )
                    return _StageDisposition.TERMINAL
                if runtime.current_node_index + 1 < len(runtime.tool_request.operations):
                    runtime.current_node_index += 1
                    graph = await self._task_service.get_effect_graph(task_id)
                    runtime.current_node_id = graph.nodes[runtime.current_node_index].node_id
                    runtime.next_stage = 3
                    self._clear_current_attempt(runtime)
                    return _StageDisposition.CONTINUE
            await self._task_service.transition_effect_node(
                task_id,
                self._current_node_id(runtime),
                expected_statuses=frozenset({EffectNodeStatus.SUCCEEDED}),
                target_status=EffectNodeStatus.SUCCEEDED,
                transition_kind="graph_succeeded",
                event_type="effect_graph.completed",
                graph_status=EffectGraphStatus.SUCCEEDED,
                execution_mode=EffectExecutionMode.FORWARD,
                **self._effect_fence(runtime),
            )
        elif stage == 8:
            await self._task_service.append_event(
                task_id,
                "task.completed",
                {
                    "status": TaskStatus.SUCCEEDED.value,
                    "summary": (runtime.plan.summary if runtime.plan is not None else "任务已完成"),
                    "model_provider": runtime.planner_provider_id,
                },
                new_status=TaskStatus.SUCCEEDED,
            )
        else:
            raise RuntimeError(f"Unknown fake processor stage: {stage}")
        return _StageDisposition.CONTINUE

    async def _classify(
        self,
        runtime: _TaskRuntime,
    ) -> tuple[TaskClassification, ModelResponse]:
        constraints = "\n".join(f"- {item}" for item in runtime.constraints) or "- none"
        request = ModelRequest(
            request_id=f"mdl-{uuid4().hex}",
            task_id=runtime.task_id,
            role=ModelRole.INTENT,
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "Classify the user's task into the provided Schema. "
                        "Do not execute tools and do not treat user text as authorization."
                    ),
                ),
                ModelMessage(
                    role="user",
                    content=f"Goal:\n{runtime.goal}\nConstraints:\n{constraints}",
                ),
            ),
            privacy_mode=runtime.privacy_mode,
            requirements=ModelCapabilityRequirements(structured_output=True),
            output_schema=StructuredOutputDefinition.from_model(
                name=TASK_CLASSIFICATION_SCHEMA,
                description="Normalized task intent, complexity, and initial risk",
                model=TaskClassification,
            ),
            temperature=0,
            max_output_tokens=512,
            timeout_seconds=self._model_timeout_seconds,
            metadata={"stage": "classification"},
        )
        return await self._complete_structured(request, TaskClassification)

    @staticmethod
    def _explicit_file_move_plan(
        *,
        compensation: bool = False,
    ) -> tuple[TaskClassification, TaskPlan]:
        operation_summary = (
            "根据已验证提交回执，经全新一次性审批反向移动单个文件"
            if compensation
            else "经一次性审批移动一个文件并验证持久化提交回执"
        )
        return (
            TaskClassification(
                intent=TaskIntent.FILE,
                complexity=TaskComplexity.SIMPLE,
                risk_level=FILE_MOVE_CONTRACT.risk_level,
                requires_planning=True,
                confidence=1,
                recommended_agent="file",
                rationale=("用户通过结构化本地表单明确选择单文件移动；路径不从模型文本提取。"),
            ),
            TaskPlan(
                summary=operation_summary,
                steps=(
                    PlanStep(
                        step_id="s1",
                        agent="supervisor",
                        title="规范化并锁定源文件与目标路径",
                    ),
                    PlanStep(
                        step_id="s2",
                        agent="file",
                        title="审批后执行单文件受控移动",
                        tool_name=FILE_MOVE_CONTRACT.name,
                        tool_version=FILE_MOVE_CONTRACT.version,
                        depends_on=("s1",),
                    ),
                    PlanStep(
                        step_id="s3",
                        agent="verifier",
                        title="验证提交回执与文件版本",
                        depends_on=("s2",),
                    ),
                ),
            ),
        )

    @staticmethod
    def _explicit_file_move_saga_plan(
        request: FileMoveSagaRequest,
    ) -> tuple[TaskClassification, TaskPlan]:
        tool_steps: list[PlanStep] = []
        previous = "s0"
        for ordinal, operation in enumerate(request.operations, start=1):
            step_id = f"move_{ordinal}"
            tool_steps.append(
                PlanStep(
                    step_id=step_id,
                    agent="file",
                    title=f"审批并执行文件移动 {operation.operation_id}",
                    tool_name=FILE_MOVE_CONTRACT.name,
                    tool_version=FILE_MOVE_CONTRACT.version,
                    depends_on=(previous,),
                )
            )
            previous = step_id
        return (
            TaskClassification(
                intent=TaskIntent.FILE,
                complexity=TaskComplexity.COMPOUND,
                risk_level=FILE_MOVE_CONTRACT.risk_level,
                requires_planning=True,
                confidence=1,
                recommended_agent="file",
                rationale=(
                    "用户通过结构化本地表单明确选择有界多步文件移动；"
                    "每个路径均由应用校验且不从模型文本提取。"
                ),
            ),
            TaskPlan(
                summary=(
                    f"按顺序执行 {len(request.operations)} 个受控文件移动；"
                    "失败时对已提交效果逆序申请补偿"
                ),
                steps=(
                    PlanStep(
                        step_id="s0",
                        agent="supervisor",
                        title="固化版本化 Tool effect graph",
                    ),
                    *tool_steps,
                    PlanStep(
                        step_id="verify",
                        agent="verifier",
                        title="验证所有提交回执或补偿结果",
                        depends_on=(previous,),
                    ),
                ),
            ),
        )

    @staticmethod
    def _explicit_file_move_dag_plan(
        request: FileMoveDagRequest,
    ) -> tuple[TaskClassification, TaskPlan]:
        return (
            TaskClassification(
                intent=TaskIntent.FILE,
                complexity=TaskComplexity.COMPOUND,
                risk_level=FILE_MOVE_CONTRACT.risk_level,
                requires_planning=True,
                confidence=1,
                recommended_agent="file",
                rationale=(
                    "用户通过结构化本地表单明确选择有界 DAG 文件移动；"
                    "路径和依赖均不从模型文本提取。"
                ),
            ),
            TaskPlan(
                summary=(
                    f"按信任依赖图执行 {len(request.operations)} 个受控文件移动；"
                    "失败时按反向依赖波次补偿"
                ),
                steps=tuple(
                    PlanStep(
                        step_id=operation.operation_id,
                        agent="file",
                        title=f"审批并执行 DAG 文件移动 {operation.operation_id}",
                        tool_name=FILE_MOVE_CONTRACT.name,
                        tool_version=FILE_MOVE_CONTRACT.version,
                        depends_on=operation.depends_on,
                    )
                    for operation in request.operations
                ),
            ),
        )

    @staticmethod
    def _disk_pressure_guarded_file_move_plan(
        request: DiskPressureGuardedFileMoveRequest,
    ) -> tuple[TaskClassification, TaskPlan]:
        return (
            TaskClassification(
                intent=TaskIntent.FILE,
                complexity=TaskComplexity.COMPOUND,
                risk_level=FILE_MOVE_CONTRACT.risk_level,
                requires_planning=True,
                confidence=1,
                recommended_agent="file",
                rationale=(
                    "用户明确选择磁盘压力保护文件移动；应用只从受信磁盘 Tool 结果"
                    "计算分支，路径和阈值均不来自模型。"
                ),
            ),
            TaskPlan(
                summary=(
                    "先读取目标磁盘使用率；不高于 "
                    f"{request.maximum_used_percent:g}% 时申请移动审批，否则只读复核并跳过写入"
                ),
                steps=(
                    PlanStep(
                        step_id="inspect_capacity",
                        agent="computer",
                        title="读取目标磁盘使用率并固化证据",
                        tool_name=DISK_USAGE_CONTRACT.name,
                        tool_version=DISK_USAGE_CONTRACT.version,
                    ),
                    PlanStep(
                        step_id="move_file",
                        agent="file",
                        title="磁盘压力允许时审批并移动文件",
                        tool_name=FILE_MOVE_CONTRACT.name,
                        tool_version=FILE_MOVE_CONTRACT.version,
                        depends_on=("inspect_capacity",),
                    ),
                    PlanStep(
                        step_id="confirm_deferred",
                        agent="computer",
                        title="磁盘压力过高时复核并安全推迟移动",
                        tool_name=DISK_USAGE_CONTRACT.name,
                        tool_version=DISK_USAGE_CONTRACT.version,
                        depends_on=("inspect_capacity",),
                    ),
                ),
            ),
        )

    async def _run_trusted_effect_dag(
        self,
        runtime: _TaskRuntime,
    ) -> _StageDisposition:
        request = runtime.tool_request
        if not isinstance(
            request,
            (FileMoveDagRequest, DiskPressureGuardedFileMoveRequest),
        ):
            raise UnsupportedModelPlanError("Trusted DAG runtime request is missing")
        if isinstance(request, DiskPressureGuardedFileMoveRequest):
            resolver: EffectNodeMaterialResolver = DiskPressureGuardedMaterialResolver(
                self._task_service,
                self._policy_engine,
                request,
            )
            branch_resolver = DiskPressureBranchDecisionResolver(
                self._task_service,
                request,
            )
        else:
            resolver = FileMoveDagMaterialResolver(
                self._task_service,
                self._policy_engine,
                request,
            )
            branch_resolver = None
        owner_id = self._dag_owner_id
        for _ in range(100):
            graph = await self._task_service.get_effect_graph(runtime.task_id)
            if graph.status is EffectGraphStatus.ACTIVE:
                if runtime.stop_requested:
                    await self._cancel_inactive_effect_dag(runtime)
                    continue
                lease = await self._task_service.acquire_effect_graph_lease(
                    runtime.task_id,
                    owner_id=owner_id,
                    ttl_seconds=self._graph_lease_ttl_seconds,
                )
                pending: tuple[str, ...] = ()
                try:
                    preparer = EffectDagLedgerPreparer(
                        self._task_service,
                        resolver,
                    )
                    waiting_nodes = tuple(
                        node
                        for node in graph.nodes
                        if node.status is EffectNodeStatus.WAITING_APPROVAL
                    )
                    if waiting_nodes:
                        refreshed = await preparer.prepare_nodes(
                            runtime.task_id,
                            waiting_nodes,
                            kind=EffectAttemptKind.FORWARD,
                            lease_owner_id=owner_id,
                            fencing_token=lease.fencing_token,
                        )
                        pending = refreshed.pending_approval_ids
                    if not pending:
                        ready = await self._task_service.checkpoint_effect_dag_ready_set(
                            runtime.task_id,
                            lease_owner_id=owner_id,
                            fencing_token=lease.fencing_token,
                            page_size=self._effect_dag_ready_page_size,
                        )
                        refreshed_graph = await self._task_service.get_effect_graph(runtime.task_id)
                        nodes_by_id = {node.node_id: node for node in refreshed_graph.nodes}
                        preparation = await preparer.prepare_nodes(
                            runtime.task_id,
                            tuple(
                                nodes_by_id[proof.node_id]
                                for proof in ready.ready_nodes[: self._effect_dag_max_concurrency]
                            ),
                            kind=EffectAttemptKind.FORWARD,
                            lease_owner_id=owner_id,
                            fencing_token=lease.fencing_token,
                        )
                        pending = preparation.pending_approval_ids
                        if preparation.denied_node_ids:
                            await self._task_service.reduce_effect_dag(
                                runtime.task_id,
                                lease_owner_id=owner_id,
                                fencing_token=lease.fencing_token,
                            )
                finally:
                    await self._task_service.release_effect_graph_lease(
                        runtime.task_id,
                        owner_id=owner_id,
                        fencing_token=lease.fencing_token,
                    )
                if pending:
                    runtime.dag_approval_ids = pending
                    runtime.next_stage = 3
                    await self._schedule_dag_approval_expiries(runtime)
                    return _StageDisposition.SUSPEND
                if runtime.stop_requested:
                    await self._cancel_inactive_effect_dag(runtime)
                    continue
                executor = LedgerBoundEffectNodeExecutor(
                    self._task_service,
                    self._runner_client,
                    resolver,
                    kind=EffectAttemptKind.FORWARD,
                )
                dispatcher = EffectDagDispatcher(
                    self._task_service,
                    executor,
                    instance_id=owner_id,
                    max_concurrency=self._effect_dag_max_concurrency,
                    graph_lease_ttl_seconds=self._graph_lease_ttl_seconds,
                    node_claim_ttl_seconds=self._graph_lease_ttl_seconds,
                    admission_controller=self._effect_dag_admission,
                    ready_page_size=self._effect_dag_ready_page_size,
                    branch_decision_resolver=branch_resolver,
                    stop_after_branch_decision=branch_resolver is not None,
                )
                branch_decision_count = len(graph.branch_decisions)
                runtime.dag_dispatcher = dispatcher
                try:
                    result = await dispatcher.run_until_idle(
                        runtime.task_id,
                        max_rounds=1,
                    )
                finally:
                    if runtime.dag_dispatcher is dispatcher:
                        runtime.dag_dispatcher = None
                graph = await self._task_service.get_effect_graph(runtime.task_id)
                if (
                    result.claimed == 0
                    and graph.status is EffectGraphStatus.ACTIVE
                    and len(graph.branch_decisions) == branch_decision_count
                ):
                    raise RuntimeError("Trusted DAG made no dispatch progress")
                continue
            if graph.status is EffectGraphStatus.COMPENSATING:
                try:
                    plan = await self._task_service.get_effect_dag_compensation_plan(
                        runtime.task_id
                    )
                    plan_id: str | None = plan.plan_id
                except InvalidEffectTransitionError:
                    plan_id = None
                compensation = await EffectDagCompensationDispatcher(
                    self._task_service,
                    self._runner_client,
                    resolver,
                    instance_id=owner_id,
                    max_concurrency=self._effect_dag_max_concurrency,
                    graph_lease_ttl_seconds=self._graph_lease_ttl_seconds,
                    node_claim_ttl_seconds=self._graph_lease_ttl_seconds,
                    admission_controller=self._effect_dag_admission,
                ).run(runtime.task_id, plan_id=plan_id)
                if compensation.pending_approval_ids:
                    runtime.dag_approval_ids = compensation.pending_approval_ids
                    runtime.next_stage = 3
                    await self._schedule_dag_approval_expiries(runtime)
                    return _StageDisposition.SUSPEND
                continue
            runtime.dag_approval_ids = ()
            if graph.status is EffectGraphStatus.SUCCEEDED:
                await self._task_service.append_event(
                    runtime.task_id,
                    "task.completed",
                    {
                        "verified": True,
                        "graph_id": graph.graph_id,
                        "schema_version": graph.schema_version,
                        "branch_decisions": [
                            {
                                "decision_id": decision.decision_id,
                                "decision_key": decision.decision_key,
                                "outcome": decision.outcome,
                                "proof_digest": decision.proof_digest,
                            }
                            for decision in graph.branch_decisions
                        ],
                    },
                    new_status=TaskStatus.SUCCEEDED,
                )
            elif graph.status is EffectGraphStatus.COMPENSATED:
                await self._task_service.append_event(
                    runtime.task_id,
                    "task.failed",
                    {
                        "error_type": "EffectDagCompensatedError",
                        "code": "DAG_COMPENSATED",
                        "message": (
                            "The DAG failed, and every applied effect was compensated "
                            "through receipt-bound reverse waves."
                        ),
                        "graph_id": graph.graph_id,
                    },
                    new_status=TaskStatus.FAILED,
                )
            elif graph.status in {
                EffectGraphStatus.FAILED,
                EffectGraphStatus.BLOCKED_NON_COMPENSABLE,
                EffectGraphStatus.BLOCKED_COMPENSATION_FAILED,
            }:
                await self._task_service.append_event(
                    runtime.task_id,
                    "task.failed",
                    {
                        "error_type": "EffectDagFailedError",
                        "code": graph.status.value.upper(),
                        "message": "The trusted effect DAG reached a deterministic failure.",
                        "graph_id": graph.graph_id,
                    },
                    new_status=TaskStatus.FAILED,
                )
            elif graph.status is EffectGraphStatus.CANCELLED:
                if not runtime.stop_requested:
                    await self._task_service.append_event(
                        runtime.task_id,
                        "task.cancelled",
                        {
                            "command": "effect_dag",
                            "requested_by": "system",
                            "graph_id": graph.graph_id,
                        },
                        new_status=TaskStatus.CANCELLED,
                    )
            elif graph.status in {
                EffectGraphStatus.BLOCKED_UNKNOWN,
                EffectGraphStatus.BLOCKED_COMPENSATION_UNKNOWN,
            }:
                return _StageDisposition.TERMINAL
            else:
                raise RuntimeError(f"Unsupported trusted DAG status: {graph.status.value}")
            return _StageDisposition.TERMINAL
        raise RuntimeError("Trusted DAG exceeded its bounded orchestration rounds")

    async def _cancel_inactive_effect_dag(self, runtime: _TaskRuntime) -> None:
        """Cancel an idle/waiting v2 graph under a fresh owner fence."""
        try:
            graph = await self._task_service.get_effect_graph(runtime.task_id)
        except EffectGraphNotFoundError:
            return
        if (
            graph.schema_version != EFFECT_DAG_SCHEMA_VERSION
            or graph.status is not EffectGraphStatus.ACTIVE
        ):
            return
        owner_id = self._dag_owner_id
        lease = await self._task_service.acquire_effect_graph_lease(
            runtime.task_id,
            owner_id=owner_id,
            ttl_seconds=self._graph_lease_ttl_seconds,
        )
        try:
            await self._task_service.request_effect_dag_cancel(
                runtime.task_id,
                lease_owner_id=owner_id,
                fencing_token=lease.fencing_token,
            )
            await self._task_service.reduce_effect_dag(
                runtime.task_id,
                lease_owner_id=owner_id,
                fencing_token=lease.fencing_token,
            )
        finally:
            await self._task_service.release_effect_graph_lease(
                runtime.task_id,
                owner_id=owner_id,
                fencing_token=lease.fencing_token,
            )

    async def _ensure_effect_graph(self, runtime: _TaskRuntime) -> None:
        if runtime.plan is None:
            raise UnsupportedModelPlanError("Task has no validated plan for effect graph")
        tool_steps = tuple(step for step in runtime.plan.steps if step.tool_name is not None)
        if not tool_steps:
            raise UnsupportedModelPlanError("Task plan has no Tool effect nodes")
        if isinstance(runtime.tool_request, FileMoveDagRequest):
            if len(tool_steps) != len(runtime.tool_request.operations):
                raise UnsupportedModelPlanError(
                    "DAG plan does not match the explicit operation count"
                )
            node_keys = tuple(
                operation.operation_id for operation in runtime.tool_request.operations
            )
        elif isinstance(runtime.tool_request, DiskPressureGuardedFileMoveRequest):
            if len(tool_steps) != 3:
                raise UnsupportedModelPlanError(
                    "Disk-pressure business graph must contain exactly three Tool nodes"
                )
            node_keys = ("inspect_capacity", "move_file", "confirm_deferred")
        elif isinstance(runtime.tool_request, FileMoveSagaRequest):
            if len(tool_steps) != len(runtime.tool_request.operations):
                raise UnsupportedModelPlanError(
                    "Saga plan does not match the explicit operation count"
                )
            node_keys = tuple(
                operation.operation_id for operation in runtime.tool_request.operations
            )
        else:
            if len(tool_steps) != 1:
                raise UnsupportedModelPlanError(
                    "Current non-saga slice requires exactly one Tool node"
                )
            node_keys = (tool_steps[0].step_id,)
        definitions: list[EffectNodeDefinition] = []
        for node_key, step in zip(node_keys, tool_steps, strict=True):
            if step.tool_name is None or step.tool_version is None:
                raise UnsupportedModelPlanError("Effect node omitted its Tool reference")
            if step.tool_name == FILE_MOVE_CONTRACT.name:
                contract = FILE_MOVE_CONTRACT
                compensation = CompensationStrategy.RECEIPT_BOUND_REVERSE
            elif step.tool_name == DISK_USAGE_CONTRACT.name:
                contract = DISK_USAGE_CONTRACT
                compensation = CompensationStrategy.NONE
            else:
                raise UnsupportedModelPlanError(
                    "Effect graph references a Tool outside the trusted slice"
                )
            if step.tool_version != contract.version:
                raise UnsupportedModelPlanError("Effect graph Tool version is unsupported")
            definitions.append(
                EffectNodeDefinition(
                    node_key=node_key,
                    step_id=step.step_id,
                    tool_name=contract.name,
                    tool_version=contract.version,
                    contract_digest=contract.digest,
                    compensation_strategy=compensation,
                )
            )
        if isinstance(runtime.tool_request, FileMoveDagRequest):
            depends_on_by_key = {
                operation.operation_id: operation.depends_on
                for operation in runtime.tool_request.operations
            }
            graph = await self._task_service.create_effect_dag(
                runtime.task_id,
                tuple(
                    EffectDagNodeDefinition(
                        **definition.model_dump(mode="python"),
                        depends_on=depends_on_by_key[definition.node_key],
                    )
                    for definition in definitions
                ),
            )
            runtime.graph_id = graph.graph_id
            runtime.graph_schema_version = graph.schema_version
            runtime.graph_fencing_token = None
            runtime.execution_mode = graph.execution_mode
            runtime.current_node_id = None
            runtime.current_attempt_id = None
            runtime.tool_call_id = "dag-unbound"
            return
        if isinstance(runtime.tool_request, DiskPressureGuardedFileMoveRequest):
            conditions = {
                "inspect_capacity": (),
                "move_file": (
                    EffectDagBranchCondition(
                        predecessor_key="inspect_capacity",
                        decision_key="disk_pressure_route",
                        expected_outcome="move",
                    ),
                ),
                "confirm_deferred": (
                    EffectDagBranchCondition(
                        predecessor_key="inspect_capacity",
                        decision_key="disk_pressure_route",
                        expected_outcome="defer",
                    ),
                ),
            }
            graph = await self._task_service.create_effect_dag(
                runtime.task_id,
                tuple(
                    EffectDagNodeDefinition(
                        **definition.model_dump(mode="python"),
                        conditional_depends_on=conditions[definition.node_key],
                    )
                    for definition in definitions
                ),
            )
            runtime.graph_id = graph.graph_id
            runtime.graph_schema_version = graph.schema_version
            runtime.graph_fencing_token = None
            runtime.execution_mode = graph.execution_mode
            runtime.current_node_id = None
            runtime.current_attempt_id = None
            runtime.tool_call_id = "dag-unbound"
            return
        graph = await self._task_service.create_effect_graph(
            runtime.task_id,
            tuple(definitions),
        )
        lease = await self._task_service.acquire_effect_graph_lease(
            runtime.task_id,
            owner_id=self._instance_id,
            ttl_seconds=self._graph_lease_ttl_seconds,
        )
        runtime.graph_id = graph.graph_id
        runtime.graph_schema_version = graph.schema_version
        runtime.graph_fencing_token = lease.fencing_token
        runtime.execution_mode = graph.execution_mode
        runtime.current_node_id = graph.nodes[runtime.current_node_index].node_id
        runtime.current_attempt_id = effect_attempt_id(
            runtime.current_node_id,
            EffectAttemptKind.FORWARD,
        )
        runtime.tool_call_id = effect_call_id(
            runtime.current_node_id,
            EffectAttemptKind.FORWARD,
        )
        self._start_graph_lease_worker(runtime)

    async def _plan(
        self,
        runtime: _TaskRuntime,
        classification: TaskClassification,
    ) -> tuple[TaskPlan, ModelResponse]:
        planning_input = json.dumps(
            {
                "goal": runtime.goal,
                "constraints": runtime.constraints,
                "classification": classification.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        request = ModelRequest(
            request_id=f"mdl-{uuid4().hex}",
            task_id=runtime.task_id,
            role=ModelRole.PLANNER,
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "Create a minimal ordered plan using only tools declared by the "
                        "application. Tool references are candidates, never authorization."
                    ),
                ),
                ModelMessage(role="user", content=planning_input),
            ),
            privacy_mode=runtime.privacy_mode,
            requirements=ModelCapabilityRequirements(structured_output=True),
            output_schema=StructuredOutputDefinition.from_model(
                name=TASK_PLAN_SCHEMA,
                description="Ordered task plan with optional versioned tool references",
                model=TaskPlan,
            ),
            temperature=0,
            max_output_tokens=1_024,
            timeout_seconds=self._model_timeout_seconds,
            metadata={"stage": "planning"},
        )
        return await self._complete_structured(request, TaskPlan)

    async def _complete_structured(
        self,
        request: ModelRequest,
        output_model: type[StructuredModel],
    ) -> tuple[StructuredModel, ModelResponse]:
        try:
            provider = self._model_gateway.select_provider(request)
            descriptor = provider.descriptor
            await self._task_service.append_event(
                request.task_id,
                "model.started",
                {
                    "request_id": request.request_id,
                    "role": request.role.value,
                    "provider_id": descriptor.provider_id,
                    "model": descriptor.model,
                    "protocol": descriptor.protocol.value,
                    "location": descriptor.location.value,
                    "output_schema": (
                        request.output_schema.name if request.output_schema is not None else None
                    ),
                },
            )
            output, response = await self._model_gateway.complete_structured(request, output_model)
        except ModelGatewayError as error:
            await self._task_service.append_event(
                request.task_id,
                "model.failed",
                {
                    "request_id": request.request_id,
                    "role": request.role.value,
                    "provider_id": error.provider_id,
                    "code": error.code,
                    "retryable": error.retryable,
                },
            )
            raise ModelInvocationFailedError(error.code) from error

        await self._task_service.append_event(
            request.task_id,
            "model.usage",
            {
                "request_id": response.request_id,
                "role": request.role.value,
                "provider_id": response.provider_id,
                "model": response.model,
                "finish_reason": response.finish_reason.value,
                "latency_ms": response.latency_ms,
                "usage": response.usage.model_dump(mode="json"),
            },
        )
        return output, response

    @staticmethod
    def _tool_step(runtime: _TaskRuntime) -> PlanStep:
        if runtime.plan is None:
            raise UnsupportedModelPlanError("Task has no validated model plan")
        tool_steps = [step for step in runtime.plan.steps if step.tool_name is not None]
        if not tool_steps or runtime.current_node_index >= len(tool_steps):
            raise UnsupportedModelPlanError("Current effect node has no matching Tool plan step")
        step = tool_steps[runtime.current_node_index]
        expected_contract = (
            FILE_MOVE_CONTRACT if runtime.tool_request is not None else DISK_USAGE_CONTRACT
        )
        if (
            step.tool_name != expected_contract.name
            or step.tool_version != expected_contract.version
        ):
            raise UnsupportedModelPlanError(
                "Plan references a tool outside the explicit task slice"
            )
        return step

    @staticmethod
    def _tool_actor(runtime: _TaskRuntime) -> str:
        if runtime.tool_request is not None:
            return "local_user"
        return f"model:{runtime.planner_provider_id or 'unknown'}"

    @staticmethod
    def _tool_arguments(runtime: _TaskRuntime) -> dict[str, object]:
        if runtime.tool_arguments is None:
            raise UnsupportedModelPlanError("Tool arguments were not normalized")
        return dict(runtime.tool_arguments)

    @staticmethod
    def _current_attempt_kind(runtime: _TaskRuntime) -> EffectAttemptKind:
        return (
            EffectAttemptKind.COMPENSATION
            if runtime.execution_mode is EffectExecutionMode.COMPENSATING
            else EffectAttemptKind.FORWARD
        )

    @staticmethod
    def _current_node_id(runtime: _TaskRuntime) -> str:
        if runtime.current_node_id is None:
            raise UnsupportedModelPlanError("Task has no current effect node identity")
        return runtime.current_node_id

    async def _current_file_move_input(
        self,
        runtime: _TaskRuntime,
    ) -> tuple[FileMoveInput, str | None]:
        request = runtime.tool_request
        if isinstance(request, FileMoveSagaRequest):
            operation = request.operations[runtime.current_node_index]
            if runtime.execution_mode is EffectExecutionMode.FORWARD:
                return (
                    FileMoveInput(
                        source=operation.source,
                        destination=operation.destination,
                    ),
                    None,
                )
            graph = await self._task_service.get_effect_graph(runtime.task_id)
            node_id = self._current_node_id(runtime)
            node = next(node for node in graph.nodes if node.node_id == node_id)
            applied = next(
                (
                    effect
                    for effect in node.effects
                    if effect.kind is EffectAttemptKind.FORWARD and effect.receipt_id is not None
                ),
                None,
            )
            if applied is None or applied.receipt_id is None:
                raise UnsupportedModelPlanError(
                    "Saga compensation is missing its forward commit receipt"
                )
            receipt = await self._task_service.get_commit_receipt(applied.receipt_id)
            expected_source_version = receipt.resource_versions_after.get("destination")
            if expected_source_version is None or expected_source_version == "absent":
                raise UnsupportedModelPlanError(
                    "Saga compensation receipt omitted the reverse source version"
                )
            return (
                FileMoveInput(
                    source=operation.destination,
                    destination=operation.source,
                ),
                expected_source_version,
            )
        if isinstance(request, FileMoveCompensationRequest):
            return (
                FileMoveInput(
                    source=request.source,
                    destination=request.destination,
                ),
                request.expected_source_version,
            )
        if isinstance(request, FileMoveTaskRequest):
            return (
                FileMoveInput(
                    source=request.source,
                    destination=request.destination,
                ),
                None,
            )
        raise UnsupportedModelPlanError("Current Tool node is not a file.move request")

    async def _handle_saga_pre_dispatch_failure(
        self,
        runtime: _TaskRuntime,
    ) -> _StageDisposition:
        node_id = self._current_node_id(runtime)
        if runtime.execution_mode is EffectExecutionMode.COMPENSATING:
            await self._task_service.transition_effect_node(
                runtime.task_id,
                node_id,
                expected_statuses=frozenset({EffectNodeStatus.COMPENSATING}),
                target_status=EffectNodeStatus.COMPENSATION_FAILED,
                transition_kind="compensation_pre_dispatch_failed",
                event_type="effect.compensation.failed",
                graph_status=EffectGraphStatus.FAILED,
                execution_mode=EffectExecutionMode.COMPENSATING,
                failure_node_id=runtime.failure_node_id,
                **self._effect_fence(runtime),
            )
            await self._fail_saga_task(
                runtime,
                code="SAGA_COMPENSATION_FAILED",
                message="A receipt-bound compensation could not be prepared safely.",
            )
            return _StageDisposition.TERMINAL

        runtime.failure_node_id = node_id
        has_applied_predecessor = runtime.current_node_index > 0
        await self._task_service.transition_effect_node(
            runtime.task_id,
            node_id,
            expected_statuses=frozenset({EffectNodeStatus.ACTIVE}),
            target_status=EffectNodeStatus.FAILED,
            transition_kind="forward_pre_dispatch_failed",
            event_type="effect.node.failed",
            graph_status=(
                EffectGraphStatus.COMPENSATING
                if has_applied_predecessor
                else EffectGraphStatus.FAILED
            ),
            execution_mode=(
                EffectExecutionMode.COMPENSATING
                if has_applied_predecessor
                else EffectExecutionMode.FORWARD
            ),
            failure_node_id=node_id,
            **self._effect_fence(runtime),
        )
        if not has_applied_predecessor:
            await self._fail_saga_task(
                runtime,
                code="SAGA_FORWARD_FAILED",
                message="The first saga node failed before dispatch; no effect was applied.",
            )
            return _StageDisposition.TERMINAL
        runtime.execution_mode = EffectExecutionMode.COMPENSATING
        runtime.current_node_index -= 1
        graph = await self._task_service.get_effect_graph(runtime.task_id)
        runtime.current_node_id = graph.nodes[runtime.current_node_index].node_id
        runtime.next_stage = 3
        self._clear_current_attempt(runtime)
        return _StageDisposition.CONTINUE

    async def _finish_effect_unknown(
        self,
        runtime: _TaskRuntime,
        *,
        error_code: str,
        retryable: bool = False,
    ) -> None:
        if runtime.current_attempt_id is None:
            raise RuntimeError("Effect attempt identity was not initialized")
        is_compensation = runtime.execution_mode is EffectExecutionMode.COMPENSATING
        if runtime.failure_node_id is None:
            runtime.failure_node_id = self._current_node_id(runtime)
        await self._task_service.finish_effect_tool_call(
            runtime.task_id,
            self._current_node_id(runtime),
            call_id=runtime.tool_call_id,
            attempt_id=runtime.current_attempt_id,
            status=ToolCallStatus.UNKNOWN,
            target_status=(
                EffectNodeStatus.COMPENSATION_UNKNOWN
                if is_compensation
                else EffectNodeStatus.UNKNOWN
            ),
            transition_kind=("compensation_unknown" if is_compensation else "forward_unknown"),
            event_type="effect.attempt.unknown",
            attempt_status=EffectAttemptStatus.UNKNOWN,
            graph_status=EffectGraphStatus.BLOCKED_UNKNOWN,
            execution_mode=runtime.execution_mode,
            failure_node_id=runtime.failure_node_id,
            error_code=error_code,
            retryable=retryable,
            resolution_source="control_plane",
            checkpoint=self._checkpoint(runtime),
            **self._effect_fence(runtime),
        )

    async def _finish_effect_failure(
        self,
        runtime: _TaskRuntime,
        *,
        terminal_status: ToolCallStatus,
        error_code: str,
        retryable: bool,
        resolution_source: str = "runner",
    ) -> None:
        if runtime.current_attempt_id is None:
            raise RuntimeError("Effect attempt identity was not initialized")
        attempt_status = {
            ToolCallStatus.FAILED: EffectAttemptStatus.FAILED,
            ToolCallStatus.CANCELLED: EffectAttemptStatus.CANCELLED,
        }[terminal_status]
        node_id = self._current_node_id(runtime)
        is_compensation = runtime.execution_mode is EffectExecutionMode.COMPENSATING
        is_saga = isinstance(runtime.tool_request, FileMoveSagaRequest)
        has_applied_predecessor = not is_compensation and is_saga and runtime.current_node_index > 0
        checkpoint: TaskCheckpointPayload | None = None
        if has_applied_predecessor:
            graph = await self._task_service.get_effect_graph(runtime.task_id)
            previous = graph.nodes[runtime.current_node_index - 1]
            checkpoint = self._checkpoint(runtime).model_copy(
                update={
                    "next_stage": 3,
                    "tool_call_id": effect_call_id(
                        previous.node_id,
                        EffectAttemptKind.COMPENSATION,
                    ),
                    "tool_arguments": None,
                    "tool_resources": (),
                    "expected_resource_versions": {},
                    "tool_idempotency_key": None,
                    "policy_request": None,
                    "policy_decision": None,
                    "approval_id": None,
                    "current_node_id": previous.node_id,
                    "current_node_index": previous.ordinal,
                    "execution_mode": EffectExecutionMode.COMPENSATING,
                    "failure_node_id": node_id,
                    "reconciled_call_id": None,
                    "reconciled_outcome": None,
                }
            )
        await self._task_service.finish_effect_tool_call(
            runtime.task_id,
            node_id,
            call_id=runtime.tool_call_id,
            attempt_id=runtime.current_attempt_id,
            status=terminal_status,
            target_status=(
                EffectNodeStatus.COMPENSATION_FAILED if is_compensation else EffectNodeStatus.FAILED
            ),
            transition_kind=("compensation_failed" if is_compensation else "forward_failed"),
            event_type=("effect.compensation.failed" if is_compensation else "effect.node.failed"),
            attempt_status=attempt_status,
            graph_status=(
                EffectGraphStatus.COMPENSATING
                if has_applied_predecessor
                else EffectGraphStatus.FAILED
            ),
            execution_mode=(
                EffectExecutionMode.COMPENSATING
                if has_applied_predecessor or is_compensation
                else EffectExecutionMode.FORWARD
            ),
            failure_node_id=(runtime.failure_node_id if is_compensation else node_id),
            error_code=error_code,
            retryable=retryable,
            resolution_source=resolution_source,
            checkpoint=checkpoint,
            **self._effect_fence(runtime),
        )

    async def _handle_effect_call_failure(
        self,
        runtime: _TaskRuntime,
        *,
        terminal_status: ToolCallStatus,
        task_failure_code: str | None = None,
        task_failure_message: str | None = None,
        task_failure_error_type: str = "ToolSagaError",
        transition_committed: bool = False,
    ) -> _StageDisposition:
        if runtime.current_attempt_id is None:
            raise RuntimeError("Effect attempt identity was not initialized")
        attempt_status = {
            ToolCallStatus.FAILED: EffectAttemptStatus.FAILED,
            ToolCallStatus.CANCELLED: EffectAttemptStatus.CANCELLED,
        }[terminal_status]
        node_id = self._current_node_id(runtime)
        is_compensation = runtime.execution_mode is EffectExecutionMode.COMPENSATING
        if is_compensation:
            if not transition_committed:
                await self._task_service.transition_effect_node(
                    runtime.task_id,
                    node_id,
                    expected_statuses=frozenset(
                        {
                            EffectNodeStatus.RUNNING,
                            EffectNodeStatus.WAITING_APPROVAL,
                            EffectNodeStatus.ACTIVE,
                            EffectNodeStatus.COMPENSATING,
                        }
                    ),
                    target_status=EffectNodeStatus.COMPENSATION_FAILED,
                    transition_kind="compensation_failed",
                    event_type="effect.compensation.failed",
                    attempt_id=runtime.current_attempt_id,
                    attempt_status=attempt_status,
                    graph_status=EffectGraphStatus.FAILED,
                    execution_mode=EffectExecutionMode.COMPENSATING,
                    failure_node_id=runtime.failure_node_id,
                    **self._effect_fence(runtime),
                )
            await self._fail_saga_task(
                runtime,
                code="SAGA_COMPENSATION_FAILED",
                message="A separately authorized saga compensation failed.",
            )
            return _StageDisposition.TERMINAL

        runtime.failure_node_id = node_id
        is_saga = isinstance(runtime.tool_request, FileMoveSagaRequest)
        has_applied_predecessor = is_saga and runtime.current_node_index > 0
        if not transition_committed:
            await self._task_service.transition_effect_node(
                runtime.task_id,
                node_id,
                expected_statuses=frozenset(
                    {
                        EffectNodeStatus.RUNNING,
                        EffectNodeStatus.WAITING_APPROVAL,
                        EffectNodeStatus.ACTIVE,
                    }
                ),
                target_status=EffectNodeStatus.FAILED,
                transition_kind="forward_failed",
                event_type="effect.node.failed",
                attempt_id=runtime.current_attempt_id,
                attempt_status=attempt_status,
                graph_status=(
                    EffectGraphStatus.COMPENSATING
                    if has_applied_predecessor
                    else EffectGraphStatus.FAILED
                ),
                execution_mode=(
                    EffectExecutionMode.COMPENSATING
                    if has_applied_predecessor
                    else EffectExecutionMode.FORWARD
                ),
                failure_node_id=node_id,
                **self._effect_fence(runtime),
            )
        if not has_applied_predecessor:
            await self._fail_saga_task(
                runtime,
                code=(
                    task_failure_code or ("SAGA_FORWARD_FAILED" if is_saga else "TOOL_CALL_FAILED")
                ),
                message=(
                    task_failure_message
                    or "The Tool effect failed and no automatic compensation was started."
                ),
                error_type=task_failure_error_type,
            )
            return _StageDisposition.TERMINAL
        runtime.execution_mode = EffectExecutionMode.COMPENSATING
        runtime.current_node_index -= 1
        graph = await self._task_service.get_effect_graph(runtime.task_id)
        runtime.current_node_id = graph.nodes[runtime.current_node_index].node_id
        runtime.next_stage = 3
        self._clear_current_attempt(runtime)
        return _StageDisposition.CONTINUE

    async def _fail_saga_task(
        self,
        runtime: _TaskRuntime,
        *,
        code: str,
        message: str,
        error_type: str = "ToolSagaError",
    ) -> None:
        await self._task_service.append_event(
            runtime.task_id,
            "task.failed",
            {
                "error_type": error_type,
                "code": code,
                "message": message,
                "graph_id": runtime.graph_id,
                "failure_node_id": runtime.failure_node_id,
            },
            new_status=TaskStatus.FAILED,
        )

    @staticmethod
    def _clear_current_attempt(runtime: _TaskRuntime) -> None:
        if runtime.current_node_id is not None:
            kind = (
                EffectAttemptKind.COMPENSATION
                if runtime.execution_mode is EffectExecutionMode.COMPENSATING
                else EffectAttemptKind.FORWARD
            )
            runtime.current_attempt_id = effect_attempt_id(
                runtime.current_node_id,
                kind,
            )
            runtime.tool_call_id = effect_call_id(runtime.current_node_id, kind)
        else:
            runtime.current_attempt_id = None
            runtime.tool_call_id = "unbound"
        runtime.tool_arguments = None
        runtime.tool_resources = ()
        runtime.expected_resource_versions = {}
        runtime.tool_idempotency_key = None
        runtime.policy_request = None
        runtime.policy_decision = None
        runtime.approval_id = None
        runtime.reconciled_call_id = None
        runtime.reconciled_outcome = None

    async def _authorization_grant(
        self,
        runtime: _TaskRuntime,
    ) -> ToolAuthorizationGrant:
        request = runtime.policy_request
        decision = runtime.policy_decision
        if request is None or decision is None:
            raise ToolAuthorizationError(
                runtime.tool_call_id,
                "Tool call has no durable policy decision",
            )
        approval_id: str | None = None
        preview_hash: str | None = None
        approved_at = None
        grant_expires_at = None
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            if runtime.approval_id is None:
                raise ToolAuthorizationError(
                    runtime.tool_call_id,
                    "Tool call has no approval identity",
                )
            approval = await self._task_service.get_approval(runtime.approval_id)
            if approval.status is not ApprovalStatus.APPROVED:
                raise ToolAuthorizationError(
                    runtime.tool_call_id,
                    "Tool call approval is not approved",
                )
            approval_id = approval.approval_id
            preview_hash = approval.preview_hash
            if approval.resolved_at is None:
                raise ToolAuthorizationError(
                    runtime.tool_call_id,
                    "Approved Tool call has no approval timestamp",
                )
            approved_at = approval.resolved_at
            grant_expires_at = approval.expires_at
        elif decision.effect is not PolicyEffect.ALLOW:
            raise ToolAuthorizationError(
                runtime.tool_call_id,
                "Denied policy decision cannot produce an authorization grant",
            )

        return ToolAuthorizationGrant.issue(
            decision_id=decision.decision_id,
            request_digest=decision.request_digest,
            task_id=request.task_id,
            step_id=request.step_id,
            call_id=request.call_id,
            actor_id=request.actor,
            origin=request.origin,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            contract_digest=request.contract_digest,
            policy_revision=decision.policy_revision,
            rule_id=decision.rule_id,
            reason_code=decision.reason_code,
            effective_risk=decision.effective_risk,
            arguments_digest=request.arguments_digest,
            resource_scope_digest=request.resource_scope_digest,
            expected_resource_versions_digest=(request.expected_resource_versions_digest),
            capabilities=request.capabilities,
            network_access=request.network_access,
            data_egress=request.data_egress,
            side_effects=request.side_effects,
            reversible=request.reversible,
            resources=request.resources,
            interactive=request.interactive,
            batch_count=request.batch_count,
            approval_id=approval_id,
            preview_hash=preview_hash,
            approved_at=approved_at,
            grant_expires_at=grant_expires_at,
        )

    def _schedule_approval_expiry(
        self,
        runtime: _TaskRuntime,
        approval: ApprovalRead,
    ) -> None:
        self._cancel_approval_expiry(runtime)
        runtime.approval_expiry_worker = asyncio.create_task(
            self._expire_approval_after(runtime, approval),
            name=f"approval-expiry:{approval.approval_id}",
        )

    async def _schedule_dag_approval_expiries(
        self,
        runtime: _TaskRuntime,
    ) -> None:
        self._cancel_approval_expiry(runtime)
        approvals = await asyncio.gather(
            *(
                self._task_service.get_approval(approval_id)
                for approval_id in runtime.dag_approval_ids
            )
        )
        for approval in approvals:
            if approval.status is not ApprovalStatus.PENDING:
                continue
            runtime.dag_approval_expiry_workers[approval.approval_id] = asyncio.create_task(
                self._expire_approval_after(runtime, approval),
                name=f"approval-expiry:{approval.approval_id}",
            )

    def _effect_fence(self, runtime: _TaskRuntime) -> _EffectFence:
        if runtime.graph_fencing_token is None or runtime.graph_lease_lost:
            raise EffectGraphFenceRejectedError(runtime.graph_id or "unknown")
        return {
            "lease_owner_id": self._instance_id,
            "fencing_token": runtime.graph_fencing_token,
        }

    def _start_graph_lease_worker(self, runtime: _TaskRuntime) -> None:
        worker = runtime.graph_lease_worker
        if worker is not None and not worker.done():
            return
        runtime.graph_lease_lost = False
        runtime.graph_lease_worker = asyncio.create_task(
            self._renew_graph_lease(runtime),
            name=f"graph-lease-renew:{runtime.task_id}",
        )

    async def _renew_graph_lease(self, runtime: _TaskRuntime) -> None:
        interval = max(0.25, self._graph_lease_ttl_seconds / 3)
        try:
            while runtime.graph_fencing_token is not None:
                await asyncio.sleep(interval)
                await self._task_service.renew_effect_graph_lease(
                    runtime.task_id,
                    owner_id=self._instance_id,
                    fencing_token=runtime.graph_fencing_token,
                    ttl_seconds=self._graph_lease_ttl_seconds,
                )
        except asyncio.CancelledError:
            raise
        except (EffectGraphFenceRejectedError, EffectGraphLeaseUnavailableError):
            runtime.graph_lease_lost = True
            runtime.stop_requested = True
            runtime.stop_signal.set()

    @staticmethod
    def _cancel_graph_lease_worker(runtime: _TaskRuntime) -> None:
        worker = runtime.graph_lease_worker
        runtime.graph_lease_worker = None
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()

    async def _release_graph_lease(self, runtime: _TaskRuntime) -> None:
        fencing_token = runtime.graph_fencing_token
        runtime.graph_fencing_token = None
        if fencing_token is None:
            return
        await self._task_service.release_effect_graph_lease(
            runtime.task_id,
            owner_id=self._instance_id,
            fencing_token=fencing_token,
        )

    async def _expire_approval_after(
        self,
        runtime: _TaskRuntime,
        approval: ApprovalRead,
    ) -> None:
        try:
            current = approval
            while True:
                delay = max(
                    0.0,
                    (current.expires_at - datetime.now(UTC)).total_seconds(),
                )
                await asyncio.sleep(delay)
                result = await self._task_service.expire_approval(current.approval_id)
                if result.approval.status is ApprovalStatus.PENDING:
                    current = result.approval
                    continue
                if result.approval.status is ApprovalStatus.EXPIRED:
                    self.forget(runtime.task_id)
                return
        except asyncio.CancelledError:
            raise
        except (ApprovalAlreadyResolvedError, ApprovalExpiredError):
            return

    @staticmethod
    def _cancel_approval_expiry(runtime: _TaskRuntime) -> None:
        worker = runtime.approval_expiry_worker
        runtime.approval_expiry_worker = None
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()
        workers = tuple(runtime.dag_approval_expiry_workers.values())
        runtime.dag_approval_expiry_workers.clear()
        for dag_worker in workers:
            if not dag_worker.done() and dag_worker is not asyncio.current_task():
                dag_worker.cancel()

    async def _interruptible_delay(self, runtime: _TaskRuntime) -> None:
        try:
            await asyncio.wait_for(
                runtime.stop_signal.wait(),
                timeout=self._step_delay_seconds,
            )
        except TimeoutError:
            pass

    async def _finish_tool_without_dispatch(
        self,
        runtime: _TaskRuntime,
        error: RunnerClientError,
    ) -> _StageDisposition:
        await self._finish_effect_failure(
            runtime,
            terminal_status=ToolCallStatus.FAILED,
            error_code=error.code,
            retryable=True,
            resolution_source="control_plane",
        )
        return await self._handle_effect_call_failure(
            runtime,
            terminal_status=ToolCallStatus.FAILED,
            transition_committed=True,
        )


class TaskRuntimeUnavailableError(RuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task runtime is unavailable: {task_id}")
        self.task_id = task_id


class ModelInvocationFailedError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Model invocation failed with code {code}")
        self.code = code


class UnsupportedModelPlanError(RuntimeError):
    pass


class _ToolCallAlreadyFinalizedError(RuntimeError):
    """Internal signal that the TaskService already wrote the task terminal event."""
