"""Deterministic bounded graph helpers for the persistent coding Route."""

from __future__ import annotations

from collections.abc import Mapping

WORKSPACE_CODING_MIN_FILES = 2
WORKSPACE_CODING_MAX_FILES = 8


def workspace_coding_coordinator_output_tokens(file_count: int) -> int:
    """Return enough sealed output budget for the exact Coordinator graph."""

    _validate_file_count(file_count)
    if file_count == WORKSPACE_CODING_MIN_FILES:
        return 2_000
    return 2_200 + (100 * file_count)


def workspace_coding_max_output_tokens(file_count: int) -> int:
    """Return the Contract output ceiling for Coordinator and Patch Planners."""

    _validate_file_count(file_count)
    if file_count == WORKSPACE_CODING_MIN_FILES:
        return 6_000
    return workspace_coding_coordinator_output_tokens(file_count) + (
        1_501 * file_count
    )


def workspace_coding_file_count(parameters: Mapping[str, object]) -> int:
    """Recover the server-fixed file count while preserving legacy two-file Offers."""

    raw = parameters.get("file_count")
    if raw is None:
        return WORKSPACE_CODING_MIN_FILES
    if not isinstance(raw, str) or not raw.isdecimal():
        raise ValueError("Workspace coding file count is not canonical")
    count = int(raw)
    if not WORKSPACE_CODING_MIN_FILES <= count <= WORKSPACE_CODING_MAX_FILES:
        raise ValueError("Workspace coding file count is outside the bounded graph")
    return count


def workspace_coding_variant_key(test_kind: str, file_count: int) -> str:
    _validate_file_count(file_count)
    base = f"workspace_coding_loop:{test_kind}"
    return base if file_count == WORKSPACE_CODING_MIN_FILES else f"{base}:{file_count}"


def workspace_coding_fixed_parameters(
    test_kind: str,
    file_count: int,
) -> dict[str, str]:
    _validate_file_count(file_count)
    fixed = {"test_kind": test_kind}
    if file_count > WORKSPACE_CODING_MIN_FILES:
        fixed["file_count"] = str(file_count)
    return fixed


def workspace_coding_path_parameter(index: int) -> str:
    _validate_file_index(index)
    if index == 1:
        return "primary_path"
    if index == 2:
        return "secondary_path"
    return f"file_{index:02d}_path"


def workspace_coding_reader_key(index: int) -> str:
    _validate_file_index(index)
    if index == 1:
        return "inspect_primary"
    if index == 2:
        return "inspect_secondary"
    return f"inspect_file_{index:02d}"


def workspace_coding_planner_key(index: int) -> str:
    _validate_file_index(index)
    if index == 1:
        return "plan_primary_patch"
    if index == 2:
        return "plan_secondary_patch"
    return f"plan_file_{index:02d}_patch"


def workspace_coding_reader_keys(file_count: int) -> tuple[str, ...]:
    _validate_file_count(file_count)
    return tuple(workspace_coding_reader_key(index) for index in range(1, file_count + 1))


def workspace_coding_planner_keys(file_count: int) -> tuple[str, ...]:
    _validate_file_count(file_count)
    return tuple(workspace_coding_planner_key(index) for index in range(1, file_count + 1))


def workspace_coding_graph_keys(file_count: int) -> tuple[str, ...]:
    """Return the exact Coordinator-visible graph, excluding its own control node."""

    return (
        *workspace_coding_reader_keys(file_count),
        *workspace_coding_planner_keys(file_count),
        "apply_patch",
        "run_fixed_test",
        "commit_git",
    )


def workspace_coding_parameter_for_key(local_key: str) -> str | None:
    for index in range(1, WORKSPACE_CODING_MAX_FILES + 1):
        if local_key in {
            workspace_coding_reader_key(index),
            workspace_coding_planner_key(index),
        }:
            return workspace_coding_path_parameter(index)
    return None


def is_workspace_coding_reader_key(local_key: str) -> bool:
    return local_key in set(workspace_coding_reader_keys(WORKSPACE_CODING_MAX_FILES))


def is_workspace_coding_planner_key(local_key: str) -> bool:
    return local_key in set(workspace_coding_planner_keys(WORKSPACE_CODING_MAX_FILES))


def _validate_file_count(file_count: int) -> None:
    if not WORKSPACE_CODING_MIN_FILES <= file_count <= WORKSPACE_CODING_MAX_FILES:
        raise ValueError("Workspace coding file count is outside the bounded graph")


def _validate_file_index(index: int) -> None:
    if not 1 <= index <= WORKSPACE_CODING_MAX_FILES:
        raise ValueError("Workspace coding file index is outside the bounded graph")


__all__ = [
    "WORKSPACE_CODING_MAX_FILES",
    "WORKSPACE_CODING_MIN_FILES",
    "is_workspace_coding_planner_key",
    "is_workspace_coding_reader_key",
    "workspace_coding_file_count",
    "workspace_coding_fixed_parameters",
    "workspace_coding_graph_keys",
    "workspace_coding_coordinator_output_tokens",
    "workspace_coding_max_output_tokens",
    "workspace_coding_parameter_for_key",
    "workspace_coding_path_parameter",
    "workspace_coding_planner_key",
    "workspace_coding_planner_keys",
    "workspace_coding_reader_key",
    "workspace_coding_reader_keys",
    "workspace_coding_variant_key",
]
