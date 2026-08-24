import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import TaskEventRecord, TaskRecord

CURRENT_REVISION = "0054_task_loop_cycle_events"


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


def _assert_populated_downgrade_refused(
    database_path: Path,
    config: Config,
    *,
    revision: str,
    target_revision: str,
    insert_statement: str,
    parameters: dict[str, object],
    snapshot_statement: str,
    cleanup_statement: str,
) -> None:
    engine = create_engine(_sync_url(database_path))
    with engine.begin() as connection:
        connection.execute(text(insert_statement), parameters)
    with engine.connect() as connection:
        before = [
            tuple(row)
            for row in connection.execute(text(snapshot_statement), parameters)
        ]
    assert before
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match=r"DESKPILOT_DOWNGRADE_UNSAFE.*Restore the reviewed stage backup",
    ):
        command.downgrade(config, target_revision)

    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        after_revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        after = [
            tuple(row)
            for row in connection.execute(text(snapshot_statement), parameters)
        ]
    assert after_revision == revision
    assert after == before
    with engine.begin() as connection:
        connection.execute(text(cleanup_statement), parameters)
    engine.dispose()


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
            "workbench_runtime_items",
            "agent_delegations",
            "agent_task_graphs",
            "agent_task_graph_nodes",
            "agent_replans",
            "workspace_agent_results",
            "turn_planning_offers",
            "turn_planner_runs",
            "turn_planner_adjudications",
            "turn_plan_bindings",
            "task_loops",
            "task_loop_events",
            "model_planner_drafts",
            "model_planner_step_bindings",
            "task_loop_executions",
            "task_loop_execution_events",
            "model_planner_node_bindings",
            "task_loop_node_attempts",
            "task_loop_verified_results",
            "task_loop_capability_approvals",
            "task_loop_cycle_events",
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


