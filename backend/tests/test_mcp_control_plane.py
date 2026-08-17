import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from deskpilot.application.mcp_stdio import (
    McpBundleRejectedError,
    McpStdioHost,
    McpStdioTimeoutError,
)
from deskpilot.mcp_servers.readonly_text_server import TOOL_NAME

SERVER_ID = "deskpilot.readonly-text"


def test_mcp_server_is_disabled_by_default_and_rejects_calls(client: TestClient) -> None:
    response = client.get("/api/v1/mcp/servers")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    server = response.json()[0]
    assert server["server_id"] == SERVER_ID
    assert server["enabled"] is False
    assert server["network_access"] is False
    assert server["filesystem_roots"] == []
    assert server["client_capabilities"] == []
    assert server["command_preview"] == [
        "python",
        "-I",
        "<bundled>/readonly_text_server.py",
    ]
    assert server["tools"][0]["risk_floor"] == "R0"

    rejected = client.post(
        f"/api/v1/mcp/servers/{SERVER_ID}/tools:call",
        json={"tool_name": TOOL_NAME, "arguments": {"text": "local only"}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "MCP_SERVER_DISABLED"
    audit = client.get("/api/v1/mcp/audit").json()["events"]
    assert [event["action"] for event in audit] == ["tool_failed"]
    assert audit[0]["details"]["error_code"] == "MCP_SERVER_DISABLED"
    assert "local only" not in str(audit)


def test_enable_call_disable_runs_real_stdio_protocol(client: TestClient) -> None:
    enabled_response = client.post(f"/api/v1/mcp/servers/{SERVER_ID}:enable")
    assert enabled_response.status_code == 200
    enabled = enabled_response.json()
    assert enabled["server"]["enabled"] is True
    assert enabled["audit_event_id"].startswith("mca_")

    text = "DeskPilot MCP\n只读协议"
    called_response = client.post(
        f"/api/v1/mcp/servers/{SERVER_ID}/tools:call",
        json={"tool_name": TOOL_NAME, "arguments": {"text": text}},
    )
    assert called_response.status_code == 200
    assert called_response.headers["cache-control"] == "no-store"
    called = called_response.json()
    assert called["protocol_version"] == "2025-11-25"
    assert called["structured_content"] == {
        "character_count": len(text),
        "line_count": 2,
        "word_count": 3,
        "text_digest": hashlib.sha256(text.encode()).hexdigest(),
    }
    assert len(called["request_digest"]) == 64
    assert len(called["result_digest"]) == 64

    disabled = client.post(f"/api/v1/mcp/servers/{SERVER_ID}:disable").json()
    assert disabled["server"]["enabled"] is False
    audit = client.get("/api/v1/mcp/audit").json()["events"]
    assert [event["action"] for event in audit] == ["enabled", "tool_called", "disabled"]
    assert audit[1]["previous_event_digest"] == audit[0]["event_digest"]
    assert audit[2]["previous_event_digest"] == audit[1]["event_digest"]


def test_mcp_rejects_unknown_tool_and_tampered_audit(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.post(
        f"/api/v1/mcp/servers/{SERVER_ID}/tools:call",
        json={"tool_name": "untrusted.write", "arguments": {}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MCP_TOOL_REJECTED"

    engine = create_engine(f"sqlite:///{(tmp_path / 'deskpilot-test.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE mcp_audit_events SET details = ? WHERE sequence = 1",
            ('{"status":"forged"}',),
        )
    engine.dispose()
    audit = client.get("/api/v1/mcp/audit")
    assert audit.status_code == 409
    assert audit.json()["code"] == "MCP_AUDIT_REJECTED"


@pytest.mark.asyncio
async def test_stdio_host_times_out_and_reaps_server(tmp_path: Path) -> None:
    script = tmp_path / "hung_server.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    host = McpStdioHost(script, timeout_seconds=0.1)

    with pytest.raises(McpStdioTimeoutError):
        await host.invoke(TOOL_NAME, {"text": "timeout"})


@pytest.mark.asyncio
async def test_stdio_host_rejects_bundle_changes_and_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    host = McpStdioHost(script)
    script.write_text("raise SystemExit(1)\n", encoding="utf-8")
    monkeypatch.setenv("DESKPILOT_TEST_SECRET", "must-not-cross-mcp-boundary")

    assert "DESKPILOT_TEST_SECRET" not in host._isolated_environment()
    with pytest.raises(McpBundleRejectedError):
        await host.invoke(TOOL_NAME, {"text": "tamper"})
