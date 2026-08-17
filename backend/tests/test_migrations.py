import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import TaskEventRecord, TaskRecord

CURRENT_REVISION = "0034_long_term_memory"


def _sync_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _alembic_config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        (Path(__file__).parents[1] / "src/deskpilot/infrastructure/migrations").as_posix(),
    )
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{path.as_posix()}",
    )
    return config


@pytest.mark.asyncio
async def test_migrate_empty_database_and_repeat_safely(tmp_path: Path) -> None:
    database_path = tmp_path / "empty.db"
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    await database.migrate()
    await database.migrate()
    await database.dispose()

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tool_call_columns = {
            column["name"]: column for column in inspector.get_columns("tool_calls")
        }
        assert {
            "alembic_version",
            "tasks",
            "task_runtime_checkpoints",
            "tool_effect_graphs",
            "tool_effect_nodes",
            "tool_effect_edges",
            "tool_effect_attempts",
            "tool_effects",
            "tool_effect_transitions",
            "tool_effect_branch_decisions",
            "tool_effect_graph_controls",
            "tool_effect_dag_admission_state",
            "tool_effect_dag_admission_shards",
            "tool_effect_dag_admissions",
            "tool_effect_dag_ready_states",
            "tool_effect_dag_ready_nodes",
            "effect_runtime_operations_state",
            "effect_runtime_operations_audit",
            "effect_runtime_alert_states",
            "effect_runtime_alert_notifications",
            "knowledge_artifacts",
            "knowledge_sources",
            "knowledge_chunks",
            "mcp_server_states",
            "mcp_audit_state",
            "mcp_audit_events",
            "evaluation_runs",
            "evaluation_trace_events",
            "task_planning_states",
            "task_contract_versions",
            "task_plan_generations",
            "tool_effect_ready_set_checkpoints",
            "tool_effect_compensation_plans",
            "inbox_deliveries",
            "task_events",
            "tool_calls",
            "tool_idempotency_receipts",
            "tool_commit_receipts",
            "tool_reconciliations",
            "tool_reconciliation_evidence",
            "tool_reconciliation_idempotency_records",
            "approvals",
            "outbox_messages",
            "model_provider_catalog_state",
            "model_provider_catalog_entries",
            "model_provider_runtime_configs",
            "model_provider_config_audit_events",
            "model_provider_idempotency_records",
        }.issubset(inspector.get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        assert revision == CURRENT_REVISION
        assert {"ix_tasks_conversation_id", "ix_tasks_status"} == {
            index["name"] for index in inspector.get_indexes("tasks")
        }
        checkpoint_columns = {
            column["name"] for column in inspector.get_columns("task_runtime_checkpoints")
        }
        assert checkpoint_columns == {
            "task_id",
            "schema_version",
            "next_stage",
            "event_seq",
            "revision",
            "protection_scheme",
            "protected_payload",
            "payload_digest",
            "created_at",
            "updated_at",
        }
        assert {"ix_task_runtime_checkpoints_stage"} == {
            index["name"] for index in inspector.get_indexes("task_runtime_checkpoints")
        }
        graph_columns = {column["name"] for column in inspector.get_columns("tool_effect_graphs")}
        assert {
            "lease_owner_id",
            "lease_acquired_at",
            "lease_heartbeat_at",
            "lease_expires_at",
            "fencing_token",
            "cancel_requested_at",
        }.issubset(graph_columns)
        assert "ix_tool_effect_graphs_lease_expires_at" in {
            index["name"] for index in inspector.get_indexes("tool_effect_graphs")
        }
        node_columns = {column["name"] for column in inspector.get_columns("tool_effect_nodes")}
        outbox_columns = {column["name"] for column in inspector.get_columns("outbox_messages")}
        claim_columns = {
            "claim_owner_id",
            "claim_acquired_at",
            "claim_expires_at",
            "claim_fencing_token",
        }
        assert claim_columns.issubset(node_columns)
        assert "claim_heartbeat_at" in node_columns
        edge_columns = {column["name"] for column in inspector.get_columns("tool_effect_edges")}
        assert {"decision_key", "expected_outcome"}.issubset(edge_columns)
        assert {
            "ck_tool_effect_edges_branch_metadata",
            "ck_tool_effect_edges_kind",
        } == {
            constraint["name"]
            for constraint in inspector.get_check_constraints("tool_effect_edges")
        }
        branch_decision_columns = {
            column["name"] for column in inspector.get_columns("tool_effect_branch_decisions")
        }
        assert branch_decision_columns == {
            "decision_id",
            "graph_id",
            "source_node_id",
            "decision_key",
            "outcome",
            "evidence_digest",
            "source_node_revision",
            "source_event_seq",
            "proof_digest",
            "event_id",
            "event_seq",
            "created_at",
        }
        graph_control_columns = {
            column["name"] for column in inspector.get_columns("tool_effect_graph_controls")
        }
        assert graph_control_columns == {
            "control_id",
            "task_id",
            "graph_id",
            "command",
            "reason",
            "request_digest",
            "requested_by",
            "target_owner_id",
            "target_fencing_token",
            "status",
            "revision",
            "attempt_count",
            "last_error_code",
            "available_at",
            "claim_owner_id",
            "claim_acquired_at",
            "claim_expires_at",
            "claim_fencing_token",
            "applied_graph_fencing_token",
            "created_at",
            "updated_at",
            "applied_at",
        }
        assert {
            "ix_tool_effect_graph_controls_claim_expiry",
            "ix_tool_effect_graph_controls_route",
        } == {index["name"] for index in inspector.get_indexes("tool_effect_graph_controls")}
        assert {"tasks", "tool_effect_graphs"} == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("tool_effect_graph_controls")
        }
        assert {
            "scope_id",
            "revision",
            "next_grant_sequence",
            "configuration_digest",
            "global_limit",
            "per_graph_limit",
            "default_tool_limit",
            "tool_limits_digest",
            "updated_at",
        } == {column["name"] for column in inspector.get_columns("tool_effect_dag_admission_state")}
        admission_columns = {
            column["name"] for column in inspector.get_columns("tool_effect_dag_admissions")
        }
        assert admission_columns == {
            "admission_id",
            "batch_id",
            "graph_id",
            "node_id",
            "tool_name",
            "owner_id",
            "status",
            "scheduling_shard",
            "lease_ttl_seconds",
            "revision",
            "fencing_token",
            "grant_sequence",
            "created_at",
            "updated_at",
            "granted_at",
            "heartbeat_at",
            "expires_at",
            "released_at",
        }
        assert {
            "ix_tool_effect_dag_admissions_active",
            "ix_tool_effect_dag_admissions_owner",
            "ix_tool_effect_dag_admissions_route",
            "ix_tool_effect_dag_admissions_shard_route",
        } == {index["name"] for index in inspector.get_indexes("tool_effect_dag_admissions")}
        assert {
            "shard_id",
            "revision",
            "last_grant_sequence",
            "updated_at",
        } == {
            column["name"] for column in inspector.get_columns("tool_effect_dag_admission_shards")
        }
        assert "ix_tool_effect_dag_admission_shards_fairness" in {
            index["name"] for index in inspector.get_indexes("tool_effect_dag_admission_shards")
        }
        assert {"tool_effect_graphs"} == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("tool_effect_dag_admissions")
        }
        assert {
            "graph_id",
            "revision",
            "event_seq",
            "content_digest",
            "membership_version",
            "projected_node_count",
            "ready_node_count",
            "rebuild_count",
            "last_rebuild_duration_ms",
            "rebuilt_at",
            "updated_at",
        } == {column["name"] for column in inspector.get_columns("tool_effect_dag_ready_states")}
        assert {
            "node_id",
            "graph_id",
            "ordinal",
            "remaining_predecessors",
            "unresolved_branches",
            "branch_rejected",
            "membership_ready",
            "revision",
            "proof_digest",
            "updated_at",
        } == {column["name"] for column in inspector.get_columns("tool_effect_dag_ready_nodes")}
        assert {"ix_effect_dag_ready_states_event"} == {
            index["name"] for index in inspector.get_indexes("tool_effect_dag_ready_states")
        }
        assert {
            "ix_effect_dag_ready_nodes_membership",
            "ix_effect_dag_ready_nodes_query",
        } == {index["name"] for index in inspector.get_indexes("tool_effect_dag_ready_nodes")}
        assert {"tool_effect_graphs"} == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("tool_effect_dag_ready_states")
        }
        assert {"tool_effect_graphs", "tool_effect_nodes"} == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("tool_effect_dag_ready_nodes")
        }
        assert {
            "scope_id",
            "revision",
            "next_sequence",
            "last_event_digest",
            "next_alert_sequence",
            "last_alert_event_digest",
            "last_retention_at",
            "updated_at",
        } == {column["name"] for column in inspector.get_columns("effect_runtime_operations_state")}
        assert {
            "event_id",
            "sequence",
            "action",
            "actor_id",
            "idempotency_key_digest",
            "request_digest",
            "result_digest",
            "previous_event_digest",
            "event_digest",
            "details",
            "occurred_at",
        } == {column["name"] for column in inspector.get_columns("effect_runtime_operations_audit")}
        assert {"ix_effect_runtime_operations_audit_occurred"} == {
            index["name"] for index in inspector.get_indexes("effect_runtime_operations_audit")
        }
        assert claim_columns.issubset(outbox_columns)
        assert {
            "delivery_id",
            "delivery_attempted_at",
            "dead_lettered_at",
            "dead_letter_reason",
        }.issubset(outbox_columns)
        assert "ix_effect_nodes_claim_expires_at" in {
            index["name"] for index in inspector.get_indexes("tool_effect_nodes")
        }
        assert "ix_effect_nodes_graph_claim_expires" in {
            index["name"] for index in inspector.get_indexes("tool_effect_nodes")
        }
        assert {
            "ix_outbox_claim_expires_at",
            "ix_outbox_claimable",
        }.issubset({index["name"] for index in inspector.get_indexes("outbox_messages")})
        assert {"tasks"} == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("task_runtime_checkpoints")
        }
        assert {
            "ck_task_runtime_checkpoints_next_stage",
            "ck_task_runtime_checkpoints_positive_versions",
        } == {
            constraint["name"]
            for constraint in inspector.get_check_constraints("task_runtime_checkpoints")
        }
        assert {"ix_model_provider_catalog_entries_enabled"} == {
            index["name"] for index in inspector.get_indexes("model_provider_catalog_entries")
        }
        assert {
            "ix_model_provider_config_audit_occurred_at",
            "ix_model_provider_config_audit_provider_sequence",
        } == {
            index["name"]
            for index in inspector.get_indexes("model_provider_config_audit_events")
            if index["name"] is not None
        }
        assert {"ix_model_provider_idempotency_expires_at"} == {
            index["name"]
            for index in inspector.get_indexes("model_provider_idempotency_records")
            if index["name"] is not None
        }
        assert {"ix_tool_calls_recovery", "ix_tool_calls_task_status"} == {
            index["name"]
            for index in inspector.get_indexes("tool_calls")
            if index["name"] is not None
        }
        assert {
            "call_id",
            "task_id",
            "step_id",
            "attempt",
            "tool_name",
            "tool_version",
            "contract_digest",
            "arguments_digest",
            "policy_decision_id",
            "policy_revision",
            "policy_effect",
            "resource_scope_digest",
            "policy_event_id",
            "authorization_id",
            "idempotency",
            "idempotency_key_digest",
            "status",
            "runner_id",
            "resolution_source",
            "error_code",
            "terminal_event_id",
            "requested_at",
            "started_at",
            "finished_at",
            "updated_at",
        } == set(tool_call_columns)
        assert tool_call_columns["policy_decision_id"]["type"].length == 80
        assert tool_call_columns["authorization_id"]["type"].length == 80
        tool_call_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("tool_calls")
        }
        assert {"ck_tool_calls_policy_effect", "ck_tool_calls_status"} == set(tool_call_checks)
        assert "require_approval" in tool_call_checks["ck_tool_calls_policy_effect"]
        assert "'ask'" not in tool_call_checks["ck_tool_calls_policy_effect"]
        approval_columns = {column["name"]: column for column in inspector.get_columns("approvals")}
        assert {
            "ix_approvals_status_expires_at",
            "ix_approvals_task_status",
        } == {
            index["name"]
            for index in inspector.get_indexes("approvals")
            if index["name"] is not None
        }
        assert {
            "approval_id",
            "decision_id",
            "task_id",
            "call_id",
            "tool_name",
            "tool_version",
            "risk_level",
            "policy_decision",
            "policy_rule_id",
            "policy_revision",
            "reason_code",
            "contract_digest",
            "arguments_digest",
            "binding_digest",
            "title",
            "purpose",
            "capabilities",
            "resource_scope",
            "consequences",
            "reversible",
            "data_egress",
            "expected_resource_versions",
            "preview_hash",
            "status",
            "decision",
            "scope",
            "resolved_by",
            "resolution_reason",
            "requested_at",
            "expires_at",
            "resolved_at",
            "consumed_at",
            "updated_at",
        } == set(approval_columns)
        assert approval_columns["decision_id"]["type"].length == 80
        approval_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("approvals")
        }
        assert {
            "ck_approvals_decision",
            "ck_approvals_policy_decision",
            "ck_approvals_scope",
            "ck_approvals_status",
        } == set(approval_checks)
        assert "require_approval" in approval_checks["ck_approvals_policy_decision"]
        assert "'ask'" not in approval_checks["ck_approvals_policy_decision"]
        assert {"uq_approvals_call_id"} == {
            constraint["name"] for constraint in inspector.get_unique_constraints("approvals")
        }
        assert {"tasks", "tool_calls"} == {
            foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys("approvals")
        }
        assert {
            "ix_tool_reconciliations_status_unknown_at",
            "ix_tool_reconciliations_task_status",
        } == {
            index["name"]
            for index in inspector.get_indexes("tool_reconciliations")
            if index["name"] is not None
        }
        assert {
            "reconciliation_id",
            "task_id",
            "call_id",
            "status",
            "outcome",
            "evidence_summary",
            "resolved_by",
            "unknown_at",
            "resolved_at",
            "graph_recovery_status",
            "graph_recovery_action",
            "graph_recovery_event_id",
            "graph_recovered_at",
            "new_attempt_task_id",
            "new_attempt_created_at",
            "compensation_task_id",
            "compensation_receipt_id",
            "compensation_created_at",
            "updated_at",
        } == {column["name"] for column in inspector.get_columns("tool_reconciliations")}
        assert {
            "uq_tool_reconciliations_call_id",
            "uq_tool_reconciliations_compensation_task_id",
            "uq_tool_reconciliations_new_attempt_task_id",
        } == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("tool_reconciliations")
        }
        assert {
            "tasks",
            "task_events",
            "tool_calls",
            "tool_commit_receipts",
        } == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("tool_reconciliations")
        }
        reconciliation_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("tool_reconciliations")
        }
        assert {
            "ck_tool_reconciliations_outcome",
            "ck_tool_reconciliations_resolution",
            "ck_tool_reconciliations_status",
        } == set(reconciliation_checks)
        assert "confirmed_no_effect" in reconciliation_checks["ck_tool_reconciliations_outcome"]
        assert {
            "uq_tool_idempotency_receipts_call_id",
            "uq_tool_idempotency_receipts_scope_key",
        } == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("tool_idempotency_receipts")
        }
        assert {"ix_tool_reconciliation_idempotency_reconciliation"} == {
            index["name"]
            for index in inspector.get_indexes("tool_reconciliation_idempotency_records")
            if index["name"] is not None
        }
        assert {"uq_tool_commit_receipts_call_id"} == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("tool_commit_receipts")
        }
        assert {"ix_tool_reconciliation_evidence_observed"} == {
            index["name"]
            for index in inspector.get_indexes("tool_reconciliation_evidence")
            if index["name"] is not None
        }
        assert {"uq_tool_reconciliation_evidence_digest"} == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("tool_reconciliation_evidence")
        }
        assert {
            "ck_tool_reconciliation_evidence_kind",
            "ck_tool_reconciliation_evidence_payload",
        } == {
            constraint["name"]
            for constraint in inspector.get_check_constraints("tool_reconciliation_evidence")
        }
    engine.dispose()


