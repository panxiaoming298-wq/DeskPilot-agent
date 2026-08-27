import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenSuiteLoader,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.workspace_coding_evaluations import WorkspaceCodingGoldenCase
from deskpilot.domain.workspace_files import (
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "stage-116b-golden-session-token-at-least-32-chars"
BACKEND_ROOT = Path(__file__).parents[1]


class _RecordedGoldenPythonTests:
    enabled = True

    def run(self, snapshot: WorkspacePythonTestSnapshot) -> WorkspacePythonTestRead:
        material = {
            "schema_version": "deskpilot.workspace-python-test.v1",
            "profile": "pytest-file",
            "project_path": snapshot.project_path,
            "test_path": snapshot.test_path,
            "snapshot_digest": snapshot.snapshot_digest,
            "runtime_digest": "7" * 64,
            "status": "passed",
            "exit_code": 0,
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "duration_ms": 20,
            "output": "1 passed in 0.02s",
            "output_truncated": False,
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
            "process_limit": 1,
        }
        return WorkspacePythonTestRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )


class _RecordedGoldenNodeTests:
    enabled = True

    def run(self, snapshot: WorkspaceNodeTestSnapshot) -> WorkspaceNodeTestRead:
        material = {
            "schema_version": "deskpilot.workspace-node-test.v1",
            "profile": "node-test-file",
            "project_path": snapshot.project_path,
            "test_path": snapshot.test_path,
            "snapshot_digest": snapshot.snapshot_digest,
            "runtime_digest": "8" * 64,
            "status": "passed",
            "exit_code": 0,
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "duration_ms": 18,
            "output": "tests 1, pass 1, fail 0",
            "output_truncated": False,
            "isolation_mode": "windows_appcontainer",
            "network_access": False,
            "process_limit": 1,
        }
        return WorkspaceNodeTestRead.model_validate(
            {**material, "result_digest": sha256_digest(material)}
        )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }


