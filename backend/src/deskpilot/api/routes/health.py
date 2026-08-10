"""Health endpoints."""

from fastapi import APIRouter, Request

from deskpilot import __version__
from deskpilot.application.model_gateway import ModelGateway
from deskpilot.application.runner_supervisor import RunnerSupervisor

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, str | int | float | None]:
    runner: RunnerSupervisor = request.app.state.runner_supervisor
    model_gateway: ModelGateway = request.app.state.model_gateway
    runner_snapshot = runner.snapshot()
    runner_status = "ready" if runner.is_running else "recovering"
    if runner_snapshot.state.value == "open":
        runner_status = "circuit_open"
    default_provider = model_gateway.default_descriptor()
    return {
        "status": "ok" if runner.is_running else "degraded",
        "service": "deskpilot-api",
        "version": __version__,
        "processor": "model-gateway+runner",
        "runner": runner_status,
        "runner_state": runner_snapshot.state.value,
        "runner_generation": runner_snapshot.generation,
        "runner_consecutive_failures": runner_snapshot.consecutive_failures,
        "runner_restart_attempts": max(0, runner_snapshot.start_attempts - 1),
        "runner_retry_in_seconds": runner_snapshot.retry_in_seconds,
        "runner_last_failure_code": runner_snapshot.last_failure_code,
        "model_provider": default_provider.provider_id,
    }
