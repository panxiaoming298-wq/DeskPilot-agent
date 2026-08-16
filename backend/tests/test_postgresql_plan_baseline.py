from pathlib import Path

import pytest

from deskpilot.infrastructure.postgresql_plan_baseline import (
    PostgreSQLPlanBaselineError,
    build_plan_baseline,
    compare_plan_baseline,
    load_plan_baseline,
    query_shape_sha256,
    summarize_json_plan,
    write_plan_baseline,
)


def _raw_plan(*, execution_time: float = 1.25, shared_hits: int = 12) -> list[object]:
    return [
        {
            "Plan": {
                "Node Type": "Limit",
                "Actual Rows": 101,
                "Actual Loops": 1,
                "Shared Hit Blocks": shared_hits,
                "Shared Read Blocks": 2,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Index Name": "ix_effect_dag_ready_nodes_query",
                        "Relation Name": "tool_effect_dag_ready_nodes",
                        "Actual Rows": 101,
                        "Actual Loops": 1,
                        "Rows Removed by Filter": 3,
                    }
                ],
            },
            "Planning Time": 0.2,
            "Execution Time": execution_time,
        }
    ]


def _baseline(*, execution_time: float = 1.25, shared_hits: int = 12) -> dict[str, object]:
    return build_plan_baseline(
        baseline_id="ready-v6-membership-1000-nodes-pg17",
        workload={"graph_node_count": 1_000, "page_size": 100},
        query_shape_digest=query_shape_sha256("SELECT *  FROM ready WHERE graph_id = $1"),
        postgresql_version="PostgreSQL 17.10 test build",
        server_version="17.10",
        server_version_num=170010,
        raw_plan=_raw_plan(execution_time=execution_time, shared_hits=shared_hits),
    )


def test_plan_summary_records_rows_buffers_indexes_and_node_shape() -> None:
    summary = summarize_json_plan(_raw_plan())

    assert summary == {
        "planning_time_ms": 0.2,
        "execution_time_ms": 1.25,
        "root_actual_rows": 101,
        "scan_actual_rows": 101,
        "rows_removed": 3,
        "shared_hit_blocks": 12,
        "shared_read_blocks": 2,
        "shared_total_blocks": 14,
        "index_names": ["ix_effect_dag_ready_nodes_query"],
        "node_type_counts": {"Index Scan": 1, "Limit": 1},
        "scan_nodes": [
            {
                "node_type": "Index Scan",
                "relation_name": "tool_effect_dag_ready_nodes",
                "index_name": "ix_effect_dag_ready_nodes_query",
                "actual_rows": 101,
                "actual_loops": 1,
            }
        ],
    }


def test_query_shape_hash_ignores_whitespace_but_not_sql_shape() -> None:
    assert query_shape_sha256("SELECT *\nFROM ready") == query_shape_sha256(
        " SELECT   * FROM ready "
    )
    assert query_shape_sha256("SELECT * FROM ready") != query_shape_sha256("SELECT id FROM ready")


def test_plan_comparison_accepts_cache_variation_within_policy() -> None:
    baseline = _baseline()
    candidate = _baseline(execution_time=6.0, shared_hits=20)

    assert compare_plan_baseline(baseline, candidate) == ()


def test_plan_comparison_reports_shape_time_and_buffer_regressions() -> None:
    baseline = _baseline()
    candidate = _baseline(execution_time=7.0, shared_hits=60)
    summary = candidate["summary"]
    assert isinstance(summary, dict)
    summary["root_actual_rows"] = 100
    summary["index_names"] = []

    issues = compare_plan_baseline(baseline, candidate)

    assert "plan root_actual_rows changed" in issues
    assert "plan index_names changed" in issues
    assert any(issue.startswith("execution time regressed:") for issue in issues)
    assert any(issue.startswith("shared buffers regressed:") for issue in issues)


def test_plan_baseline_round_trip_is_atomic_and_unicode_safe(tmp_path: Path) -> None:
    path = tmp_path / "基线.json"
    baseline = _baseline()

    write_plan_baseline(path, baseline)

    assert load_plan_baseline(path) == baseline
    assert list(tmp_path.iterdir()) == [path]


def test_plan_summary_rejects_malformed_explain_json() -> None:
    with pytest.raises(PostgreSQLPlanBaselineError, match="exactly one"):
        summarize_json_plan([])
