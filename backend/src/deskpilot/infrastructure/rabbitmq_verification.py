"""Fail-closed configuration guard for real RabbitMQ verification drills."""

import ipaddress
import re
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

_TEST_VHOST_TOKEN = re.compile(r"(?:^|[_-])test(?:[_-]|$)", re.IGNORECASE)


class RabbitMqVerificationConfigurationError(ValueError):
    """A RabbitMQ verification target is absent or insufficiently isolated."""


def load_rabbitmq_verification_url(environ: Mapping[str, str]) -> str | None:
    """Load an acknowledged loopback RabbitMQ URL with a test-named vhost."""
    raw_url = environ.get("DESKPILOT_TEST_RABBITMQ_URL")
    if raw_url is None:
        return None
    if environ.get("DESKPILOT_TEST_RABBITMQ_ALLOW") != "1":
        raise RabbitMqVerificationConfigurationError(
            "Set DESKPILOT_TEST_RABBITMQ_ALLOW=1 to acknowledge a disposable test broker"
        )
    parsed = urlsplit(raw_url)
    if parsed.scheme != "amqp":
        raise RabbitMqVerificationConfigurationError("RabbitMQ verification requires an amqp URL")
    if parsed.username is None or parsed.password is None:
        raise RabbitMqVerificationConfigurationError(
            "RabbitMQ verification requires explicit disposable credentials"
        )
    hostname = parsed.hostname
    if hostname is None:
        raise RabbitMqVerificationConfigurationError(
            "RabbitMQ verification URL requires a loopback host"
        )
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower() == "localhost"
    if not loopback:
        raise RabbitMqVerificationConfigurationError(
            "RabbitMQ verification is restricted to a loopback host"
        )
    vhost = unquote(parsed.path.removeprefix("/"))
    if not vhost or _TEST_VHOST_TOKEN.search(vhost) is None:
        raise RabbitMqVerificationConfigurationError(
            "RabbitMQ verification requires 'test' as a vhost-name token"
        )
    return raw_url


__all__ = [
    "RabbitMqVerificationConfigurationError",
    "load_rabbitmq_verification_url",
]
