import pytest

from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)


def test_postgresql_verification_is_absent_without_a_url() -> None:
    assert load_postgresql_verification_url({}) is None


@pytest.mark.parametrize(
    ("environ", "detail"),
    [
        (
            {"DESKPILOT_TEST_POSTGRESQL_URL": "postgresql+asyncpg://db/deskpilot_test"},
            "ALLOW=1",
        ),
        (
            {
                "DESKPILOT_TEST_POSTGRESQL_URL": "sqlite+aiosqlite:///deskpilot-test.db",
                "DESKPILOT_TEST_POSTGRESQL_ALLOW": "1",
            },
            r"postgresql\+asyncpg",
        ),
        (
            {
                "DESKPILOT_TEST_POSTGRESQL_URL": "postgresql+asyncpg://db/deskpilot",
                "DESKPILOT_TEST_POSTGRESQL_ALLOW": "1",
            },
            "database-name token",
        ),
        (
            {
                "DESKPILOT_TEST_POSTGRESQL_URL": "postgresql+asyncpg://db/contest",
                "DESKPILOT_TEST_POSTGRESQL_ALLOW": "1",
            },
            "database-name token",
        ),
    ],
)
def test_postgresql_verification_rejects_unsafe_targets(
    environ: dict[str, str],
    detail: str,
) -> None:
    with pytest.raises(PostgreSQLVerificationConfigurationError, match=detail):
        load_postgresql_verification_url(environ)


def test_postgresql_verification_accepts_an_acknowledged_test_database() -> None:
    raw_url = "postgresql+asyncpg://user:secret@db/deskpilot_test?ssl=require"

    assert (
        load_postgresql_verification_url(
            {
                "DESKPILOT_TEST_POSTGRESQL_URL": raw_url,
                "DESKPILOT_TEST_POSTGRESQL_ALLOW": "1",
            }
        )
        == raw_url
    )
