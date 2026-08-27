from __future__ import annotations

from pathlib import Path

import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenSidecarSoakSuiteLoader,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_coding_sidecar_soak_v1.yaml"
)


def test_sidecar_soak_suite_is_strict_and_cross_digest_bound(tmp_path: Path) -> None:
    bundle = WorkspaceCodingGoldenSidecarSoakSuiteLoader().load()
    scenario = bundle.suite.scenario

    assert bundle.suite.schema_version == (
        "deskpilot.workspace-coding-sidecar-soak-suite.v1"
    )
    assert bundle.suite.resilience_suite_digest == bundle.resilience.suite_digest
    assert scenario.command_profile_ids == (
        "python.ruff.v1",
        "python.mypy.v1",
    )
    assert scenario.observation_seconds * 1_000 // scenario.poll_interval_ms == 20
    assert scenario.expected_process_generations == scenario.expected_restart_count + 1

    drifted = tmp_path / "sidecar-soak-digest-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.resilience.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="resilience digest"):
        WorkspaceCodingGoldenSidecarSoakSuiteLoader(drifted).load()


@pytest.mark.parametrize(
    ("old", "new", "expected_error"),
    (
        (
            "command_project_path: backend",
            "command_project_path: backend-drifted",
            "resilience authority",
        ),
        ("max_advances: 24", "max_advances: 25", "resilience authority"),
        (
            "no_automatic_replay: true",
            "no_automatic_replay: false",
            "strict validation",
        ),
    ),
)
def test_sidecar_soak_suite_rejects_resilience_authority_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    drifted = tmp_path / "sidecar-soak-authority-drifted.yaml"
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert source.count(old) == 1
    drifted.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenSidecarSoakSuiteLoader(drifted).load()
