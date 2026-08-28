from pathlib import Path

import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenFrozenConcurrencySuiteLoader,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_coding_frozen_concurrency_v1.yaml"
)


def test_frozen_concurrency_is_exact_recovery_bound_and_failure_isolated(
    tmp_path: Path,
) -> None:
    bundle = WorkspaceCodingGoldenFrozenConcurrencySuiteLoader().load()
    scenario = bundle.suite.scenario

    assert bundle.suite.schema_version == (
        "deskpilot.workspace-coding-frozen-concurrency-suite.v1"
    )
    assert bundle.suite.frozen_command_recovery_suite_digest == (
        bundle.frozen_command_recovery.suite_digest
    )
    assert len(scenario.repositories) == 3
    assert sum(item.pytest_outcome == "passed" for item in scenario.repositories) == 2
    assert sum(item.pytest_outcome == "failed" for item in scenario.repositories) == 1
    assert all(
        item.command_profile_ids == ("python.ruff.v1", "python.pytest.v1")
        for item in scenario.repositories
    )
    assert scenario.workbench_concurrency == 2
    assert scenario.expected_total_attempt_count == 7
    assert scenario.expected_total_resultref_count == 7
    assert scenario.no_automatic_replay is True

    drifted = tmp_path / "frozen-concurrency-digest-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.frozen_command_recovery.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="recovery suite digest"):
        WorkspaceCodingGoldenFrozenConcurrencySuiteLoader(drifted).load()


@pytest.mark.parametrize(
    ("old", "new", "expected_error"),
    (
        (
            "command_profile_ids: [python.ruff.v1, python.pytest.v1]",
            "command_profile_ids: [python.pytest.v1, python.ruff.v1]",
            "strict validation",
        ),
        (
            "workbench_concurrency: 2",
            "workbench_concurrency: 3",
            "strict validation",
        ),
        (
            "expected_total_attempt_count: 7",
            "expected_total_attempt_count: 6",
            "strict validation",
        ),
        (
            "pytest_outcome: failed",
            "pytest_outcome: passed",
            "strict validation",
        ),
        (
            "frozen_command_recovery_scenario_id: installed.python-command.between-step-recovery",
            "frozen_command_recovery_scenario_id: installed.python-command.other-recovery",
            "recovery safety boundary",
        ),
    ),
)
def test_frozen_concurrency_rejects_scheduler_result_or_recovery_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    source = SUITE_PATH.read_text(encoding="utf-8")
    if old.startswith("command_profile_ids"):
        assert source.count(old) == 3
        source = source.replace(old, new, 1)
    else:
        assert source.count(old) == 1
        source = source.replace(old, new)
    drifted = tmp_path / "frozen-concurrency-drifted.yaml"
    drifted.write_text(source, encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenFrozenConcurrencySuiteLoader(drifted).load()
