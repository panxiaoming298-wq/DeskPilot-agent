"""Versioned PostgreSQL JSON-plan capture and regression comparison."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PLAN_BASELINE_SCHEMA_VERSION = 1
_SCAN_NODE_TYPES = frozenset(
    {
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
        "Index Only Scan",
        "Index Scan",
        "Seq Scan",
        "Subquery Scan",
        "Tid Range Scan",
        "Tid Scan",
    }
)


class PostgreSQLPlanBaselineError(ValueError):
    """A captured or stored plan baseline is malformed or incomparable."""


def query_shape_sha256(sql: str) -> str:
    """Hash a parameterized SQL shape without embedding workload values."""
    normalized = re.sub(r"\s+", " ", sql).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PostgreSQLPlanBaselineError(f"plan field {field!r} is not numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PostgreSQLPlanBaselineError(f"plan field {field!r} is invalid")
    return result


def _integer(value: object, *, field: str) -> int:
    number = _number(value, field=field)
    if not number.is_integer():
        raise PostgreSQLPlanBaselineError(f"plan field {field!r} is not an integer")
    return int(number)


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PostgreSQLPlanBaselineError(f"plan field {field!r} is not an object")
    if not all(isinstance(key, str) for key in value):
        raise PostgreSQLPlanBaselineError(f"plan field {field!r} has a non-string key")
    return value


def _sequence(value: object, *, field: str) -> Sequence[object]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise PostgreSQLPlanBaselineError(f"plan field {field!r} is not an array")
    return value


def _walk_plan(node: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    descendants: list[Mapping[str, Any]] = [node]
    raw_children = node.get("Plans", ())
    children = _sequence(raw_children, field="Plans")
    for raw_child in children:
        descendants.extend(_walk_plan(_mapping(raw_child, field="Plans[]")))
    return tuple(descendants)


def summarize_json_plan(raw_plan: object) -> dict[str, object]:
    """Extract stable shape, row, buffer, and index facts from EXPLAIN JSON."""
    documents = _sequence(raw_plan, field="root")
    if len(documents) != 1:
        raise PostgreSQLPlanBaselineError("EXPLAIN JSON must contain exactly one plan")
    document = _mapping(documents[0], field="root[0]")
    root = _mapping(document.get("Plan"), field="Plan")
    nodes = _walk_plan(root)

    node_types: list[str] = []
    scan_nodes: list[dict[str, object]] = []
    index_names: set[str] = set()
    rows_removed = 0
    scan_rows = 0
    for node in nodes:
        node_type = node.get("Node Type")
        if not isinstance(node_type, str) or not node_type:
            raise PostgreSQLPlanBaselineError("every plan node must have a Node Type")
        node_types.append(node_type)
        loops = _integer(node.get("Actual Loops", 1), field="Actual Loops")
        actual_rows = _integer(node.get("Actual Rows", 0), field="Actual Rows") * loops
        rows_removed += (
            _integer(
                node.get("Rows Removed by Filter", 0),
                field="Rows Removed by Filter",
            )
            * loops
        )
        rows_removed += (
            _integer(
                node.get("Rows Removed by Index Recheck", 0),
                field="Rows Removed by Index Recheck",
            )
            * loops
        )
        if node_type in _SCAN_NODE_TYPES:
            index_name = node.get("Index Name")
            relation_name = node.get("Relation Name")
            if index_name is not None and not isinstance(index_name, str):
                raise PostgreSQLPlanBaselineError("Index Name must be a string")
            if relation_name is not None and not isinstance(relation_name, str):
                raise PostgreSQLPlanBaselineError("Relation Name must be a string")
            if index_name:
                index_names.add(index_name)
            scan_rows += actual_rows
            scan_nodes.append(
                {
                    "node_type": node_type,
                    "relation_name": relation_name,
                    "index_name": index_name,
                    "actual_rows": actual_rows,
                    "actual_loops": loops,
                }
            )

    counts = Counter(node_types)
    shared_hit = _integer(root.get("Shared Hit Blocks", 0), field="Shared Hit Blocks")
    shared_read = _integer(root.get("Shared Read Blocks", 0), field="Shared Read Blocks")
    return {
        "planning_time_ms": _number(document.get("Planning Time"), field="Planning Time"),
        "execution_time_ms": _number(document.get("Execution Time"), field="Execution Time"),
        "root_actual_rows": _integer(root.get("Actual Rows"), field="Actual Rows"),
        "scan_actual_rows": scan_rows,
        "rows_removed": rows_removed,
        "shared_hit_blocks": shared_hit,
        "shared_read_blocks": shared_read,
        "shared_total_blocks": shared_hit + shared_read,
        "index_names": sorted(index_names),
        "node_type_counts": dict(sorted(counts.items())),
        "scan_nodes": scan_nodes,
    }


def build_plan_baseline(
    *,
    baseline_id: str,
    workload: Mapping[str, object],
    query_shape_digest: str,
    postgresql_version: str,
    server_version: str,
    server_version_num: int,
    raw_plan: object,
) -> dict[str, object]:
    """Build the versioned document stored in source control."""
    if not baseline_id:
        raise PostgreSQLPlanBaselineError("baseline_id must not be empty")
    if not re.fullmatch(r"[0-9a-f]{64}", query_shape_digest):
        raise PostgreSQLPlanBaselineError("query_shape_digest must be SHA-256 hex")
    if server_version_num <= 0:
        raise PostgreSQLPlanBaselineError("server_version_num must be positive")
    summary = summarize_json_plan(raw_plan)
    return {
        "schema_version": PLAN_BASELINE_SCHEMA_VERSION,
        "baseline_id": baseline_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "workload": dict(workload),
        "query_shape_sha256": query_shape_digest,
        "postgresql": {
            "version": postgresql_version,
            "server_version": server_version,
            "server_version_num": server_version_num,
        },
        "comparison_policy": {
            "execution_time_max_ratio": 5.0,
            "execution_time_slack_ms": 5.0,
            "scan_rows_max_ratio": 1.05,
            "shared_blocks_max_ratio": 1.5,
            "shared_blocks_slack": 32,
        },
        "summary": summary,
        "plan": raw_plan,
    }


def _summary(document: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(document.get("summary"), field="summary")


def _policy(document: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(document.get("comparison_policy"), field="comparison_policy")


def _major_version(document: Mapping[str, Any]) -> int:
    postgresql = _mapping(document.get("postgresql"), field="postgresql")
    version_num = _integer(
        postgresql.get("server_version_num"),
        field="postgresql.server_version_num",
    )
    return version_num // 10_000


def compare_plan_baseline(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return deterministic regression descriptions; an empty tuple means pass."""
    if baseline.get("schema_version") != PLAN_BASELINE_SCHEMA_VERSION:
        raise PostgreSQLPlanBaselineError("unsupported baseline schema_version")
    if candidate.get("schema_version") != PLAN_BASELINE_SCHEMA_VERSION:
        raise PostgreSQLPlanBaselineError("unsupported candidate schema_version")
    issues: list[str] = []
    if candidate.get("baseline_id") != baseline.get("baseline_id"):
        issues.append("baseline_id changed")
    if candidate.get("query_shape_sha256") != baseline.get("query_shape_sha256"):
        issues.append("parameterized query shape changed")
    if candidate.get("workload") != baseline.get("workload"):
        issues.append("workload definition changed")
    if _major_version(candidate) != _major_version(baseline):
        issues.append("PostgreSQL major version changed; record a separate baseline")

    expected = _summary(baseline)
    actual = _summary(candidate)
    for field in ("root_actual_rows", "node_type_counts", "scan_nodes", "index_names"):
        if actual.get(field) != expected.get(field):
            issues.append(f"plan {field} changed")

    policy = _policy(baseline)
    expected_scan_rows = _integer(
        expected.get("scan_actual_rows"),
        field="summary.scan_actual_rows",
    )
    actual_scan_rows = _integer(
        actual.get("scan_actual_rows"),
        field="summary.scan_actual_rows",
    )
    scan_ratio = _number(
        policy.get("scan_rows_max_ratio"),
        field="comparison_policy.scan_rows_max_ratio",
    )
    if actual_scan_rows > math.ceil(expected_scan_rows * scan_ratio):
        issues.append(
            f"scan rows regressed: baseline={expected_scan_rows}, candidate={actual_scan_rows}"
        )

    expected_time = _number(
        expected.get("execution_time_ms"),
        field="summary.execution_time_ms",
    )
    actual_time = _number(
        actual.get("execution_time_ms"),
        field="summary.execution_time_ms",
    )
    time_ratio = _number(
        policy.get("execution_time_max_ratio"),
        field="comparison_policy.execution_time_max_ratio",
    )
    time_slack = _number(
        policy.get("execution_time_slack_ms"),
        field="comparison_policy.execution_time_slack_ms",
    )
    time_limit = max(expected_time * time_ratio, expected_time + time_slack)
    if actual_time > time_limit:
        issues.append(
            "execution time regressed: "
            f"baseline={expected_time:.3f}ms, candidate={actual_time:.3f}ms, "
            f"limit={time_limit:.3f}ms"
        )

    expected_blocks = _integer(
        expected.get("shared_total_blocks"),
        field="summary.shared_total_blocks",
    )
    actual_blocks = _integer(
        actual.get("shared_total_blocks"),
        field="summary.shared_total_blocks",
    )
    block_ratio = _number(
        policy.get("shared_blocks_max_ratio"),
        field="comparison_policy.shared_blocks_max_ratio",
    )
    block_slack = _integer(
        policy.get("shared_blocks_slack"),
        field="comparison_policy.shared_blocks_slack",
    )
    block_limit = max(math.ceil(expected_blocks * block_ratio), expected_blocks + block_slack)
    if actual_blocks > block_limit:
        issues.append(
            "shared buffers regressed: "
            f"baseline={expected_blocks}, candidate={actual_blocks}, limit={block_limit}"
        )
    return tuple(issues)


def load_plan_baseline(path: Path) -> dict[str, Any]:
    """Load one checked-in baseline without accepting non-object JSON."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostgreSQLPlanBaselineError(f"cannot load plan baseline: {path}") from exc
    return dict(_mapping(value, field="baseline"))


def write_plan_baseline(path: Path, document: Mapping[str, object]) -> None:
    """Atomically write a human-reviewable versioned JSON baseline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "PLAN_BASELINE_SCHEMA_VERSION",
    "PostgreSQLPlanBaselineError",
    "build_plan_baseline",
    "compare_plan_baseline",
    "load_plan_baseline",
    "query_shape_sha256",
    "summarize_json_plan",
    "write_plan_baseline",
]
