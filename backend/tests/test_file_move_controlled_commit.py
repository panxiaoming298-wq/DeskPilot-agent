import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from deskpilot.application.runner_client import RunnerClient
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.runner.commit_receipts import CommitReceiptStore, PreparedCommitRecord
from deskpilot.tools.builtins import create_builtin_executor, create_builtin_registry
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FileMoveInput,
    FileMoveOutput,
    FileMovePrepare,
    expected_file_move_versions,
    project_file_move_resources,
    read_file_version,
)
from tests.authorization_helpers import make_tool_authorization


def test_file_move_contract_projects_exact_versioned_resources(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "renamed.txt"
    source.write_text("controlled commit", encoding="utf-8")
    arguments = FileMoveInput(source=str(source), destination=str(destination))

    versions = expected_file_move_versions(arguments)
    resources = project_file_move_resources(arguments)

    assert FILE_MOVE_CONTRACT.risk_level == "R1"
    assert FILE_MOVE_CONTRACT.reversible is True
    assert FILE_MOVE_CONTRACT.execution.commit_protocol == "brokered"
    assert FILE_MOVE_CONTRACT.execution.idempotency == "key_required"
    assert versions == {
        "destination": "absent",
        "source": read_file_version(source),
    }
    assert {resource.identifier for resource in resources} == {
        str(source.resolve()),
        str(destination.resolve()),
    }
    assert [resource.version_digest for resource in resources] == [
        None,
        versions["source"],
    ]


@pytest.mark.asyncio
async def test_real_runner_moves_once_and_receipt_survives_restart(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    receipt_db = tmp_path / "runner-receipts.db"
    source.write_text("durable move evidence", encoding="utf-8")
    arguments = {"source": str(source), "destination": str(destination)}
    versions = expected_file_move_versions(FileMoveInput.model_validate(arguments))
    call_id = "call-file-move-durable"
    authorization = make_tool_authorization(
        FILE_MOVE_CONTRACT,
        task_id="task-file-move",
        step_id="step-file-move",
        call_id=call_id,
        actor_id="local-user",
        arguments=arguments,
        expected_resource_versions=versions,
    )
    registry = create_builtin_registry()
    first = RunnerClient(
        registry=registry,
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(receipt_db),
    )
    await first.start()
    try:
        result = await first.call_tool(
            task_id="task-file-move",
            step_id="step-file-move",
            tool_name=FILE_MOVE_CONTRACT.name,
            tool_version=FILE_MOVE_CONTRACT.version,
            arguments=arguments,
            actor="local-user",
            call_id=call_id,
            idempotency_key="file-move-key-00000001",
            expected_resource_versions=versions,
            authorization=authorization,
        )
        receipt = await first.get_commit_receipt(call_id)
    finally:
        await first.stop()

    assert result.status == "succeeded"
    assert result.output is not None
    output = FileMoveOutput.model_validate(result.output)
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "durable move evidence"
    assert receipt == output.commit_receipt
    assert receipt.resource_versions_before == versions
    assert receipt.resource_versions_after == {
        "destination": versions["source"],
        "source": "absent",
    }

    restarted = RunnerClient(
        registry=registry,
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(receipt_db),
    )
    await restarted.start()
    try:
        recovered = await restarted.get_commit_receipt(call_id)
    finally:
        await restarted.stop()
    assert recovered == receipt

    connection = sqlite3.connect(receipt_db)
    try:
        row = connection.execute(
            """
            SELECT state, idempotency_key_digest, receipt_json
            FROM controlled_commit_attempts
            WHERE call_id = ?
            """,
            (call_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[0] == "committed"
    assert row[1] != "file-move-key-00000001"
    assert "file-move-key-00000001" not in row[2]


@pytest.mark.asyncio
async def test_file_move_rejects_stale_source_before_worker_commit(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("approved content", encoding="utf-8")
    arguments = {"source": str(source), "destination": str(destination)}
    versions = expected_file_move_versions(FileMoveInput.model_validate(arguments))
    authorization = make_tool_authorization(
        FILE_MOVE_CONTRACT,
        task_id="task-stale-move",
        step_id="step-stale-move",
        call_id="call-stale-move",
        actor_id="local-user",
        arguments=arguments,
        expected_resource_versions=versions,
    )
    source.write_text("changed after approval", encoding="utf-8")
    client = RunnerClient(
        registry=create_builtin_registry(),
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(tmp_path / "receipts.db"),
    )
    await client.start()
    try:
        result = await client.call_tool(
            task_id="task-stale-move",
            step_id="step-stale-move",
            tool_name=FILE_MOVE_CONTRACT.name,
            tool_version=FILE_MOVE_CONTRACT.version,
            arguments=arguments,
            actor="local-user",
            call_id="call-stale-move",
            idempotency_key="file-move-key-stale-01",
            expected_resource_versions=versions,
            authorization=authorization,
        )
    finally:
        await client.stop()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "POLICY_AUTHORIZATION_MISMATCH"
    assert source.read_text(encoding="utf-8") == "changed after approval"
    assert not destination.exists()


def test_startup_recovery_distinguishes_committed_from_no_effect(tmp_path: Path) -> None:
    executor = create_builtin_executor()
    source = tmp_path / "recover-source.txt"
    destination = tmp_path / "recover-destination.txt"
    source.write_text("crash after external commit", encoding="utf-8")
    source_version = read_file_version(source)
    prepared = FileMovePrepare(
        source=str(source.resolve()),
        destination=str(destination.resolve()),
        source_version=source_version,
    )
    timestamp = datetime.now(UTC)
    committed_store = CommitReceiptStore(tmp_path / "committed.db")
    record = PreparedCommitRecord(
        receipt_id=f"cmt_{'a' * 64}",
        call_id="call-recover-committed",
        tool_name=FILE_MOVE_CONTRACT.name,
        tool_version=FILE_MOVE_CONTRACT.version,
        authorization_id=f"auth_{'b' * 64}",
        approval_id="apr_recover_committed",
        preview_hash="c" * 64,
        prepare_digest="d" * 64,
        idempotency_key_digest="e" * 64,
        binding_digest="f" * 64,
        state="prepared",
        prepared_payload=prepared.model_dump(mode="json"),
        commit_started_at=None,
        receipt=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    committed_store.stage(record)
    committed_store.mark_committing(
        record.receipt_id,
        commit_started_at=timestamp,
    )
    source.rename(destination)

    executor.recover_commit_receipts(committed_store)

    recovered = committed_store.get_for_call(record.call_id)
    assert recovered is not None
    assert recovered.state == "committed"
    assert recovered.receipt is not None
    assert recovered.receipt.resource_versions_after["destination"] == source_version

    no_effect_source = tmp_path / "no-effect-source.txt"
    no_effect_destination = tmp_path / "no-effect-destination.txt"
    no_effect_source.write_text("commit never happened", encoding="utf-8")
    no_effect_prepare = FileMovePrepare(
        source=str(no_effect_source.resolve()),
        destination=str(no_effect_destination.resolve()),
        source_version=read_file_version(no_effect_source),
    )
    no_effect_store = CommitReceiptStore(tmp_path / "no-effect.db")
    no_effect_record = PreparedCommitRecord(
        receipt_id=f"cmt_{'1' * 64}",
        call_id="call-recover-no-effect",
        tool_name=FILE_MOVE_CONTRACT.name,
        tool_version=FILE_MOVE_CONTRACT.version,
        authorization_id=f"auth_{'2' * 64}",
        approval_id="apr_recover_no_effect",
        preview_hash="3" * 64,
        prepare_digest="4" * 64,
        idempotency_key_digest="5" * 64,
        binding_digest=sha256_digest({"kind": "no_effect"}),
        state="prepared",
        prepared_payload=no_effect_prepare.model_dump(mode="json"),
        commit_started_at=None,
        receipt=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    no_effect_store.stage(no_effect_record)
    no_effect_store.mark_committing(
        no_effect_record.receipt_id,
        commit_started_at=timestamp,
    )

    executor.recover_commit_receipts(no_effect_store)

    no_effect = no_effect_store.get_for_call(no_effect_record.call_id)
    assert no_effect is not None
    assert no_effect.state == "no_effect"
    assert no_effect.receipt is None


def test_startup_recovery_never_attributes_external_move_to_prepared_attempt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "external-source.txt"
    destination = tmp_path / "external-destination.txt"
    source.write_text("moved by someone else", encoding="utf-8")
    prepared = FileMovePrepare(
        source=str(source.resolve()),
        destination=str(destination.resolve()),
        source_version=read_file_version(source),
    )
    timestamp = datetime.now(UTC)
    store = CommitReceiptStore(tmp_path / "prepared-only.db")
    record = PreparedCommitRecord(
        receipt_id=f"cmt_{'6' * 64}",
        call_id="call-prepared-external-move",
        tool_name=FILE_MOVE_CONTRACT.name,
        tool_version=FILE_MOVE_CONTRACT.version,
        authorization_id=f"auth_{'7' * 64}",
        approval_id="apr_prepared_external_move",
        preview_hash="8" * 64,
        prepare_digest="9" * 64,
        idempotency_key_digest="a" * 64,
        binding_digest="b" * 64,
        state="prepared",
        prepared_payload=prepared.model_dump(mode="json"),
        commit_started_at=None,
        receipt=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    store.stage(record)
    source.rename(destination)

    create_builtin_executor().recover_commit_receipts(store)

    recovered = store.get_for_call(record.call_id)
    assert recovered is not None
    assert recovered.state == "no_effect"
    assert recovered.receipt is None


@pytest.mark.asyncio
async def test_file_move_cancel_before_dispatch_has_no_receipt(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("not moved", encoding="utf-8")
    client = RunnerClient(
        registry=create_builtin_registry(),
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(tmp_path / "receipts.db"),
    )
    await client.start()
    try:
        assert await client.get_commit_receipt("call-never-dispatched") is None
    finally:
        await client.stop()
    assert source.exists()
    assert not destination.exists()


@pytest.mark.asyncio
async def test_concurrent_receipt_queries_are_independent(tmp_path: Path) -> None:
    client = RunnerClient(
        registry=create_builtin_registry(),
        require_windows_sandbox=False,
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
        commit_receipt_database_path=str(tmp_path / "receipts.db"),
    )
    await client.start()
    try:
        results = await asyncio.gather(
            client.get_commit_receipt("call-query-one"),
            client.get_commit_receipt("call-query-two"),
        )
    finally:
        await client.stop()
    assert results == [None, None]
