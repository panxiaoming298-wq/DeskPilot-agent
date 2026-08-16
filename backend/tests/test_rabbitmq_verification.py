import pytest
from pydantic import ValidationError

from deskpilot.core.config import Settings
from deskpilot.infrastructure.rabbitmq_verification import (
    RabbitMqVerificationConfigurationError,
    load_rabbitmq_verification_url,
)


def test_rabbitmq_is_opt_in_and_requires_a_url() -> None:
    assert Settings(_env_file=None).event_transport == "local"
    with pytest.raises(ValidationError, match="rabbitmq_url is required"):
        Settings(_env_file=None, event_transport="rabbitmq")


def test_rabbitmq_verification_is_absent_without_a_url() -> None:
    assert load_rabbitmq_verification_url({}) is None


@pytest.mark.parametrize(
    ("environ", "detail"),
    [
        (
            {"DESKPILOT_TEST_RABBITMQ_URL": "amqp://user:secret@127.0.0.1/broker_test"},
            "ALLOW=1",
        ),
        (
            {
                "DESKPILOT_TEST_RABBITMQ_URL": "amqps://user:secret@127.0.0.1/broker_test",
                "DESKPILOT_TEST_RABBITMQ_ALLOW": "1",
            },
            "amqp URL",
        ),
        (
            {
                "DESKPILOT_TEST_RABBITMQ_URL": "amqp://user:secret@broker/broker_test",
                "DESKPILOT_TEST_RABBITMQ_ALLOW": "1",
            },
            "loopback",
        ),
        (
            {
                "DESKPILOT_TEST_RABBITMQ_URL": "amqp://user:secret@127.0.0.1/deskpilot",
                "DESKPILOT_TEST_RABBITMQ_ALLOW": "1",
            },
            "vhost-name token",
        ),
        (
            {
                "DESKPILOT_TEST_RABBITMQ_URL": "amqp://127.0.0.1/broker_test",
                "DESKPILOT_TEST_RABBITMQ_ALLOW": "1",
            },
            "credentials",
        ),
    ],
)
def test_rabbitmq_verification_rejects_unsafe_targets(
    environ: dict[str, str],
    detail: str,
) -> None:
    with pytest.raises(RabbitMqVerificationConfigurationError, match=detail):
        load_rabbitmq_verification_url(environ)


def test_rabbitmq_verification_accepts_acknowledged_loopback_test_vhost() -> None:
    raw_url = "amqp://user:secret@127.0.0.1:5672/deskpilot_test"
    assert (
        load_rabbitmq_verification_url(
            {
                "DESKPILOT_TEST_RABBITMQ_URL": raw_url,
                "DESKPILOT_TEST_RABBITMQ_ALLOW": "1",
            }
        )
        == raw_url
    )