def test_stage_112_model_planner_task_loop_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-112-model-planner-task-loop.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        loop_columns = {item["name"] for item in inspector.get_columns("task_loops")}
        loop_constraints = {
            item["name"] for item in inspector.get_check_constraints("task_loops")
        }
        loop_indexes = {item["name"] for item in inspector.get_indexes("task_loops")}
        event_foreign_keys = {
            item["name"] for item in inspector.get_foreign_keys("task_loop_events")
        }
        draft_columns = {
            item["name"] for item in inspector.get_columns("model_planner_drafts")
        }
        step_foreign_keys = {
            item["name"]
            for item in inspector.get_foreign_keys("model_planner_step_bindings")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()

    assert {
        "task_loops",
        "task_loop_events",
        "model_planner_drafts",
        "model_planner_step_bindings",
    }.issubset(tables)
    assert {
        "source_run_id",
        "source_run_digest",
        "source_adjudication_id",
        "source_adjudication_digest",
        "source_turn_plan_binding_id",
        "source_turn_plan_binding_digest",
        "active_draft_id",
        "failure_manifest",
        "progress_digest",
        "loop_digest",
    }.issubset(loop_columns)
    assert {"ck_task_loop_state", "ck_task_loop_lifecycle"}.issubset(
        loop_constraints
    )
    assert {"ix_task_loops_recovery", "ix_task_loops_message"}.issubset(
        loop_indexes
    )
    assert {
        "fk_task_loop_event_scope",
        "fk_task_loop_event_previous",
    }.issubset(event_foreign_keys)
    assert {
        "ordered_steps_manifest",
        "step_set_digest",
        "task_contract_manifest",
        "draft_plan_manifest",
        "expected_plan_manifest",
        "expected_plan_manifest_digest",
        "draft_record_digest",
    }.issubset(draft_columns)
    assert {
        "fk_model_planner_step_draft_scope",
        "fk_model_planner_step_offer_scope",
    }.issubset(step_foreign_keys)
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0052_model_planner_task_loop")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0052_model_planner_task_loop",
        target_revision="0051_turn_planning_offers",
        insert_statement="""
            INSERT INTO task_loops (
                loop_id, task_id, user_message_id, user_message_digest,
                source_run_id, source_run_digest,
                source_adjudication_id, source_adjudication_digest,
                source_turn_plan_binding_id, source_turn_plan_binding_digest,
                phase, status, revision, event_count,
                latest_event_id, latest_event_digest, progress_digest,
                active_draft_id, active_draft_record_digest,
                failure_manifest, failure_digest, manifest, loop_digest,
                created_at, updated_at
            ) VALUES (
                :row_id, :task_id, :message_id, :digest,
                :run_id, :digest,
                :adjudication_id, :digest,
                :binding_id, :digest,
                'observe', 'observed', 1, 1,
                :event_id, :digest, :digest,
                NULL, NULL,
                NULL, NULL, :manifest, :loop_digest,
                :created_at, :created_at
            )
        """,
        parameters={
            "row_id": "tlp_" + "1" * 64,
            "task_id": "tsk_" + "2" * 32,
            "message_id": "msg_" + "3" * 32,
            "run_id": "tpr_" + "4" * 64,
            "adjudication_id": "tpa_" + "5" * 64,
            "binding_id": "tpb_" + "6" * 64,
            "event_id": "tle_" + "7" * 64,
            "digest": "8" * 64,
            "loop_digest": "9" * 64,
            "manifest": "{}",
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT loop_id, status, revision, latest_event_id, loop_digest
            FROM task_loops WHERE loop_id = :row_id
        """,
        cleanup_statement="DELETE FROM task_loops WHERE loop_id = :row_id",
    )

    command.downgrade(config, "0051_turn_planning_offers")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert not {
        "task_loops",
        "task_loop_events",
        "model_planner_drafts",
        "model_planner_step_bindings",
    } & tables
    assert revision == "0051_turn_planning_offers"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_112_task_loop_execution_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-112-task-loop-execution.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        execution_columns = {
            item["name"] for item in inspector.get_columns("task_loop_executions")
        }
        binding_columns = {
            item["name"]
            for item in inspector.get_columns("model_planner_node_bindings")
        }
        attempt_columns = {
            item["name"] for item in inspector.get_columns("task_loop_node_attempts")
        }
        attempt_constraints = {
            item["name"]
            for item in inspector.get_check_constraints("task_loop_node_attempts")
        }
        result_foreign_keys = {
            item["referred_table"]
            for item in inspector.get_foreign_keys("task_loop_verified_results")
        }
        result_columns = {
            item["name"]
            for item in inspector.get_columns("task_loop_verified_results")
        }
        result_constraints = {
            item["name"]
            for item in inspector.get_check_constraints("task_loop_verified_results")
        }
        cycle_columns = {
            item["name"] for item in inspector.get_columns("task_loop_cycle_events")
        }
        cycle_constraints = {
            item["name"]
            for item in inspector.get_check_constraints("task_loop_cycle_events")
        }
        approval_columns = {
            item["name"]
            for item in inspector.get_columns("task_loop_capability_approvals")
        }
        approval_constraints = {
            item["name"]
            for item in inspector.get_check_constraints(
                "task_loop_capability_approvals"
            )
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()

    assert {
        "task_loop_executions",
        "task_loop_execution_events",
        "model_planner_node_bindings",
        "task_loop_node_attempts",
        "task_loop_verified_results",
        "task_loop_capability_approvals",
        "task_loop_cycle_events",
    }.issubset(tables)
    assert {
        "loop_id",
        "draft_id",
        "plan_id",
        "plan_generation",
        "run_id",
        "latest_event_digest",
        "binding_set_digest",
        "execution_digest",
    }.issubset(execution_columns)
    assert {
        "step_binding_id",
        "offer_id",
        "policy_snapshot_digest",
        "source_contract_digest",
        "source_node_id",
        "composite_contract_digest",
        "composite_node_id",
        "bound_input_manifest",
        "bound_input_digest",
        "effective_authority_manifest",
        "effective_authority_digest",
        "runtime_eligibility_manifest",
        "runtime_eligibility_digest",
    }.issubset(binding_columns)
    assert {
        "claim_fencing_token",
        "input_manifest",
        "context_manifest",
        "receipt_manifest",
        "candidate_manifest",
        "candidate_digest",
        "candidate_recorded_at",
        "verification_manifest",
        "verification_digest",
        "verified_at",
        "attempt_digest",
    }.issubset(attempt_columns)
    assert "ck_task_loop_node_attempt_evidence" in attempt_constraints
    assert {
        "node_binding_id",
        "node_binding_digest",
        "producer_kind",
        "capability_manifest",
        "capability_digest",
        "agent_binding_manifest",
        "agent_binding_digest",
        "executor_manifest_digest",
        "agent_result_proof_digest",
        "input_binding_digest",
        "context_digest",
        "candidate_digest",
        "output_manifest",
        "output_schema_digest",
        "output_digest",
        "verification_manifest",
        "verification_digest",
        "result_ref_manifest",
        "result_ref_digest",
    }.issubset(result_columns)
    assert {
        "ck_task_loop_verified_result_producer",
        "ck_task_loop_verified_result_producer_evidence",
    }.issubset(result_constraints)
    assert {
        "task_loop_node_attempts",
        "task_loop_executions",
        "model_planner_node_bindings",
        "task_execution_runs",
        "task_execution_nodes",
    } == result_foreign_keys
    assert {
        "attempt_id",
        "node_binding_id",
        "input_binding_digest",
        "executor_manifest_digest",
        "preview_schema_digest",
        "preview_manifest",
        "confirmation_digest",
        "requested_execution_revision",
        "status",
        "approval_digest",
    }.issubset(approval_columns)
    assert {
        "ck_task_loop_capability_approval_versions",
        "ck_task_loop_capability_approval_status",
        "ck_task_loop_capability_approval_lifecycle",
    }.issubset(approval_constraints)
    assert {
        "previous_event_digest",
        "kind",
        "plan_generation",
        "source_progress_digest",
        "reason_code",
        "evidence_manifest",
        "evidence_digest",
        "event_digest",
    }.issubset(cycle_columns)
    assert {
        "ck_task_loop_cycle_event_versions",
        "ck_task_loop_cycle_event_kind",
        "ck_task_loop_cycle_event_chain_root",
    }.issubset(cycle_constraints)
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0053_task_loop_execution")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0053_task_loop_execution",
        target_revision="0052_model_planner_task_loop",
        insert_statement="""
            INSERT INTO task_loop_executions (
                execution_id, loop_id, draft_id, task_id,
                plan_id, plan_generation, plan_manifest_digest, run_id,
                status, revision, event_count,
                latest_event_id, latest_event_digest,
                node_binding_count, binding_set_digest,
                manifest, execution_digest, created_at, updated_at
            ) VALUES (
                :row_id, :loop_id, :draft_id, :task_id,
                :plan_id, 1, :digest, :run_id,
                'active', 1, 1,
                :event_id, :digest,
                1, :digest,
                :manifest, :execution_digest, :created_at, :created_at
            )
        """,
        parameters={
            "row_id": "tlx_" + "1" * 64,
            "loop_id": "tlp_" + "2" * 64,
            "draft_id": "mpd_" + "3" * 64,
            "task_id": "tsk_" + "4" * 32,
            "plan_id": "epl_" + "5" * 64,
            "run_id": "run_" + "6" * 64,
            "event_id": "txe_" + "7" * 64,
            "digest": "8" * 64,
            "execution_digest": "9" * 64,
            "manifest": "{}",
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT execution_id, status, revision, run_id, execution_digest
            FROM task_loop_executions WHERE execution_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM task_loop_executions WHERE execution_id = :row_id"
        ),
    )

    command.downgrade(config, "0052_model_planner_task_loop")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert not {
        "task_loop_executions",
        "task_loop_execution_events",
        "model_planner_node_bindings",
        "task_loop_node_attempts",
        "task_loop_verified_results",
        "task_loop_capability_approvals",
        "task_loop_cycle_events",
    } & tables
    assert revision == "0052_model_planner_task_loop"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_101_dynamic_patch_approval_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-101-dynamic-patch-approval.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        node_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graph_nodes")
        }
        constraints = {
            item["name"]: str(item["sqltext"])
            for item in inspector.get_check_constraints("workspace_agent_results")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {"approval_manifest", "approval_digest"}.issubset(node_columns)
    assert "patch_test" in constraints["ck_workspace_agent_result_kind"]
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0049_agent_graph_patch_approvals")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0049_agent_graph_patch_approvals",
        target_revision="0048_agent_test_capability_inputs",
        insert_statement="""
            INSERT INTO workspace_agent_results (
                invocation_id, run_id, result_kind, manifest, result_digest, created_at
            ) VALUES (
                :row_id, :run_id, 'patch_test', :manifest, :digest, :created_at
            )
        """,
        parameters={
            "row_id": "invocation-stage-101-proof",
            "run_id": "run-stage-101-proof",
            "manifest": '{"approved":true}',
            "digest": "a" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT invocation_id, run_id, result_kind, manifest, result_digest
            FROM workspace_agent_results WHERE invocation_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM workspace_agent_results WHERE invocation_id = :row_id"
        ),
    )

    command.downgrade(config, "0048_agent_test_capability_inputs")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        node_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graph_nodes")
        }
        constraints = {
            item["name"]: str(item["sqltext"])
            for item in inspector.get_check_constraints("workspace_agent_results")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert not {"approval_manifest", "approval_digest"} & node_columns
    assert "patch_test" not in constraints["ck_workspace_agent_result_kind"]
    assert revision == "0048_agent_test_capability_inputs"

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


def test_stage_102_graph_test_condition_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-102-graph-test-condition.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("task_execution_edges")}
        constraints = {
            item["name"]: str(item["sqltext"])
            for item in inspector.get_check_constraints("task_execution_edges")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {
        "condition_manifest",
        "condition_digest",
        "decision_manifest",
        "decision_digest",
    }.issubset(columns)
    assert "server_condition" in constraints["ck_execution_edge_requirement"]
    assert revision == CURRENT_REVISION

    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0050_agent_graph_test_conditions",
        target_revision="0049_agent_graph_patch_approvals",
        insert_statement="""
            INSERT INTO task_execution_edges (
                run_id, from_node_id, to_node_id, requirement,
                condition_manifest, condition_digest,
                decision_manifest, decision_digest
            ) VALUES (
                :run_id, :from_node_id, :to_node_id, 'server_condition',
                :condition_manifest, :condition_digest,
                :decision_manifest, :decision_digest
            )
        """,
        parameters={
            "run_id": "run-stage-102-proof",
            "from_node_id": "node-stage-102-from",
            "to_node_id": "node-stage-102-to",
            "condition_manifest": '{"result_kind":"python_test"}',
            "condition_digest": "b" * 64,
            "decision_manifest": '{"satisfied":false}',
            "decision_digest": "c" * 64,
        },
        snapshot_statement="""
            SELECT requirement, condition_manifest, condition_digest,
                   decision_manifest, decision_digest
            FROM task_execution_edges
            WHERE run_id = :run_id
              AND from_node_id = :from_node_id
              AND to_node_id = :to_node_id
        """,
        cleanup_statement="""
            DELETE FROM task_execution_edges
            WHERE run_id = :run_id
              AND from_node_id = :from_node_id
              AND to_node_id = :to_node_id
        """,
    )

    command.downgrade(config, "0049_agent_graph_patch_approvals")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("task_execution_edges")}
        constraints = {
            item["name"]: str(item["sqltext"])
            for item in inspector.get_check_constraints("task_execution_edges")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {
        "condition_manifest",
        "condition_digest",
        "decision_manifest",
        "decision_digest",
    }.isdisjoint(columns)
    assert "server_condition" not in constraints["ck_execution_edge_requirement"]
    assert revision == "0049_agent_graph_patch_approvals"

    command.upgrade(config, "head")
    command.check(config)


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


def test_stage_74_context_compaction_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-74-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    expected = {
        "compaction_snapshots",
        "compaction_source_refs",
        "compaction_coverage_items",
    }
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0034_long_term_memory")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert not expected & set(inspect(connection).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_76_artifact_export_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-76-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "artifact_exports" in inspect(connection).get_table_names()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0035_context_compaction")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "artifact_exports" not in inspect(connection).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_78_turn_route_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-78-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "turn_routes" in inspect(connection).get_table_names()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0037_turn_routes")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0037_turn_routes",
        target_revision="0036_artifact_exports",
        insert_statement="""
            INSERT INTO turn_routes (
                task_id, conversation_id, user_message_id, decision,
                candidate_digest, parameters, parameter_digest, reason_code,
                status, revision, created_at, updated_at
            ) VALUES (
                :row_id, :conversation_id, :message_id, 'unsupported',
                :digest, :parameters, :digest, 'NO_ROUTE_MATCHED',
                'not_applicable', 1, :created_at, :created_at
            )
        """,
        parameters={
            "row_id": "task-stage-78-proof",
            "conversation_id": "conversation-stage-78-proof",
            "message_id": "message-stage-78-proof",
            "digest": "7" * 64,
            "parameters": "{}",
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT task_id, decision, candidate_digest, parameter_digest,
                   reason_code, status, revision
            FROM turn_routes WHERE task_id = :row_id
        """,
        cleanup_statement="DELETE FROM turn_routes WHERE task_id = :row_id",
    )

    command.downgrade(config, "0036_artifact_exports")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "turn_routes" not in inspect(connection).get_table_names()
        assert "artifact_exports" in inspect(connection).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_86_pdf_render_evidence_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-86-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("artifact_revisions")}
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {"render_evidence", "render_evidence_digest"}.issubset(columns)
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0038_pdf_render_evidence")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0038_pdf_render_evidence",
        target_revision="0037_turn_routes",
        insert_statement="""
            INSERT INTO artifact_revisions (
                revision_id, artifact_id, revision_no, media_type,
                content_digest, byte_count, blob_name, patch_receipt_id,
                created_at, render_evidence, render_evidence_digest
            ) VALUES (
                :row_id, :artifact_id, 1, 'application/pdf',
                :content_digest, 1, :blob_name, :patch_receipt_id,
                :created_at, :render_evidence, :render_evidence_digest
            )
        """,
        parameters={
            "row_id": "revision-stage-86-proof",
            "artifact_id": "artifact-stage-86-proof",
            "content_digest": "8" * 64,
            "blob_name": "stage-86-proof.pdf",
            "patch_receipt_id": "receipt-stage-86-proof",
            "created_at": "2026-08-24 00:00:00+00:00",
            "render_evidence": '{"pages":1}',
            "render_evidence_digest": "9" * 64,
        },
        snapshot_statement="""
            SELECT revision_id, render_evidence, render_evidence_digest
            FROM artifact_revisions WHERE revision_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM artifact_revisions WHERE revision_id = :row_id"
        ),
    )

    command.downgrade(config, "0037_turn_routes")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("artifact_revisions")}
    engine.dispose()
    assert not {"render_evidence", "render_evidence_digest"} & columns

    command.upgrade(config, "head")
    command.check(config)


def test_stage_88_turn_route_resolution_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-88-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("turn_routes")}
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {
        "resolved_from_task_id",
        "resolution_rule",
        "resolution_digest",
    }.issubset(columns)
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0039_turn_route_resolutions")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0039_turn_route_resolutions",
        target_revision="0038_pdf_render_evidence",
        insert_statement="""
            INSERT INTO turn_routes (
                task_id, conversation_id, user_message_id, decision,
                candidate_digest, parameters, parameter_digest, reason_code,
                status, revision, created_at, updated_at,
                resolved_from_task_id, resolution_rule, resolution_digest
            ) VALUES (
                :row_id, :conversation_id, :message_id, 'routed',
                :digest, :parameters, :digest, 'ROUTE_RESOLVED',
                'ready', 1, :created_at, :created_at,
                :source_task_id, 'clarification.v1', :resolution_digest
            )
        """,
        parameters={
            "row_id": "task-stage-88-proof",
            "conversation_id": "conversation-stage-88-proof",
            "message_id": "message-stage-88-proof",
            "source_task_id": "task-stage-88-source",
            "digest": "a" * 64,
            "parameters": "{}",
            "resolution_digest": "b" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT task_id, resolved_from_task_id, resolution_rule,
                   resolution_digest
            FROM turn_routes WHERE task_id = :row_id
        """,
        cleanup_statement="DELETE FROM turn_routes WHERE task_id = :row_id",
    )

    command.downgrade(config, "0038_pdf_render_evidence")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("turn_routes")}
    engine.dispose()
    assert (
        not {
            "resolved_from_task_id",
            "resolution_rule",
            "resolution_digest",
        }
        & columns
    )

    command.upgrade(config, "head")
    command.check(config)


