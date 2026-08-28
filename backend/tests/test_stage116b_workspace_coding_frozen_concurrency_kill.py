from pathlib import Path

import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenFrozenConcurrencyKillSuiteLoader,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_coding_frozen_concurrency_kill_v1.yaml"
)


def test_frozen_concurrency_kill_is_exact_bound_and_claim_isolated(
    tmp_path: Path,
) -> None:
    bundle = WorkspaceCodingGoldenFrozenConcurrencyKillSuiteLoader().load()
    scenario = bundle.suite.scenario

    assert bundle.suite.schema_version == (
        "deskpilot.workspace-coding-frozen-concurrency-kill-suite.v1"
    )
    assert bundle.suite.frozen_concurrency_suite_digest == (
        bundle.frozen_concurrency.suite_digest
    )
    assert len(scenario.repositories) == 3
    assert all(item.pytest_outcome == "passed" for item in scenario.repositories)
    assert scenario.workbench_concurrency == 2
    assert scenario.expected_unknown_task_count == 2
    assert scenario.expected_success_count == 1
    assert scenario.expected_total_attempt_count == 6
    assert scenario.expected_verified_resultref_count == 4
    assert scenario.expected_unknown_attempt_count == 2
    assert scenario.fault_domain == "claimed_tasks_only"
    assert scenario.no_automatic_replay is True

    drifted = tmp_path / "frozen-concurrency-kill-digest-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.frozen_concurrency.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="concurrency digest"):
        WorkspaceCodingGoldenFrozenConcurrencyKillSuiteLoader(drifted).load()


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
            "expected_unknown_task_count: 2",
            "expected_unknown_task_count: 1",
            "strict validation",
        ),
        (
            "expected_verified_resultref_count: 4",
            "expected_verified_resultref_count: 5",
            "strict validation",
        ),
        (
            "pytest_outcome: passed",
            "pytest_outcome: failed",
            "strict validation",
        ),
        (
            "frozen_concurrency_scenario_id: "
            "installed.python-command.fair-concurrency-fault-isolation",
            "frozen_concurrency_scenario_id: installed.python-command.other-concurrency",
            "concurrency safety boundary",
        ),
    ),
)
def test_frozen_concurrency_kill_rejects_fault_domain_or_binding_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    source = SUITE_PATH.read_text(encoding="utf-8")
    if old.startswith("command_profile_ids") or old == "pytest_outcome: passed":
        assert source.count(old) == 3
        source = source.replace(old, new, 1)
    else:
        assert source.count(old) == 1
        source = source.replace(old, new)
    drifted = tmp_path / "frozen-concurrency-kill-drifted.yaml"
    drifted.write_text(source, encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenFrozenConcurrencyKillSuiteLoader(drifted).load()
