import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.task_service import (
    ReconciliationAttemptAlreadyCreatedError,
    ReconciliationAttemptNotAllowedError,
    ReconciliationIdempotencyConflictError,
    TaskService,
    ToolCallStatus,
    ToolIdempotencyKeyAlreadyUsedError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import DataEgress
from deskpilot.domain.policy import (
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.reconciliations import (
    ReconciliationOutcome,
    ReconciliationStatus,
)
from deskpilot.domain.schemas import TaskCreate, TaskEventRead, TaskStatus
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import ToolIdempotency, ToolRiskLevel
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    OutboxMessageRecord,
    TaskEventRecord,
    TaskRecord,
    ToolCallRecord,
    ToolCommitReceiptRecord,
    ToolIdempotencyReceiptRecord,
    ToolReconciliationRecord,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT
from deskpilot.tools.files import FILE_MOVE_CONTRACT

CONTRACT_DIGEST = DISK_USAGE_CONTRACT.digest


async def _service(tmp_path: Path, name: str = "tool-calls.db") -> tuple[Database, TaskService]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")
    await database.migrate()
    return database, TaskService(database, "/api/v1")


async def _running_task(service: TaskService, goal: str) -> str:
    task = await service.create_task(TaskCreate(goal=goal))
    await service.transition_task(
        task.task_id,
        TaskStatus.CLASSIFYING,
        command="test",
        requested_by="system",
    )
    await service.transition_task(
        task.task_id,
        TaskStatus.RUNNING,
        command="test",
        requested_by="system",
    )
    return task.task_id


async def _record_requested(
    service: TaskService,
    task_id: str,
    *,
    call_id: str,
    step_id: str = "step-1",
    arguments: dict[str, Any] | None = None,
    idempotency: ToolIdempotency = ToolIdempotency.IDEMPOTENT,
    idempotency_key: str | None = None,
) -> None:
    await service.record_tool_requested(
        task_id,
        call_id=call_id,
        step_id=step_id,
        tool_name="computer.disk_usage",
        tool_version="1.0.0",
        contract_digest=CONTRACT_DIGEST,
        arguments=arguments or {"path": "."},
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        risk="R0",
    )


async def _start_allowed_call(
    service: TaskService,
    task_id: str,
    call_id: str,
    *,
    runner_id: str,
    step_id: str = "step-1",
    arguments: dict[str, Any] | None = None,
) -> TaskEventRead:
    resolved_arguments = arguments or {"path": "."}
    resource = PolicyResource(
        kind="filesystem_path",
        identifier=".",
        operations=("filesystem.metadata.read",),
        display_name="Current directory",
    )
    expected_resource_versions: dict[str, str] = {}
    request = ToolAuthorizationRequest(
        task_id=task_id,
        step_id=step_id,
        call_id=call_id,
        actor="test",
        tool_name="computer.disk_usage",
        tool_version="1.0.0",
        contract_digest=CONTRACT_DIGEST,
        arguments_digest=sha256_digest(resolved_arguments),
        risk_level=ToolRiskLevel.R0,
        capabilities=("filesystem.metadata.read",),
        resources=(resource,),
        expected_resource_versions_digest=sha256_digest(expected_resource_versions),
    )
    decision = BuiltinPolicyEngine(
        allowed_resource_scopes=(resource.scope_key,),
    ).evaluate(request)
    assert decision.effect is PolicyEffect.ALLOW
    approval = await service.apply_policy_decision(
        task_id,
        call_id,
        request=request,
        decision=decision,
        title="Inspect disk capacity",
        purpose="Test the Tool call ledger",
        consequences=(),
        data_egress=DataEgress(enabled=False),
        expected_resource_versions=expected_resource_versions,
    )
    assert approval is None
    grant = ToolAuthorizationGrant.issue(
        decision_id=decision.decision_id,
        request_digest=decision.request_digest,
        task_id=task_id,
        step_id=request.step_id,
        call_id=call_id,
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
    )
    return await service.start_tool_call(
        task_id,
        call_id,
        runner_id=runner_id,
        authorization=grant,
        arguments=resolved_arguments,
        expected_resource_versions=expected_resource_versions,
    )


async def _unknown_call(
    service: TaskService,
    *,
    goal: str,
    call_id: str,
) -> tuple[str, str]:
    task_id = await _running_task(service, goal)
    await _record_requested(service, task_id, call_id=call_id)
    await _start_allowed_call(
        service,
        task_id,
        call_id,
        runner_id="runner-uncertain",
    )
    await service.finish_tool_call(
        task_id,
        call_id,
        status=ToolCallStatus.UNKNOWN,
        error_code="RUNNER_EXITED",
        resolution_source="control_plane",
    )
    reconciliations = await service.list_reconciliations(task_id=task_id)
    assert len(reconciliations) == 1
    return task_id, reconciliations[0].reconciliation_id


@pytest.mark.asyncio
async def test_requested_call_persists_only_argument_and_idempotency_digests(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path)
    arguments = {"path": "C:/private/location", "opaque": "argument-secret"}
    idempotency_key = "idempotency-key-secret"
    try:
        task_id = await _running_task(service, "persist a tool call")

        event = await service.record_tool_requested(
            task_id,
            call_id="call-digest",
            step_id="step-digest",
            tool_name="computer.disk_usage",
            tool_version="1.0.0",
            contract_digest=CONTRACT_DIGEST,
            arguments=arguments,
            idempotency=ToolIdempotency.KEY_REQUIRED,
            idempotency_key=idempotency_key,
            risk="R0",
        )

        async with database.session() as session:
            call = await session.get(ToolCallRecord, "call-digest")
            receipt = await session.scalar(
                select(ToolIdempotencyReceiptRecord).where(
                    ToolIdempotencyReceiptRecord.call_id == "call-digest"
                )
            )
            outbox = await session.scalar(
                select(OutboxMessageRecord).where(OutboxMessageRecord.event_id == event.event_id)
            )
        assert call is not None
        assert receipt is not None
        assert call.status == "requested"
        assert call.arguments_digest == sha256_digest(arguments)
        assert (
            call.idempotency_key_digest
            == hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        )
        assert outbox is not None
        persisted = json.dumps(
            {
                "event": event.model_dump(mode="json"),
                "outbox": outbox.payload,
                "arguments_digest": call.arguments_digest,
                "idempotency_key_digest": call.idempotency_key_digest,
            },
            sort_keys=True,
        )
        assert "argument-secret" not in persisted
        assert "C:/private/location" not in persisted
        assert idempotency_key not in persisted
        assert event.payload["arguments_digest"] == call.arguments_digest
        assert event.payload["idempotency_key_digest"] == call.idempotency_key_digest
        assert receipt.key_digest == call.idempotency_key_digest
        assert receipt.arguments_digest == call.arguments_digest
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "event_type", "result", "error_code"),
    [
        (ToolCallStatus.SUCCEEDED, "tool.completed", {"ok": True}, None),
        (ToolCallStatus.FAILED, "tool.failed", None, "TOOL_EXECUTION_FAILED"),
        (ToolCallStatus.CANCELLED, "tool.cancelled", None, "TOOL_CANCELLED"),
    ],
)
async def test_running_call_commits_one_terminal_event_and_ignores_late_result(
    tmp_path: Path,
    terminal_status: ToolCallStatus,
    event_type: str,
    result: dict[str, Any] | None,
    error_code: str | None,
) -> None:
    database, service = await _service(tmp_path, f"terminal-{terminal_status}.db")
    try:
        task_id = await _running_task(service, f"finish {terminal_status}")
        call_id = f"call-{terminal_status}"
        await _record_requested(service, task_id, call_id=call_id)
        started = await _start_allowed_call(
            service,
            task_id,
            call_id,
            runner_id="runner-1",
        )

        events = await service.finish_tool_call(
            task_id,
            call_id,
            status=terminal_status,
            result=result,
            error_code=error_code,
        )
        event_count = len(await service.list_events(task_id))
        late = await service.finish_tool_call(
            task_id,
            call_id,
            status=ToolCallStatus.UNKNOWN,
            error_code="RUNNER_EXITED",
            resolution_source="control_plane",
        )

        assert started.type == "tool.started"
        expected_event_types = (
            [event_type]
            if terminal_status is ToolCallStatus.SUCCEEDED
            else [event_type, "task.failed"]
        )
        assert [event.type for event in events] == expected_event_types
        assert late == ()
        assert len(await service.list_events(task_id)) == event_count
        task = await service.get_task(task_id)
        expected_task_status = (
            TaskStatus.RUNNING if terminal_status is ToolCallStatus.SUCCEEDED else TaskStatus.FAILED
        )
        assert task.status is expected_task_status
        async with database.session() as session:
            call = await session.get(ToolCallRecord, call_id)
        assert call is not None
        assert call.status == terminal_status.value
        assert call.runner_id == "runner-1"
        assert call.policy_effect == "allow"
        assert call.policy_decision_id is not None
        assert call.policy_event_id is not None
        assert call.authorization_id is not None
        assert call.terminal_event_id == events[0].event_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_brokered_success_projects_exact_commit_receipt_atomically(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path, "commit-projection.db")
    call_id = "call-file-move-projection"
    authorization_id = f"auth_{'1' * 64}"
    key_digest = "2" * 64
    version = "3" * 64
    timestamp = datetime.now(UTC)
    receipt = ToolCommitReceipt(
        receipt_id=f"cmt_{'4' * 64}",
        call_id=call_id,
        tool_name="file.move",
        tool_version="1.0.0",
        authorization_id=authorization_id,
        approval_id="apr_projection",
        preview_hash="5" * 64,
        prepare_digest="6" * 64,
        idempotency_key_digest=key_digest,
        resource_versions_before={"destination": "absent", "source": version},
        resource_versions_after={"destination": version, "source": "absent"},
        commit_started_at=timestamp,
        receipt_recorded_at=timestamp,
    )
    try:
        task_id = await _running_task(service, "project a commit receipt")
        async with database.session() as session:
            async with session.begin():
                session.add(
                    ToolCallRecord(
                        call_id=call_id,
                        task_id=task_id,
                        step_id="step-file-move",
                        attempt=1,
                        tool_name="file.move",
                        tool_version="1.0.0",
                        contract_digest="7" * 64,
                        arguments_digest="8" * 64,
                        authorization_id=authorization_id,
                        idempotency=ToolIdempotency.KEY_REQUIRED.value,
                        idempotency_key_digest=key_digest,
                        status=ToolCallStatus.RUNNING.value,
                        runner_id="runner-file-move",
                        requested_at=timestamp,
                        started_at=timestamp,
                        updated_at=timestamp,
                    )
                )

        events = await service.finish_tool_call(
            task_id,
            call_id,
            status=ToolCallStatus.SUCCEEDED,
            result={
                "source": "C:/approved/source.txt",
                "destination": "C:/approved/destination.txt",
                "source_version_before": version,
                "destination_version_after": version,
                "reversible": True,
                "commit_receipt": receipt.model_dump(mode="json"),
            },
            fail_task=False,
        )

        async with database.session() as session:
            projected = await session.get(ToolCommitReceiptRecord, receipt.receipt_id)
        assert [event.type for event in events] == ["tool.completed"]
        assert projected is not None
        assert projected.call_id == call_id
        assert projected.preview_hash == receipt.preview_hash
        assert projected.resource_versions_before == receipt.resource_versions_before
        assert projected.resource_versions_after == receipt.resource_versions_after
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_requested_call_admission_failure_atomically_fails_task(tmp_path: Path) -> None:
    database, service = await _service(tmp_path)
    try:
        task_id = await _running_task(service, "runner admission failure")
        await _record_requested(service, task_id, call_id="call-admission")

        events = await service.finish_tool_call(
            task_id,
            "call-admission",
            status=ToolCallStatus.FAILED,
            error_code="RUNNER_UNAVAILABLE",
            resolution_source="control_plane",
        )

        assert [event.type for event in events] == ["tool.failed", "task.failed"]
        assert events[0].payload["runner_id"] is None
        assert events[1].payload["tool_error_code"] == "RUNNER_UNAVAILABLE"
        assert (await service.get_task(task_id)).status is TaskStatus.FAILED
        async with database.session() as session:
            call = await session.get(ToolCallRecord, "call-admission")
        assert call is not None
        assert call.status == "failed"
        assert call.started_at is None
        assert call.terminal_event_id == events[0].event_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unknown_result_and_task_failure_commit_atomically(tmp_path: Path) -> None:
    database, service = await _service(tmp_path)
    try:
        task_id = await _running_task(service, "uncertain call")
        await _record_requested(service, task_id, call_id="call-unknown")
        await _start_allowed_call(
            service,
            task_id,
            "call-unknown",
            runner_id="runner-lost",
        )

        events = await service.finish_tool_call(
            task_id,
            "call-unknown",
            status=ToolCallStatus.UNKNOWN,
            error_code="RUNNER_EXITED",
            resolution_source="control_plane",
        )

        assert [event.type for event in events] == ["tool.unknown", "task.failed"]
        assert events[0].payload["requires_reconciliation"] is True
        assert events[0].payload["retryable"] is False
        assert events[1].payload["code"] == "TOOL_RESULT_UNKNOWN"
        task = await service.get_task(task_id)
        assert task.status is TaskStatus.FAILED
        async with database.session() as session:
            call = await session.get(ToolCallRecord, "call-unknown")
            reconciliation = await session.scalar(
                select(ToolReconciliationRecord).where(
                    ToolReconciliationRecord.call_id == "call-unknown"
                )
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxMessageRecord)
                .where(OutboxMessageRecord.event_id.in_([event.event_id for event in events]))
            )
        assert call is not None
        assert reconciliation is not None
        assert call.status == "unknown"
        assert call.error_code == "RUNNER_EXITED"
        assert call.terminal_event_id == events[0].event_id
        assert reconciliation.status == "pending"
        assert reconciliation.outcome is None
        assert outbox_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_requested_event_and_ledger_roll_back_when_outbox_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service = await _service(tmp_path)
    try:
        task_id = await _running_task(service, "rollback requested call")
        before = await service.get_task(task_id)

        def fail_outbox(_: object) -> OutboxMessageRecord:
            raise RuntimeError("injected outbox failure")

        monkeypatch.setattr(service, "_to_outbox", fail_outbox)
        with pytest.raises(RuntimeError, match="injected outbox failure"):
            await _record_requested(service, task_id, call_id="call-rollback")

        async with database.session() as session:
            call = await session.get(ToolCallRecord, "call-rollback")
            requested_count = await session.scalar(
                select(func.count())
                .select_from(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == task_id,
                    TaskEventRecord.type == "tool.requested",
                )
            )
        after = await service.get_task(task_id)
        assert call is None
        assert requested_count == 0
        assert after.last_event_seq == before.last_event_seq
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unknown_transition_rolls_back_ledger_events_outbox_and_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service = await _service(tmp_path)
    try:
        task_id = await _running_task(service, "rollback unknown result")
        await _record_requested(service, task_id, call_id="call-atomic")
        await _start_allowed_call(
            service,
            task_id,
            "call-atomic",
            runner_id="runner-atomic",
        )
        before = await service.get_task(task_id)
        original_to_outbox = TaskService._to_outbox

        def fail_task_outbox(event: object) -> OutboxMessageRecord:
            if getattr(event, "type", None) == "task.failed":
                raise RuntimeError("injected task outbox failure")
            return original_to_outbox(event)  # type: ignore[arg-type]

        monkeypatch.setattr(service, "_to_outbox", fail_task_outbox)
        with pytest.raises(RuntimeError, match="injected task outbox failure"):
            await service.finish_tool_call(
                task_id,
                "call-atomic",
                status=ToolCallStatus.UNKNOWN,
                error_code="RUNNER_EXITED",
            )

        async with database.session() as session:
            call = await session.get(ToolCallRecord, "call-atomic")
            reconciliation = await session.scalar(
                select(ToolReconciliationRecord).where(
                    ToolReconciliationRecord.call_id == "call-atomic"
                )
            )
            unknown_count = await session.scalar(
                select(func.count())
                .select_from(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == task_id,
                    TaskEventRecord.type.in_(("tool.unknown", "task.failed")),
                )
            )
        after = await service.get_task(task_id)
        assert call is not None
        assert reconciliation is None
        assert call.status == "running"
        assert call.finished_at is None
        assert unknown_count == 0
        assert after.status is TaskStatus.RUNNING
        assert after.last_event_seq == before.last_event_seq
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_startup_recovery_is_atomic_and_idempotent_for_requested_and_running(
    tmp_path: Path,
) -> None:
    database, first_service = await _service(tmp_path)
    try:
        requested_task_id = await _running_task(first_service, "requested restart")
        await _record_requested(
            first_service,
            requested_task_id,
            call_id="call-requested-restart",
        )
        running_task_id = await _running_task(first_service, "running restart")
        await _record_requested(
            first_service,
            running_task_id,
            call_id="call-running-restart",
        )
        await _start_allowed_call(
            first_service,
            running_task_id,
            "call-running-restart",
            runner_id="runner-before-restart",
        )
        recovery_service = TaskService(database, "/api/v1")

        recovered = await recovery_service.recover_incomplete_tool_calls()
        requested_events = await recovery_service.list_events(requested_task_id)
        running_events = await recovery_service.list_events(running_task_id)
        event_counts = (len(requested_events), len(running_events))
        repeated = await recovery_service.recover_incomplete_tool_calls()

        assert recovered.requested_failed == 1
        assert recovered.running_unknown == 1
        assert recovered.tasks_failed == 2
        assert recovered.events_created == 4
        assert requested_events[-2].type == "tool.failed"
        assert requested_events[-2].payload["code"] == ("TOOL_CALL_NOT_DISPATCHED_AFTER_RESTART")
        assert requested_events[-1].type == "task.failed"
        assert requested_events[-1].payload["code"] == ("TOOL_CALL_INTERRUPTED_BEFORE_DISPATCH")
        assert running_events[-2].type == "tool.unknown"
        assert running_events[-2].payload["code"] == ("TOOL_RESULT_UNCERTAIN_AFTER_RESTART")
        assert running_events[-1].type == "task.failed"
        assert running_events[-1].payload["code"] == "TOOL_RESULT_UNKNOWN"
        assert repeated.requested_failed == 0
        assert repeated.running_unknown == 0
        assert repeated.tasks_failed == 0
        assert repeated.events_created == 0
        assert (
            len(await recovery_service.list_events(requested_task_id)),
            len(await recovery_service.list_events(running_task_id)),
        ) == event_counts

        async with database.session() as session:
            requested_call = await session.get(
                ToolCallRecord,
                "call-requested-restart",
            )
            running_call = await session.get(
                ToolCallRecord,
                "call-running-restart",
            )
            running_reconciliation = await session.scalar(
                select(ToolReconciliationRecord).where(
                    ToolReconciliationRecord.call_id == "call-running-restart"
                )
            )
            tasks = list(
                (
                    await session.scalars(
                        select(TaskRecord).where(
                            TaskRecord.task_id.in_((requested_task_id, running_task_id))
                        )
                    )
                ).all()
            )
        assert requested_call is not None
        assert requested_call.status == "failed"
        assert requested_call.resolution_source == "startup_recovery"
        assert running_call is not None
        assert running_reconciliation is not None
        assert running_call.status == "unknown"
        assert running_call.resolution_source == "startup_recovery"
        assert {task.status for task in tasks} == {"failed"}
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_tool_idempotency_receipt_blocks_cross_task_key_reuse(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path)
    try:
        first_task = await _running_task(service, "first key owner")
        second_task = await _running_task(service, "second key owner")
        await _record_requested(
            service,
            first_task,
            call_id="call-key-owner",
            idempotency=ToolIdempotency.KEY_REQUIRED,
            idempotency_key="durable-tool-key",
        )

        with pytest.raises(ToolIdempotencyKeyAlreadyUsedError) as raised:
            await _record_requested(
                service,
                second_task,
                call_id="call-key-reuse",
                arguments={"path": "different"},
                idempotency=ToolIdempotency.KEY_REQUIRED,
                idempotency_key="durable-tool-key",
            )

        assert raised.value.existing_call_id == "call-key-owner"
        async with database.session() as session:
            assert await session.get(ToolCallRecord, "call-key-reuse") is None
            receipts = list(
                (await session.scalars(select(ToolIdempotencyReceiptRecord))).all()
            )
        assert [receipt.call_id for receipt in receipts] == ["call-key-owner"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_verdict_and_explicit_attempt_are_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path)
    try:
        original_task_id, reconciliation_id = await _unknown_call(
            service,
            goal="perform an uncertain operation",
            call_id="call-reconcile",
        )
        pending = await service.get_reconciliation(reconciliation_id)
        assert pending.status is ReconciliationStatus.PENDING
        assert pending.can_create_attempt is False

        with pytest.raises(ReconciliationAttemptNotAllowedError):
            await service.create_reconciliation_attempt(
                reconciliation_id,
                idempotency_key="attempt-before-resolution",
            )

        resolved = await service.resolve_reconciliation(
            reconciliation_id,
            outcome=ReconciliationOutcome.CONFIRMED_NO_EFFECT,
            evidence_summary="Verified the target resource was not changed.",
            idempotency_key="resolve-no-effect-key",
        )
        replayed_resolution = await service.resolve_reconciliation(
            reconciliation_id,
            outcome=ReconciliationOutcome.CONFIRMED_NO_EFFECT,
            evidence_summary="Verified the target resource was not changed.",
            idempotency_key="resolve-no-effect-key",
        )

        assert resolved.replayed is False
        assert resolved.reconciliation.can_create_attempt is True
        assert replayed_resolution.replayed is True
        with pytest.raises(ReconciliationIdempotencyConflictError):
            await service.resolve_reconciliation(
                reconciliation_id,
                outcome=ReconciliationOutcome.ACCEPTED_UNKNOWN,
                evidence_summary="Use the same key for a different verdict.",
                idempotency_key="resolve-no-effect-key",
            )

        concurrent_attempts = await asyncio.gather(
            *(
                service.create_reconciliation_attempt(
                    reconciliation_id,
                    idempotency_key="create-explicit-attempt-key",
                )
                for _ in range(4)
            )
        )
        attempt = next(result for result in concurrent_attempts if not result.replayed)
        replayed_attempt = next(result for result in concurrent_attempts if result.replayed)

        assert sum(not result.replayed for result in concurrent_attempts) == 1
        assert {result.task.task_id for result in concurrent_attempts} == {
            attempt.task.task_id
        }
        assert replayed_attempt.task.task_id == attempt.task.task_id
        assert attempt.task.task_id != original_task_id
        assert attempt.task.status is TaskStatus.CREATED
        assert attempt.reconciliation.new_attempt_task_id == attempt.task.task_id
        assert attempt.reconciliation.can_create_attempt is False
        with pytest.raises(ReconciliationAttemptAlreadyCreatedError):
            await service.create_reconciliation_attempt(
                reconciliation_id,
                idempotency_key="create-second-attempt-key",
            )

        original_call = None
        async with database.session() as session:
            original_call = await session.get(ToolCallRecord, "call-reconcile")
        assert original_call is not None
        assert original_call.status == "unknown"
        new_events = await service.list_events(attempt.task.task_id)
        assert len(new_events) == 1
        assert new_events[0].type == "task.created"
        assert new_events[0].payload["retry_of"] == {
            "reconciliation_id": reconciliation_id,
            "task_id": original_task_id,
            "call_id": "call-reconcile",
            "source_attempt": 1,
        }
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_only_confirmed_no_effect_can_create_a_new_attempt(tmp_path: Path) -> None:
    database, service = await _service(tmp_path)
    try:
        for index, outcome in enumerate(
            (
                ReconciliationOutcome.CONFIRMED_SUCCEEDED,
                ReconciliationOutcome.CONFIRMED_FAILED,
                ReconciliationOutcome.ACCEPTED_UNKNOWN,
            )
        ):
            _, reconciliation_id = await _unknown_call(
                service,
                goal=f"unsafe retry outcome {outcome.value}",
                call_id=f"call-no-retry-{index}",
            )
            await service.resolve_reconciliation(
                reconciliation_id,
                outcome=outcome,
                evidence_summary=f"Operator selected {outcome.value}.",
                idempotency_key=f"resolve-no-retry-{index}-key",
            )
            with pytest.raises(ReconciliationAttemptNotAllowedError):
                await service.create_reconciliation_attempt(
                    reconciliation_id,
                    idempotency_key=f"attempt-no-retry-{index}-key",
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_side_effecting_call_without_request_snapshot_cannot_create_attempt(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path)
    try:
        _, reconciliation_id = await _unknown_call(
            service,
            goal="do not reconstruct a side-effecting call from its goal",
            call_id="call-side-effect-no-snapshot",
        )
        async with database.session() as session:
            async with session.begin():
                call = await session.get(
                    ToolCallRecord,
                    "call-side-effect-no-snapshot",
                )
                assert call is not None
                call.tool_name = FILE_MOVE_CONTRACT.name
                call.tool_version = FILE_MOVE_CONTRACT.version
                call.contract_digest = FILE_MOVE_CONTRACT.digest

        resolved = await service.resolve_reconciliation(
            reconciliation_id,
            outcome=ReconciliationOutcome.CONFIRMED_NO_EFFECT,
            evidence_summary="Verified no move occurred, but arguments are not persisted.",
            idempotency_key="resolve-side-effect-no-snapshot",
        )

        assert resolved.reconciliation.can_create_attempt is False
        with pytest.raises(ReconciliationAttemptNotAllowedError):
            await service.create_reconciliation_attempt(
                reconciliation_id,
                idempotency_key="blocked-side-effect-attempt",
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_new_attempt_rolls_back_task_lineage_outbox_and_receipt_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service = await _service(tmp_path)
    try:
        original_task_id, reconciliation_id = await _unknown_call(
            service,
            goal="rollback a fresh attempt",
            call_id="call-attempt-rollback",
        )
        await service.resolve_reconciliation(
            reconciliation_id,
            outcome=ReconciliationOutcome.CONFIRMED_NO_EFFECT,
            evidence_summary="Verified no effect before the rollback test.",
            idempotency_key="resolve-attempt-rollback",
        )

        def fail_outbox(_: object) -> OutboxMessageRecord:
            raise RuntimeError("injected attempt outbox failure")

        monkeypatch.setattr(service, "_to_outbox", fail_outbox)
        with pytest.raises(RuntimeError, match="injected attempt outbox failure"):
            await service.create_reconciliation_attempt(
                reconciliation_id,
                idempotency_key="create-attempt-rollback",
            )

        reconciliation = await service.get_reconciliation(reconciliation_id)
        assert reconciliation.new_attempt_task_id is None
        assert reconciliation.can_create_attempt is True
        async with database.session() as session:
            task_count = await session.scalar(
                select(func.count()).select_from(TaskRecord)
            )
        assert task_count == 1
        assert (await service.get_task(original_task_id)).status is TaskStatus.FAILED
    finally:
        await database.dispose()
