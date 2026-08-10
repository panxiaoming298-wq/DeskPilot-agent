from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

import deskpilot.application.task_service as task_service_module
from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.task_service import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalStaleError,
    InvalidToolCallTransitionError,
    TaskService,
    ToolAuthorizationError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import (
    ApprovalDecision,
    ApprovalRead,
    ApprovalStatus,
    DataEgress,
)
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.schemas import TaskCreate, TaskStatus
from deskpilot.domain.tool_contracts import (
    ToolIdempotency,
    ToolRiskLevel,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ApprovalRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    ToolCallRecord,
)

CONTRACT_DIGEST = "c" * 64
ARGUMENTS: dict[str, Any] = {"path": "."}
EXPECTED_RESOURCE_VERSIONS: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class PendingApprovalFixture:
    task_id: str
    call_id: str
    request: ToolAuthorizationRequest
    decision: PolicyDecision
    approval: ApprovalRead


async def _service(tmp_path: Path, name: str) -> tuple[Database, TaskService]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")
    await database.migrate()
    return database, TaskService(database, "/api/v1")


async def _running_requested_call(
    service: TaskService,
    *,
    goal: str,
    call_id: str,
) -> tuple[str, ToolAuthorizationRequest, PolicyResource]:
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
    await service.record_tool_requested(
        task.task_id,
        call_id=call_id,
        step_id="step-1",
        tool_name="computer.disk_usage",
        tool_version="1.0.0",
        contract_digest=CONTRACT_DIGEST,
        arguments=ARGUMENTS,
        idempotency=ToolIdempotency.IDEMPOTENT,
        risk=ToolRiskLevel.R0.value,
    )
    resource = PolicyResource(
        kind="filesystem_path",
        identifier=".",
        operations=("filesystem.metadata.read",),
        display_name="Current directory",
    )
    request = ToolAuthorizationRequest(
        task_id=task.task_id,
        step_id="step-1",
        call_id=call_id,
        actor="test",
        tool_name="computer.disk_usage",
        tool_version="1.0.0",
        contract_digest=CONTRACT_DIGEST,
        arguments_digest=sha256_digest(ARGUMENTS),
        risk_level=ToolRiskLevel.R0,
        capabilities=("filesystem.metadata.read",),
        resources=(resource,),
        expected_resource_versions_digest=sha256_digest(EXPECTED_RESOURCE_VERSIONS),
    )
    return task.task_id, request, resource


async def _pending_approval(
    service: TaskService,
    *,
    goal: str,
    call_id: str,
    ttl_seconds: int = 60,
) -> PendingApprovalFixture:
    task_id, request, resource = await _running_requested_call(
        service,
        goal=goal,
        call_id=call_id,
    )
    decision = BuiltinPolicyEngine(
        allowed_resource_scopes=(resource.scope_key,),
        require_approval_for_r0=True,
        approval_ttl_seconds=ttl_seconds,
    ).evaluate(request)
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    approval = await service.apply_policy_decision(
        task_id,
        call_id,
        request=request,
        decision=decision,
        title="Inspect disk capacity",
        purpose="Read capacity metadata for the selected path.",
        consequences=("Reads disk capacity metadata without modifying files.",),
        data_egress=DataEgress(enabled=False),
        expected_resource_versions=EXPECTED_RESOURCE_VERSIONS,
    )
    assert approval is not None
    return PendingApprovalFixture(
        task_id=task_id,
        call_id=call_id,
        request=request,
        decision=decision,
        approval=approval,
    )


def _grant(
    fixture: PendingApprovalFixture,
    *,
    approved_at: datetime,
) -> ToolAuthorizationGrant:
    return ToolAuthorizationGrant.issue(
        decision_id=fixture.decision.decision_id,
        request_digest=fixture.decision.request_digest,
        task_id=fixture.task_id,
        step_id=fixture.request.step_id,
        call_id=fixture.call_id,
        actor_id=fixture.request.actor,
        origin=fixture.request.origin,
        tool_name=fixture.request.tool_name,
        tool_version=fixture.request.tool_version,
        contract_digest=fixture.request.contract_digest,
        policy_revision=fixture.decision.policy_revision,
        rule_id=fixture.decision.rule_id,
        reason_code=fixture.decision.reason_code,
        effective_risk=fixture.decision.effective_risk,
        arguments_digest=fixture.request.arguments_digest,
        resource_scope_digest=fixture.request.resource_scope_digest,
        expected_resource_versions_digest=(fixture.request.expected_resource_versions_digest),
        capabilities=fixture.request.capabilities,
        network_access=fixture.request.network_access,
        data_egress=fixture.request.data_egress,
        side_effects=fixture.request.side_effects,
        reversible=fixture.request.reversible,
        resources=fixture.request.resources,
        interactive=fixture.request.interactive,
        batch_count=fixture.request.batch_count,
        approval_id=fixture.approval.approval_id,
        preview_hash=fixture.approval.preview_hash,
        approved_at=approved_at,
        grant_expires_at=fixture.approval.expires_at,
    )


