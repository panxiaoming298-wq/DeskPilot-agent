"""Cross-dialect Alembic version-table compatibility helpers."""

from sqlalchemy import (
    Column,
    Connection,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    inspect,
    text,
)

ALEMBIC_VERSION_NUM_LENGTH = 128
ALEMBIC_VERSION_TABLE = "alembic_version"


def build_alembic_version_table() -> Table:
    """Build the version table with room for DeskPilot's descriptive revisions."""
    table = Table(
        ALEMBIC_VERSION_TABLE,
        MetaData(),
        Column(
            "version_num",
            String(ALEMBIC_VERSION_NUM_LENGTH),
            nullable=False,
        ),
    )
    table.append_constraint(
        PrimaryKeyConstraint(
            "version_num",
            name=f"{ALEMBIC_VERSION_TABLE}_pkc",
        )
    )
    return table


def prepare_alembic_version_table(connection: Connection) -> None:
    """Create or losslessly widen PostgreSQL's default 32-character version key."""
    if connection.dialect.name != "postgresql":
        return

    inspector = inspect(connection)
    if not inspector.has_table(ALEMBIC_VERSION_TABLE):
        build_alembic_version_table().create(connection)
        return

    version_column = next(
        (
            column
            for column in inspector.get_columns(ALEMBIC_VERSION_TABLE)
            if column["name"] == "version_num"
        ),
        None,
    )
    if version_column is None:
        raise RuntimeError("Alembic version table has no version_num column")
    column_type = version_column["type"]
    current_length = getattr(column_type, "length", None)
    if current_length is None or current_length >= ALEMBIC_VERSION_NUM_LENGTH:
        return

    connection.execute(
        text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")
    )


__all__ = [
    "ALEMBIC_VERSION_NUM_LENGTH",
    "build_alembic_version_table",
    "prepare_alembic_version_table",
]
