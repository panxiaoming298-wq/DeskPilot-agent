import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deskpilot.application.agent_release_lifecycle import (
    AgentReleaseActivationPolicy,
    load_agent_release_bundle,
)
from deskpilot.phase115_release_gate import main as phase115_release_main

FIXED_NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _run(monkeypatch, capsys, *arguments: str) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["phase115-release", *arguments])
    code = phase115_release_main()
    output = json.loads(capsys.readouterr().out)
    return code, output


def test_release_cli_builds_registers_activates_and_disables_exact_cohort(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    registered_path = tmp_path / "registered.json"
    active_path = tmp_path / "active.json"
    disabled_path = tmp_path / "disabled.json"
    created_at = FIXED_NOW.isoformat()
    valid_until = (FIXED_NOW + timedelta(days=30)).isoformat()

    code, manifest_summary = _run(
        monkeypatch,
        capsys,
        "manifest",
        "--build-id",
        "phase115-test-build",
        "--created-at",
        created_at,
        "--valid-until",
        valid_until,
        "--output",
        str(manifest_path),
    )
    assert code == 0
    assert len(manifest_summary["cohort"]) == 3
    assert len(manifest_summary["companions"]) == 2

    code, registered_summary = _run(
        monkeypatch,
        capsys,
        "register",
        "--manifest",
        str(manifest_path),
        "--actor",
        "release-test-reviewer",
        "--at",
        created_at,
        "--output",
        str(registered_path),
    )
    assert code == 0
    assert registered_summary["revision"] == 1

    release_id = str(manifest_summary["release_id"])
    code, active_summary = _run(
        monkeypatch,
        capsys,
        "activate",
        "--input",
        str(registered_path),
        "--release-id",
        release_id,
        "--actor",
        "release-test-reviewer",
        "--at",
        created_at,
        "--output",
        str(active_path),
    )
    assert code == 0
    assert active_summary["active_release_id"] == release_id
    active = load_agent_release_bundle(active_path)
    assert AgentReleaseActivationPolicy(active, now=FIXED_NOW).active_release_id == (
        release_id
    )

    code, disabled_summary = _run(
        monkeypatch,
        capsys,
        "disable",
        "--input",
        str(active_path),
        "--release-id",
        release_id,
        "--actor",
        "release-test-reviewer",
        "--at",
        created_at,
        "--output",
        str(disabled_path),
    )
    assert code == 0
    assert disabled_summary["active_release_id"] is None
    assert disabled_summary["revision"] == 3


def test_release_cli_refuses_overwrite_and_unregistered_activation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    created_at = FIXED_NOW.isoformat()
    arguments = (
        "manifest",
        "--build-id",
        "phase115-immutable-output",
        "--created-at",
        created_at,
        "--valid-until",
        (FIXED_NOW + timedelta(days=7)).isoformat(),
        "--output",
        str(manifest_path),
    )
    assert _run(monkeypatch, capsys, *arguments)[0] == 0
    code, error = _run(monkeypatch, capsys, *arguments)
    assert code == 2
    assert error["code"] == "AGENT_RELEASE_REJECTED"
