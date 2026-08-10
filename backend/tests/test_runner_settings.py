import pytest
from pydantic import ValidationError

from deskpilot.core.config import Settings


def test_runner_recovery_settings_accept_a_valid_policy() -> None:
    settings = Settings(
        _env_file=None,
        runner_heartbeat_interval_seconds=0.2,
        runner_heartbeat_timeout_seconds=1,
        runner_restart_base_delay_seconds=0.5,
        runner_restart_max_delay_seconds=4,
        runner_circuit_failure_threshold=4,
        runner_circuit_recovery_timeout_seconds=12,
        runner_stable_window_seconds=3,
    )

    assert settings.runner_restart_base_delay_seconds == 0.5
    assert settings.runner_restart_max_delay_seconds == 4
    assert settings.runner_circuit_failure_threshold == 4


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "runner_heartbeat_interval_seconds": 1,
                "runner_heartbeat_timeout_seconds": 1,
            },
            "runner_heartbeat_timeout_seconds must exceed",
        ),
        (
            {
                "runner_restart_base_delay_seconds": 2,
                "runner_restart_max_delay_seconds": 1,
            },
            "runner_restart_max_delay_seconds must be at least",
        ),
    ],
)
def test_runner_recovery_settings_reject_invalid_cross_field_values(
    overrides: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **overrides)  # type: ignore[arg-type]
