from pathlib import Path

import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_coding_frozen_command_task_v1.yaml"
)


def test_frozen_command_task_is_release_digest_and_real_profile_bound(
    tmp_path: Path,
) -> None:
    bundle = WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader().load()
    scenario = bundle.suite.scenario

    assert (
        bundle.suite.schema_version
        == "deskpilot.workspace-coding-frozen-command-task-suite.v1"
    )
    assert bundle.suite.frozen_release_suite_digest == bundle.frozen_release.suite_digest
    assert scenario.command_profile_ids == ("python.ruff.v1", "python.pytest.v1")
    assert scenario.expected_verified_result_count_before_kill == 1
    assert scenario.expected_isolation_mode == "windows_appcontainer"
    assert scenario.production_fake_provider_unsupported is True
    assert scenario.no_automatic_replay is True

    drifted = tmp_path / "frozen-command-release-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.frozen_release.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="release digest"):
        WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader(drifted).load()


@pytest.mark.parametrize(
    ("old", "new", "expected_error"),
    (
        (
            "command_profile_ids: [python.ruff.v1, python.pytest.v1]",
            "command_profile_ids: [python.pytest.v1, python.ruff.v1]",
            "strict validation",
        ),
        (
            "expected_process_generations: 2",
            "expected_process_generations: 3",
            "strict validation",
        ),
        (
            "production_fake_provider_unsupported: true",
            "production_fake_provider_unsupported: false",
            "strict validation",
        ),
        (
            "frozen_release_scenario_id: installed.sidecar.supervisor.resource-soak",
            "frozen_release_scenario_id: installed.sidecar.other-soak",
            "release safety boundary",
        ),
    ),
)
def test_frozen_command_task_rejects_authority_or_replay_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert source.count(old) == 1
    drifted = tmp_path / "frozen-command-authority-drifted.yaml"
    drifted.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenFrozenCommandTaskSuiteLoader(drifted).load()
