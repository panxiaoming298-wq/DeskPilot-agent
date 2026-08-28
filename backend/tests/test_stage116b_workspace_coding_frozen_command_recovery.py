from pathlib import Path

import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_coding_frozen_command_recovery_v1.yaml"
)


def test_frozen_command_recovery_is_exact_predecessor_and_success_bound(
    tmp_path: Path,
) -> None:
    bundle = WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader().load()
    scenario = bundle.suite.scenario

    assert bundle.suite.schema_version == (
        "deskpilot.workspace-coding-frozen-command-recovery-suite.v1"
    )
    assert bundle.suite.frozen_command_suite_digest == bundle.frozen_command.suite_digest
    assert scenario.command_profile_ids == ("python.ruff.v1", "python.pytest.v1")
    assert scenario.restart_after_step_sequence == 1
    assert scenario.expected_pending_attempt_count_before_restart == 0
    assert scenario.expected_post_restart_advances == 3
    assert scenario.expected_verified_result_count_after_restart == 2
    assert scenario.reaches_final_delivery is True
    assert scenario.no_automatic_replay is True

    drifted = tmp_path / "frozen-command-recovery-digest-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.frozen_command.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="command suite digest"):
        WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader(drifted).load()


@pytest.mark.parametrize(
    ("old", "new", "expected_error"),
    (
        (
            "command_profile_ids: [python.ruff.v1, python.pytest.v1]",
            "command_profile_ids: [python.pytest.v1, python.ruff.v1]",
            "strict validation",
        ),
        (
            "expected_post_restart_advances: 3",
            "expected_post_restart_advances: 4",
            "strict validation",
        ),
        (
            "reaches_final_delivery: true",
            "reaches_final_delivery: false",
            "strict validation",
        ),
        (
            "frozen_command_scenario_id: installed.python-command.resultref-interruption",
            "frozen_command_scenario_id: installed.python-command.other-interruption",
            "command safety boundary",
        ),
    ),
)
def test_frozen_command_recovery_rejects_boundary_or_delivery_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert source.count(old) == 1
    drifted = tmp_path / "frozen-command-recovery-drifted.yaml"
    drifted.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenFrozenCommandRecoverySuiteLoader(drifted).load()