@pytest.mark.asyncio
async def test_approval_request_commits_policy_preview_events_and_outbox_atomically(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path, "approval-request.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="approval request",
            call_id="call-request",
        )
        events = await service.list_events(fixture.task_id)

        assert fixture.approval.status is ApprovalStatus.PENDING
        assert (await service.get_task(fixture.task_id)).status is (TaskStatus.WAITING_APPROVAL)
        assert [event.type for event in events[-3:]] == [
            "policy.evaluated",
            "approval.required",
            "task.status_changed",
        ]
        assert events[-2].payload == {
            "approval_id": fixture.approval.approval_id,
            "call_id": fixture.call_id,
            "preview_hash": fixture.approval.preview_hash,
            "title": fixture.approval.title,
            "risk_level": "R0",
            "expires_at": fixture.approval.expires_at.isoformat(),
        }

        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)
            record = await session.get(
                ApprovalRecord,
                fixture.approval.approval_id,
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxMessageRecord)
                .where(OutboxMessageRecord.task_id == fixture.task_id)
            )
        assert call is not None
        assert record is not None
        assert call.policy_decision_id == fixture.decision.decision_id
        assert call.policy_effect == PolicyEffect.REQUIRE_APPROVAL.value
        assert call.policy_event_id == events[-3].event_id
        assert record.binding_digest == fixture.request.request_digest
        assert record.expected_resource_versions == EXPECTED_RESOURCE_VERSIONS
        assert outbox_count == len(events)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_approval_request_rolls_back_policy_approval_task_and_outbox_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service = await _service(tmp_path, "approval-request-rollback.db")
    try:
        task_id, request, resource = await _running_requested_call(
            service,
            goal="approval rollback",
            call_id="call-request-rollback",
        )
        decision = BuiltinPolicyEngine(
            allowed_resource_scopes=(resource.scope_key,),
            require_approval_for_r0=True,
        ).evaluate(request)
        before = await service.get_task(task_id)
        original_to_outbox = TaskService._to_outbox

        def fail_approval_outbox(event: object) -> OutboxMessageRecord:
            if getattr(event, "type", None) == "approval.required":
                raise RuntimeError("injected approval outbox failure")
            return original_to_outbox(event)  # type: ignore[arg-type]

        monkeypatch.setattr(service, "_to_outbox", fail_approval_outbox)
        with pytest.raises(RuntimeError, match="approval outbox failure"):
            await service.apply_policy_decision(
                task_id,
                "call-request-rollback",
                request=request,
                decision=decision,
                title="Inspect disk capacity",
                purpose="Rollback this approval request.",
                consequences=(),
                data_egress=DataEgress(enabled=False),
                expected_resource_versions=EXPECTED_RESOURCE_VERSIONS,
            )

        async with database.session() as session:
            call = await session.get(ToolCallRecord, "call-request-rollback")
            approval_count = await session.scalar(select(func.count()).select_from(ApprovalRecord))
            policy_event_count = await session.scalar(
                select(func.count())
                .select_from(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == task_id,
                    TaskEventRecord.type.in_(("policy.evaluated", "approval.required")),
                )
            )
        assert call is not None
        assert call.policy_decision_id is None
        assert call.policy_effect is None
        assert approval_count == 0
        assert policy_event_count == 0
        assert await service.get_task(task_id) == before
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_approve_is_idempotent_and_reject_after_approval_conflicts(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path, "approval-idempotency.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="approve once",
            call_id="call-approve-once",
        )

        approved = await service.resolve_approval(
            fixture.approval.approval_id,
            decision=ApprovalStatus.APPROVED,
            preview_hash=fixture.approval.preview_hash,
            reason="Reviewed",
        )
        event_count = len(await service.list_events(fixture.task_id))
        replayed = await service.resolve_approval(
            fixture.approval.approval_id,
            decision=ApprovalStatus.APPROVED,
            preview_hash=fixture.approval.preview_hash,
            reason="A retry must not rewrite audit history",
        )

        assert approved.replayed is False
        assert approved.approval.status is ApprovalStatus.APPROVED
        assert approved.task.status is TaskStatus.RUNNING
        assert replayed.replayed is True
        assert replayed.approval.resolution_reason == "Reviewed"
        assert len(await service.list_events(fixture.task_id)) == event_count
        with pytest.raises(ApprovalAlreadyResolvedError) as conflict:
            await service.resolve_approval(
                fixture.approval.approval_id,
                decision=ApprovalStatus.REJECTED,
                preview_hash=fixture.approval.preview_hash,
            )
        assert conflict.value.current is ApprovalStatus.APPROVED
        assert conflict.value.requested is ApprovalStatus.REJECTED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_preview_does_not_resolve_or_emit_events(tmp_path: Path) -> None:
    database, service = await _service(tmp_path, "approval-stale.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="stale preview",
            call_id="call-stale",
        )
        event_count = len(await service.list_events(fixture.task_id))

        with pytest.raises(ApprovalStaleError):
            await service.resolve_approval(
                fixture.approval.approval_id,
                decision=ApprovalStatus.APPROVED,
                preview_hash="f" * 64,
            )

        assert (await service.get_approval(fixture.approval.approval_id)).status is (
            ApprovalStatus.PENDING
        )
        assert (await service.get_task(fixture.task_id)).status is (TaskStatus.WAITING_APPROVAL)
        assert len(await service.list_events(fixture.task_id)) == event_count
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_approval_cancels_call_and_task_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service = await _service(tmp_path, "approval-expired.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="expire before dispatch",
            call_id="call-expired",
            ttl_seconds=1,
        )
        expired_now = fixture.approval.expires_at + timedelta(microseconds=1)
        monkeypatch.setattr(task_service_module, "utc_now", lambda: expired_now)

        with pytest.raises(ApprovalExpiredError):
            await service.resolve_approval(
                fixture.approval.approval_id,
                decision=ApprovalStatus.APPROVED,
                preview_hash=fixture.approval.preview_hash,
            )

        approval = await service.get_approval(fixture.approval.approval_id)
        task = await service.get_task(fixture.task_id)
        events = await service.list_events(fixture.task_id)
        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)
        assert approval.status is ApprovalStatus.EXPIRED
        assert task.status is TaskStatus.CANCELLED
        assert call is not None
        assert call.status == "cancelled"
        assert call.error_code == "APPROVAL_EXPIRED"
        assert "tool.started" not in [event.type for event in events]
        assert [event.type for event in events[-3:]] == [
            "approval.expired",
            "tool.cancelled",
            "task.cancelled",
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_task_cancel_invalidates_pending_approval_and_call_atomically(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path, "approval-task-cancel.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="cancel pending approval",
            call_id="call-task-cancel",
        )
        cancelled = await service.cancel_task(
            fixture.task_id,
            reason="User cancelled",
        )
        events = await service.list_events(fixture.task_id)
        approval = await service.get_approval(fixture.approval.approval_id)
        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxMessageRecord)
                .where(OutboxMessageRecord.task_id == fixture.task_id)
            )

        assert cancelled.status is TaskStatus.CANCELLED
        assert approval.status is ApprovalStatus.CANCELLED
        assert call is not None
        assert call.status == "cancelled"
        assert call.error_code == "APPROVAL_CANCELLED_WITH_TASK"
        assert [event.type for event in events[-3:]] == [
            "approval.resolved",
            "tool.cancelled",
            "task.cancelled",
        ]
        assert outbox_count == len(events)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cancel_invalidates_approved_unconsumed_grant_without_rewriting_decision(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path, "approval-approved-cancel.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="cancel an approved but undispatched call",
            call_id="call-approved-cancel",
        )
        resolution = await service.resolve_approval(
            fixture.approval.approval_id,
            decision=ApprovalStatus.APPROVED,
            preview_hash=fixture.approval.preview_hash,
            reason="Reviewed before cancellation",
        )
        original_resolved_at = resolution.approval.resolved_at

        await service.cancel_task(fixture.task_id, reason="Cancel before dispatch")

        approval = await service.get_approval(fixture.approval.approval_id)
        events = await service.list_events(fixture.task_id)
        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)
            record = await session.get(
                ApprovalRecord,
                fixture.approval.approval_id,
            )

        assert approval.status is ApprovalStatus.CANCELLED
        assert approval.decision is ApprovalDecision.APPROVED
        assert approval.resolved_at == original_resolved_at
        assert approval.resolution_reason == "Reviewed before cancellation"
        assert approval.consumed_at is None
        assert record is not None
        assert record.resolved_by == "local_user"
        assert call is not None
        assert call.status == "cancelled"
        assert call.error_code == "APPROVAL_CANCELLED_WITH_TASK"
        assert [event.type for event in events[-3:]] == [
            "approval.invalidated",
            "tool.cancelled",
            "task.cancelled",
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatch_expiry_preserves_approved_audit_and_is_stable_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service = await _service(tmp_path, "approval-approved-expiry.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="expire after approval but before dispatch",
            call_id="call-approved-expiry",
            ttl_seconds=1,
        )
        resolution = await service.resolve_approval(
            fixture.approval.approval_id,
            decision=ApprovalStatus.APPROVED,
            preview_hash=fixture.approval.preview_hash,
            reason="Reviewed before expiry",
        )
        assert resolution.approval.resolved_at is not None
        grant = _grant(
            fixture,
            approved_at=resolution.approval.resolved_at,
        )
        expired_now = fixture.approval.expires_at + timedelta(microseconds=1)
        monkeypatch.setattr(task_service_module, "utc_now", lambda: expired_now)

        with pytest.raises(ApprovalExpiredError):
            await service.start_tool_call(
                fixture.task_id,
                fixture.call_id,
                runner_id="runner-too-late",
                authorization=grant,
                arguments=ARGUMENTS,
                expected_resource_versions=EXPECTED_RESOURCE_VERSIONS,
            )
        with pytest.raises(ApprovalExpiredError):
            await service.resolve_approval(
                fixture.approval.approval_id,
                decision=ApprovalStatus.APPROVED,
                preview_hash=fixture.approval.preview_hash,
            )

        approval = await service.get_approval(fixture.approval.approval_id)
        task = await service.get_task(fixture.task_id)
        events = await service.list_events(fixture.task_id)
        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)
            record = await session.get(
                ApprovalRecord,
                fixture.approval.approval_id,
            )

        assert approval.status is ApprovalStatus.EXPIRED
        assert approval.decision is ApprovalDecision.APPROVED
        assert approval.resolved_at == resolution.approval.resolved_at
        assert approval.resolution_reason == "Reviewed before expiry"
        assert record is not None
        assert record.resolved_by == "local_user"
        assert task.status is TaskStatus.CANCELLED
        assert call is not None
        assert call.status == "cancelled"
        assert "tool.started" not in [event.type for event in events]
        assert [event.type for event in events[-3:]] == [
            "approval.expired",
            "tool.cancelled",
            "task.cancelled",
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_start_boundary_checks_binding_and_consumes_approval_once(
    tmp_path: Path,
) -> None:
    database, service = await _service(tmp_path, "approval-consume.db")
    try:
        fixture = await _pending_approval(
            service,
            goal="consume approval",
            call_id="call-consume",
        )
        resolution = await service.resolve_approval(
            fixture.approval.approval_id,
            decision=ApprovalStatus.APPROVED,
            preview_hash=fixture.approval.preview_hash,
        )
        assert resolution.approval.resolved_at is not None
        grant = _grant(
            fixture,
            approved_at=resolution.approval.resolved_at,
        )

        with pytest.raises(ToolAuthorizationError):
            await service.start_tool_call(
                fixture.task_id,
                fixture.call_id,
                runner_id="runner-tampered",
                authorization=grant,
                arguments={"path": "tampered"},
                expected_resource_versions=EXPECTED_RESOURCE_VERSIONS,
            )
        before_start = await service.get_approval(fixture.approval.approval_id)
        assert before_start.consumed_at is None

        started = await service.start_tool_call(
            fixture.task_id,
            fixture.call_id,
            runner_id="runner-approved",
            authorization=grant,
            arguments=ARGUMENTS,
            expected_resource_versions=EXPECTED_RESOURCE_VERSIONS,
        )
        consumed = await service.get_approval(fixture.approval.approval_id)
        assert started.type == "tool.started"
        assert consumed.consumed_at is not None
        assert started.payload["authorization_id"] == grant.authorization_id
        assert started.payload["approval_id"] == fixture.approval.approval_id

        with pytest.raises(InvalidToolCallTransitionError):
            await service.start_tool_call(
                fixture.task_id,
                fixture.call_id,
                runner_id="runner-replay",
                authorization=grant,
                arguments=ARGUMENTS,
                expected_resource_versions=EXPECTED_RESOURCE_VERSIONS,
            )
        events = await service.list_events(fixture.task_id)
        assert [event.type for event in events].count("tool.started") == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_restart_cancels_pending_approval_before_generic_call_recovery(
    tmp_path: Path,
) -> None:
    database, first_service = await _service(tmp_path, "approval-restart.db")
    try:
        fixture = await _pending_approval(
            first_service,
            goal="restart pending approval",
            call_id="call-restart-pending",
        )
        recovery_service = TaskService(database, "/api/v1")

        recovered = await recovery_service.recover_pending_approvals()
        repeated = await recovery_service.recover_pending_approvals()
        generic = await recovery_service.recover_incomplete_tool_calls()
        approval = await recovery_service.get_approval(fixture.approval.approval_id)
        task = await recovery_service.get_task(fixture.task_id)
        events = await recovery_service.list_events(fixture.task_id)
        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)

        assert recovered.approvals_cancelled == 1
        assert recovered.tasks_cancelled == 1
        assert recovered.events_created == 3
        assert repeated.approvals_cancelled == 0
        assert repeated.tasks_cancelled == 0
        assert repeated.events_created == 0
        assert generic.requested_failed == 0
        assert generic.running_unknown == 0
        assert approval.status is ApprovalStatus.CANCELLED
        assert task.status is TaskStatus.CANCELLED
        assert call is not None
        assert call.status == "cancelled"
        assert call.error_code == "APPROVAL_RUNTIME_LOST"
        assert [event.type for event in events[-3:]] == [
            "approval.resolved",
            "tool.cancelled",
            "task.cancelled",
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_restart_invalidates_approved_unconsumed_grant_explicitly(
    tmp_path: Path,
) -> None:
    database, first_service = await _service(
        tmp_path,
        "approval-approved-restart.db",
    )
    try:
        fixture = await _pending_approval(
            first_service,
            goal="restart after approval before dispatch",
            call_id="call-approved-restart",
        )
        resolution = await first_service.resolve_approval(
            fixture.approval.approval_id,
            decision=ApprovalStatus.APPROVED,
            preview_hash=fixture.approval.preview_hash,
            reason="Reviewed before restart",
        )
        recovery_service = TaskService(database, "/api/v1")

        recovered = await recovery_service.recover_pending_approvals()
        generic = await recovery_service.recover_incomplete_tool_calls()
        approval = await recovery_service.get_approval(fixture.approval.approval_id)
        task = await recovery_service.get_task(fixture.task_id)
        events = await recovery_service.list_events(fixture.task_id)
        async with database.session() as session:
            call = await session.get(ToolCallRecord, fixture.call_id)
            record = await session.get(
                ApprovalRecord,
                fixture.approval.approval_id,
            )

        assert recovered.approvals_cancelled == 1
        assert recovered.tasks_cancelled == 1
        assert recovered.events_created == 3
        assert generic.requested_failed == 0
        assert approval.status is ApprovalStatus.CANCELLED
        assert approval.decision is ApprovalDecision.APPROVED
        assert approval.resolved_at == resolution.approval.resolved_at
        assert approval.resolution_reason == "Reviewed before restart"
        assert record is not None
        assert record.resolved_by == "local_user"
        assert task.status is TaskStatus.CANCELLED
        assert call is not None
        assert call.status == "cancelled"
        assert call.error_code == "APPROVAL_RUNTIME_LOST"
        assert [event.type for event in events[-3:]] == [
            "approval.invalidated",
            "tool.cancelled",
            "task.cancelled",
        ]
    finally:
        await database.dispose()
