import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_health_remains_public(raw_client: TestClient) -> None:
    response = raw_client.get("/api/v1/health")

    assert response.status_code == 200


def test_session_bootstrap_requires_trusted_origin(
    raw_client: TestClient,
    allowed_origin: str,
    session_token: str,
) -> None:
    missing_origin = raw_client.get("/api/v1/session")
    denied_origin = raw_client.get(
        "/api/v1/session",
        headers={"Origin": "https://attacker.example"},
    )
    allowed = raw_client.get(
        "/api/v1/session",
        headers={"Origin": allowed_origin},
    )

    assert missing_origin.status_code == 403
    assert missing_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert denied_origin.status_code == 403
    assert denied_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert allowed.status_code == 200
    assert allowed.json()["access_token"] == session_token
    assert allowed.json()["websocket_protocol"] == "deskpilot.v1"
    assert allowed.headers["cache-control"] == "no-store"


def test_same_origin_fetch_metadata_can_bootstrap_through_dev_proxy(
    raw_client: TestClient,
    session_token: str,
) -> None:
    response = raw_client.get(
        "/api/v1/session",
        headers={
            "Sec-Fetch-Site": "same-origin",
            "X-DeskPilot-Client": "deskpilot-web-v1",
        },
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == session_token


def test_task_api_rejects_missing_token_and_untrusted_origin(
    raw_client: TestClient,
    allowed_origin: str,
    session_token: str,
) -> None:
    missing_token = raw_client.post(
        "/api/v1/tasks",
        headers={"Origin": allowed_origin},
        json={"goal": "不应创建"},
    )
    untrusted_origin = raw_client.post(
        "/api/v1/tasks",
        headers={
            "Authorization": f"Bearer {session_token}",
            "Origin": "https://attacker.example",
        },
        json={"goal": "不应创建"},
    )
    missing_origin = raw_client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"goal": "不应创建"},
    )

    assert missing_token.status_code == 401
    assert missing_token.json()["code"] == "SESSION_TOKEN_INVALID"
    assert untrusted_origin.status_code == 403
    assert untrusted_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert missing_origin.status_code == 403
    assert missing_origin.json()["code"] == "ORIGIN_REQUIRED"


def test_validation_and_framework_errors_use_problem_details(client: TestClient) -> None:
    validation = client.post("/api/v1/tasks", json={"goal": ""})
    missing_route = client.get("/api/v1/not-a-route")

    assert validation.status_code == 422
    assert validation.headers["content-type"] == "application/problem+json"
    assert validation.json()["code"] == "REQUEST_VALIDATION_FAILED"
    assert validation.json()["errors"][0]["pointer"] == "/body/goal"
    assert missing_route.status_code == 404
    assert missing_route.headers["content-type"] == "application/problem+json"
    assert missing_route.json()["status"] == 404


def test_websocket_rejects_invalid_origin_and_session(
    raw_client: TestClient,
    allowed_origin: str,
    session_token: str,
) -> None:
    with pytest.raises(WebSocketDisconnect) as invalid_origin:
        with raw_client.websocket_connect(
            "/api/v1/ws/tasks/tsk_missing",
            headers={"Origin": "https://attacker.example"},
            subprotocols=["deskpilot.v1", f"deskpilot.auth.{session_token}"],
        ):
            pass

    with pytest.raises(WebSocketDisconnect) as invalid_session:
        with raw_client.websocket_connect(
            "/api/v1/ws/tasks/tsk_missing",
            headers={"Origin": allowed_origin},
            subprotocols=["deskpilot.v1", "deskpilot.auth.invalid-token"],
        ):
            pass

    assert invalid_origin.value.code == 4403
    assert invalid_session.value.code == 4401