def _run_git(project: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is unavailable")
    subprocess.run(  # noqa: S603 - fixed test-only Git executable and arguments.
        (executable, *arguments),
        cwd=project,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )


def _materialize_case(workspace_root: Path, case: WorkspaceCodingGoldenCase) -> Path:
    project = workspace_root / case.case_id
    project.mkdir(parents=True)
    for item in case.files:
        target = project.joinpath(*item.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8", newline="\n")
    _run_git(project, "init")
    _run_git(project, "config", "user.email", "deskpilot-golden@example.invalid")
    _run_git(project, "config", "user.name", "DeskPilot Golden")
    _run_git(project, "config", "core.autocrlf", "false")
    _run_git(project, "add", "--", ".")
    _run_git(project, "commit", "-m", "golden fixture")
    return project


def _enabled(workbench: dict[str, Any], action: str) -> bool:
    return any(item["action"] == action and item["enabled"] for item in workbench["actions"])


def _node_states(workbench: dict[str, Any]) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (node["local_key"], node["status"], node["attempt_count"])
        for node in workbench["task_loop"]["nodes"]
    )


def _response_json(response: Any, expected_status: int = 200) -> dict[str, Any]:
    assert response.status_code == expected_status, response.text
    return dict(response.json())


def _prepare_confirmed_write(
    client: Any,
    case: WorkspaceCodingGoldenCase,
) -> tuple[str, dict[str, Any]]:
    source = _response_json(
        client.post(
            "/api/v1/conversation-turns",
            json={
                "message": case.goal,
                "workspace_coding": {
                    "project_path": case.case_id,
                    "ecosystem": case.ecosystem,
                    "test_path": case.test_path,
                },
            },
        ),
        201,
    )
    source_task_id = source["task"]["task_id"]
    proposal = source
    for _ in range(case.max_advances):
        exploration = proposal["workspace_coding_exploration"]
        if exploration["phase"] == "proposal_ready":
            break
        proposal = _response_json(
            client.post(f"/api/v1/tasks/{source_task_id}/workbench:advance")
        )
    else:
        enabled_actions = tuple(
            item["action"] for item in proposal["actions"] if item["enabled"]
        )
        pytest.fail(
            f"Golden case {case.case_id} did not produce an Explorer Proposal: "
            f"phase={proposal['workspace_coding_exploration']['phase']}, "
            f"stage={proposal['stage']}, actions={enabled_actions}"
        )
    exploration = proposal["workspace_coding_exploration"]
    assert tuple(item["relative_path"] for item in exploration["candidates"]) == (
        case.expect.candidate_paths
    )
    reader = _response_json(
        client.post(
            f"/api/v1/tasks/{source_task_id}/conversation-turns",
            json={"message": exploration["confirmation_text"]},
        ),
        201,
    )
    reader_task_id = reader["task"]["task_id"]
    for _ in range(case.max_advances):
        change = reader["workspace_coding_change"]
        if change is not None and change["phase"] == "proposal_ready":
            break
        reader = _response_json(
            client.post(f"/api/v1/tasks/{reader_task_id}/workbench:advance")
        )
    else:
        pytest.fail(f"Golden case {case.case_id} did not produce a Change Proposal")
    change = reader["workspace_coding_change"]
    assert tuple(item["relative_path"] for item in change["changes"]) == (
        case.expect.changed_paths
    )
    write = _response_json(
        client.post(
            f"/api/v1/tasks/{reader_task_id}/conversation-turns",
            json={"message": change["confirmation_text"]},
        ),
        201,
    )
    return str(write["task"]["task_id"]), write


def _advance_until_action(
    client: Any,
    task_id: str,
    workbench: dict[str, Any],
    action: str,
    max_advances: int,
) -> dict[str, Any]:
    for _ in range(max_advances):
        if _enabled(workbench, action) or workbench["stage"] == "delivered":
            return workbench
        workbench = _response_json(
            client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
        )
    pytest.fail(f"Task {task_id} did not expose {action}")


def _approve_patch(client: Any, task_id: str, workbench: dict[str, Any]) -> dict[str, Any]:
    assert _enabled(workbench, "commit_workspace_patch")
    return _response_json(
        client.post(
            f"/api/v1/tasks/{task_id}/workspace-patch:commit",
            json={
                "confirmation_digest": workbench["workspace_patch"]["confirmation_digest"]
            },
        )
    )


def _approve_git(client: Any, task_id: str, workbench: dict[str, Any]) -> dict[str, Any]:
    assert _enabled(workbench, "commit_workspace_git")
    return _response_json(
        client.post(
            f"/api/v1/tasks/{task_id}/workspace-git:commit",
            json={
                "confirmation_digest": workbench["workspace_git_commit"][
                    "confirmation_digest"
                ]
            },
        )
    )


def _assert_delivery(
    case: WorkspaceCodingGoldenCase,
    project: Path,
    workbench: dict[str, Any],
) -> None:
    assert workbench["stage"] == case.expect.final_stage
    delivery = workbench["task_loop"]["coding_delivery"]
    assert tuple(delivery["changed_files"]) == tuple(
        f"{case.case_id}/{path}" for path in case.expect.changed_paths
    )
    assert delivery["git_commit"]["push_disabled"] is case.expect.push_disabled
    assert case.expect.rollback_backups_retained is True
    status_lines = tuple(_run_git_output(project, "status", "--porcelain").splitlines())
    assert len(status_lines) == len(case.expect.changed_paths)
    for changed_path, status_line in zip(case.expect.changed_paths, status_lines, strict=True):
        changed = Path(changed_path)
        expected = re.compile(
            rf"^\?\? {re.escape(changed.parent.as_posix())}/\."
            rf"{re.escape(changed.name)}\.deskpilot-[0-9a-f]{{16}}\.backup$"
        )
        assert expected.fullmatch(status_line), status_line
    assert _run_git_output(project, "log", "-1", "--format=%s").startswith(
        "完成 DeskPilot 任务"
    )


def _run_git_output(project: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    completed = subprocess.run(  # noqa: S603 - fixed test-only Git arguments.
        (executable, *arguments),
        cwd=project,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        close_fds=True,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    return completed.stdout.strip()


def _in_process_client(tmp_path: Path, workspace_root: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'golden.db').as_posix()}",
        artifact_workspace_root=str(tmp_path / "artifacts"),
        conversation_workspace_root=str(workspace_root),
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "receipts.db"),
        workbench_runtime_enabled=False,
        model_gateway_policy=ModelGatewayPolicy(
            provider_pricing=(ModelProviderPricing(provider_id="fake-local"),),
        ),
    )
    app = create_app(
        settings,
        workspace_python_test_runtime=_RecordedGoldenPythonTests(),
        workspace_node_test_runtime=_RecordedGoldenNodeTests(),
    )
    with TestClient(app, headers=_headers()) as client:
        yield client


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _ApiProcess:
    def __init__(self, tmp_path: Path, workspace_root: Path) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._tmp_path = tmp_path
        self._workspace_root = workspace_root
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: Any | None = None

    def start(self) -> None:
        assert self._process is None
        assert self._log_stream is None
        environment = os.environ.copy()
        environment.update(
            {
                "DESKPILOT_DATABASE_URL": (
                    f"sqlite+aiosqlite:///{(self._tmp_path / 'api-process.db').as_posix()}"
                ),
                "DESKPILOT_ARTIFACT_WORKSPACE_ROOT": str(self._tmp_path / "api-artifacts"),
                "DESKPILOT_CONVERSATION_WORKSPACE_ROOT": str(self._workspace_root),
                "DESKPILOT_SESSION_TOKEN": TEST_TOKEN,
                "DESKPILOT_CORS_ORIGINS": json.dumps([TEST_ORIGIN]),
                "DESKPILOT_RUNNER_COMMIT_RECEIPT_DATABASE_PATH": str(
                    self._tmp_path / "api-receipts.db"
                ),
                "DESKPILOT_RUNNER_WORKER_RUNTIME_ROOT": str(
                    self._tmp_path / "api-worker-runtime"
                ),
                "DESKPILOT_RUNNER_APPCONTAINER_PROFILE_JOURNAL_PATH": str(
                    self._tmp_path / "api-appcontainer-profiles.json"
                ),
                "DESKPILOT_FAKE_STEP_DELAY_SECONDS": "0.001",
                "DESKPILOT_MODEL_GATEWAY_POLICY": json.dumps(
                    {"provider_pricing": [{"provider_id": "fake-local"}]}
                ),
                "DESKPILOT_RESEARCH_RUNTIME_ENABLED": "false",
                "DESKPILOT_WORKBENCH_RUNTIME_ENABLED": "false",
                "PYTHONUTF8": "1",
            }
        )
        self._log_stream = (self._tmp_path / "api-process.log").open("ab")
        self._process = subprocess.Popen(  # noqa: S603 - fixed local Uvicorn module.
            (
                sys.executable,
                "-m",
                "uvicorn",
                "deskpilot.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--no-access-log",
            ),
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
                raise AssertionError(f"API process exited with {self._process.returncode}")
            try:
                response = httpx.get(f"{self.base_url}/api/v1/health", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("API process did not become healthy")

    def client(self) -> httpx.Client:
        assert self._process is not None and self._process.poll() is None
        return httpx.Client(base_url=self.base_url, headers=_headers(), timeout=300)

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


def test_workspace_coding_golden_suite_is_strict_versioned_and_content_addressed(
    tmp_path: Path,
) -> None:
    bundle = WorkspaceCodingGoldenSuiteLoader().load()
    assert bundle.suite.schema_version == "deskpilot.workspace-coding-golden-suite.v1"
    assert bundle.suite.version == 1
    assert tuple(case.ecosystem for case in bundle.suite.cases) == ("python", "node")
    assert len(bundle.suite_digest) == 64

    source = BACKEND_ROOT / "src" / "deskpilot" / "evaluations" / "workspace_coding_v1.yaml"
    copied = tmp_path / "copied.yaml"
    copied.write_bytes(source.read_bytes())
    assert WorkspaceCodingGoldenSuiteLoader(copied).load().suite_digest == bundle.suite_digest

    extra = tmp_path / "extra.yaml"
    extra.write_text(source.read_text(encoding="utf-8") + "unexpected: true\n", encoding="utf-8")
    with pytest.raises(WorkspaceCodingEvaluationError):
        WorkspaceCodingGoldenSuiteLoader(extra).load()

    aliased = tmp_path / "aliased.yaml"
    aliased.write_text(
        source.read_text(encoding="utf-8").replace(
            "candidate_paths: [src/calculator.py, src/constants.py]",
            "candidate_paths: &paths [src/calculator.py, src/constants.py]",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError):
        WorkspaceCodingGoldenSuiteLoader(aliased).load()


def test_versioned_node_golden_task_reaches_delivery_through_public_api(tmp_path: Path) -> None:
    case = next(
        item
        for item in WorkspaceCodingGoldenSuiteLoader().load().suite.cases
        if item.ecosystem == "node"
    )
    workspace_root = tmp_path / "node-workspace"
    project = _materialize_case(workspace_root, case)
    clients = _in_process_client(tmp_path, workspace_root)
    client = next(clients)
    try:
        task_id, workbench = _prepare_confirmed_write(client, case)
        workbench = _advance_until_action(
            client, task_id, workbench, "commit_workspace_patch", case.max_advances
        )
        workbench = _approve_patch(client, task_id, workbench)
        workbench = _advance_until_action(
            client, task_id, workbench, "commit_workspace_git", case.max_advances
        )
        workbench = _approve_git(client, task_id, workbench)
        workbench = _advance_until_action(
            client, task_id, workbench, "never", case.max_advances
        )
        _assert_delivery(case, project, workbench)
    finally:
        clients.close()


@pytest.mark.skipif(os.name != "nt", reason="Strong Workspace Coding isolation is Windows-only")
def test_versioned_python_golden_task_recovers_patch_and_git_across_api_processes(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    case = next(
        item
        for item in WorkspaceCodingGoldenSuiteLoader().load().suite.cases
        if item.ecosystem == "python"
    )
    assert case.restart_checkpoints == ("patch_approval", "git_approval")
    # Keep the real Windows staging paths beneath the legacy MAX_PATH boundary.
    # The runtime deliberately fails closed when an OS path cannot be verified;
    # a short pytest-owned root keeps this recovery test focused on API restarts.
    tmp_path = tmp_path_factory.mktemp("g116b")
    workspace_root = tmp_path / "python-workspace"
    project = _materialize_case(workspace_root, case)
    api = _ApiProcess(tmp_path, workspace_root)
    try:
        api.start()
        with api.client() as client:
            task_id, workbench = _prepare_confirmed_write(client, case)
            patch_ready = _advance_until_action(
                client, task_id, workbench, "commit_workspace_patch", case.max_advances
            )
            patch_digest = patch_ready["workspace_patch"]["confirmation_digest"]
            patch_nodes = _node_states(patch_ready)
        api.stop()

        api.start()
        with api.client() as client:
            recovered_patch = _response_json(
                client.get(f"/api/v1/tasks/{task_id}/workbench")
            )
            assert recovered_patch["workspace_patch"]["confirmation_digest"] == patch_digest
            assert _node_states(recovered_patch) == patch_nodes
            workbench = _approve_patch(client, task_id, recovered_patch)
            git_ready = _advance_until_action(
                client, task_id, workbench, "commit_workspace_git", case.max_advances
            )
            git_digest = git_ready["workspace_git_commit"]["confirmation_digest"]
            git_nodes = _node_states(git_ready)
            patch_verified_attempts = {
                local_key: attempt_count
                for local_key, status, attempt_count in patch_nodes
                if status == "verified"
            }
            assert {
                local_key: attempt_count
                for local_key, status, attempt_count in git_nodes
                if local_key in patch_verified_attempts and status == "verified"
            } == patch_verified_attempts
        api.stop()

        api.start()
        with api.client() as client:
            recovered_git = _response_json(
                client.get(f"/api/v1/tasks/{task_id}/workbench")
            )
            assert recovered_git["workspace_git_commit"]["confirmation_digest"] == git_digest
            assert _node_states(recovered_git) == git_nodes
            workbench = _approve_git(client, task_id, recovered_git)
            delivered = _advance_until_action(
                client, task_id, workbench, "never", case.max_advances
            )
            git_verified_attempts = {
                local_key: attempt_count
                for local_key, status, attempt_count in git_nodes
                if status == "verified"
            }
            assert {
                local_key: attempt_count
                for local_key, status, attempt_count in _node_states(delivered)
                if local_key in git_verified_attempts and status == "verified"
            } == git_verified_attempts
            _assert_delivery(case, project, delivered)
    finally:
        api.stop()
