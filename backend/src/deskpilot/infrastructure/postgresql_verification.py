"""Fail-closed configuration guard for destructive PostgreSQL verification drills."""

import re
from collections.abc import Mapping

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_TEST_DATABASE_TOKEN = re.compile(r"(?:^|[_-])test(?:[_-]|$)", re.IGNORECASE)


class PostgreSQLVerificationConfigurationError(ValueError):
    """A PostgreSQL verification target is absent or insufficiently isolated."""


def load_postgresql_verification_url(environ: Mapping[str, str]) -> str | None:
    """Load an explicitly acknowledged, clearly named disposable test database URL."""
    raw_url = environ.get("DESKPILOT_TEST_POSTGRESQL_URL")
    if raw_url is None:
        return None
    if environ.get("DESKPILOT_TEST_POSTGRESQL_ALLOW") != "1":
        raise PostgreSQLVerificationConfigurationError(
            "Set DESKPILOT_TEST_POSTGRESQL_ALLOW=1 to acknowledge a disposable test DB"
        )
    try:
        url = make_url(raw_url)
    except ArgumentError as exc:
        raise PostgreSQLVerificationConfigurationError(
            "PostgreSQL verification URL is invalid"
        ) from exc
    if url.drivername != "postgresql+asyncpg":
        raise PostgreSQLVerificationConfigurationError(
            "PostgreSQL verification requires a postgresql+asyncpg URL"
        )
    if url.database is None or _TEST_DATABASE_TOKEN.search(url.database) is None:
        raise PostgreSQLVerificationConfigurationError(
            "PostgreSQL verification requires 'test' as a database-name token"
        )
    return raw_url


__all__ = [
    "PostgreSQLVerificationConfigurationError",
    "load_postgresql_verification_url",
]