def test_stage_89_durable_agent_model_loop_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-89-round-trip.db"
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
    assert {
        "model_dispatch_attempts",
        "agent_decisions",
        "agent_observations",
    }.issubset(tables)
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0040_durable_agent_model_loop")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0040_durable_agent_model_loop",
        target_revision="0039_turn_route_resolutions",
        insert_statement="""
            INSERT INTO model_dispatch_attempts (
                dispatch_attempt_id, turn_id, attempt_no, status,
                provider_id, model, request_digest, input_tokens,
                output_tokens, cost_micros, claim_owner_id,
                claim_fencing_token, created_at, updated_at
            ) VALUES (
                :row_id, :turn_id, 1, 'prepared',
                'local', 'proof-model', :digest, 0,
                0, 0, 'worker-stage-89', 1, :created_at, :created_at
            )
        """,
        parameters={
            "row_id": "dispatch-stage-89-proof",
            "turn_id": "turn-stage-89-proof",
            "digest": "c" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT dispatch_attempt_id, turn_id, status, request_digest,
                   claim_owner_id, claim_fencing_token
            FROM model_dispatch_attempts WHERE dispatch_attempt_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM model_dispatch_attempts WHERE dispatch_attempt_id = :row_id"
        ),
    )

    command.downgrade(config, "0039_turn_route_resolutions")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    engine.dispose()
    assert (
        not {
            "model_dispatch_attempts",
            "agent_decisions",
            "agent_observations",
        }
        & tables
    )

    command.upgrade(config, "head")
    command.check(config)