@pytest.mark.asyncio
async def test_migrate_adopts_pre_alembic_schema_without_data_loss(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    engine = create_engine(_sync_url(database_path))
    TaskRecord.__table__.create(engine)
    TaskEventRecord.__table__.create(engine)
    created_at = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            TaskRecord.__table__.insert().values(
                task_id="tsk_legacy",
                conversation_id=None,
                goal="保留旧版本任务",
                status="created",
                mode="fake",
                privacy_mode="local_only",
                constraints=[],
                last_event_seq=0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
    engine.dispose()

    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await database.migrate()
    await database.dispose()

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        goal = connection.execute(
            select(TaskRecord.goal).where(TaskRecord.task_id == "tsk_legacy")
        ).scalar_one()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        assert goal == "保留旧版本任务"
        assert revision == CURRENT_REVISION
    engine.dispose()


def test_reconciliation_migration_backfills_unknown_calls_and_key_receipts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciliation-backfill.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0007_policy_approvals")
    timestamp = datetime.now(UTC).isoformat()

    engine = create_engine(_sync_url(database_path))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO tasks (
                task_id, conversation_id, goal, status, mode, privacy_mode,
                constraints, last_event_seq, created_at, updated_at
            ) VALUES (?, NULL, ?, 'failed', 'fake_model', 'local_preferred',
                      '[]', 0, ?, ?)
            """,
            ("tsk_backfill", "backfill unknown", timestamp, timestamp),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO tool_calls (
                call_id, task_id, step_id, attempt, tool_name, tool_version,
                contract_digest, arguments_digest, idempotency,
                idempotency_key_digest, status, runner_id, resolution_source,
                error_code, terminal_event_id, requested_at, started_at,
                finished_at, updated_at, policy_decision_id, policy_revision,
                policy_effect, resource_scope_digest, policy_event_id,
                authorization_id
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, 'key_required', ?, 'unknown',
                      ?, 'startup_recovery', ?, NULL, ?, ?, ?, ?, NULL, NULL,
                      NULL, NULL, NULL, NULL)
            """,
            (
                "call-backfill",
                "tsk_backfill",
                "step-backfill",
                "system.write",
                "1.0.0",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "runner-old",
                "TOOL_RESULT_UNCERTAIN_AFTER_RESTART",
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        reconciliation = connection.exec_driver_sql(
            """
            SELECT task_id, call_id, status, outcome
            FROM tool_reconciliations
            """
        ).one()
        receipt = connection.exec_driver_sql(
            """
            SELECT call_id, key_digest, arguments_digest
            FROM tool_idempotency_receipts
            """
        ).one()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()

    assert reconciliation == ("tsk_backfill", "call-backfill", "pending", None)
    assert receipt == ("call-backfill", "c" * 64, "b" * 64)
    assert revision == CURRENT_REVISION


def test_stage_42_migration_round_trips_and_matches_model_metadata(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-42-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0015_database_claims_dag")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "inbox_deliveries" not in inspector.get_table_names()
        assert "tool_effect_compensation_plans" not in inspector.get_table_names()
        assert "cancel_requested_at" not in {
            column["name"] for column in inspector.get_columns("tool_effect_graphs")
        }
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION


def test_stage_44_branch_decision_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-44-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0017_parallel_compensation")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "tool_effect_branch_decisions" not in inspector.get_table_names()
        assert {"decision_key", "expected_outcome"}.isdisjoint(
            column["name"] for column in inspector.get_columns("tool_effect_edges")
        )
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_47_graph_control_mailbox_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-47-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0018_branch_decision_proofs")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "tool_effect_graph_controls" not in inspect(connection).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION


def test_stage_48_cluster_dag_admission_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-48-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0019_graph_control_mailbox")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        assert "tool_effect_dag_admission_state" not in tables
        assert "tool_effect_dag_admissions" not in tables
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0019_graph_control_mailbox"

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        scheduler = connection.exec_driver_sql(
            "SELECT revision, next_grant_sequence "
            "FROM tool_effect_dag_admission_state WHERE scope_id = 'global'"
        ).one()
    engine.dispose()
    assert revision == CURRENT_REVISION
    assert scheduler == (1, 1)


def test_stage_49_incremental_ready_projection_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-49-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)


def test_stage_50_effect_runtime_operations_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-50-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0021_incremental_ready")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = inspector.get_table_names()
        assert "effect_runtime_operations_state" not in tables
        assert "effect_runtime_operations_audit" not in tables
        assert "rebuild_count" not in {
            column["name"] for column in inspector.get_columns("tool_effect_dag_ready_states")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0021_incremental_ready"

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        state = connection.exec_driver_sql(
            "SELECT revision, next_sequence, last_event_digest "
            "FROM effect_runtime_operations_state "
            "WHERE scope_id = 'effect_runtime'"
        ).one()
    engine.dispose()
    assert revision == CURRENT_REVISION
    assert state == (1, 1, None)
    command.downgrade(config, "0020_cluster_dag_admission")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        assert "tool_effect_dag_ready_states" not in tables
        assert "tool_effect_dag_ready_nodes" not in tables
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0020_cluster_dag_admission"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_58_ready_membership_count_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-58-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0022_effect_runtime_ops")

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        state_columns = {
            column["name"] for column in inspector.get_columns("tool_effect_dag_ready_states")
        }
        node_columns = {
            column["name"] for column in inspector.get_columns("tool_effect_dag_ready_nodes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0022_effect_runtime_ops"
    assert {
        "membership_version",
        "projected_node_count",
        "ready_node_count",
    }.isdisjoint(state_columns)
    assert "membership_ready" not in node_columns

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        indexes = {index["name"] for index in inspector.get_indexes("tool_effect_dag_ready_nodes")}
    engine.dispose()
    assert revision == CURRENT_REVISION
    assert "ix_effect_dag_ready_nodes_membership" in indexes


def test_stage_59_admission_shard_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-59-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0023_ready_membership")
    graph_id = "teg_stage_59_backfill"
    timestamp = datetime.now(UTC).isoformat()
    engine = create_engine(_sync_url(database_path))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO tasks (
                task_id, conversation_id, goal, status, mode, privacy_mode,
                constraints, last_event_seq, created_at, updated_at
            ) VALUES (?, NULL, ?, 'created', 'fake_model', 'local_preferred',
                      '[]', 0, ?, ?)
            """,
            ("tsk_stage_59", "admission shard migration", timestamp, timestamp),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO tool_effect_graphs (
                graph_id, task_id, schema_version, status, execution_mode,
                revision, last_event_seq, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', 'forward', 1, 1, ?, ?)
            """,
            (graph_id, "tsk_stage_59", "test.v1", timestamp, timestamp),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO tool_effect_dag_admissions (
                admission_id, batch_id, graph_id, node_id, tool_name, owner_id,
                status, lease_ttl_seconds, revision, fencing_token,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 30, 1, 0, ?, ?, ?)
            """,
            (
                "eda_stage_59",
                "edb_stage_59",
                graph_id,
                "ten_stage_59",
                "test.tool",
                "stage_59_owner",
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        shards = connection.exec_driver_sql(
            "SELECT shard_id, revision, last_grant_sequence "
            "FROM tool_effect_dag_admission_shards ORDER BY shard_id"
        ).all()
        backfilled_shard = connection.exec_driver_sql(
            "SELECT scheduling_shard FROM tool_effect_dag_admissions "
            "WHERE admission_id = 'eda_stage_59'"
        ).scalar_one()
    engine.dispose()
    assert shards == [(shard_id, 1, None) for shard_id in range(16)]
    expected_digest = hashlib.sha256(graph_id.encode("utf-8")).digest()
    assert backfilled_shard == int.from_bytes(expected_digest[:8], "big") % 16

    command.downgrade(config, "0023_ready_membership")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        columns = {column["name"] for column in inspector.get_columns("tool_effect_dag_admissions")}
        tables = inspector.get_table_names()
    engine.dispose()
    assert revision == "0023_ready_membership"
    assert "tool_effect_dag_admission_shards" not in tables
    assert "scheduling_shard" not in columns

    command.upgrade(config, "head")
    command.check(config)


def test_stage_60_graph_control_claim_index_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-60-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        index_definitions = {
            index["name"]: index for index in inspector.get_indexes("tool_effect_graph_controls")
        }
    engine.dispose()
    assert index_definitions["ix_tool_effect_graph_controls_route"]["column_names"] == [
        "status",
        "target_owner_id",
        "available_at",
        "created_at",
        "control_id",
    ]

    command.downgrade(config, "0024_admission_shards")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        index_definitions = {
            index["name"]: index for index in inspector.get_indexes("tool_effect_graph_controls")
        }
    engine.dispose()
    assert revision == "0024_admission_shards"
    assert index_definitions["ix_tool_effect_graph_controls_route"]["column_names"] == [
        "status",
        "target_owner_id",
        "available_at",
    ]

    command.upgrade(config, "head")
    command.check(config)


def test_stage_61_alert_notification_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-61-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        state_columns = {
            column["name"] for column in inspector.get_columns("effect_runtime_operations_state")
        }
        tables = set(inspector.get_table_names())
    engine.dispose()
    assert {"next_alert_sequence", "last_alert_event_digest"}.issubset(state_columns)
    assert {
        "effect_runtime_alert_states",
        "effect_runtime_alert_notifications",
    }.issubset(tables)

    command.downgrade(config, "0025_graph_control_claims")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        state_columns = {
            column["name"] for column in inspector.get_columns("effect_runtime_operations_state")
        }
        tables = set(inspector.get_table_names())
    engine.dispose()
    assert revision == "0025_graph_control_claims"
    assert "next_alert_sequence" not in state_columns
    assert "last_alert_event_digest" not in state_columns
    assert "effect_runtime_alert_states" not in tables
    assert "effect_runtime_alert_notifications" not in tables

    command.upgrade(config, "head")
    command.check(config)


def test_stage_63_local_knowledge_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-63-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert {
            "knowledge_artifacts",
            "knowledge_sources",
            "knowledge_chunks",
        }.issubset(inspect(connection).get_table_names())
    engine.dispose()

    command.downgrade(config, "0026_alert_notifications")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0026_alert_notifications"
    assert not {"knowledge_artifacts", "knowledge_sources", "knowledge_chunks"} & tables

    command.upgrade(config, "head")
    command.check(config)


def test_stage_64_controlled_mcp_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-64-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert {"mcp_server_states", "mcp_audit_state", "mcp_audit_events"}.issubset(
            inspect(connection).get_table_names()
        )
        audit_state = connection.exec_driver_sql(
            "SELECT state_id, next_sequence FROM mcp_audit_state"
        ).one()
    engine.dispose()
    assert audit_state == ("mcp", 1)

    command.downgrade(config, "0027_local_knowledge")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    engine.dispose()
    assert not {"mcp_server_states", "mcp_audit_state", "mcp_audit_events"} & tables

    command.upgrade(config, "head")
    command.check(config)


def test_stage_65_evaluation_trace_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-65-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert {"evaluation_runs", "evaluation_trace_events"}.issubset(
            inspect(connection).get_table_names()
        )
    engine.dispose()

    command.downgrade(config, "0028_controlled_mcp")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    engine.dispose()
    assert not {"evaluation_runs", "evaluation_trace_events"} & tables

    command.upgrade(config, "head")
    command.check(config)


def test_stage_69_task_contract_plan_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-69-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION
    assert {
        "task_planning_states",
        "task_contract_versions",
        "task_plan_generations",
    }.issubset(tables)

    command.downgrade(config, "0029_evaluation_traces")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0029_evaluation_traces"
    assert (
        not {
            "task_planning_states",
            "task_contract_versions",
            "task_plan_generations",
        }
        & tables
    )

    command.upgrade(config, "head")
    command.check(config)


def test_stage_70_agent_research_runtime_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-70-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    engine.dispose()
    expected = {
        "task_execution_runs",
        "task_execution_nodes",
        "task_execution_edges",
        "agent_handoffs",
        "agent_invocations",
        "agent_model_turns",
        "agent_results",
        "research_sessions",
        "research_search_calls",
        "research_page_snapshots",
        "research_claims",
        "research_citations",
    }
    assert expected.issubset(tables)

    command.downgrade(config, "0030_task_contract_plans")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == "0030_task_contract_plans"
    assert not expected & tables

    command.upgrade(config, "head")
    command.check(config)


def test_stage_71_verified_artifact_delivery_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-71-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    expected = {
        "verification_runs",
        "verification_evidence_snapshots",
        "claim_verdicts",
        "task_artifact_workspaces",
        "artifacts",
        "artifact_revisions",
        "artifact_patch_receipts",
        "browser_render_runs",
        "delivery_manifests",
    }
    with engine.connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0031_agent_research_runtime")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert not expected & set(inspect(connection).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_72_context_working_memory_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-72-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    expected = {
        "conversations",
        "conversation_messages",
        "working_memory_items",
        "context_requests",
        "context_manifests",
    }
    with engine.connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0032_verified_artifact_delivery")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert not expected & set(inspect(connection).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_73_long_term_memory_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-73-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    expected = {
        "long_term_memory_proposals",
        "long_term_memory_items",
        "long_term_memory_conflicts",
        "long_term_memory_tombstones",
        "long_term_memory_usage",
    }
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0033_context_working_memory")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert not expected & set(inspect(connection).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
