from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from deskpilot.application.workspace_repository_evaluation import (
    WorkspaceRepositoryEvaluationError,
    WorkspaceRepositoryOfflinePreflight,
    WorkspaceRepositoryTaskSuiteBundle,
    WorkspaceRepositoryTaskSuiteLoader,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.workspace_repository_evaluations import WorkspaceRepositoryTaskSuite
from deskpilot.phase116c_offline_gate import main as offline_gate_main

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "workspace_repository_tasks_v1.yaml"
)


def _git(executable: str, repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(  # noqa: S603 - fixed test-only Git arguments.
        (executable, "-C", str(repository), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        close_fds=True,
    )
    return completed.stdout


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _seed_local_mirrors(
    tmp_path: Path,
    suite: WorkspaceRepositoryTaskSuite,
) -> WorkspaceRepositoryTaskSuiteBundle:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is unavailable")
    source = tmp_path / "source"
    source.mkdir()
    _git(executable, source, "init")
    _git(executable, source, "config", "user.email", "offline-preflight@example.invalid")
    _git(executable, source, "config", "user.name", "Offline Preflight")
    _git(executable, source, "config", "core.autocrlf", "false")
    _write(source / "LICENSE", "MIT License\n")
    _write(source / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(source / "src" / "module.py", "VALUE = 1\n")
    _write(source / "tests" / "test_module.py", "def test_value():\n    assert True\n")
    _write(source / "src" / "index.ts", "export const value = 1\n")
    _write(source / "test" / "index.test.ts", "export const healthy = true\n")
    _git(executable, source, "add", "--", ".")
    _git(executable, source, "commit", "-m", "base")
    base_python = _git(executable, source, "rev-parse", "HEAD").decode().strip()

    _write(source / "src" / "module.py", "VALUE = 2\n")
    _git(executable, source, "add", "--", "src/module.py")
    _git(executable, source, "commit", "-m", "python single")
    python_single = _git(executable, source, "rev-parse", "HEAD").decode().strip()

    _write(source / "tests" / "test_module.py", "def test_value():\n    assert 2 == 2\n")
    _git(executable, source, "add", "--", "tests/test_module.py")
    _git(executable, source, "commit", "-m", "python multi")
    python_multi = _git(executable, source, "rev-parse", "HEAD").decode().strip()
    base_node = python_multi

    _write(source / "src" / "index.ts", "export const value = 2\n")
    _git(executable, source, "add", "--", "src/index.ts")
    _git(executable, source, "commit", "-m", "node single")
    node_single = _git(executable, source, "rev-parse", "HEAD").decode().strip()

    _write(source / "test" / "index.test.ts", "export const healthy = 2 === 2\n")
    _git(executable, source, "add", "--", "test/index.test.ts")
    _git(executable, source, "commit", "-m", "node multi")
    node_multi = _git(executable, source, "rev-parse", "HEAD").decode().strip()

    mirror_root = tmp_path / "mirrors"
    repositories_root = mirror_root / "repositories"
    repositories_root.mkdir(parents=True)
    material = suite.model_dump(mode="json")
    for repository in material["repositories"]:
        mirror = mirror_root.joinpath(*repository["mirror_path"].split("/"))
        subprocess.run(  # noqa: S603 - fixed test-only local bare clone.
            (executable, "clone", "--quiet", "--bare", str(source), str(mirror)),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        repository["frozen_head_commit"] = node_multi
        repository["license_path"] = "LICENSE"
        repository["package_lock_path"] = (
            "pnpm-lock.yaml" if repository["ecosystem"] == "node" else None
        )

    variants: dict[tuple[str, str], tuple[str, str, tuple[str, ...], str]] = {
        ("python", "single_file"): (
            base_python,
            python_single,
            ("src/module.py",),
            "tests/test_module.py",
        ),
        ("python", "multi_file"): (
            base_python,
            python_multi,
            ("src/module.py", "tests/test_module.py"),
            "tests/test_module.py",
        ),
        ("node", "single_file"): (
            base_node,
            node_single,
            ("src/index.ts",),
            "test/index.test.ts",
        ),
        ("node", "multi_file"): (
            base_node,
            node_multi,
            ("src/index.ts", "test/index.test.ts"),
            "test/index.test.ts",
        ),
    }
    for task in material["tasks"]:
        file_coverage = (
            "single_file" if "single_file" in task["coverage"] else "multi_file"
        )
        base, reference, changed_paths, test_path = variants[
            (task["ecosystem"], file_coverage)
        ]
        listing = _git(executable, source, "ls-tree", "-r", "-z", "--long", base)
        diff = _git(
            executable,
            source,
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--binary",
            base,
            reference,
        )
        task.update(
            {
                "base_commit": base,
                "reference_commit": reference,
                "base_tree_listing_sha256": hashlib.sha256(listing).hexdigest(),
                "reference_diff_sha256": hashlib.sha256(diff).hexdigest(),
                "reference_changed_paths": changed_paths,
                "acceptance_test_paths": (test_path,),
                "command_profile_ids": (
                    ["python.pytest.v1"]
                    if task["ecosystem"] == "python"
                    else ["node.pnpm_test.v1"]
                ),
            }
        )
    local_suite = WorkspaceRepositoryTaskSuite.model_validate(material)
    return WorkspaceRepositoryTaskSuiteBundle(
        suite=local_suite,
        suite_digest=sha256_digest(local_suite.model_dump(mode="json")),
    )


def test_repository_task_suite_freezes_twenty_real_tasks_and_offline_boundary() -> None:
    bundle = WorkspaceRepositoryTaskSuiteLoader().load()
    suite = bundle.suite

    assert bundle.suite_digest == "8260414a0f4ed8cc513d8519e6ebe9afd4ad6d228054a6574b4a947d72afffa9"
    assert len(suite.repositories) == 8
    assert len(suite.tasks) == 20
    assert [task.ecosystem for task in suite.tasks].count("python") == 10
    assert [task.ecosystem for task in suite.tasks].count("node") == 10
    assert suite.thresholds.minimum_successful_trials == 48
    assert suite.thresholds.false_success_maximum == 0
    assert suite.thresholds.unauthorized_effect_maximum == 0
    assert suite.cleanup.on_failure == "cleanup_pending_and_never_reuse"
    assert suite.offline_boundary.model_dump() == {
        "network_access": False,
        "real_model_capture": False,
        "candidate_provider_calls": False,
        "judge_provider_calls": False,
        "human_grading": False,
        "production_admission": False,
        "cloud_activation": False,
        "dependency_installation": False,
        "automatic_push": False,
    }
    assert all(
        source.upstream_url.startswith("https://github.com/")
        for source in suite.repositories
    )
    assert all(task.local_commit_required and task.push_disabled for task in suite.tasks)


def test_offline_gate_manifest_reports_only_frozen_local_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert offline_gate_main(("manifest",)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["task_count"] == 20
    assert report["trial_count"] == 60
    assert report["minimum_successful_trials"] == 48
    assert report["false_success_maximum"] == 0
    assert report["unauthorized_effect_maximum"] == 0
    assert not any(report["offline_boundary"].values())


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("real_model_capture: false", "real_model_capture: true"),
        ("minimum_success_rate_basis_points: 8000", "minimum_success_rate_basis_points: 7900"),
        ("network_effects_maximum: 0", "network_effects_maximum: 1"),
        (
            "upstream_url: https://github.com/pypa/sampleproject.git",
            "upstream_url: https://example.com/pypa/sampleproject.git",
        ),
    ),
)
def test_repository_task_suite_rejects_boundary_drift(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    payload = SUITE_PATH.read_text(encoding="utf-8")
    assert old in payload
    changed = tmp_path / "changed.yaml"
    changed.write_text(payload.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(WorkspaceRepositoryEvaluationError):
        WorkspaceRepositoryTaskSuiteLoader(changed).load()


def test_repository_task_suite_rejects_aliases_unknown_fields_and_missing_task(
    tmp_path: Path,
) -> None:
    payload = SUITE_PATH.read_text(encoding="utf-8")
    aliased = tmp_path / "aliased.yaml"
    aliased.write_text(
        payload.replace(
            "coverage: [multi_file]",
            "coverage: &coverage [multi_file]",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceRepositoryEvaluationError, match="aliases"):
        WorkspaceRepositoryTaskSuiteLoader(aliased).load()

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(payload + "unexpected: true\n", encoding="utf-8")
    with pytest.raises(WorkspaceRepositoryEvaluationError, match="strict validation"):
        WorkspaceRepositoryTaskSuiteLoader(unknown).load()

    parsed = yaml.safe_load(payload)
    assert isinstance(parsed, dict)
    parsed["tasks"] = parsed["tasks"][:-1]
    missing = tmp_path / "missing.yaml"
    missing.write_text(yaml.safe_dump(parsed, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WorkspaceRepositoryEvaluationError, match="strict validation"):
        WorkspaceRepositoryTaskSuiteLoader(missing).load()


def test_offline_preflight_verifies_all_local_git_objects_without_network(
    tmp_path: Path,
) -> None:
    frozen = WorkspaceRepositoryTaskSuiteLoader().load().suite
    local_bundle = _seed_local_mirrors(tmp_path, frozen)
    report = WorkspaceRepositoryOfflinePreflight(
        local_bundle,
        tmp_path / "mirrors",
    ).run()

    assert report.repository_count == 8
    assert report.task_count == 20
    assert report.trial_count == 60
    assert report.mirror_preflight_read_only is True
    assert report.network_access is False
    assert report.real_model_capture is False
    assert report.production_admission is False
    assert report.cloud_activation is False


def test_offline_preflight_fails_closed_for_missing_mirror_or_diff_drift(
    tmp_path: Path,
) -> None:
    frozen = WorkspaceRepositoryTaskSuiteLoader().load().suite
    local_bundle = _seed_local_mirrors(tmp_path, frozen)
    first_mirror = local_bundle.suite.repositories[0].mirror_path
    mirror = (tmp_path / "mirrors").joinpath(*first_mirror.split("/"))
    moved = mirror.with_name(f"{mirror.name}.missing")
    mirror.rename(moved)
    with pytest.raises(WorkspaceRepositoryEvaluationError, match="unavailable"):
        WorkspaceRepositoryOfflinePreflight(local_bundle, tmp_path / "mirrors").run()
    moved.rename(mirror)

    material: dict[str, Any] = local_bundle.suite.model_dump(mode="json")
    first_repository_id = material["repositories"][0]["repository_id"]
    first_repository_task = next(
        task for task in material["tasks"] if task["repository_id"] == first_repository_id
    )
    material["repositories"][0]["frozen_head_commit"] = first_repository_task[
        "base_commit"
    ]
    head_drifted = WorkspaceRepositoryTaskSuite.model_validate(material)
    with pytest.raises(WorkspaceRepositoryEvaluationError, match="frozen head drifted"):
        WorkspaceRepositoryOfflinePreflight(
            WorkspaceRepositoryTaskSuiteBundle(
                suite=head_drifted,
                suite_digest=sha256_digest(head_drifted.model_dump(mode="json")),
            ),
            tmp_path / "mirrors",
        ).run()

    material = local_bundle.suite.model_dump(mode="json")
    material["tasks"][0]["reference_diff_sha256"] = "0" * 64
    drifted = WorkspaceRepositoryTaskSuite.model_validate(material)
    drifted_bundle = WorkspaceRepositoryTaskSuiteBundle(
        suite=drifted,
        suite_digest=sha256_digest(drifted.model_dump(mode="json")),
    )
    with pytest.raises(WorkspaceRepositoryEvaluationError, match="reference diff drifted"):
        WorkspaceRepositoryOfflinePreflight(drifted_bundle, tmp_path / "mirrors").run()
