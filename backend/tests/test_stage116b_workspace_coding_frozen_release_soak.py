from pathlib import Path

import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_coding_frozen_release_soak_v1.yaml"
)


def test_frozen_release_suite_is_strict_and_concurrency_digest_bound(tmp_path: Path) -> None:
    bundle = WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader().load()
    scenario = bundle.suite.scenario

    assert (
        bundle.suite.schema_version
        == "deskpilot.workspace-coding-frozen-release-soak-suite.v1"
    )
    assert bundle.suite.concurrency_suite_digest == bundle.concurrency.suite_digest
    assert scenario.concurrency_scenario_id == bundle.concurrency.suite.scenario.scenario_id
    assert scenario.expected_external_kill_count == 2
    assert scenario.expected_process_generations == 3
    assert scenario.health_only_canary is True
    assert scenario.replays_command_tasks is False

    drifted = tmp_path / "frozen-release-digest-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.concurrency.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="concurrency digest"):
        WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader(drifted).load()


@pytest.mark.parametrize(
    ("old", "new", "expected_error"),
    (
        (
            "concurrency_scenario_id: mixed.command-plan.fair-concurrency",
            "concurrency_scenario_id: mixed.command-plan.other-concurrency",
            "concurrency safety contract",
        ),
        (
            "expected_process_generations: 3",
            "expected_process_generations: 2",
            "strict validation",
        ),
        (
            "health_only_canary: true",
            "health_only_canary: false",
            "strict validation",
        ),
        (
            "max_process_tree_handle_count: 2048",
            "max_process_tree_handle_count: 8192",
            "strict validation",
        ),
    ),
)
def test_frozen_release_suite_rejects_scope_or_resource_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert source.count(old) == 1
    drifted = tmp_path / "frozen-release-scope-drifted.yaml"
    drifted.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenFrozenReleaseSoakSuiteLoader(drifted).load()
