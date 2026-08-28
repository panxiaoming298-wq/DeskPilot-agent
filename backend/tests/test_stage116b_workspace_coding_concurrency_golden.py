from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenConcurrencySuiteLoader,
)
from deskpilot.domain.workspace_coding_evaluations import (
    WorkspaceCodingConcurrencyRepository,
    WorkspaceCodingConcurrencyScenario,
)

BACKEND_ROOT = Path(__file__).parents[1]
SUITE_PATH = (
    BACKEND_ROOT / "src" / "deskpilot" / "evaluations" / "workspace_coding_concurrency_v1.yaml"
)
TEST_TOKEN = "stage116b-concurrency-token-000000000"
TEST_ORIGIN = "http://stage116b-concurrency.local"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "Content-Type": "application/json",
    }


def _response_json(response: httpx.Response, expected_status: int = 200) -> dict[str, Any]:
    assert response.status_code == expected_status, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _read_json_list(path: Path) -> tuple[Any, ...]:
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return tuple(payload)


class _ConcurrentCommandApiProcess:
    def __init__(
        self,
        tmp_path: Path,
        workspace_root: Path,
        scenario: WorkspaceCodingConcurrencyScenario,
    ) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.database_url = (
            f"sqlite+aiosqlite:///{(tmp_path / 'concurrent-command-api.db').as_posix()}"
        )
        self.command_calls_path = tmp_path / "concurrent-command-calls.json"
        self.command_activity_path = tmp_path / "concurrent-command-activity.json"
        self.provider_calls_path = tmp_path / "concurrent-provider-calls.json"
        self.command_started_path = tmp_path / "concurrent-command-started.txt"
        self.runtime_control_path = tmp_path / "workbench-runtime.txt"
        self.runtime_control_path.write_text("false", encoding="utf-8")
        self._tmp_path = tmp_path
        self._workspace_root = workspace_root
        self._scenario = scenario
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: Any | None = None

    def start(self) -> None:
        assert self._process is None
        profiles = tuple(
            dict.fromkeys(
                profile_id
                for repository in self._scenario.repositories
                for profile_id in repository.command_profile_ids
            )
        )
        failing = next(item for item in self._scenario.repositories if item.fail_first_profile_once)
        routes = [
            {
                "repository_id": item.repository_id,
                "message_marker": item.message_marker,
                "project_path": item.project_path,
                "profile_ids": list(item.command_profile_ids),
            }
            for item in self._scenario.repositories
        ]
        environment = os.environ.copy()
        environment.update(
            {
                "DESKPILOT_DATABASE_URL": self.database_url,
                "DESKPILOT_ARTIFACT_WORKSPACE_ROOT": str(
                    self._tmp_path / "concurrent-command-artifacts"
                ),
                "DESKPILOT_CONVERSATION_WORKSPACE_ROOT": str(self._workspace_root),
                "DESKPILOT_SESSION_TOKEN": TEST_TOKEN,
                "DESKPILOT_CORS_ORIGINS": json.dumps([TEST_ORIGIN]),
                "DESKPILOT_RUNNER_COMMIT_RECEIPT_DATABASE_PATH": str(
                    self._tmp_path / "concurrent-command-receipts.db"
                ),
                "DESKPILOT_RUNNER_WORKER_RUNTIME_ROOT": str(
                    self._tmp_path / "concurrent-command-worker-runtime"
                ),
                "DESKPILOT_RUNNER_APPCONTAINER_PROFILE_JOURNAL_PATH": str(
                    self._tmp_path / "concurrent-command-appcontainer-profiles.json"
                ),
                "DESKPILOT_MODEL_GATEWAY_POLICY": json.dumps(
                    {"provider_pricing": [{"provider_id": "fake-local"}]}
                ),
                "DESKPILOT_RESEARCH_RUNTIME_ENABLED": "false",
                "DESKPILOT_FAKE_STEP_DELAY_SECONDS": "0.001",
                "DESKPILOT_GOLDEN_API_PORT": str(self.port),
                "DESKPILOT_GOLDEN_COMMAND_PROJECT": self._scenario.repositories[0].project_path,
                "DESKPILOT_GOLDEN_COMMAND_PROFILES": ",".join(profiles),
                "DESKPILOT_GOLDEN_COMMAND_ROUTES": json.dumps(routes),
                "DESKPILOT_GOLDEN_COMMAND_FAULT_MODE": "fail_target_once",
                "DESKPILOT_GOLDEN_COMMAND_FAILURE_TARGET": (
                    f"{failing.project_path}|{failing.command_profile_ids[0]}"
                ),
                "DESKPILOT_GOLDEN_COMMAND_DELAY_MS": str(self._scenario.command_delay_ms),
                "DESKPILOT_GOLDEN_COMMAND_ACTIVITY_PATH": str(self.command_activity_path),
                "DESKPILOT_GOLDEN_COMMAND_CALLS_PATH": str(self.command_calls_path),
                "DESKPILOT_GOLDEN_PROVIDER_CALLS_PATH": str(self.provider_calls_path),
                "DESKPILOT_GOLDEN_COMMAND_STARTED_PATH": str(self.command_started_path),
                "DESKPILOT_GOLDEN_WORKBENCH_RUNTIME_CONTROL_PATH": str(self.runtime_control_path),
                "DESKPILOT_GOLDEN_WORKBENCH_RUNTIME_CONCURRENCY": str(
                    self._scenario.workbench_concurrency
                ),
                "PYTHONUTF8": "1",
            }
        )
        self._log_stream = (self._tmp_path / "concurrent-command-api.log").open("ab")
        self._process = subprocess.Popen(  # noqa: S603 - fixed test-only module.
            (sys.executable, "-m", "tests.fixtures.workspace_command_fault_server"),
            cwd=BACKEND_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                log = (self._tmp_path / "concurrent-command-api.log").read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                raise AssertionError(
                    f"Concurrent command API exited with {self._process.returncode}: {log[-4000:]}"
                )
            try:
                response = httpx.get(f"{self.base_url}/api/v1/health", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("Concurrent command API process did not become healthy")

    def client(self) -> httpx.Client:
        assert self._process is not None and self._process.poll() is None
        return httpx.Client(base_url=self.base_url, headers=_headers(), timeout=60)

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        finally:
            self._process = None
            assert self._log_stream is not None
            self._log_stream.close()
            self._log_stream = None


def _command_nodes(workbench: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    task_loop = workbench.get("task_loop")
    if not isinstance(task_loop, dict) or not isinstance(task_loop.get("nodes"), list):
        return ()
    nodes = tuple(
        item
        for item in task_loop["nodes"]
        if isinstance(item, dict) and item.get("command_plan_id") is not None
    )
    return tuple(sorted(nodes, key=lambda item: int(item["command_step_sequence"])))


def _create_ready_task(
    client: httpx.Client,
    repository: WorkspaceCodingConcurrencyRepository,
    *,
    max_advances: int,
) -> tuple[str, dict[str, Any]]:
    workbench = _response_json(
        client.post(
            "/api/v1/conversation-turns",
            json={
                "message": (
                    f"运行 {repository.project_path} 的固定检查 [{repository.message_marker}]"
                )
            },
        ),
        201,
    )
    task_id = str(workbench["task"]["task_id"])
    for _ in range(max_advances):
        nodes = _command_nodes(workbench)
        if nodes and nodes[0]["status"] == "ready":
            return task_id, workbench
        workbench = _response_json(client.post(f"/api/v1/tasks/{task_id}/workbench:advance"))
    pytest.fail(f"{repository.repository_id} did not reach its first ready Profile")


def _materialize_repository(
    root: Path,
    repository: WorkspaceCodingConcurrencyRepository,
) -> None:
    project = root.joinpath(*repository.project_path.split("/"))
    project.mkdir(parents=True)
    if repository.ecosystem == "python":
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'concurrency-fixture'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        source = project / "src" / repository.repository_id.replace("-", "_")
        source.mkdir(parents=True)
        for index in range(repository.source_file_count):
            (source / f"module_{index:02d}.py").write_text(
                f"VALUE_{index}: int = {index}\n",
                encoding="utf-8",
            )
        return
    package = {
        "name": repository.repository_id,
        "private": True,
        "scripts": {
            "test": "node --test",
            "type-check": "tsc --noEmit",
            "build": "tsc",
        },
    }
    (project / "package.json").write_text(
        json.dumps(package, separators=(",", ":")),
        encoding="utf-8",
    )
    (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    source = project / "src"
    source.mkdir()
    for index in range(repository.source_file_count):
        (source / f"module_{index:02d}.ts").write_text(
            f"export const value{index}: number = {index};\n",
            encoding="utf-8",
        )


def test_concurrency_suite_is_strict_and_sidecar_digest_bound(tmp_path: Path) -> None:
    bundle = WorkspaceCodingGoldenConcurrencySuiteLoader().load()
    scenario = bundle.suite.scenario

    assert bundle.suite.schema_version == "deskpilot.workspace-coding-concurrency-suite.v1"
    assert bundle.suite.sidecar_suite_digest == bundle.sidecar.suite_digest
    assert len(scenario.repositories) == 3
    assert {item.ecosystem for item in scenario.repositories} == {"python", "node"}
    assert sum(item.source_file_count for item in scenario.repositories) == 72
    assert sum(item.fail_first_profile_once for item in scenario.repositories) == 1

    drifted = tmp_path / "concurrency-digest-drifted.yaml"
    drifted.write_text(
        SUITE_PATH.read_text(encoding="utf-8").replace(
            bundle.sidecar.suite_digest,
            "f" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="sidecar digest"):
        WorkspaceCodingGoldenConcurrencySuiteLoader(drifted).load()


@pytest.mark.parametrize(
    ("old", "new", "expected_error"),
    (
        (
            "project_path: projects/python-worker",
            "project_path: projects/python-api",
            "strict validation",
        ),
        ("workbench_concurrency: 2", "workbench_concurrency: 3", "strict validation"),
        ("max_advances: 24", "max_advances: 25", "sidecar safety contract"),
    ),
)
def test_concurrency_suite_rejects_scheduler_or_safety_drift(
    tmp_path: Path,
    old: str,
    new: str,
    expected_error: str,
) -> None:
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert source.count(old) == 1
    drifted = tmp_path / "concurrency-scheduler-drifted.yaml"
    drifted.write_text(source.replace(old, new), encoding="utf-8")

    with pytest.raises(WorkspaceCodingEvaluationError, match=expected_error):
        WorkspaceCodingGoldenConcurrencySuiteLoader(drifted).load()


def test_three_repositories_are_fair_bounded_and_failure_isolated_via_public_api(
    tmp_path: Path,
) -> None:
    scenario = WorkspaceCodingGoldenConcurrencySuiteLoader().load().suite.scenario
    workspace_root = tmp_path / "concurrent-workspace"
    for repository in scenario.repositories:
        _materialize_repository(workspace_root, repository)
    api = _ConcurrentCommandApiProcess(tmp_path, workspace_root, scenario)
    task_by_repository: dict[str, str] = {}
    final_by_repository: dict[str, dict[str, Any]] = {}
    try:
        api.start()
        with api.client() as client:
            for repository in scenario.repositories:
                task_id, _ready = _create_ready_task(
                    client,
                    repository,
                    max_advances=scenario.max_advances,
                )
                task_by_repository[repository.repository_id] = task_id
        assert _read_json_list(api.command_calls_path) == ()
        assert len(_read_json_list(api.provider_calls_path)) == 3

        api.stop()
        api.runtime_control_path.write_text("true", encoding="utf-8")
        api.start()
        deadline = time.monotonic() + scenario.completion_timeout_seconds
        with api.client() as client:
            while time.monotonic() < deadline:
                for repository_id, task_id in task_by_repository.items():
                    current = _response_json(client.get(f"/api/v1/tasks/{task_id}/workbench"))
                    task_loop = current.get("task_loop")
                    if (
                        isinstance(task_loop, dict)
                        and task_loop.get("execution_status") == "succeeded"
                    ):
                        final_by_repository[repository_id] = current
                if len(final_by_repository) == len(scenario.repositories):
                    break
                time.sleep(scenario.poll_interval_ms / 1_000)
        assert set(final_by_repository) == {item.repository_id for item in scenario.repositories}
    finally:
        api.stop()

    provider_calls = tuple(str(item) for item in _read_json_list(api.provider_calls_path))
    assert len(provider_calls) == 3
    assert {item.split(":", maxsplit=1)[0] for item in provider_calls} == {
        item.repository_id for item in scenario.repositories
    }

    activity = tuple(dict(item) for item in _read_json_list(api.command_activity_path))
    starts = tuple(item for item in activity if item["event"] == "start")
    finishes = tuple(item for item in activity if item["event"] == "finish")
    assert max(int(item["active"]) for item in activity) == (
        scenario.expected_peak_command_concurrency
    )
    assert all(0 <= int(item["active"]) <= scenario.workbench_concurrency for item in activity)
    assert int(activity[-1]["active"]) == 0

    expected_calls: Counter[tuple[str, str]] = Counter()
    for repository in scenario.repositories:
        for profile_id in repository.command_profile_ids:
            expected_calls[(repository.project_path, profile_id)] += 1
        if repository.fail_first_profile_once:
            expected_calls[(repository.project_path, repository.command_profile_ids[0])] += 1
    observed_calls = Counter(
        (str(item["project_path"]), str(item["command_profile_id"])) for item in starts
    )
    assert observed_calls == expected_calls
    assert len(finishes) == sum(expected_calls.values())

    first_start_sequence = {
        repository.project_path: min(
            int(item["sequence"])
            for item in starts
            if item["project_path"] == repository.project_path
        )
        for repository in scenario.repositories
    }
    second_profile_sequences = tuple(
        int(item["sequence"])
        for item in starts
        for repository in scenario.repositories
        if item["project_path"] == repository.project_path
        and item["command_profile_id"] == repository.command_profile_ids[1]
    )
    assert max(first_start_sequence.values()) < min(second_profile_sequences)

    for repository in scenario.repositories:
        final = final_by_repository[repository.repository_id]
        nodes = _command_nodes(final)
        assert tuple(item["command_profile_id"] for item in nodes) == (
            repository.command_profile_ids
        )
        task_loop = final["task_loop"]
        if repository.fail_first_profile_once:
            assert task_loop["repair_count"] == 1
            assert nodes[0]["attempt_count"] == 2
            assert nodes[0]["verified_failure_result_count"] == 1
        else:
            assert task_loop["repair_count"] == 0
            assert all(item["attempt_count"] == 1 for item in nodes)
            assert all(item["verified_failure_result_count"] == 0 for item in nodes)
        assert all(item["status"] == "verified" for item in nodes)
        assert all(item["verified_result_present"] for item in nodes)

    failing = next(item for item in scenario.repositories if item.fail_first_profile_once)
    target_finishes = tuple(
        item
        for item in finishes
        if item["project_path"] == failing.project_path
        and item["command_profile_id"] == failing.command_profile_ids[0]
    )
    assert tuple(item["status"] for item in target_finishes) == ("failed", "passed")
    assert any(
        item["project_path"] != failing.project_path
        and item["status"] == "passed"
        and int(item["sequence"]) < int(target_finishes[1]["sequence"])
        for item in finishes
    )
