from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.runner_supervisor import (
    RunnerSupervisor,
    RunnerSupervisorSnapshot,
    RunnerSupervisorState,
)
from deskpilot.core.config import Settings
from deskpilot.main import create_app


class _OpenCircuitRunner:
    is_running = False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def snapshot(self) -> RunnerSupervisorSnapshot:
        return RunnerSupervisorSnapshot(
            state=RunnerSupervisorState.OPEN,
            runner_id=None,
            process_id=None,
            generation=2,
            start_attempts=4,
            consecutive_failures=3,
            total_failures=3,
            next_retry_at_monotonic=42.5,
            retry_in_seconds=12.5,
            stable_since_monotonic=None,
            stable_for_seconds=None,
            last_failure_code="RUNNER_EXITED",
        )


def test_health_exposes_degraded_runner_recovery_without_sensitive_session_data(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    settings = Settings(
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'runner-health.db').as_posix()}"
        ),
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    runner = _OpenCircuitRunner()

    with TestClient(
        create_app(
            settings,
            runner_supervisor=cast(RunnerSupervisor, runner),
        )
    ) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "service": "deskpilot-api",
        "version": "0.1.0",
        "processor": "model-gateway+runner",
        "runner": "circuit_open",
        "runner_state": "open",
        "runner_generation": 2,
        "runner_consecutive_failures": 3,
        "runner_restart_attempts": 3,
        "runner_retry_in_seconds": 12.5,
        "runner_last_failure_code": "RUNNER_EXITED",
        "model_provider": "fake-local",
    }
    serialized = response.text.lower()
    assert "startup_nonce" not in serialized
    assert "secret" not in serialized
