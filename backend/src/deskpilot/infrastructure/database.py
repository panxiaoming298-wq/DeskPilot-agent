"""Async SQLAlchemy setup and schema migration entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    """Owns the database engine and session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite+aiosqlite:///./"):
            relative_path = url.removeprefix("sqlite+aiosqlite:///./")
            Path(relative_path).parent.mkdir(parents=True, exist_ok=True)

        self.engine: AsyncEngine = create_async_engine(url, future=True)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def migrate(self) -> None:
        """Upgrade the database to the packaged Alembic head revision."""
        migrations_path = Path(__file__).with_name("migrations")
        config = Config()
        config.set_main_option("script_location", migrations_path.as_posix())
        config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        await asyncio.to_thread(command.upgrade, config, "head")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
