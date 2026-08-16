"""Real Runner process-kill drills for controlled file.move commit boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.runner_client import RunnerClient, RunnerExitedError
from deskpilot.application.task_service import TaskService, ToolCallStatus
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import ApprovalStatus, DataEgress
from deskpilot.domain.policy import (
    PolicyEffect,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.reconciliations import ReconciliationStatus
from deskpilot.domain.schemas import TaskCreate, TaskStatus
from deskpilot.infrastructure.database import Database
from deskpilot.runner.commit_receipts import CommitReceiptStore
from deskpilot.tools.builtins import create_builtin_registry
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FileMoveInput,
    expected_file_move_versions,
    project_file_move_resources,
)
from tests.authorization_helpers import make_tool_authorization

_FAULT_MODULE = "tests.fault_injection.runner_commit_boundary"
_CHECKPOINT_KIND = "runner_file_move_commit"


def _fault_command() -> tuple[str, ...]:
    backend_root = Path(__file__).parents[1]
    bootstrap = (
        "import runpy,sys;"
        f"sys.path.insert(0,{str(backend_root)!r});"
        f"runpy.run_module({_FAULT_MODULE!r},run_name='__main__')"
    )
    return sys.executable, "-c", bootstrap


def _process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


async def _wait_for_checkpoint(
    path: Path,
    client: RunnerClient,
) -> Mapping[str, Any]:
    for _ in range(400):
        if await asyncio.to_thread(path.exists):
            try:
                raw_payload = await asyncio.to_thread(path.read_text, encoding="utf-8")
                payload = json.loads(raw_payload)
            except (json.JSONDecodeError, OSError):
                await asyncio.sleep(0.05)
                continue
            if isinstance(payload, dict):
                return payload
        if not client.is_running:
            pytest.fail("fault Runner exited before reaching its commit checkpoint")
        await asyncio.sleep(0.05)
    pytest.fail("fault Runner did not reach its commit checkpoint")


async def _kill_checkpoint_process(
    client: RunnerClient,
    checkpoint: Mapping[str, Any],
) -> None:
    process_id = checkpoint.get("process_id")
    assert isinstance(process_id, int)
    launcher_id = client.process_id
    assert launcher_id is not None
    if os.name == "nt":
        assert checkpoint.get("parent_process_id") == launcher_id
    else:
        assert process_id == launcher_id
    os.kill(process_id, signal.SIGTERM)
    for _ in range(200):
        if not _process_is_alive(process_id):
            return
        await asyncio.sleep(0.01)
    pytest.fail("fault Runner process survived SIGTERM")


def _fault_client(receipt_db: Path) -> RunnerClient:
    return RunnerClient(
        registry=create_builtin_registry(),
        command=_fault_command(),
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(receipt_db),
    )


def _normal_client(receipt_db: Path) -> RunnerClient:
    return RunnerClient(
        registry=create_builtin_registry(),
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(receipt_db),
    )


async def _start_fault_call(
    client: RunnerClient,
    *,
    task_id: str,
    call_id: str,
    arguments: dict[str, object],
    versions: dict[str, str],
    authorization: ToolAuthorizationGrant,
    idempotency_key: str,
) -> asyncio.Task[Any]:
    return asyncio.create_task(
        client.call_tool(
            task_id=task_id,
            step_id="step-file-move",
            tool_name=FILE_MOVE_CONTRACT.name,
            tool_version=FILE_MOVE_CONTRACT.version,
            arguments=arguments,
            actor="local_user",
            call_id=call_id,
            idempotency_key=idempotency_key,
            expected_resource_versions=versions,
            authorization=authorization,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "journal_before_kill", "journal_after_restart", "has_receipt"),
    [
        ("prepared", "prepared", "no_effect", False),
        ("external_effect_applied", "committing", "committed", True),
    ],
)
async def test_runner_kill_recovers_proven_commit_outcome_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    journal_before_kill: str,
    journal_after_restart: str,
    has_receipt: bool,
) -> None:
    source = tmp_path / f"{phase}-source.txt"
    destination = tmp_path / f"{phase}-destination.txt"
    receipt_db = tmp_path / f"{phase}-receipts.db"
    checkpoint_path = tmp_path / f"{phase}-checkpoint.json"
    source.write_text(f"durable effect for {phase}", encoding="utf-8")
    arguments: dict[str, object] = {
        "source": str(source),
        "destination": str(destination),
    }
    versions = expected_file_move_versions(FileMoveInput.model_validate(arguments))
    call_id = f"call-kill-{phase}"
    authorization = make_tool_authorization(
        FILE_MOVE_CONTRACT,
        task_id="task-runner-kill",
        step_id="step-file-move",
        call_id=call_id,
        actor_id="local_user",
        arguments=arguments,
        expected_resource_versions=versions,
    )
    monkeypatch.setenv("DESKPILOT_TEST_RUNNER_FAULT_PHASE", phase)
    monkeypatch.setenv(
        "DESKPILOT_TEST_RUNNER_FAULT_CHECKPOINT",
        str(checkpoint_path),
    )

    fault = _fault_client(receipt_db)
    call_task: asyncio.Task[Any] | None = None
    await fault.start()
    try:
        call_task = await _start_fault_call(
            fault,
            task_id="task-runner-kill",
            call_id=call_id,
            arguments=arguments,
            versions=versions,
            authorization=authorization,
            idempotency_key=f"runner-kill-{phase}-key",
        )
        checkpoint = await _wait_for_checkpoint(checkpoint_path, fault)
        assert checkpoint["checkpoint"] == _CHECKPOINT_KIND
        assert checkpoint["phase"] == phase
        assert checkpoint["call_id"] == call_id
        assert checkpoint["journal_state"] == journal_before_kill
        before_kill = CommitReceiptStore(receipt_db).get_for_call(call_id)
        assert before_kill is not None
        assert before_kill.state == journal_before_kill
        if phase == "prepared":
            assert source.exists()
            assert not destination.exists()
        else:
            assert not source.exists()
            assert destination.read_text(encoding="utf-8") == (
                f"durable effect for {phase}"
            )

        await _kill_checkpoint_process(fault, checkpoint)
        with pytest.raises(RunnerExitedError):
            await asyncio.wait_for(call_task, timeout=5)
    finally:
        if call_task is not None and not call_task.done():
            call_task.cancel()
            await asyncio.gather(call_task, return_exceptions=True)
        await fault.stop()

    restarted = _normal_client(receipt_db)
    await restarted.start()
    try:
        first_query = await restarted.get_commit_receipt(call_id)
        second_query = await restarted.get_commit_receipt(call_id)
    finally:
        await restarted.stop()

    recovered = CommitReceiptStore(receipt_db).get_for_call(call_id)
    assert recovered is not None
    assert recovered.state == journal_after_restart
    assert (first_query is not None) is has_receipt
    assert second_query == first_query
    if has_receipt:
        assert first_query is not None
        assert first_query.call_id == call_id
        assert first_query.resource_versions_before == versions
        assert not source.exists()
        assert destination.read_text(encoding="utf-8") == (
            f"durable effect for {phase}"
        )
    else:
        assert source.read_text(encoding="utf-8") == f"durable effect for {phase}"
        assert not destination.exists()


async def _create_running_file_move_call(
    service: TaskService,
    *,
    runner_id: str,
    call_id: str,
    arguments: dict[str, object],
    versions: dict[str, str],
    idempotency_key: str,
) -> tuple[str, ToolAuthorizationGrant]:
    task = await service.create_task(TaskCreate(goal="reconcile killed file move Runner"))
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
        step_id="step-file-move",
        tool_name=FILE_MOVE_CONTRACT.name,
        tool_version=FILE_MOVE_CONTRACT.version,
        contract_digest=FILE_MOVE_CONTRACT.digest,
        arguments=arguments,
        idempotency=FILE_MOVE_CONTRACT.execution.idempotency,
        idempotency_key=idempotency_key,
        risk=FILE_MOVE_CONTRACT.risk_level.value,
    )
    resources = project_file_move_resources(FileMoveInput.model_validate(arguments))
    request = ToolAuthorizationRequest(
        task_id=task.task_id,
        step_id="step-file-move",
        call_id=call_id,
        actor="local_user",
        tool_name=FILE_MOVE_CONTRACT.name,
        tool_version=FILE_MOVE_CONTRACT.version,
        contract_digest=FILE_MOVE_CONTRACT.digest,
        arguments_digest=sha256_digest(arguments),
        risk_level=FILE_MOVE_CONTRACT.risk_level,
        side_effects=FILE_MOVE_CONTRACT.side_effects,
        reversible=FILE_MOVE_CONTRACT.reversible,
        capabilities=FILE_MOVE_CONTRACT.security.capabilities,
        network_access=FILE_MOVE_CONTRACT.security.network_access,
        resources=resources,
        expected_resource_versions_digest=sha256_digest(versions),
    )
    decision = BuiltinPolicyEngine(
        allowed_capabilities=FILE_MOVE_CONTRACT.security.capabilities,
        allowed_resource_scopes=tuple(resource.scope_key for resource in resources),
    ).evaluate(request)
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    approval = await service.apply_policy_decision(
        task.task_id,
        call_id,
        request=request,
        decision=decision,
        title="Move one approved file",
        purpose="Exercise Runner commit-boundary crash recovery.",
        consequences=("Moves the approved source to the approved destination.",),
        data_egress=DataEgress(enabled=False),
        expected_resource_versions=versions,
    )
    assert approval is not None
    resolution = await service.resolve_approval(
        approval.approval_id,
        decision=ApprovalStatus.APPROVED,
        preview_hash=approval.preview_hash,
    )
    assert resolution.approval.resolved_at is not None
    grant = ToolAuthorizationGrant.issue(
        decision_id=decision.decision_id,
        request_digest=decision.request_digest,
        task_id=task.task_id,
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
        approval_id=approval.approval_id,
        preview_hash=approval.preview_hash,
        approved_at=resolution.approval.resolved_at,
        grant_expires_at=approval.expires_at,
    )
    await service.start_tool_call(
        task.task_id,
        call_id,
        runner_id=runner_id,
        authorization=grant,
        arguments=arguments,
        expected_resource_versions=versions,
    )
    return task.task_id, grant


@pytest.mark.asyncio
async def test_runner_kill_with_ambiguous_external_state_enters_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "unknown-source.txt"
    destination = tmp_path / "unknown-destination.txt"
    receipt_db = tmp_path / "unknown-receipts.db"
    checkpoint_path = tmp_path / "unknown-checkpoint.json"
    source.write_text("approved source version", encoding="utf-8")
    arguments: dict[str, object] = {
        "source": str(source),
        "destination": str(destination),
    }
    versions = expected_file_move_versions(FileMoveInput.model_validate(arguments))
    call_id = "call-kill-committing-unknown"
    idempotency_key = "runner-kill-committing-unknown-key"
    control_db = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'unknown-control.db').as_posix()}"
    )
    await control_db.migrate()
    service = TaskService(control_db, "/api/v1")
    monkeypatch.setenv("DESKPILOT_TEST_RUNNER_FAULT_PHASE", "committing")
    monkeypatch.setenv(
        "DESKPILOT_TEST_RUNNER_FAULT_CHECKPOINT",
        str(checkpoint_path),
    )

    fault = _fault_client(receipt_db)
    call_task: asyncio.Task[Any] | None = None
    await fault.start()
    try:
        runner_id = fault.runner_id
        assert runner_id is not None
        task_id, authorization = await _create_running_file_move_call(
            service,
            runner_id=runner_id,
            call_id=call_id,
            arguments=arguments,
            versions=versions,
            idempotency_key=idempotency_key,
        )
        call_task = await _start_fault_call(
            fault,
            task_id=task_id,
            call_id=call_id,
            arguments=arguments,
            versions=versions,
            authorization=authorization,
            idempotency_key=idempotency_key,
        )
        checkpoint = await _wait_for_checkpoint(checkpoint_path, fault)
        assert checkpoint["checkpoint"] == _CHECKPOINT_KIND
        assert checkpoint["phase"] == "committing"
        assert checkpoint["journal_state"] == "committing"
        assert source.exists()
        assert not destination.exists()

        await _kill_checkpoint_process(fault, checkpoint)
        with pytest.raises(RunnerExitedError):
            await asyncio.wait_for(call_task, timeout=5)
    finally:
        if call_task is not None and not call_task.done():
            call_task.cancel()
            await asyncio.gather(call_task, return_exceptions=True)
        await fault.stop()

    source.write_text("changed while Runner was down", encoding="utf-8")
    restarted = _normal_client(receipt_db)
    await restarted.start()
    try:
        assert await restarted.get_commit_receipt(call_id) is None
        assert await restarted.get_commit_receipt(call_id) is None
    finally:
        await restarted.stop()

    recovered = CommitReceiptStore(receipt_db).get_for_call(call_id)
    assert recovered is not None
    assert recovered.state == "unknown"
    assert recovered.receipt is None
    assert source.read_text(encoding="utf-8") == "changed while Runner was down"
    assert not destination.exists()

    await service.finish_tool_call(
        task_id,
        call_id,
        status=ToolCallStatus.UNKNOWN,
        error_code="RUNNER_EXITED",
        resolution_source="runner_process_kill",
    )
    assert await service.get_tool_call_status(task_id, call_id) is ToolCallStatus.UNKNOWN
    assert (await service.get_task(task_id)).status is TaskStatus.WAITING_RECONCILIATION
    reconciliations = await service.list_reconciliations(task_id=task_id)
    assert len(reconciliations) == 1
    assert reconciliations[0].status is ReconciliationStatus.PENDING
    assert reconciliations[0].call_id == call_id
    assert reconciliations[0].call_error_code == "RUNNER_EXITED"
    assert reconciliations[0].call_resolution_source == "runner_process_kill"

    recovery = await service.recover_incomplete_tool_calls()
    assert recovery.running_unknown == 0
    assert recovery.requested_failed == 0
    assert len(await service.list_reconciliations(task_id=task_id)) == 1
    events = await service.list_events(task_id)
    assert sum(event.type == "tool.requested" for event in events) == 1
    assert sum(event.type == "tool.started" for event in events) == 1
    assert sum(event.type == "tool.unknown" for event in events) == 1
    assert not any(event.type == "tool.completed" for event in events)
    await control_db.dispose()