def test_stage_90_agent_input_request_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-90-round-trip.db"
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
    assert "agent_input_requests" in tables
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0041_agent_input_requests")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0041_agent_input_requests",
        target_revision="0040_durable_agent_model_loop",
        insert_statement="""
            INSERT INTO agent_input_requests (
                input_request_id, invocation_id, decision_id, question_code,
                question, blocking_fields, answer_schema, request_digest,
                status, created_at
            ) VALUES (
                :row_id, :invocation_id, :decision_id, 'workspace_path',
                'Which workspace?', :blocking_fields, 'workspace_path.v1',
                :digest, 'pending', :created_at
            )
        """,
        parameters={
            "row_id": "input-stage-90-proof",
            "invocation_id": "invocation-stage-90-proof",
            "decision_id": "decision-stage-90-proof",
            "blocking_fields": '["workspace_path"]',
            "digest": "d" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT input_request_id, invocation_id, decision_id, request_digest, status
            FROM agent_input_requests WHERE input_request_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM agent_input_requests WHERE input_request_id = :row_id"
        ),
    )

    command.downgrade(config, "0040_durable_agent_model_loop")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "agent_input_requests" not in inspect(connection).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_91_workbench_runtime_item_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-91-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "workbench_runtime_items" in inspect(connection).get_table_names()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0042_workbench_runtime_items")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0042_workbench_runtime_items",
        target_revision="0041_agent_input_requests",
        insert_statement="""
            INSERT INTO workbench_runtime_items (
                work_item_id, task_id, action, status, revision,
                attempt_count, consecutive_failure_count, available_at,
                claim_fencing_token, created_at, updated_at
            ) VALUES (
                :row_id, :task_id, 'advance', 'pending', 1,
                0, 0, :created_at, 0, :created_at, :created_at
            )
        """,
        parameters={
            "row_id": "work-item-stage-91-proof",
            "task_id": "task-stage-91-proof",
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT work_item_id, task_id, status, revision,
                   attempt_count, consecutive_failure_count,
                   claim_fencing_token
            FROM workbench_runtime_items WHERE work_item_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM workbench_runtime_items WHERE work_item_id = :row_id"
        ),
    )

    command.downgrade(config, "0041_agent_input_requests")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        assert "workbench_runtime_items" not in inspect(connection).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_93_agent_delegation_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-93-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        node_columns = {item["name"] for item in inspector.get_columns("task_execution_nodes")}
        invocation_columns = {item["name"] for item in inspector.get_columns("agent_invocations")}
        delegation_columns = {item["name"] for item in inspector.get_columns("agent_delegations")}
    engine.dispose()
    assert "agent_delegations" in tables
    assert "handoff_parent_node_id" in node_columns
    assert "parent_invocation_id" in invocation_columns
    assert {
        "delegation_id",
        "parent_invocation_id",
        "child_invocation_id",
        "decision_id",
        "binding_id",
        "status",
        "depth",
        "proposal_digest",
        "budget_allocation",
        "child_result_id",
        "observation_id",
    }.issubset(delegation_columns)

    command.downgrade(config, "0043_agent_delegations")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0043_agent_delegations",
        target_revision="0042_workbench_runtime_items",
        insert_statement="""
            INSERT INTO agent_delegations (
                delegation_id, run_id, parent_invocation_id, parent_node_id,
                child_node_id, decision_id, binding_id, status, depth,
                proposal_manifest, proposal_digest, budget_allocation,
                created_at, updated_at
            ) VALUES (
                :row_id, :run_id, :parent_invocation_id, :parent_node_id,
                :child_node_id, :decision_id, :binding_id, 'waiting_child', 1,
                :proposal_manifest, :proposal_digest, :budget_allocation,
                :created_at, :updated_at
            )
        """,
        parameters={
            "row_id": "delegation-stage-93-proof",
            "run_id": "run-stage-93-proof",
            "parent_invocation_id": "invocation-stage-93-parent",
            "parent_node_id": "node-stage-93-parent",
            "child_node_id": "node-stage-93-child",
            "decision_id": "decision-stage-93-proof",
            "binding_id": "binding-stage-93-proof",
            "proposal_manifest": '{"agent":"workspace_reader"}',
            "proposal_digest": "e" * 64,
            "budget_allocation": '{"max_turns":1}',
            "created_at": "2026-08-24 00:00:00+00:00",
            "updated_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT delegation_id, status, proposal_manifest, proposal_digest,
                   budget_allocation
            FROM agent_delegations WHERE delegation_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM agent_delegations WHERE delegation_id = :row_id"
        ),
    )

    command.downgrade(config, "0042_workbench_runtime_items")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "agent_delegations" not in inspector.get_table_names()
        assert "handoff_parent_node_id" not in {
            item["name"] for item in inspector.get_columns("task_execution_nodes")
        }
        assert "parent_invocation_id" not in {
            item["name"] for item in inspector.get_columns("agent_invocations")
        }
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_stage_94_agent_task_graph_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-94-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        graph_columns = {item["name"] for item in inspector.get_columns("agent_task_graphs")}
        graph_node_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graph_nodes")
        }
        decision_check = next(
            item["sqltext"]
            for item in inspector.get_check_constraints("agent_decisions")
            if item["name"] == "ck_agent_decision_kind"
        )
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {
        "agent_task_graphs",
        "agent_task_graph_nodes",
        "workspace_agent_results",
    }.issubset(tables)
    assert {
        "graph_id",
        "parent_invocation_id",
        "decision_id",
        "binding_id",
        "status",
        "manifest",
        "graph_digest",
        "node_count",
        "max_depth",
        "observation_id",
    }.issubset(graph_columns)
    assert {
        "graph_id",
        "local_key",
        "child_node_id",
        "child_invocation_id",
        "binding_id",
        "status",
        "budget_allocation",
        "child_result_id",
    }.issubset(graph_node_columns)
    assert "propose_task_graph" in decision_check
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0044_agent_task_graphs")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0044_agent_task_graphs",
        target_revision="0043_agent_delegations",
        insert_statement="""
            INSERT INTO workspace_agent_results (
                invocation_id, run_id, result_kind, manifest, result_digest, created_at
            ) VALUES (
                :row_id, :run_id, 'file', :manifest, :digest, :created_at
            )
        """,
        parameters={
            "row_id": "invocation-stage-94-proof",
            "run_id": "run-stage-94-proof",
            "manifest": '{"path":"proof.txt"}',
            "digest": "f" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT invocation_id, run_id, result_kind, manifest, result_digest
            FROM workspace_agent_results WHERE invocation_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM workspace_agent_results WHERE invocation_id = :row_id"
        ),
    )

    command.downgrade(config, "0043_agent_delegations")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        decision_check = next(
            item["sqltext"]
            for item in inspector.get_check_constraints("agent_decisions")
            if item["name"] == "ck_agent_decision_kind"
        )
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert (
        not {
            "agent_task_graphs",
            "agent_task_graph_nodes",
            "workspace_agent_results",
        }
        & tables
    )
    assert "propose_task_graph" not in decision_check
    assert revision == "0043_agent_delegations"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_95_agent_task_graph_result_ref_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-95-result-ref-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0045_agent_task_graph_result_refs")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        graph_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graphs")
        }
        node_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graph_nodes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {"output_local_key", "output_node_id"}.issubset(graph_columns)
    assert {"result_ref_manifest", "result_ref_digest"}.issubset(node_columns)
    assert revision == "0045_agent_task_graph_result_refs"

    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0045_agent_task_graph_result_refs",
        target_revision="0044_agent_task_graphs",
        insert_statement="""
            INSERT INTO agent_task_graphs (
                graph_id, run_id, parent_invocation_id, parent_node_id,
                decision_id, binding_id, status, manifest, graph_digest,
                node_count, max_depth, created_at, updated_at,
                output_local_key, output_node_id
            ) VALUES (
                :row_id, :run_id, :parent_invocation_id, :parent_node_id,
                :decision_id, :binding_id, 'running', :manifest, :digest,
                1, 1, :created_at, :created_at,
                'result', :output_node_id
            )
        """,
        parameters={
            "row_id": "graph-stage-95-proof",
            "run_id": "run-stage-95-proof",
            "parent_invocation_id": "invocation-stage-95-parent",
            "parent_node_id": "node-stage-95-parent",
            "decision_id": "decision-stage-95-proof",
            "binding_id": "binding-stage-95-proof",
            "manifest": '{"nodes":["result"]}',
            "digest": "d" * 64,
            "output_node_id": "node-stage-95-output",
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT graph_id, output_local_key, output_node_id, graph_digest
            FROM agent_task_graphs WHERE graph_id = :row_id
        """,
        cleanup_statement="DELETE FROM agent_task_graphs WHERE graph_id = :row_id",
    )

    command.downgrade(config, "0044_agent_task_graphs")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        graph_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graphs")
        }
        node_columns = {
            item["name"] for item in inspector.get_columns("agent_task_graph_nodes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert not {"output_local_key", "output_node_id"} & graph_columns
    assert not {"result_ref_manifest", "result_ref_digest"} & node_columns
    assert revision == "0044_agent_task_graphs"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_96_agent_task_graph_capability_input_migration_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-96-capability-input-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0046_agent_task_graph_capability_inputs")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        columns = {
            item["name"] for item in inspect(connection).get_columns("agent_task_graph_nodes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {"input_manifest", "input_digest"}.issubset(columns)
    assert revision == "0046_agent_task_graph_capability_inputs"

    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0046_agent_task_graph_capability_inputs",
        target_revision="0045_agent_task_graph_result_refs",
        insert_statement="""
            INSERT INTO agent_task_graph_nodes (
                graph_id, local_key, child_node_id, binding_id, status,
                budget_allocation, created_at, updated_at,
                input_manifest, input_digest
            ) VALUES (
                :graph_id, :row_id, :child_node_id, :binding_id,
                'waiting_child', :budget_allocation, :created_at, :created_at,
                :input_manifest, :input_digest
            )
        """,
        parameters={
            "graph_id": "graph-stage-96-proof",
            "row_id": "node-stage-96-proof",
            "child_node_id": "child-node-stage-96-proof",
            "binding_id": "binding-stage-96-proof",
            "budget_allocation": '{"max_turns":1}',
            "input_manifest": '{"path":"README.md"}',
            "input_digest": "e" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT graph_id, local_key, input_manifest, input_digest
            FROM agent_task_graph_nodes
            WHERE graph_id = :graph_id AND local_key = :row_id
        """,
        cleanup_statement="""
            DELETE FROM agent_task_graph_nodes
            WHERE graph_id = :graph_id AND local_key = :row_id
        """,
    )

    command.downgrade(config, "0045_agent_task_graph_result_refs")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        columns = {
            item["name"] for item in inspect(connection).get_columns("agent_task_graph_nodes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert not {"input_manifest", "input_digest"} & columns
    assert revision == "0045_agent_task_graph_result_refs"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_97_agent_replan_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-97-agent-replan-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0047_agent_replans")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("agent_replans")}
        indexes = {item["name"] for item in inspector.get_indexes("agent_replans")}
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {
        "replan_id",
        "task_id",
        "source_run_id",
        "source_plan_generation",
        "source_plan_digest",
        "target_run_id",
        "target_plan_generation",
        "target_plan_digest",
        "contract_version",
        "contract_digest",
        "status",
        "manifest",
        "replan_digest",
        "created_at",
    } == columns
    assert "ix_agent_replans_task" in indexes
    assert revision == "0047_agent_replans"

    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0047_agent_replans",
        target_revision="0046_agent_task_graph_capability_inputs",
        insert_statement="""
            INSERT INTO agent_replans (
                replan_id, task_id, source_run_id, source_plan_generation,
                source_plan_digest, target_run_id, target_plan_generation,
                target_plan_digest, contract_version, contract_digest,
                status, manifest, replan_digest, created_at
            ) VALUES (
                :row_id, :task_id, :source_run_id, 1,
                :source_digest, :target_run_id, 2,
                :target_digest, 1, :contract_digest,
                'activated', :manifest, :replan_digest, :created_at
            )
        """,
        parameters={
            "row_id": "replan-stage-97-proof",
            "task_id": "task-stage-97-proof",
            "source_run_id": "run-stage-97-source",
            "source_digest": "f" * 64,
            "target_run_id": "run-stage-97-target",
            "target_digest": "0" * 64,
            "contract_digest": "1" * 64,
            "manifest": '{"reason":"proof"}',
            "replan_digest": "2" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT replan_id, source_run_id, source_plan_generation,
                   target_run_id, target_plan_generation, replan_digest
            FROM agent_replans WHERE replan_id = :row_id
        """,
        cleanup_statement="DELETE FROM agent_replans WHERE replan_id = :row_id",
    )

    command.downgrade(config, "0046_agent_task_graph_capability_inputs")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert "agent_replans" not in tables
    assert revision == "0046_agent_task_graph_capability_inputs"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_98_agent_test_result_kind_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-98-agent-test-result-kind.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        constraints = {
            item["name"]: str(item["sqltext"])
            for item in inspect(connection).get_check_constraints("workspace_agent_results")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert "python_test" in constraints["ck_workspace_agent_result_kind"]
    assert "node_test" in constraints["ck_workspace_agent_result_kind"]
    assert revision == CURRENT_REVISION

    command.downgrade(config, "0048_agent_test_capability_inputs")
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0048_agent_test_capability_inputs",
        target_revision="0047_agent_replans",
        insert_statement="""
            INSERT INTO workspace_agent_results (
                invocation_id, run_id, result_kind, manifest, result_digest, created_at
            ) VALUES (
                :row_id, :run_id, 'python_test', :manifest, :digest, :created_at
            )
        """,
        parameters={
            "row_id": "invocation-stage-98-proof",
            "run_id": "run-stage-98-proof",
            "manifest": '{"passed":false}',
            "digest": "0" * 64,
            "created_at": "2026-08-24 00:00:00+00:00",
        },
        snapshot_statement="""
            SELECT invocation_id, run_id, result_kind, manifest, result_digest
            FROM workspace_agent_results WHERE invocation_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM workspace_agent_results WHERE invocation_id = :row_id"
        ),
    )

    command.downgrade(config, "0047_agent_replans")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        constraints = {
            item["name"]: str(item["sqltext"])
            for item in inspect(connection).get_check_constraints("workspace_agent_results")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert "python_test" not in constraints["ck_workspace_agent_result_kind"]
    assert "node_test" not in constraints["ck_workspace_agent_result_kind"]
    assert revision == "0047_agent_replans"

    command.upgrade(config, "head")
    command.check(config)


def test_stage_111_turn_planning_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "stage-111-turn-planning-round-trip.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        route_columns = {
            item["name"] for item in inspector.get_columns("turn_routes")
        }
        offer_columns = {
            item["name"] for item in inspector.get_columns("turn_planning_offers")
        }
        offer_constraints = {
            item["name"]
            for item in inspector.get_check_constraints("turn_planning_offers")
        }
        run_columns = {
            item["name"] for item in inspector.get_columns("turn_planner_runs")
        }
        run_indexes = {
            item["name"] for item in inspector.get_indexes("turn_planner_runs")
        }
        run_unique_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("turn_planner_runs")
        }
        route_constraints = {
            item["name"] for item in inspector.get_check_constraints("turn_routes")
        }
        route_foreign_keys = {
            item["name"]: item for item in inspector.get_foreign_keys("turn_routes")
        }
        route_indexes = {
            item["name"] for item in inspector.get_indexes("turn_routes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert {
        "turn_planning_offers",
        "turn_planner_runs",
        "turn_planner_adjudications",
        "turn_plan_bindings",
    }.issubset(tables)
    assert {
        "turn_planner_run_id",
        "turn_planning_reservation_digest",
        "turn_planning_adjudication_id",
        "turn_plan_binding_id",
        "turn_plan_binding_digest",
        "turn_planning_provenance_digest",
    }.issubset(route_columns)
    assert {
        "execution_agents_manifest",
        "execution_agents_digest",
        "expected_plan_manifest",
        "expected_plan_id",
        "expected_plan_generation",
        "expected_plan_manifest_digest",
        "expected_plan_binding_snapshot_digest",
    }.issubset(offer_columns)
    assert {
        "agent_id",
        "agent_version",
        "agent_contract_digest",
        "prompt_package_digest",
    }.isdisjoint(offer_columns)
    assert "ck_turn_planning_offer_expected_plan" in offer_constraints
    assert {
        "revision",
        "claim_owner_id",
        "claim_fencing_token",
        "claim_expires_at",
        "request_dispatched_at",
        "fallback_candidate_digest",
        "reservation_digest",
        "completed_at",
        "updated_at",
    }.issubset(run_columns)
    assert {
        "ix_turn_planner_runs_message",
        "ix_turn_planner_runs_claim",
    }.issubset(run_indexes)
    assert "uq_turn_planner_run_reservation" in run_unique_constraints
    assert {
        "ck_turn_route_planner_reservation",
        "ck_turn_route_planning_provenance",
    }.issubset(route_constraints)
    reservation_fk = route_foreign_keys["fk_turn_route_planner_reservation"]
    assert reservation_fk["referred_table"] == "turn_planner_runs"
    assert reservation_fk["options"].get("ondelete") == "RESTRICT"
    assert "ix_turn_routes_planner_run" in route_indexes
    assert revision == CURRENT_REVISION

    common = {
        "created_at": "2026-08-24 00:00:00+00:00",
        "digest": "a" * 64,
        "message_digest": "b" * 64,
    }
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0051_turn_planning_offers",
        target_revision="0050_agent_graph_test_conditions",
        insert_statement="""
            INSERT INTO turn_planning_offers (
                offer_id, offer_key, task_id, user_message_id, user_message_digest,
                contract_id, contract_version, contract_digest,
                execution_agents_manifest, execution_agents_digest,
                expected_plan_manifest, expected_plan_id, expected_plan_generation,
                expected_plan_manifest_digest, expected_plan_binding_snapshot_digest,
                capabilities_manifest, capabilities_digest,
                provider_id, provider_model, provider_snapshot_digest,
                recipe_id, recipe_version, recipe_digest,
                budget_manifest, budget_digest,
                parameter_schema_manifest, parameter_schema_digest,
                policy_snapshot_digest, manifest, offer_digest, created_at
            ) VALUES (
                :row_id, :offer_key, :task_id, :message_id, :message_digest,
                :contract_id, 1, :digest,
                :empty_list, :digest,
                :empty_object, :plan_id, 1, :digest, :digest,
                :empty_list, :digest,
                'local', 'test', :digest,
                'research', '2', :digest,
                :empty_object, :digest,
                :empty_list, :digest,
                :digest, :empty_object, :offer_digest, :created_at
            )
        """,
        parameters={
            **common,
            "row_id": "tpo_" + "1" * 64,
            "offer_key": "ofk_" + "2" * 64,
            "task_id": "tsk_" + "3" * 32,
            "message_id": "msg_" + "4" * 32,
            "contract_id": "tc_" + "5" * 32,
            "plan_id": "epl_" + "7" * 64,
            "empty_list": "[]",
            "empty_object": "{}",
            "offer_digest": "6" * 64,
        },
        snapshot_statement="""
            SELECT offer_id, offer_key, offer_digest
            FROM turn_planning_offers WHERE offer_id = :row_id
        """,
        cleanup_statement="DELETE FROM turn_planning_offers WHERE offer_id = :row_id",
    )
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0051_turn_planning_offers",
        target_revision="0050_agent_graph_test_conditions",
        insert_statement="""
            INSERT INTO turn_planner_runs (
                run_id, task_id, user_message_id, user_message_digest,
                planner_agent_id, planner_agent_version, planner_contract_digest,
                planner_prompt_package_digest, provider_id, provider_model,
                provider_snapshot_digest, offer_set_digest, request_digest,
                fallback_candidate_digest, reservation_digest,
                status, revision, claim_owner_id, claim_fencing_token,
                claim_expires_at, request_dispatched_at,
                response_digest, failure_code, failure_digest, manifest, run_digest,
                completed_at, created_at, updated_at
            ) VALUES (
                :row_id, :task_id, :message_id, :message_digest,
                'builtin.turn_planner', '1.0.0', :digest,
                :digest, 'local', 'test',
                :digest, :digest, :digest,
                :digest, :reservation_digest,
                'prepared', 1, NULL, 0,
                NULL, NULL,
                NULL, NULL, NULL, :empty_object, :run_digest,
                NULL, :created_at, :created_at
            )
        """,
        parameters={
            **common,
            "row_id": "tpr_" + "1" * 64,
            "task_id": "tsk_" + "3" * 32,
            "message_id": "msg_" + "4" * 32,
            "empty_object": "{}",
            "reservation_digest": "8" * 64,
            "run_digest": "7" * 64,
        },
        snapshot_statement="""
            SELECT run_id, status, revision, claim_fencing_token, run_digest
            FROM turn_planner_runs WHERE run_id = :row_id
        """,
        cleanup_statement="DELETE FROM turn_planner_runs WHERE run_id = :row_id",
    )
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0051_turn_planning_offers",
        target_revision="0050_agent_graph_test_conditions",
        insert_statement="""
            INSERT INTO turn_planner_adjudications (
                adjudication_id, task_id, user_message_id, user_message_digest,
                run_id, run_digest, outcome, selected_offer_count,
                parameter_bindings_manifest, parameter_bindings_digest,
                proposal_digest, reason_code, manifest,
                adjudication_digest, created_at
            ) VALUES (
                :row_id, :task_id, :message_id, :message_digest,
                :run_id, :run_digest, 'deterministic_fallback', 0,
                NULL, NULL,
                NULL, 'PLANNER_TIMEOUT', :empty_object,
                :adjudication_digest, :created_at
            )
        """,
        parameters={
            **common,
            "row_id": "tpa_" + "1" * 64,
            "task_id": "tsk_" + "3" * 32,
            "message_id": "msg_" + "4" * 32,
            "run_id": "tpr_" + "5" * 64,
            "run_digest": "6" * 64,
            "empty_object": "{}",
            "adjudication_digest": "8" * 64,
        },
        snapshot_statement="""
            SELECT adjudication_id, run_id, outcome, adjudication_digest
            FROM turn_planner_adjudications WHERE adjudication_id = :row_id
        """,
        cleanup_statement=(
            "DELETE FROM turn_planner_adjudications WHERE adjudication_id = :row_id"
        ),
    )
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0051_turn_planning_offers",
        target_revision="0050_agent_graph_test_conditions",
        insert_statement="""
            INSERT INTO turn_plan_bindings (
                binding_id, task_id, user_message_id, user_message_digest,
                adjudication_id, adjudication_digest,
                status, offer_id, offer_digest, plan_id, plan_generation,
                plan_manifest_digest, contract_id, contract_version, contract_digest,
                reason_code, manifest, binding_digest, created_at
            ) VALUES (
                :row_id, :task_id, :message_id, :message_digest,
                :adjudication_id, :adjudication_digest,
                'not_applicable', NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL,
                'PLANNER_TIMEOUT', :empty_object, :binding_digest, :created_at
            )
        """,
        parameters={
            **common,
            "row_id": "tpb_" + "1" * 64,
            "task_id": "tsk_" + "3" * 32,
            "message_id": "msg_" + "4" * 32,
            "adjudication_id": "tpa_" + "5" * 64,
            "adjudication_digest": "6" * 64,
            "empty_object": "{}",
            "binding_digest": "9" * 64,
        },
        snapshot_statement="""
            SELECT binding_id, adjudication_id, status, binding_digest
            FROM turn_plan_bindings WHERE binding_id = :row_id
        """,
        cleanup_statement="DELETE FROM turn_plan_bindings WHERE binding_id = :row_id",
    )
    _assert_populated_downgrade_refused(
        database_path,
        config,
        revision="0051_turn_planning_offers",
        target_revision="0050_agent_graph_test_conditions",
        insert_statement="""
            INSERT INTO turn_routes (
                task_id, conversation_id, user_message_id, decision,
                route_id, route_version, route_manifest_digest,
                candidate_digest, parameters, parameter_digest,
                resolved_from_task_id, resolution_rule, resolution_digest,
                turn_planner_run_id, turn_planning_reservation_digest,
                turn_planning_adjudication_id, turn_plan_binding_id,
                turn_plan_binding_digest,
                turn_planning_provenance_digest,
                reason_code, status, result_manifest, result_digest, error_code,
                revision, created_at, updated_at
            ) VALUES (
                :row_id, :conversation_id, :message_id, 'unsupported',
                NULL, NULL, NULL,
                :digest, :empty_object, :digest,
                NULL, NULL, NULL,
                :run_id, :reservation_digest,
                :adjudication_id, :binding_id, :binding_digest, :digest,
                'PLANNER_TIMEOUT', 'not_applicable', NULL, NULL, NULL,
                1, :created_at, :created_at
            )
        """,
        parameters={
            **common,
            "row_id": "tsk_" + "1" * 32,
            "conversation_id": "conv_" + "2" * 32,
            "message_id": "msg_" + "3" * 32,
            "run_id": "tpr_" + "7" * 64,
            "reservation_digest": "8" * 64,
            "adjudication_id": "tpa_" + "4" * 64,
            "binding_id": "tpb_" + "5" * 64,
            "binding_digest": "6" * 64,
            "empty_object": "{}",
        },
        snapshot_statement="""
            SELECT task_id, turn_planner_run_id, turn_planning_reservation_digest,
                   turn_planning_adjudication_id,
                   turn_plan_binding_id, turn_plan_binding_digest,
                   turn_planning_provenance_digest
            FROM turn_routes WHERE task_id = :row_id
        """,
        cleanup_statement="DELETE FROM turn_routes WHERE task_id = :row_id",
    )

    command.downgrade(config, "0050_agent_graph_test_conditions")
    engine = create_engine(_sync_url(database_path))
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        route_columns = {
            item["name"] for item in inspector.get_columns("turn_routes")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    engine.dispose()
    assert not {
        "turn_planning_offers",
        "turn_planner_runs",
        "turn_planner_adjudications",
        "turn_plan_bindings",
    } & tables
    assert not {
        "turn_planner_run_id",
        "turn_planning_reservation_digest",
        "turn_planning_adjudication_id",
        "turn_plan_binding_id",
        "turn_plan_binding_digest",
        "turn_planning_provenance_digest",
    } & route_columns
    assert revision == "0050_agent_graph_test_conditions"

    command.upgrade(config, "head")
    command.check(config)
