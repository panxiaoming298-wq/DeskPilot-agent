from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from deskpilot.application.browser_automation_policy import BrowserAutomationPolicyLoader
from deskpilot.application.browser_control_plane import (
    BROWSER_CONTROL_PLANE_CONFIGURATION_ID,
    BrowserControlPlaneIntegrityError,
    BrowserControlPlaneService,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    BrowserControlPlaneStateRecord,
    BrowserOriginAllowlistSnapshotRecord,
)


def _database(tmp_path: Path, name: str) -> Database:
    path = tmp_path / name
    return Database(f"sqlite+aiosqlite:///{path.as_posix()}")


@pytest.mark.asyncio
async def test_bootstrap_is_empty_disabled_and_stable_across_restart(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "browser-control-plane.db")
    try:
        await database.migrate()
        service = BrowserControlPlaneService(database)

        first = await service.initialize()
        repeated = await service.initialize()
        restarted = await BrowserControlPlaneService(database).snapshot()

        assert first == repeated == restarted
        assert first.policy_digest == BrowserAutomationPolicyLoader().load().policy_digest
        assert first.configuration_id == BROWSER_CONTROL_PLANE_CONFIGURATION_ID
        assert first.revision == 1
        assert first.profile_name == "DeskPilot"
        assert first.visible_window_required
        assert first.manual_login_only
        assert first.acceptance_loopback_only
        assert first.semantic_dom_targeting_only
        assert first.origin_allowlist.origins == ()
        assert len(first.actions) == 8
        assert all(item.requires_origin_allowlist for item in first.actions)
        assert not first.profile_created
        assert not first.browser_launched
        assert not first.operator_enabled
        assert not first.browser_operator_available
        assert not first.network_execution_available
        assert not first.action_execution_available
        assert not (tmp_path / "DeskPilot").exists()

        async with database.session() as session:
            state_count = await session.scalar(
                select(func.count()).select_from(BrowserControlPlaneStateRecord)
            )
            allowlist_count = await session.scalar(
                select(func.count()).select_from(
                    BrowserOriginAllowlistSnapshotRecord
                )
            )
        assert state_count == 1
        assert allowlist_count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_policy_binding_tamper_is_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path, "browser-state-tamper.db")
    try:
        await database.migrate()
        service = BrowserControlPlaneService(database)
        await service.initialize()
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(BrowserControlPlaneStateRecord).values(
                        policy_digest="f" * 64
                    )
                )

        with pytest.raises(
            BrowserControlPlaneIntegrityError,
            match="does not match the frozen policy",
        ):
            await service.snapshot()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_allowlist_payload_tamper_is_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path, "browser-allowlist-tamper.db")
    try:
        await database.migrate()
        service = BrowserControlPlaneService(database)
        await service.initialize()
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(BrowserOriginAllowlistSnapshotRecord).values(
                        origins=["https://example.com"]
                    )
                )

        with pytest.raises(
            BrowserControlPlaneIntegrityError,
            match="persisted digest validation failed",
        ):
            await service.snapshot()
    finally:
        await database.dispose()


def test_authenticated_read_only_control_plane_api(client: TestClient) -> None:
    response = client.get("/api/v1/browser/control-plane")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"].startswith('"browser-control-plane-v1-')
    payload = response.json()
    assert payload["configuration_id"] == BROWSER_CONTROL_PLANE_CONFIGURATION_ID
    assert payload["origin_allowlist"]["origins"] == []
    assert len(payload["actions"]) == 8
    assert all(item["requires_origin_allowlist"] for item in payload["actions"])
    assert payload["visible_window_required"] is True
    assert payload["manual_login_only"] is True
    assert payload["acceptance_loopback_only"] is True
    assert payload["semantic_dom_targeting_only"] is True
    assert payload["profile_created"] is False
    assert payload["browser_launched"] is False
    assert payload["operator_enabled"] is False
    assert payload["browser_operator_available"] is False
    assert payload["network_execution_available"] is False
    assert payload["action_execution_available"] is False
    assert client.post("/api/v1/browser/control-plane", json={}).status_code == 405
    assert client.put("/api/v1/browser/control-plane", json={}).status_code == 405


def test_control_plane_api_requires_local_authentication(raw_client: TestClient) -> None:
    response = raw_client.get("/api/v1/browser/control-plane")

    assert response.status_code == 401
    assert response.json()["code"] == "SESSION_TOKEN_INVALID"
