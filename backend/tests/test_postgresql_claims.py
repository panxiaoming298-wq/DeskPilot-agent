from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from deskpilot.infrastructure.admission_shard_queries import (
    build_postgresql_admission_candidate_statement,
    build_postgresql_admission_shard_lock_statement,
)
from deskpilot.infrastructure.effect_ready_queries import (
    build_effect_ready_page_statement,
)
from deskpilot.infrastructure.postgresql_claims import (
    build_postgresql_graph_control_claim_statement,
    build_postgresql_node_claim_statement,
    build_postgresql_node_lock_statement,
    build_postgresql_outbox_claim_statement,
)


def _sql(statement: object) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_postgresql_outbox_claim_is_one_skip_locked_returning_update() -> None:
    now = datetime.now(UTC)
    sql = _sql(
        build_postgresql_outbox_claim_statement(
            owner_id="postgres_publisher",
            database_now=now,
            expires_at=now + timedelta(seconds=15),
            batch_size=100,
        )
    ).upper()

    assert "FOR UPDATE OF OUTBOX_MESSAGES SKIP LOCKED" in sql
    assert "UPDATE OUTBOX_MESSAGES" in sql
    assert "RETURNING OUTBOX_MESSAGES.MESSAGE_ID" in sql
    assert "NOT (EXISTS" in sql


def test_postgresql_graph_control_claim_binds_live_graph_fence_in_one_update() -> None:
    now = datetime.now(UTC)
    sql = _sql(
        build_postgresql_graph_control_claim_statement(
            owner_id="postgres_control_owner",
            database_now=now,
            expires_at=now + timedelta(seconds=15),
            batch_size=100,
        )
    ).upper()

    assert "FOR UPDATE OF TOOL_EFFECT_GRAPH_CONTROLS SKIP LOCKED" in sql
    assert "SELECT TOOL_EFFECT_GRAPHS.GRAPH_ID" in sql
    assert "FROM TOOL_EFFECT_GRAPHS" in sql
    assert "LEASE_OWNER_ID = 'POSTGRES_CONTROL_OWNER'" in sql
    assert "FENCING_TOKEN = LOCKED_GRAPH_CONTROLS.TARGET_FENCING_TOKEN" in sql
    assert "UPDATE TOOL_EFFECT_GRAPH_CONTROLS" in sql
    assert "RETURNING TOOL_EFFECT_GRAPH_CONTROLS.CONTROL_ID" in sql
    assert "CLAIM_FENCING_TOKEN + 1" in sql
    assert "LIMIT 100" in sql


def test_postgresql_dag_claim_locks_then_updates_and_returns_all_fences() -> None:
    now = datetime.now(UTC)
    lock_sql = _sql(
        build_postgresql_node_lock_statement(
            graph_id="teg_test",
            node_ids=("node_a", "node_b"),
            database_now=now,
        )
    ).upper()
    claim_sql = _sql(
        build_postgresql_node_claim_statement(
            graph_id="teg_test",
            node_ids=("node_a", "node_b"),
            owner_id="postgres_dispatcher",
            database_now=now,
            expires_at=now + timedelta(seconds=15),
        )
    ).upper()

    assert "FOR UPDATE OF TOOL_EFFECT_NODES SKIP LOCKED" in lock_sql
    assert "UPDATE TOOL_EFFECT_NODES" in claim_sql
    assert "RETURNING TOOL_EFFECT_NODES.NODE_ID" in claim_sql
    assert "CLAIM_FENCING_TOKEN + 1" in claim_sql


def test_postgresql_ready_page_is_ordinal_keyset_without_offset() -> None:
    sql = _sql(
        build_effect_ready_page_statement(
            graph_id="teg_test",
            page_size=100,
            after_ordinal=900,
        )
    ).upper()

    assert "TOOL_EFFECT_DAG_READY_NODES.ORDINAL > 900" in sql
    assert "ORDER BY TOOL_EFFECT_DAG_READY_NODES.ORDINAL" in sql
    assert "LIMIT 101" in sql
    assert "OFFSET" not in sql
    assert "MEMBERSHIP_READY IS TRUE" in sql
    assert "COUNT(" not in sql


def test_postgresql_admission_shard_lock_and_candidates_are_bounded() -> None:
    database_time = datetime.now(UTC)
    shard_sql = _sql(
        build_postgresql_admission_shard_lock_statement(
            database_time=database_time,
        )
    ).upper()
    candidate_sql = _sql(
        build_postgresql_admission_candidate_statement(
            shard_id=7,
            database_time=database_time,
            candidate_limit=2_048,
        )
    ).upper()

    assert "FOR UPDATE OF TOOL_EFFECT_DAG_ADMISSION_SHARDS SKIP LOCKED" in shard_sql
    assert "LIMIT 1" in shard_sql
    assert "STATUS = 'PENDING'" in shard_sql
    assert "SCHEDULING_SHARD = 7" in candidate_sql
    assert "ORDER BY TOOL_EFFECT_DAG_ADMISSIONS.EXPIRES_AT" in candidate_sql
    assert "LIMIT 2048" in candidate_sql
    assert "OFFSET" not in candidate_sql
