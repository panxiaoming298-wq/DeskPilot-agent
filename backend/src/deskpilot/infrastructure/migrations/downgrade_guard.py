"""Shared fail-closed checks for lossy Alembic downgrades."""

import sqlalchemy as sa
from sqlalchemy.engine import Connection


class UnsafeDowngradeError(RuntimeError):
    """Raised before a downgrade that cannot preserve newer-version data."""


def refuse_downgrade_if_rows(
    connection: Connection,
    *,
    revision: str,
    checks: tuple[tuple[str, str], ...],
) -> None:
    """Refuse a downgrade when any read-only probe finds unrepresentable data."""

    for description, statement in checks:
        if connection.execute(sa.text(statement)).first() is not None:
            raise UnsafeDowngradeError(
                "DESKPILOT_DOWNGRADE_UNSAFE: "
                f"{revision} downgrade refused because {description} cannot be "
                "represented by the previous revision. Restore the reviewed stage "
                "backup instead."
            )
