from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import TaskEventRecord, TaskRecord

CURRENT_REVISION = "0012_task_runtime_checkpoints"


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
            column["name"]
            for column in inspector.get_columns("task_runtime_checkpoints")
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
            index["name"]
            for index in inspector.get_indexes("task_runtime_checkpoints")
        }
        assert {"tasks"} == {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys(
                "task_runtime_checkpoints"
            )
        }
        assert {
            "ck_task_runtime_checkpoints_next_stage",
            "ck_task_runtime_checkpoints_positive_versions",
        } == {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "task_runtime_checkpoints"
            )
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
            "new_attempt_task_id",
            "new_attempt_created_at",
            "compensation_task_id",
            "compensation_receipt_id",
            "compensation_created_at",
            "updated_at",
        } == {
            column["name"]
            for column in inspector.get_columns("tool_reconciliations")
        }
        assert {
            "uq_tool_reconciliations_call_id",
            "uq_tool_reconciliations_compensation_task_id",
            "uq_tool_reconciliations_new_attempt_task_id",
        } == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "tool_reconciliations"
            )
        }
        assert {"tasks", "tool_calls", "tool_commit_receipts"} == {
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
        assert "confirmed_no_effect" in reconciliation_checks[
            "ck_tool_reconciliations_outcome"
        ]
        assert {
            "uq_tool_idempotency_receipts_call_id",
            "uq_tool_idempotency_receipts_scope_key",
        } == {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "tool_idempotency_receipts"
            )
        }
        assert {"ix_tool_reconciliation_idempotency_reconciliation"} == {
            index["name"]
            for index in inspector.get_indexes(
                "tool_reconciliation_idempotency_records"
            )
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
            for constraint in inspector.get_unique_constraints(
                "tool_reconciliation_evidence"
            )
        }
        assert {
            "ck_tool_reconciliation_evidence_kind",
            "ck_tool_reconciliation_evidence_payload",
        } == {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "tool_reconciliation_evidence"
            )
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
