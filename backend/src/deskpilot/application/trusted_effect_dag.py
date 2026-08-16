"""Trusted v2 DAG preparation, ledger-bound Runner execution, and compensation."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from deskpilot.application.effect_dag_admission import (
    EffectDagAdmissionController,
    EffectDagAdmissionControllerPort,
    EffectDagAdmissionPermitPort,
    EffectDagAdmissionRequest,
)
from deskpilot.application.effect_dag_cluster_admission import (
    EffectDagAdmissionPermitLostError,
)
from deskpilot.application.effect_dag_dispatcher import (
    EffectBranchDecisionSelection,
    EffectNodeExecutionResult,
)
from deskpilot.application.policy_engine import PolicyEngine
from deskpilot.application.runner_client import ProgressCallback
from deskpilot.application.runner_supervisor import RunnerLease
from deskpilot.application.task_service import (
    EffectDagAdmissionProofRejectedError,
    EffectGraphCancelRequestedError,
    TaskService,
    ToolAuthorizationError,
    ToolCallStatus,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import ApprovalRead, ApprovalStatus, DataEgress
from deskpilot.domain.effect_graph import (
    EffectAttemptKind,
    EffectAttemptStatus,
    EffectCompensationPlanRead,
    EffectExecutionMode,
    EffectGraphStatus,
    EffectNodeClaimRead,
    EffectNodeRead,
    EffectNodeStatus,
    effect_attempt_id,
    effect_call_id,
)
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.schemas import (
    DiskPressureGuardedFileMoveRequest,
    FileMoveDagOperation,
    FileMoveDagRequest,
)
from deskpilot.domain.tool_contracts import ToolContract
from deskpilot.runner.ipc_protocol import ToolCallResult
from deskpilot.tools.computer import (
    DISK_USAGE_CONTRACT,
    DiskUsageInput,
    DiskUsageOutput,
    project_disk_usage_resources,
)
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FILE_MOVE_SOURCE_CAPABILITY,
    FileMoveInput,
    project_file_move_resources,
)


@dataclass(frozen=True, slots=True)
class EffectNodeMaterial:
    """Reconstructable sensitive material for one trusted graph attempt."""

    arguments: dict[str, object]
    actor: str
    idempotency_key: str
    expected_resource_versions: dict[str, str]
    policy_request: ToolAuthorizationRequest
    policy_decision: PolicyDecision
    contract: ToolContract


class EffectNodeMaterialResolver(Protocol):
    async def resolve(
        self,
        task_id: str,
        node: EffectNodeRead,
        kind: EffectAttemptKind,
    ) -> EffectNodeMaterial: ...


class LedgerRunnerPort(Protocol):
    def ensure_ready(self, *, expected_runner_id: str | None = None) -> RunnerLease: ...

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


class FileMoveDagMaterialResolver:
    """Resolve only application-validated file paths from a protected task request."""

    def __init__(
        self,
        task_service: TaskService,
        policy_engine: PolicyEngine,
        request: FileMoveDagRequest | DiskPressureGuardedFileMoveRequest,
    ) -> None:
        self._task_service = task_service
        self._policy_engine = policy_engine
        if isinstance(request, FileMoveDagRequest):
            self._operations: dict[
                str, FileMoveDagOperation | DiskPressureGuardedFileMoveRequest
            ] = {
                operation.operation_id: operation for operation in request.operations
            }
        else:
            self._operations = {"move_file": request}

    async def resolve(
        self,
        task_id: str,
        node: EffectNodeRead,
        kind: EffectAttemptKind,
    ) -> EffectNodeMaterial:
        operation = self._operations.get(node.node_key)
        if operation is None:
            raise ValueError("DAG node is not bound to the trusted file-move request")
        required_source_version: str | None = None
        if kind is EffectAttemptKind.FORWARD:
            file_input = FileMoveInput(
                source=operation.source,
                destination=operation.destination,
            )
        else:
            applied = next(
                (
                    effect
                    for effect in node.effects
                    if effect.kind is EffectAttemptKind.FORWARD and effect.receipt_id is not None
                ),
                None,
            )
            if applied is None or applied.receipt_id is None:
                raise ValueError("Compensation node has no receipt-bound forward effect")
            receipt = await self._task_service.get_commit_receipt(applied.receipt_id)
            required_source_version = receipt.resource_versions_after.get("destination")
            if required_source_version in {None, "absent"}:
                raise ValueError("Forward receipt omitted the reverse source version")
            file_input = FileMoveInput(
                source=operation.destination,
                destination=operation.source,
            )
        resources = await asyncio.to_thread(project_file_move_resources, file_input)
        source_resource = next(
            resource
            for resource in resources
            if resource.operations == (FILE_MOVE_SOURCE_CAPABILITY,)
        )
        if source_resource.version_digest is None:
            raise ValueError("file.move source projection omitted its version")
        if (
            required_source_version is not None
            and source_resource.version_digest != required_source_version
        ):
            raise ValueError("Receipt-bound reverse source version changed")
        arguments = file_input.model_dump(mode="python")
        expected_versions = {
            "destination": "absent",
            "source": source_resource.version_digest,
        }
        call_id = effect_call_id(node.node_id, kind)
        request = ToolAuthorizationRequest(
            task_id=task_id,
            step_id=node.step_id,
            call_id=call_id,
            actor="local_user",
            origin="builtin",
            tool_name=node.tool_name,
            tool_version=node.tool_version,
            contract_digest=node.contract_digest,
            arguments_digest=sha256_digest(arguments),
            risk_level=FILE_MOVE_CONTRACT.risk_level,
            side_effects=FILE_MOVE_CONTRACT.side_effects,
            reversible=FILE_MOVE_CONTRACT.reversible,
            capabilities=FILE_MOVE_CONTRACT.security.capabilities,
            network_access=FILE_MOVE_CONTRACT.security.network_access,
            data_egress=False,
            resources=resources,
            expected_resource_versions_digest=sha256_digest(expected_versions),
            interactive=True,
            batch_count=1,
        )
        return EffectNodeMaterial(
            arguments=arguments,
            actor="local_user",
            idempotency_key=f"dag:{effect_attempt_id(node.node_id, kind)}",
            expected_resource_versions=expected_versions,
            policy_request=request,
            policy_decision=self._policy_engine.evaluate(request),
            contract=FILE_MOVE_CONTRACT,
        )


class DiskPressureGuardedMaterialResolver:
    """Resolve the fixed read/write nodes of the disk-pressure business graph."""

    _READ_NODE_KEYS = frozenset({"inspect_capacity", "confirm_deferred"})

    def __init__(
        self,
        task_service: TaskService,
        policy_engine: PolicyEngine,
        request: DiskPressureGuardedFileMoveRequest,
    ) -> None:
        self._policy_engine = policy_engine
        self._request = request
        self._disk_path = str(Path(request.destination).parent)
        self._file_move = FileMoveDagMaterialResolver(
            task_service,
            policy_engine,
            request,
        )

    async def resolve(
        self,
        task_id: str,
        node: EffectNodeRead,
        kind: EffectAttemptKind,
    ) -> EffectNodeMaterial:
        if node.node_key == "move_file":
            return await self._file_move.resolve(task_id, node, kind)
        if node.node_key not in self._READ_NODE_KEYS or kind is not EffectAttemptKind.FORWARD:
            raise ValueError("Node is not bound to the disk-pressure business graph")
        disk_input = DiskUsageInput(path=self._disk_path)
        arguments = disk_input.model_dump(mode="python")
        resources = await asyncio.to_thread(project_disk_usage_resources, disk_input)
        expected_versions: dict[str, str] = {}
        call_id = effect_call_id(node.node_id, kind)
        policy_request = ToolAuthorizationRequest(
            task_id=task_id,
            step_id=node.step_id,
            call_id=call_id,
            actor="local_user",
            origin="builtin",
            tool_name=node.tool_name,
            tool_version=node.tool_version,
            contract_digest=node.contract_digest,
            arguments_digest=sha256_digest(arguments),
            risk_level=DISK_USAGE_CONTRACT.risk_level,
            side_effects=DISK_USAGE_CONTRACT.side_effects,
            reversible=DISK_USAGE_CONTRACT.reversible,
            capabilities=DISK_USAGE_CONTRACT.security.capabilities,
            network_access=DISK_USAGE_CONTRACT.security.network_access,
            data_egress=False,
            resources=resources,
            expected_resource_versions_digest=sha256_digest(expected_versions),
            interactive=True,
            batch_count=1,
        )
        return EffectNodeMaterial(
            arguments=arguments,
            actor="local_user",
            idempotency_key=f"dag:{effect_attempt_id(node.node_id, kind)}",
            expected_resource_versions=expected_versions,
            policy_request=policy_request,
            policy_decision=self._policy_engine.evaluate(policy_request),
            contract=DISK_USAGE_CONTRACT,
        )


class DiskPressureBranchDecisionResolver:
    """Select move/defer only from the persisted, ledger-bound disk usage result."""

    def __init__(
        self,
        task_service: TaskService,
        request: DiskPressureGuardedFileMoveRequest,
    ) -> None:
        self._task_service = task_service
        self._request = request
        self._disk_path = str(Path(request.destination).parent)

    async def resolve(
        self,
        task_id: str,
        source_node: EffectNodeRead,
        decision_key: str,
        declared_outcomes: tuple[str, ...],
    ) -> EffectBranchDecisionSelection | None:
        if (
            source_node.node_key != "inspect_capacity"
            or decision_key != "disk_pressure_route"
            or declared_outcomes != ("defer", "move")
        ):
            raise ValueError("Branch is not declared by the disk-pressure business graph")
        call_id = effect_call_id(source_node.node_id, EffectAttemptKind.FORWARD)
        completed = next(
            (
                event
                for event in reversed(await self._task_service.list_events(task_id))
                if event.type == "tool.completed"
                and event.payload.get("call_id") == call_id
            ),
            None,
        )
        if completed is None:
            return None
        if (
            completed.payload.get("tool") != DISK_USAGE_CONTRACT.name
            or completed.payload.get("tool_version") != DISK_USAGE_CONTRACT.version
            or completed.payload.get("contract_digest") != DISK_USAGE_CONTRACT.digest
            or completed.payload.get("status") != "succeeded"
        ):
            raise ValueError("Disk-pressure evidence is not bound to the expected Tool ledger")
        output = DiskUsageOutput.model_validate(completed.payload.get("result"))
        if output.resolved_path != self._disk_path:
            raise ValueError("Disk-pressure evidence resolved an unexpected filesystem path")
        outcome = (
            "move"
            if output.used_percent <= self._request.maximum_used_percent
            else "defer"
        )
        evidence = {
            "schema_version": "deskpilot.disk-pressure-branch-evidence.v1",
            "task_id": task_id,
            "source_node_id": source_node.node_id,
            "call_id": call_id,
            "terminal_event_id": completed.event_id,
            "terminal_event_seq": completed.seq,
            "result_digest": sha256_digest(output.model_dump(mode="json")),
            "maximum_used_percent": self._request.maximum_used_percent,
            "observed_used_percent": output.used_percent,
            "comparison": "less_than_or_equal",
            "outcome": outcome,
        }
        return EffectBranchDecisionSelection(
            outcome=outcome,
            evidence_digest=sha256_digest(evidence),
        )


@dataclass(frozen=True, slots=True)
class EffectPreparationResult:
    pending_approval_ids: tuple[str, ...] = ()
    denied_node_ids: tuple[str, ...] = ()


class EffectDagLedgerPreparer:
    """Create per-node call/attempt/policy truth before a node becomes claimable."""

    def __init__(
        self,
        task_service: TaskService,
        resolver: EffectNodeMaterialResolver,
    ) -> None:
        self._task_service = task_service
        self._resolver = resolver

    async def prepare_nodes(
        self,
        task_id: str,
        nodes: tuple[EffectNodeRead, ...],
        *,
        kind: EffectAttemptKind,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectPreparationResult:
        approvals = await self._task_service.list_approvals(task_id=task_id)
        approvals_by_call = {approval.call_id: approval for approval in approvals}
        pending: list[str] = []
        denied: list[str] = []
        for node in nodes:
            attempt_id = effect_attempt_id(node.node_id, kind)
            call_id = effect_call_id(node.node_id, kind)
            attempt = next(
                (attempt for attempt in node.attempts if attempt.attempt_id == attempt_id),
                None,
            )
            waiting_status = EffectNodeStatus.WAITING_APPROVAL
            resting_status = (
                EffectNodeStatus.PENDING
                if kind is EffectAttemptKind.FORWARD
                else EffectNodeStatus.SUCCEEDED
            )
            failed_status = (
                EffectNodeStatus.FAILED
                if kind is EffectAttemptKind.FORWARD
                else EffectNodeStatus.COMPENSATION_FAILED
            )
            graph_status = (
                EffectGraphStatus.ACTIVE
                if kind is EffectAttemptKind.FORWARD
                else EffectGraphStatus.COMPENSATING
            )
            execution_mode = (
                EffectExecutionMode.FORWARD
                if kind is EffectAttemptKind.FORWARD
                else EffectExecutionMode.COMPENSATING
            )
            if attempt is not None:
                approval = approvals_by_call.get(call_id)
                if node.status is waiting_status:
                    if approval is None or approval.status is ApprovalStatus.PENDING:
                        if approval is not None:
                            pending.append(approval.approval_id)
                        continue
                    if approval.status is ApprovalStatus.APPROVED:
                        await self._task_service.transition_effect_node(
                            task_id,
                            node.node_id,
                            expected_statuses=frozenset({waiting_status}),
                            target_status=resting_status,
                            transition_kind="approval_ready",
                            event_type="effect.authorization.ready",
                            graph_status=graph_status,
                            execution_mode=execution_mode,
                            lease_owner_id=lease_owner_id,
                            fencing_token=fencing_token,
                        )
                    else:
                        denied.append(node.node_id)
                continue

            material = await self._resolver.resolve(task_id, node, kind)
            await self._task_service.request_effect_tool_call(
                task_id,
                node.node_id,
                call_id=call_id,
                attempt_id=attempt_id,
                attempt_kind=kind,
                step_id=node.step_id,
                tool_name=node.tool_name,
                tool_version=node.tool_version,
                contract_digest=node.contract_digest,
                arguments=material.arguments,
                idempotency=material.contract.execution.idempotency,
                idempotency_key=material.idempotency_key,
                tool_attempt=1 if kind is EffectAttemptKind.FORWARD else 2,
                risk=material.contract.risk_level.value,
                checkpoint=None,
                lease_owner_id=lease_owner_id,
                fencing_token=fencing_token,
            )
            approval = await self._task_service.apply_policy_decision(
                task_id,
                call_id,
                request=material.policy_request,
                decision=material.policy_decision,
                title=self._approval_title(material.contract, kind),
                purpose=self._approval_purpose(material.contract, kind),
                consequences=(
                    "每个 DAG 节点使用独立账本、一次性授权和提交回执。",
                    "资源版本变化时不会执行。",
                ),
                data_egress=DataEgress(enabled=False),
                expected_resource_versions=material.expected_resource_versions,
                fail_task_on_deny=False,
            )
            if material.policy_decision.effect is PolicyEffect.DENY:
                await self._task_service.transition_effect_node(
                    task_id,
                    node.node_id,
                    expected_statuses=frozenset({resting_status}),
                    target_status=failed_status,
                    transition_kind="policy_denied",
                    event_type=(
                        "effect.compensation.failed"
                        if kind is EffectAttemptKind.COMPENSATION
                        else "effect.node.failed"
                    ),
                    attempt_id=attempt_id,
                    attempt_status=EffectAttemptStatus.FAILED,
                    graph_status=graph_status,
                    execution_mode=execution_mode,
                    lease_owner_id=lease_owner_id,
                    fencing_token=fencing_token,
                )
                denied.append(node.node_id)
            elif material.policy_decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                if approval is None:
                    raise RuntimeError("Policy required an approval but none was persisted")
                await self._task_service.transition_effect_node(
                    task_id,
                    node.node_id,
                    expected_statuses=frozenset({resting_status}),
                    target_status=waiting_status,
                    transition_kind="approval_required",
                    event_type="effect.authorization.waiting",
                    graph_status=graph_status,
                    execution_mode=execution_mode,
                    lease_owner_id=lease_owner_id,
                    fencing_token=fencing_token,
                )
                pending.append(approval.approval_id)
        return EffectPreparationResult(
            pending_approval_ids=tuple(pending),
            denied_node_ids=tuple(denied),
        )

    @staticmethod
    def _approval_title(contract: ToolContract, kind: EffectAttemptKind) -> str:
        if contract.key == FILE_MOVE_CONTRACT.key:
            return (
                "撤销 DAG 节点的文件移动"
                if kind is EffectAttemptKind.COMPENSATION
                else "执行 DAG 节点的文件移动"
            )
        return "执行 DAG 只读磁盘检查"

    @staticmethod
    def _approval_purpose(contract: ToolContract, kind: EffectAttemptKind) -> str:
        if contract.key == FILE_MOVE_CONTRACT.key:
            return (
                "按已验证的提交回执反向移动同一版本的文件。"
                if kind is EffectAttemptKind.COMPENSATION
                else "执行受信应用预先构建的 v2 DAG 文件移动节点。"
            )
        return "读取目标磁盘容量，形成受信条件分支证据。"


class LedgerBoundEffectNodeExecutor:
    """Execute a claimed node through Tool call, policy, authorization, and receipt ledgers."""

    def __init__(
        self,
        task_service: TaskService,
        runner: LedgerRunnerPort,
        resolver: EffectNodeMaterialResolver,
        *,
        kind: EffectAttemptKind,
        graph_lease_owner_id: str | None = None,
        graph_fencing_token: int | None = None,
    ) -> None:
        self._task_service = task_service
        self._runner = runner
        self._resolver = resolver
        self._kind = kind
        self._graph_lease_owner_id = graph_lease_owner_id
        self._graph_fencing_token = graph_fencing_token
        self._active_calls: dict[tuple[str, int], tuple[str, str]] = {}
        self._cancelled_claims: set[tuple[str, int]] = set()
        self._calls_lock = asyncio.Lock()

    def bind_graph_lease(self, owner_id: str, fencing_token: int) -> None:
        """Bind the exact lease acquired by the dispatcher immediately before claims."""
        self._graph_lease_owner_id = owner_id
        self._graph_fencing_token = fencing_token

    async def execute(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
    ) -> EffectNodeExecutionResult:
        owner_id = self._graph_lease_owner_id
        fencing_token = self._graph_fencing_token
        if owner_id is None or fencing_token is None:
            raise RuntimeError("Ledger executor has no active graph lease binding")
        call_id = effect_call_id(node.node_id, self._kind)
        attempt_id = effect_attempt_id(node.node_id, self._kind)
        claim_key = (claim.node_id, claim.fencing_token)
        try:
            material = await self._resolver.resolve(task_id, node, self._kind)
            approvals = await self._task_service.list_approvals(task_id=task_id)
            approval = next(
                (approval for approval in approvals if approval.call_id == call_id),
                None,
            )
            authorization = self._authorization(material, approval)
            lease = self._runner.ensure_ready()
            graph = await self._task_service.get_effect_graph(task_id)
        except Exception as error:
            await self._finish(
                task_id,
                node,
                claim,
                status=ToolCallStatus.FAILED,
                result=None,
                error_code=getattr(error, "code", "DAG_PRE_DISPATCH_FAILED"),
            )
            return EffectNodeExecutionResult(
                status=self._target_status(ToolCallStatus.FAILED),
                error_code=getattr(error, "code", "DAG_PRE_DISPATCH_FAILED"),
                transition_committed=True,
            )
        async with self._calls_lock:
            cancelled_before_start = claim_key in self._cancelled_claims
        if cancelled_before_start:
            await self._finish(
                task_id,
                node,
                claim,
                status=ToolCallStatus.CANCELLED,
                result=None,
                error_code="DAG_CANCELLED_BEFORE_RUNNER_DISPATCH",
            )
            return EffectNodeExecutionResult(
                status=self._target_status(ToolCallStatus.CANCELLED),
                error_code="DAG_CANCELLED_BEFORE_RUNNER_DISPATCH",
                transition_committed=True,
            )
        try:
            await self._task_service.start_tool_call(
                task_id,
                call_id,
                runner_id=lease.runner_id,
                authorization=authorization,
                arguments=material.arguments,
                expected_resource_versions=material.expected_resource_versions,
                effect_node_id=node.node_id,
                effect_attempt_id=attempt_id,
                effect_graph_status=(
                    EffectGraphStatus.ACTIVE
                    if self._kind is EffectAttemptKind.FORWARD
                    else EffectGraphStatus.COMPENSATING
                ),
                effect_execution_mode=(
                    EffectExecutionMode.FORWARD
                    if self._kind is EffectAttemptKind.FORWARD
                    else EffectExecutionMode.COMPENSATING
                ),
                effect_failure_node_id=graph.failure_node_id,
                lease_owner_id=owner_id,
                fencing_token=fencing_token,
                node_claim_owner_id=claim.owner_id,
                node_claim_fencing_token=claim.fencing_token,
            )
        except EffectGraphCancelRequestedError as error:
            await self._finish(
                task_id,
                node,
                claim,
                status=ToolCallStatus.CANCELLED,
                result=None,
                error_code=error.code,
            )
            return EffectNodeExecutionResult(
                status=self._target_status(ToolCallStatus.CANCELLED),
                error_code=error.code,
                transition_committed=True,
            )
        except Exception as error:
            await self._finish(
                task_id,
                node,
                claim,
                status=ToolCallStatus.FAILED,
                result=None,
                error_code=getattr(error, "code", "DAG_AUTHORIZATION_INVALID"),
            )
            return EffectNodeExecutionResult(
                status=self._target_status(ToolCallStatus.FAILED),
                error_code=getattr(error, "code", "DAG_AUTHORIZATION_INVALID"),
                transition_committed=True,
            )
        async with self._calls_lock:
            self._active_calls[claim_key] = (call_id, lease.runner_id)
            cancelled_before_dispatch = claim_key in self._cancelled_claims
        if cancelled_before_dispatch:
            async with self._calls_lock:
                self._active_calls.pop(claim_key, None)
                self._cancelled_claims.discard(claim_key)
            await self._finish(
                task_id,
                node,
                claim,
                status=ToolCallStatus.CANCELLED,
                result=None,
                error_code="DAG_CANCELLED_BEFORE_RUNNER_DISPATCH",
            )
            return EffectNodeExecutionResult(
                status=self._target_status(ToolCallStatus.CANCELLED),
                error_code="DAG_CANCELLED_BEFORE_RUNNER_DISPATCH",
                transition_committed=True,
            )
        try:
            result = await self._runner.call_tool(
                task_id=task_id,
                step_id=node.step_id,
                tool_name=node.tool_name,
                tool_version=node.tool_version,
                arguments=material.arguments,
                actor=material.actor,
                expected_runner_id=lease.runner_id,
                call_id=call_id,
                idempotency_key=material.idempotency_key,
                expected_resource_versions=material.expected_resource_versions,
                authorization=authorization,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._finish(
                task_id,
                node,
                claim,
                status=ToolCallStatus.UNKNOWN,
                result=None,
                error_code=getattr(error, "code", "RUNNER_CALL_OUTCOME_UNKNOWN"),
            )
            return EffectNodeExecutionResult(
                status=self._target_status(ToolCallStatus.UNKNOWN),
                error_code=getattr(error, "code", "RUNNER_CALL_OUTCOME_UNKNOWN"),
                transition_committed=True,
            )
        finally:
            async with self._calls_lock:
                self._active_calls.pop(claim_key, None)
                self._cancelled_claims.discard(claim_key)
        status = ToolCallStatus(result.status)
        await self._finish(
            task_id,
            node,
            claim,
            status=status,
            result=result.output if status is ToolCallStatus.SUCCEEDED else None,
            error_code=result.error.code if result.error is not None else None,
            retryable=result.error.retryable if result.error is not None else False,
        )
        return EffectNodeExecutionResult(
            status=self._target_status(status),
            error_code=result.error.code if result.error is not None else None,
            transition_committed=True,
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
            active = self._active_calls.get(claim_key)
        if active is None:
            return
        call_id, runner_id = active
        try:
            await self._runner.cancel_call(
                call_id,
                reason,
                expected_runner_id=runner_id,
            )
        except Exception:
            # A lost generation is resolved by the already-running call path as unknown.
            return

    async def _finish(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        *,
        status: ToolCallStatus,
        result: dict[str, Any] | None,
        error_code: str | None,
        retryable: bool = False,
    ) -> None:
        owner_id = self._graph_lease_owner_id
        fencing_token = self._graph_fencing_token
        if owner_id is None or fencing_token is None:
            raise RuntimeError("Ledger executor lost its graph lease binding")
        target = self._target_status(status)
        attempt_status = EffectAttemptStatus(status.value)
        await self._task_service.finish_effect_tool_call(
            task_id,
            node.node_id,
            call_id=effect_call_id(node.node_id, self._kind),
            attempt_id=effect_attempt_id(node.node_id, self._kind),
            status=status,
            result=result,
            error_code=error_code,
            retryable=retryable,
            target_status=target,
            transition_kind=(
                f"compensation_{status.value}"
                if self._kind is EffectAttemptKind.COMPENSATION
                else f"forward_{status.value}"
            ),
            event_type=(
                f"effect.compensation.{target.value}"
                if self._kind is EffectAttemptKind.COMPENSATION
                else f"effect.node.{target.value}"
            ),
            attempt_status=attempt_status,
            graph_status=(
                EffectGraphStatus.COMPENSATING
                if self._kind is EffectAttemptKind.COMPENSATION
                else EffectGraphStatus.ACTIVE
            ),
            execution_mode=(
                EffectExecutionMode.COMPENSATING
                if self._kind is EffectAttemptKind.COMPENSATION
                else EffectExecutionMode.FORWARD
            ),
            failure_node_id=(
                node.node_id
                if self._kind is EffectAttemptKind.FORWARD
                and status is not ToolCallStatus.SUCCEEDED
                else (await self._task_service.get_effect_graph(task_id)).failure_node_id
            ),
            create_effect=status is ToolCallStatus.SUCCEEDED,
            lease_owner_id=owner_id,
            fencing_token=fencing_token,
            node_claim_owner_id=claim.owner_id,
            node_claim_fencing_token=claim.fencing_token,
        )

    def _authorization(
        self,
        material: EffectNodeMaterial,
        approval: ApprovalRead | None,
    ) -> ToolAuthorizationGrant:
        decision = material.policy_decision
        request = material.policy_request
        approval_id: str | None = None
        preview_hash: str | None = None
        approved_at = None
        grant_expires_at = None
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            if approval is None or approval.status is not ApprovalStatus.APPROVED:
                raise ToolAuthorizationError(request.call_id, "DAG approval is not approved")
            if approval.resolved_at is None:
                raise ToolAuthorizationError(request.call_id, "DAG approval has no timestamp")
            approval_id = approval.approval_id
            preview_hash = approval.preview_hash
            approved_at = approval.resolved_at
            grant_expires_at = approval.expires_at
        elif decision.effect is not PolicyEffect.ALLOW:
            raise ToolAuthorizationError(request.call_id, "DAG policy did not allow execution")
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
            expected_resource_versions_digest=request.expected_resource_versions_digest,
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

    def _target_status(self, status: ToolCallStatus) -> EffectNodeStatus:
        if self._kind is EffectAttemptKind.FORWARD:
            return {
                ToolCallStatus.SUCCEEDED: EffectNodeStatus.SUCCEEDED,
                ToolCallStatus.FAILED: EffectNodeStatus.FAILED,
                ToolCallStatus.CANCELLED: EffectNodeStatus.CANCELLED,
                ToolCallStatus.UNKNOWN: EffectNodeStatus.UNKNOWN,
            }[status]
        return {
            ToolCallStatus.SUCCEEDED: EffectNodeStatus.COMPENSATED,
            ToolCallStatus.FAILED: EffectNodeStatus.COMPENSATION_FAILED,
            ToolCallStatus.CANCELLED: EffectNodeStatus.COMPENSATION_FAILED,
            ToolCallStatus.UNKNOWN: EffectNodeStatus.COMPENSATION_UNKNOWN,
        }[status]


@dataclass(frozen=True, slots=True)
class CompensationDispatchResult:
    graph_status: EffectGraphStatus
    plan: EffectCompensationPlanRead
    completed_waves: int
    pending_approval_ids: tuple[str, ...] = ()


class EffectDagCompensationDispatcher:
    """Consume durable compensation waves with a strict barrier between waves."""

    def __init__(
        self,
        task_service: TaskService,
        runner: LedgerRunnerPort,
        resolver: EffectNodeMaterialResolver,
        *,
        instance_id: str,
        max_concurrency: int = 4,
        graph_lease_ttl_seconds: float = 15,
        node_claim_ttl_seconds: float = 15,
        admission_controller: EffectDagAdmissionControllerPort | None = None,
    ) -> None:
        self._task_service = task_service
        self._runner = runner
        self._resolver = resolver
        self._instance_id = instance_id
        self._max_concurrency = max_concurrency
        self._graph_ttl = graph_lease_ttl_seconds
        self._node_ttl = node_claim_ttl_seconds
        self._admission = admission_controller or EffectDagAdmissionController(
            global_limit=max_concurrency,
            per_graph_limit=max_concurrency,
            default_tool_limit=max_concurrency,
        )

    async def run(
        self,
        task_id: str,
        *,
        plan_id: str | None = None,
    ) -> CompensationDispatchResult:
        lease = await self._task_service.acquire_effect_graph_lease(
            task_id,
            owner_id=self._instance_id,
            ttl_seconds=self._graph_ttl,
        )
        stop_graph_heartbeat = asyncio.Event()
        graph_heartbeat = asyncio.create_task(
            self._renew_graph(task_id, lease.fencing_token, stop_graph_heartbeat),
            name=f"dag-compensation-graph-heartbeat:{task_id}",
        )
        completed_waves = 0
        try:
            plan = (
                await self._task_service.get_effect_dag_compensation_plan(task_id, plan_id)
                if plan_id is not None
                else await self._task_service.plan_effect_dag_compensation(
                    task_id,
                    lease_owner_id=self._instance_id,
                    fencing_token=lease.fencing_token,
                )
            )
            preparer = EffectDagLedgerPreparer(self._task_service, self._resolver)
            for wave in plan.waves:
                graph = await self._task_service.get_effect_graph(task_id)
                nodes_by_id = {node.node_id: node for node in graph.nodes}
                wave_nodes = tuple(nodes_by_id[node_id] for node_id in wave.node_ids)
                if all(node.status is EffectNodeStatus.COMPENSATED for node in wave_nodes):
                    completed_waves += 1
                    continue
                preparation = await preparer.prepare_nodes(
                    task_id,
                    tuple(
                        node
                        for node in wave_nodes
                        if node.status
                        in {
                            EffectNodeStatus.SUCCEEDED,
                            EffectNodeStatus.WAITING_APPROVAL,
                        }
                    ),
                    kind=EffectAttemptKind.COMPENSATION,
                    lease_owner_id=self._instance_id,
                    fencing_token=lease.fencing_token,
                )
                if preparation.pending_approval_ids:
                    return CompensationDispatchResult(
                        graph_status=EffectGraphStatus.COMPENSATING,
                        plan=plan,
                        completed_waves=completed_waves,
                        pending_approval_ids=preparation.pending_approval_ids,
                    )
                graph = await self._task_service.get_effect_graph(task_id)
                nodes_by_id = {node.node_id: node for node in graph.nodes}
                remaining = [
                    node_id
                    for node_id in wave.node_ids
                    if nodes_by_id[node_id].status is EffectNodeStatus.SUCCEEDED
                ]
                while remaining:
                    selected = tuple(remaining[: self._max_concurrency])
                    permits = await self._admission.acquire_batch(
                        graph.graph_id,
                        tuple(
                            EffectDagAdmissionRequest(
                                node_id=node_id,
                                tool_name=nodes_by_id[node_id].tool_name,
                            )
                            for node_id in selected
                        ),
                    )
                    admitted_ids = tuple(permit.request.node_id for permit in permits)
                    try:
                        admission_proofs = {
                            permit.request.node_id: permit.proof
                            for permit in permits
                            if permit.proof is not None
                        }
                        try:
                            claims = await self._task_service.claim_effect_dag_compensation_nodes(
                                task_id,
                                admitted_ids,
                                plan_id=plan.plan_id,
                                wave_ordinal=wave.ordinal,
                                claim_owner_id=self._instance_id,
                                claim_ttl_seconds=self._node_ttl,
                                lease_owner_id=self._instance_id,
                                fencing_token=lease.fencing_token,
                                admission_proofs=admission_proofs or None,
                            )
                        except EffectDagAdmissionProofRejectedError:
                            continue
                        graph = await self._task_service.get_effect_graph(task_id)
                        nodes_by_id = {node.node_id: node for node in graph.nodes}
                        executor = LedgerBoundEffectNodeExecutor(
                            self._task_service,
                            self._runner,
                            self._resolver,
                            kind=EffectAttemptKind.COMPENSATION,
                            graph_lease_owner_id=self._instance_id,
                            graph_fencing_token=lease.fencing_token,
                        )
                        permit_by_node = {permit.request.node_id: permit for permit in permits}
                        outcomes = await asyncio.gather(
                            *(
                                self._execute_admitted_compensation(
                                    task_id,
                                    nodes_by_id[claim.node_id],
                                    claim,
                                    executor,
                                    lease.fencing_token,
                                    permit_by_node[claim.node_id],
                                )
                                for claim in claims
                            )
                        )
                        if not all(outcomes):
                            raise EffectDagAdmissionPermitLostError(graph.graph_id)
                    finally:
                        await asyncio.gather(*(permit.release() for permit in permits))
                    admitted = set(admitted_ids)
                    remaining = [node_id for node_id in remaining if node_id not in admitted]
                graph = await self._task_service.reduce_effect_dag_compensation(
                    task_id,
                    plan_id=plan.plan_id,
                    lease_owner_id=self._instance_id,
                    fencing_token=lease.fencing_token,
                )
                if graph.status is not EffectGraphStatus.COMPENSATING:
                    return CompensationDispatchResult(
                        graph_status=graph.status,
                        plan=plan,
                        completed_waves=completed_waves,
                    )
                completed_waves += 1
            graph = await self._task_service.reduce_effect_dag_compensation(
                task_id,
                plan_id=plan.plan_id,
                lease_owner_id=self._instance_id,
                fencing_token=lease.fencing_token,
            )
            return CompensationDispatchResult(
                graph_status=graph.status,
                plan=plan,
                completed_waves=completed_waves,
            )
        finally:
            stop_graph_heartbeat.set()
            await asyncio.gather(graph_heartbeat, return_exceptions=True)
            await self._task_service.release_effect_graph_lease(
                task_id,
                owner_id=self._instance_id,
                fencing_token=lease.fencing_token,
            )

    async def _execute_with_heartbeat(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        executor: LedgerBoundEffectNodeExecutor,
        graph_fencing_token: int,
    ) -> None:
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_node(task_id, claim, graph_fencing_token, stop),
            name=f"dag-compensation-node-heartbeat:{claim.node_id}",
        )
        try:
            await executor.execute(task_id, node, claim)
            if heartbeat.done():
                heartbeat.result()
        finally:
            stop.set()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _execute_admitted_compensation(
        self,
        task_id: str,
        node: EffectNodeRead,
        claim: EffectNodeClaimRead,
        executor: LedgerBoundEffectNodeExecutor,
        graph_fencing_token: int,
        permit: EffectDagAdmissionPermitPort,
    ) -> bool:
        try:
            await permit.run(
                self._execute_with_heartbeat(
                    task_id,
                    node,
                    claim,
                    executor,
                    graph_fencing_token,
                )
            )
            return True
        except EffectDagAdmissionPermitLostError:
            return False

    async def _renew_graph(
        self,
        task_id: str,
        fencing_token: int,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._graph_ttl / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                await self._task_service.renew_effect_graph_lease(
                    task_id,
                    owner_id=self._instance_id,
                    fencing_token=fencing_token,
                    ttl_seconds=self._graph_ttl,
                )

    async def _renew_node(
        self,
        task_id: str,
        claim: EffectNodeClaimRead,
        graph_fencing_token: int,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._node_ttl / 3)
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
                    claim_ttl_seconds=self._node_ttl,
                    lease_owner_id=self._instance_id,
                    fencing_token=graph_fencing_token,
                )


__all__ = [
    "CompensationDispatchResult",
    "EffectDagCompensationDispatcher",
    "EffectDagLedgerPreparer",
    "EffectNodeMaterial",
    "EffectNodeMaterialResolver",
    "EffectPreparationResult",
    "FileMoveDagMaterialResolver",
    "LedgerBoundEffectNodeExecutor",
]
