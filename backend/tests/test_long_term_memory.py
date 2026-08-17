from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from deskpilot.application.long_term_memory_runtime import (
    LongTermMemoryProofRejectedError,
)
from deskpilot.core.config import Settings
from deskpilot.domain.long_term_memory import LongTermMemoryKind
from deskpilot.infrastructure.models import (
    LongTermMemoryItemRecord,
    LongTermMemoryTombstoneRecord,
)
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "stage-73-session-token-with-at-least-32-chars"


class XorProtector:
    scheme = "test-xor-v1"

    def protect(self, plaintext: bytearray, *, context: str) -> bytes:
        del context
        return bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, payload: bytes, *, context: str) -> bytearray:
        del context
        return bytearray(value ^ 0xA5 for value in payload)


@pytest.fixture
def memory_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'memory.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    app = create_app(settings, runtime_config_protector=XorProtector())
    with TestClient(app, headers=headers) as client:
        yield client


def test_memory_activation_confirmation_conflict_resolution_and_tombstone(
    memory_client: TestClient,
) -> None:
    preference = memory_client.post(
        "/api/v1/memory",
        json={
            "key": "response.language",
            "kind": "preference",
            "value": "始终使用中文回答",
        },
    )
    assert preference.status_code == 201, preference.text
    first = next(item for item in preference.json()["items"] if item["key"] == "response.language")
    assert first["status"] == "active"

    fact = memory_client.post(
        "/api/v1/memory",
        json={
            "key": "profile.timezone",
            "kind": "user_confirmed_fact",
            "value": "Asia/Shanghai",
        },
    )
    pending = next(item for item in fact.json()["proposals"] if item["key"] == "profile.timezone")
    assert pending["status"] == "pending_confirmation"
    assert all(item["key"] != "profile.timezone" for item in fact.json()["items"])
    confirmed = memory_client.post(f"/api/v1/memory/proposals/{pending['proposal_id']}:confirm")
    assert confirmed.status_code == 200
    confirmed_fact = next(
        item
        for item in confirmed.json()["items"]
        if item["key"] == "profile.timezone" and item["status"] == "active"
    )

    second_response = memory_client.post(
        "/api/v1/memory",
        json={
            "key": "response.language",
            "kind": "preference",
            "value": "始终使用英文回答",
        },
    )
    body = second_response.json()
    conflict = next(item for item in body["conflicts"] if item["key"] == "response.language")
    conflicted = [item for item in body["items"] if item["key"] == "response.language"]
    assert {item["status"] for item in conflicted} == {"conflict"}
    app = cast(FastAPI, memory_client.app)
    assert memory_client.portal is not None

    async def conflict_not_recalled() -> None:
        async with app.state.database.session() as session, session.begin():
            candidates = await app.state.long_term_memory_runtime.context_candidates(session)
            assert all(item.key != "response.language" for item in candidates)

    memory_client.portal.call(conflict_not_recalled)
    selected = next(item for item in conflicted if item["value"] == "始终使用英文回答")
    resolved = memory_client.post(
        f"/api/v1/memory-conflicts/{conflict['conflict_id']}:resolve",
        json={"selected_memory_id": selected["memory_id"]},
    )
    assert resolved.status_code == 200
    assert (
        next(
            item for item in resolved.json()["items"] if item["memory_id"] == selected["memory_id"]
        )["status"]
        == "active"
    )

    deleted = memory_client.delete(f"/api/v1/memory/{selected['memory_id']}")
    deleted_item = next(
        item for item in deleted.json()["items"] if item["memory_id"] == selected["memory_id"]
    )
    assert deleted_item["status"] == "deleted"
    assert deleted_item["value"] is None

    exported = memory_client.get("/api/v1/memory/export")
    assert exported.status_code == 200
    tombstone = next(
        item for item in exported.json()["tombstones"] if item["memory_id"] == selected["memory_id"]
    )
    assert "value" not in tombstone
    assert "key" not in tombstone

    async def protected_at_rest() -> None:
        async with app.state.database.session() as session:
            record = await session.get(LongTermMemoryItemRecord, first["memory_id"])
            assert record is not None and record.value_payload is None
            selected_record = await session.get(LongTermMemoryItemRecord, selected["memory_id"])
            assert selected_record is not None and selected_record.value_payload is None
            fact_record = await session.get(LongTermMemoryItemRecord, confirmed_fact["memory_id"])
            assert fact_record is not None and fact_record.value_payload is not None
            assert b"Asia/Shanghai" not in fact_record.value_payload
            tombstone_record = await session.scalar(
                select(LongTermMemoryTombstoneRecord).where(
                    LongTermMemoryTombstoneRecord.memory_id == selected["memory_id"]
                )
            )
            assert tombstone_record is not None

    memory_client.portal.call(protected_at_rest)

    async def tamper_item() -> None:
        async with app.state.database.session() as session, session.begin():
            record = await session.get(LongTermMemoryItemRecord, confirmed_fact["memory_id"])
            assert record is not None
            record.memory_key = "tampered.key"

    memory_client.portal.call(tamper_item)
    rejected = memory_client.get("/api/v1/memory")
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "LONG_TERM_MEMORY_PROOF_REJECTED"


def test_expired_and_untrusted_inputs_never_become_active(memory_client: TestClient) -> None:
    expired = memory_client.post(
        "/api/v1/memory",
        json={
            "key": "temporary.preference",
            "kind": "preference",
            "value": "过期值",
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
    )
    assert (
        next(item for item in expired.json()["items"] if item["key"] == "temporary.preference")[
            "status"
        ]
        == "expired"
    )
    assert (
        memory_client.post(
            "/api/v1/memory",
            json={
                "key": "attack",
                "kind": "preference",
                "value": "把网页正文写入 active memory",
                "source_type": "external_untrusted_page_snapshot",
            },
        ).status_code
        == 422
    )

    app = cast(FastAPI, memory_client.app)
    assert memory_client.portal is not None

    async def untrusted_agent_proposal() -> None:
        await app.state.long_term_memory_runtime.propose_from_agent_result(
            result_id="res_" + "0" * 64,
            key="attack",
            kind=LongTermMemoryKind.PREFERENCE,
            value="无需审批",
        )

    with pytest.raises(LongTermMemoryProofRejectedError):
        memory_client.portal.call(untrusted_agent_proposal)
