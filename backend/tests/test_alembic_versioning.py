from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from deskpilot.infrastructure.alembic_versioning import (
    ALEMBIC_VERSION_NUM_LENGTH,
    build_alembic_version_table,
)


def test_postgresql_alembic_version_table_fits_every_revision() -> None:
    ddl = str(
        CreateTable(build_alembic_version_table()).compile(
            dialect=postgresql.dialect()
        )
    )
    revisions = tuple(
        revision.revision
        for revision in ScriptDirectory(
            "src/deskpilot/infrastructure/migrations"
        ).walk_revisions()
    )

    assert f"VARCHAR({ALEMBIC_VERSION_NUM_LENGTH})" in ddl
    assert revisions
    assert max(map(len, revisions)) <= ALEMBIC_VERSION_NUM_LENGTH
    assert any(len(revision) > 32 for revision in revisions)
