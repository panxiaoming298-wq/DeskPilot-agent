"""Cooperatively controllable task processor using Model Gateway and Runner ports."""

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel

from deskpilot.application.model_gateway import ModelGateway, ModelGatewayError
from deskpilot.application.policy_engine import PolicyEngine
from deskpilot.application.runner_client import RunnerClientError
from deskpilot.application.runner_supervisor import RunnerSupervisor
from deskpilot.application.task_service import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    InvalidTaskTransitionError,
    InvalidToolCallTransitionError,
    TaskNotFoundError,
    TaskService,
    ToolAuthorizationError,
    ToolCallNotFoundError,
    ToolCallStatus,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import ApprovalRead, ApprovalStatus, DataEgress
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
    ReconciliationCompensationRead,
    ReconciliationEvidenceKind,
    ReconciliationEvidenceRefreshRead,
)
from deskpilot.domain.schemas import (
    FileMoveCompensationRequest,
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
FileMoveRuntimeRequest = FileMoveTaskRequest | FileMoveCompensationRequest


class ReconciliationCompensationResourceConflictError(ValueError):
    code = "RECONCILIATION_COMPENSATION_RESOURCE_CONFLICT"

    def __init__(self, reconciliation_id: str) -> None:
        super().__init__(
            f"Reconciliation {reconciliation_id} reverse resources no longer match"
        )
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
    approval_expiry_worker: asyncio.Task[None] | None = None


class _StageDisposition(StrEnum):
    CONTINUE = "continue"
    SUSPEND = "suspend"
    TERMINAL = "terminal"


class ApprovalContinuationState(StrEnum):
    READY = "ready"
    IN_PROGRESS = "in_progress"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TaskRuntimeRecoveryResult:
    restored_task_ids: frozenset[str] = frozenset()
    recoverable_requested_call_ids: frozenset[str] = frozenset()
    failed_task_ids: frozenset[str] = frozenset()


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
    ) -> None:
        self._task_service = task_service
        self._model_gateway = model_gateway
        self._policy_engine = policy_engine
        self._runner_client = runner_client
        self._step_delay_seconds = step_delay_seconds
        self._disk_usage_path = str(Path(disk_usage_path).expanduser().resolve(strict=True))
        self._model_timeout_seconds = model_timeout_seconds
        self._runtimes: dict[str, _TaskRuntime] = {}
        self._recovered_autostart: set[str] = set()

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

        for task_id in loaded.invalid_task_ids:
            await self._task_service.fail_task_checkpoint(
                task_id,
                code="TASK_CHECKPOINT_INVALID",
            )
            failed.add(task_id)

        for durable in loaded.checkpoints:
            task_id = durable.payload.task_id
            try:
                task = await self._task_service.get_task(task_id)
                runtime = await self._runtime_from_checkpoint(durable, task)
            except Exception:
                await self._task_service.fail_task_checkpoint(
                    task_id,
                    code="TASK_CHECKPOINT_BINDING_INVALID",
                )
                failed.add(task_id)
                continue
            if task_id in self._runtimes:
                raise RuntimeError(f"Task processor already knows {task_id}")
            self._runtimes[task_id] = runtime
            restored.add(task_id)
            if runtime.next_stage in {5, 6}:
                recoverable_calls.add(runtime.tool_call_id)
            if task.status in {
                TaskStatus.CREATED,
                TaskStatus.CLASSIFYING,
                TaskStatus.RUNNING,
            }:
                self._recovered_autostart.add(task_id)
            elif task.status is TaskStatus.WAITING_APPROVAL:
                if runtime.approval_id is None:
                    raise RuntimeError("Recovered approval task has no approval identity")
                approval = await self._task_service.get_approval(runtime.approval_id)
                if approval.status is ApprovalStatus.PENDING:
                    self._schedule_approval_expiry(runtime, approval)

        return TaskRuntimeRecoveryResult(
            restored_task_ids=frozenset(restored),
            recoverable_requested_call_ids=frozenset(recoverable_calls),
            failed_task_ids=frozenset(failed),
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

    async def cancel(self, task_id: str) -> None:
        await self._request_stop(task_id)
        runtime = self._runtimes.get(task_id)
        if runtime is None:
            return
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

    def can_resume(self, task_id: str) -> bool:
        runtime = self._runtimes.get(task_id)
        return (
            runtime is not None
            and runtime.approval_id is None
            and runtime.next_stage < self._stage_count
            and (runtime.worker is None or runtime.worker.done())
        )

    def has_runtime(self, task_id: str) -> bool:
        """Return whether this process already owns the task's runtime checkpoint."""
        return task_id in self._runtimes

    async def refresh_reconciliation_evidence(
        self,
        reconciliation_id: str,
    ) -> ReconciliationEvidenceRefreshRead:
        """Query signed Runner state and persist evidence without replaying the call."""
        reconciliation = await self._task_service.get_reconciliation(
            reconciliation_id
        )
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
            if (
                replay.task.status is TaskStatus.CREATED
                and not self.has_runtime(replay.task.task_id)
            ):
                request = (
                    await self._task_service.get_reconciliation_compensation_request(
                        reconciliation_id
                    )
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
            raise ReconciliationCompensationResourceConflictError(
                reconciliation_id
            ) from error
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
        if result.task.status is TaskStatus.CREATED and not self.has_runtime(
            result.task.task_id
        ):
            self.start(
                result.task.task_id,
                result.task.goal,
                privacy_mode=result.task.privacy_mode,
                constraints=tuple(result.task.constraints),
                tool_request=normalized_request,
            )
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
        if runtime is None or runtime.approval_id != approval_id or runtime.next_stage != 6:
            return ApprovalContinuationState.UNAVAILABLE
        if runtime.worker is None or runtime.worker.done():
            return ApprovalContinuationState.READY
        return ApprovalContinuationState.IN_PROGRESS

    def continue_after_approval(self, task_id: str, approval_id: str) -> None:
        runtime = self._runtimes.get(task_id)
        if runtime is None or not self.can_continue_after_approval(task_id, approval_id):
            raise TaskRuntimeUnavailableError(task_id)
        self._cancel_approval_expiry(runtime)
        runtime.stop_requested = False
        runtime.stop_signal.clear()
        self._start_worker(runtime)

    def forget(self, task_id: str) -> None:
        runtime = self._runtimes.pop(task_id, None)
        if runtime is not None:
            self._cancel_approval_expiry(runtime)
        self._model_gateway.forget_task_budget(task_id)

    async def shutdown(self) -> None:
        active = [
            runtime
            for runtime in self._runtimes.values()
            if runtime.worker is not None and not runtime.worker.done()
        ]
        for runtime in active:
            runtime.stop_requested = True
            runtime.stop_signal.set()
        for runtime in self._runtimes.values():
            self._cancel_approval_expiry(runtime)
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

    async def _runtime_from_checkpoint(
        self,
        durable: DurableTaskCheckpoint,
        task: TaskRead,
    ) -> _TaskRuntime:
        payload = durable.payload
        if durable.event_seq != task.last_event_seq:
            raise RuntimeError("Task checkpoint event binding is stale")
        if payload.tool_call_id != initial_tool_call_id(task.task_id):
            raise RuntimeError("Task checkpoint call identity is invalid")
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
            if call_status is not expected_call_status:
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
                if (
                    approval.task_id != task.task_id
                    or approval.call_id != payload.tool_call_id
                ):
                    raise RuntimeError("Task checkpoint approval binding is invalid")
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
                dict(payload.tool_arguments)
                if payload.tool_arguments is not None
                else None
            ),
            tool_resources=payload.tool_resources,
            expected_resource_versions=dict(payload.expected_resource_versions),
            tool_idempotency_key=payload.tool_idempotency_key,
            policy_request=payload.policy_request,
            policy_decision=payload.policy_decision,
            approval_id=payload.approval_id,
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
                runtime.next_stage += 1
                if (
                    disposition is not _StageDisposition.TERMINAL
                    and runtime.next_stage < self._stage_count
                ):
                    await self._task_service.save_task_checkpoint(
                        self._checkpoint(runtime)
                    )
                if disposition is _StageDisposition.SUSPEND:
                    if runtime.approval_id is not None:
                        approval = await self._task_service.get_approval(
                            runtime.approval_id
                        )
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
        except Exception:
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
        elif stage == 2:
            await self._task_service.transition_task(
                task_id,
                TaskStatus.RUNNING,
                command="processor",
                requested_by="system",
            )
        elif stage == 3:
            tool_step = self._tool_step(runtime)
            await self._task_service.append_event(
                task_id,
                "step.started",
                {
                    "step_id": tool_step.step_id,
                    "agent": tool_step.agent,
                    "title": tool_step.title,
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
                is_compensation = isinstance(
                    runtime.tool_request,
                    FileMoveCompensationRequest,
                )
                normalized_arguments = FileMoveInput(
                    source=runtime.tool_request.source,
                    destination=runtime.tool_request.destination,
                )
                runtime.tool_arguments = normalized_arguments.model_dump(mode="python")
                try:
                    runtime.tool_resources = await asyncio.to_thread(
                        project_file_move_resources,
                        normalized_arguments,
                    )
                except (OSError, ToolExecutorError):
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
                    assert isinstance(
                        runtime.tool_request,
                        FileMoveCompensationRequest,
                    )
                    if (
                        source_resource.version_digest
                        != runtime.tool_request.expected_source_version
                    ):
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
            await self._task_service.record_tool_requested(
                task_id,
                call_id=runtime.tool_call_id,
                step_id=tool_step.step_id,
                tool_name=contract.name,
                tool_version=contract.version,
                contract_digest=contract.digest,
                arguments=runtime.tool_arguments,
                idempotency=contract.execution.idempotency,
                idempotency_key=runtime.tool_idempotency_key,
                risk=contract.risk_level.value,
            )
        elif stage == 5:
            tool_step = self._tool_step(runtime)
            arguments = self._tool_arguments(runtime)
            contract = (
                FILE_MOVE_CONTRACT
                if runtime.tool_request is not None
                else DISK_USAGE_CONTRACT
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
            elif isinstance(runtime.tool_request, FileMoveCompensationRequest):
                approval_title = "撤销先前的单文件移动"
                approval_purpose = (
                    "按已验证的提交回执反向移动同一版本的文件；"
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
            )
            if decision.effect is PolicyEffect.DENY:
                return _StageDisposition.TERMINAL
            if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                if approval is None:
                    raise RuntimeError("Policy required approval but none was persisted")
                runtime.approval_id = approval.approval_id
                self._schedule_approval_expiry(runtime, approval)
                return _StageDisposition.SUSPEND
        elif stage == 6:
            tool_step = self._tool_step(runtime)
            if tool_step.tool_name is None or tool_step.tool_version is None:
                raise UnsupportedModelPlanError("Selected plan step has no tool reference")
            try:
                lease = self._runner_client.ensure_ready()
            except RunnerClientError as error:
                await self._finish_tool_without_dispatch(runtime, error)
                raise _ToolCallAlreadyFinalizedError from error

            authorization = await self._authorization_grant(runtime)
            arguments = self._tool_arguments(runtime)
            try:
                await self._task_service.start_tool_call(
                    task_id,
                    runtime.tool_call_id,
                    runner_id=lease.runner_id,
                    authorization=authorization,
                    arguments=arguments,
                    expected_resource_versions=runtime.expected_resource_versions,
                )
            except ApprovalExpiredError as error:
                raise _ToolCallAlreadyFinalizedError from error
            except ToolAuthorizationError as error:
                await self._task_service.finish_tool_call(
                    task_id,
                    runtime.tool_call_id,
                    status=ToolCallStatus.FAILED,
                    error_code=error.code,
                    retryable=False,
                    resolution_source="policy",
                )
                raise _ToolCallAlreadyFinalizedError from error
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
                await self._task_service.finish_tool_call(
                    task_id,
                    runtime.tool_call_id,
                    status=ToolCallStatus.UNKNOWN,
                    error_code=getattr(
                        error,
                        "code",
                        "RUNNER_CALL_OUTCOME_UNKNOWN",
                    ),
                    resolution_source="control_plane",
                )
                raise _ToolCallAlreadyFinalizedError from error

            if result.status == "succeeded" and result.output is not None:
                await self._task_service.finish_tool_call(
                    task_id,
                    runtime.tool_call_id,
                    status=ToolCallStatus.SUCCEEDED,
                    result=result.output,
                    fail_task=False,
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
                await self._task_service.finish_tool_call(
                    task_id,
                    runtime.tool_call_id,
                    status=terminal_status,
                    error_code=error_code,
                    retryable=(result.error.retryable if result.error is not None else False),
                )
                raise _ToolCallAlreadyFinalizedError
        elif stage == 7:
            tool_step = self._tool_step(runtime)
            await self._task_service.append_event(
                task_id,
                "step.completed",
                {"step_id": tool_step.step_id, "verified": True},
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
                rationale=(
                    "用户通过结构化本地表单明确选择单文件移动；路径不从模型文本提取。"
                ),
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
        if len(tool_steps) != 1:
            raise UnsupportedModelPlanError(
                "Current single-tool slice requires exactly one tool step"
            )
        step = tool_steps[0]
        expected_contract = (
            FILE_MOVE_CONTRACT
            if runtime.tool_request is not None
            else DISK_USAGE_CONTRACT
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
    ) -> None:
        await self._task_service.finish_tool_call(
            runtime.task_id,
            runtime.tool_call_id,
            status=ToolCallStatus.FAILED,
            error_code=error.code,
            retryable=True,
            resolution_source="control_plane",
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
