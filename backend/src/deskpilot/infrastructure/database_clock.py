"""Database-authoritative UTC time for cross-instance leases and claims."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def database_utc_now(session: AsyncSession) -> datetime:
    """Read time from the shared database rather than an API process clock."""
    if session.get_bind().dialect.name == "sqlite":
        value = await session.scalar(select(func.strftime("%Y-%m-%dT%H:%M:%f+00:00", "now")))
        if not isinstance(value, str):
            raise RuntimeError("SQLite did not return a timestamp")
        return datetime.fromisoformat(value).astimezone(UTC)
    value = await session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("Database did not return a timestamp")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
