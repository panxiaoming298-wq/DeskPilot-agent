from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.core.config import Settings
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_SESSION_TOKEN = "test-session-token-with-at-least-32-characters"


@pytest.fixture
def allowed_origin() -> str:
    return TEST_ORIGIN


@pytest.fixture
def session_token() -> str:
    return TEST_SESSION_TOKEN


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "deskpilot-test.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_SESSION_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "runner-receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {TEST_SESSION_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(create_app(settings), headers=headers) as test_client:
        yield test_client


@pytest.fixture
def raw_client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "deskpilot-security-test.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_SESSION_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "runner-receipts.db"),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def slow_client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "deskpilot-control-test.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.2,
        session_token=SecretStr(TEST_SESSION_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "runner-receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {TEST_SESSION_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(create_app(settings), headers=headers) as test_client:
        yield test_client
