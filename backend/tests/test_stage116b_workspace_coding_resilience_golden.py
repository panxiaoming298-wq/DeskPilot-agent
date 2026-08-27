from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from deskpilot.application.capability_execution_runtime import CapabilityExecutionRuntime
from deskpilot.application.workspace_coding_evaluation import (
    WorkspaceCodingEvaluationError,
    WorkspaceCodingGoldenResilienceSuiteLoader,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ModelPlannerNodeBindingRecord,
    TaskExecutionNodeRecord,
    TaskLoopNodeAttemptRecord,
)

BACKEND_ROOT = Path(__file__).parents[1]
TEST_TOKEN = "stage116b-resilience-token-00000000"
TEST_ORIGIN = "http://stage116b-resilience.local"


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


def _read_calls(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return tuple(str(item) for item in payload)


class _CommandApiProcess:
    def __init__(
        self,
        tmp_path: Path,
        workspace_root: Path,
        *,
        project_path: str,
        profile_ids: tuple[str, ...],
        fault_mode: str,
    ) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.database_url = (
            f"sqlite+aiosqlite:///{(tmp_path / 'command-api.db').as_posix()}"
        )
        self.command_calls_path = tmp_path / "command-calls.json"
        self.provider_calls_path = tmp_path / "provider-calls.json"
        self.command_started_path = tmp_path / "command-started.txt"
        self.profile_drift = "none"
        self._tmp_path = tmp_path
        self._workspace_root = workspace_root
        self._project_path = project_path
        self._profile_ids = profile_ids
        self._fault_mode = fault_mode
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: Any | None = None

    def start(self) -> None:
        assert self._process is None
        environment = os.environ.copy()
        environment.update(
            {
                "DESKPILOT_DATABASE_URL": self.database_url,
                "DESKPILOT_ARTIFACT_WORKSPACE_ROOT": str(
                    self._tmp_path / "command-artifacts"
                ),
                "DESKPILOT_CONVERSATION_WORKSPACE_ROOT": str(self._workspace_root),
                "DESKPILOT_SESSION_TOKEN": TEST_TOKEN,
                "DESKPILOT_CORS_ORIGINS": json.dumps([TEST_ORIGIN]),
                "DESKPILOT_RUNNER_COMMIT_RECEIPT_DATABASE_PATH": str(
                    self._tmp_path / "command-receipts.db"
                ),
                "DESKPILOT_RUNNER_WORKER_RUNTIME_ROOT": str(
                    self._tmp_path / "command-worker-runtime"
                ),
                "DESKPILOT_RUNNER_APPCONTAINER_PROFILE_JOURNAL_PATH": str(
                    self._tmp_path / "command-appcontainer-profiles.json"
                ),
                "DESKPILOT_MODEL_GATEWAY_POLICY": json.dumps(
                    {"provider_pricing": [{"provider_id": "fake-local"}]}
                ),
                "DESKPILOT_RESEARCH_RUNTIME_ENABLED": "false",
                "DESKPILOT_WORKBENCH_RUNTIME_ENABLED": "false",
                "DESKPILOT_FAKE_STEP_DELAY_SECONDS": "0.001",
                "DESKPILOT_GOLDEN_API_PORT": str(self.port),
                "DESKPILOT_GOLDEN_COMMAND_PROJECT": self._project_path,
                "DESKPILOT_GOLDEN_COMMAND_PROFILES": ",".join(self._profile_ids),
                "DESKPILOT_GOLDEN_COMMAND_FAULT_MODE": self._fault_mode,
                "DESKPILOT_GOLDEN_PROFILE_DRIFT": self.profile_drift,
                "DESKPILOT_GOLDEN_COMMAND_CALLS_PATH": str(self.command_calls_path),
                "DESKPILOT_GOLDEN_PROVIDER_CALLS_PATH": str(self.provider_calls_path),
                "DESKPILOT_GOLDEN_COMMAND_STARTED_PATH": str(
                    self.command_started_path
                ),
                "PYTHONUTF8": "1",
            }
        )
        self._log_stream = (self._tmp_path / "command-api.log").open("ab")
        self._process = subprocess.Popen(  # noqa: S603 - fixed test-only module.
            (
                sys.executable,
                "-m",
                "tests.fixtures.workspace_command_fault_server",
            ),
            cwd=BACKEND_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                log = (self._tmp_path / "command-api.log").read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                raise AssertionError(
                    f"Command API exited with {self._process.returncode}: {log[-4000:]}"
                )
            try:
                response = httpx.get(f"{self.base_url}/api/v1/health", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("Command API process did not become healthy")

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
            self._close_process()

    def crash(self) -> None:
        assert self._process is not None
        self._process.kill()
        self._process.wait(timeout=10)
        self._close_process()

    def _close_process(self) -> None:
        self._process = None
        assert self._log_stream is not None
        self._log_stream.close()
        self._log_stream = None


def _command_nodes(workbench: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    task_loop = workbench.get("task_loop")
    if not isinstance(task_loop, dict):
        return ()
    nodes = task_loop.get("nodes")
    if not isinstance(nodes, list):
        return ()
    result = tuple(
        item
        for item in nodes
        if isinstance(item, dict) and item.get("command_plan_id") is not None
    )
    return tuple(sorted(result, key=lambda item: int(item["command_step_sequence"])))


def _command_state(workbench: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item["command_profile_id"],
            item["status"],
            item["attempt_count"],
            item["verified_result_present"],
            item["verified_failure_result_count"],
        )
        for item in _command_nodes(workbench)
    )


def _create_ready_command_task(
    client: httpx.Client,
    *,
    max_advances: int,
) -> tuple[str, dict[str, Any]]:
    workbench = _response_json(
        client.post(
            "/api/v1/conversation-turns",
            json={"message": "运行 backend 的固定 Ruff 与 mypy 检查"},
        ),
        201,
    )
    task_id = str(workbench["task"]["task_id"])
    for _ in range(max_advances):
        nodes = _command_nodes(workbench)
        if nodes and nodes[0]["status"] == "ready":
            return task_id, workbench
        workbench = _response_json(
            client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
        )
    pytest.fail("Command golden task did not reach its first ready Profile")


def _advance_to_execution_status(
    client: httpx.Client,
    task_id: str,
    workbench: dict[str, Any],
    status: str,
    max_advances: int,
) -> dict[str, Any]:
    for _ in range(max_advances):
        task_loop = workbench.get("task_loop")
        if isinstance(task_loop, dict) and task_loop.get("execution_status") == status:
            return workbench
        workbench = _response_json(
            client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
        )
    pytest.fail(f"Command golden task did not reach execution status {status}")


async def _tamper_ready_command_binding(database_url: str, scope: str) -> None:
    database = Database(database_url)
    try:
        async with database.session() as session, session.begin():
            binding = await session.scalar(
                select(ModelPlannerNodeBindingRecord)
                .where(ModelPlannerNodeBindingRecord.step_ordinal == 1)
                .limit(1)
            )
            assert binding is not None
            if scope == "node":
                binding.composite_node_spec_digest = "0" * 64
            else:
                manifest = dict(binding.bound_input_manifest)
                manifest["project_path"] = "backend-drifted"
                binding.bound_input_manifest = manifest
    finally:
        await database.dispose()


async def _expire_running_attempt(database_url: str) -> None:
    database = Database(database_url)
    try:
        now = datetime.now(UTC)
        expired_at = now - timedelta(seconds=1)
        async with database.session() as session, session.begin():
            record = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(TaskLoopNodeAttemptRecord.status == "running")
                .limit(1)
            )
            assert record is not None
            attempt = CapabilityExecutionRuntime._attempt_from_record(record)  # noqa: SLF001
            replacement = CapabilityExecutionRuntime._replace_attempt(  # noqa: SLF001
                attempt,
                revision=attempt.revision + 1,
                claim_expires_at=expired_at,
                updated_at=now,
            )
            CapabilityExecutionRuntime._apply_attempt(record, replacement)  # noqa: SLF001
            node = await session.get(TaskExecutionNodeRecord, attempt.node_id)
            assert node is not None
            node.claim_expires_at = expired_at
            node.claim_heartbeat_at = expired_at
            node.revision += 1
            node.updated_at = now
    finally:
        await database.dispose()


async def _attempt_outcomes(database_url: str) -> tuple[tuple[str, str | None], ...]:
    database = Database(database_url)
    try:
        async with database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopNodeAttemptRecord).order_by(
                            TaskLoopNodeAttemptRecord.created_at
                        )
                    )
                ).all()
            )
        return tuple((record.status, record.error_code) for record in records)
    finally:
        await database.dispose()


def _scenario() -> Any:
    return WorkspaceCodingGoldenResilienceSuiteLoader().load().suite.scenario


def _prepare_command_workspace(root: Path, project_path: str) -> Path:
    project = root / project_path
    project.mkdir(parents=True)
    (project / "sample.py").write_text("VALUE: int = 1\n", encoding="utf-8")
    return project


def test_workspace_coding_resilience_suite_is_strict_and_cross_digest_bound(
    tmp_path: Path,
) -> None:
    bundle = WorkspaceCodingGoldenResilienceSuiteLoader().load()
    assert bundle.suite.schema_version == (
        "deskpilot.workspace-coding-resilience-suite.v1"
    )
    assert bundle.suite.workspace_suite_digest == bundle.workspace.suite_digest
    assert bundle.suite.scenario.proof_drift_scopes == (
        "catalog",
        "profile",
        "project_path",
        "node",
        "input",
    )
    source = (
        BACKEND_ROOT
        / "src"
        / "deskpilot"
        / "evaluations"
        / "workspace_coding_resilience_v1.yaml"
    )
    drifted = tmp_path / "resilience-drifted.yaml"
    drifted.write_text(
            source.read_text(encoding="utf-8").replace(
                bundle.workspace.suite_digest,
                "f" * 64,
            ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceCodingEvaluationError, match="crossed"):
        WorkspaceCodingGoldenResilienceSuiteLoader(drifted).load()


def test_command_plan_failure_repair_and_bounded_restart_soak_use_public_api(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    workspace_root = tmp_path / "repair-workspace"
    _prepare_command_workspace(workspace_root, scenario.command_project_path)
    api = _CommandApiProcess(
        tmp_path,
        workspace_root,
        project_path=scenario.command_project_path,
        profile_ids=scenario.command_profile_ids,
        fault_mode="fail_once",
    )
    try:
        api.start()
        with api.client() as client:
            task_id, workbench = _create_ready_command_task(
                client,
                max_advances=scenario.max_advances,
            )
            failed = _response_json(
                client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            )
            failed_state = _command_state(failed)
            assert failed_state == (
                (scenario.command_profile_ids[0], "failed", 1, False, 1),
                (scenario.command_profile_ids[1], "pending", 0, False, 0),
            )
        api.stop()

        assert _read_calls(api.command_calls_path) == (
            scenario.command_profile_ids[0],
        )
        assert len(_read_calls(api.provider_calls_path)) == 1
        for _ in range(scenario.stable_restart_cycles):
            api.start()
            with api.client() as client:
                recovered = _response_json(
                    client.get(f"/api/v1/tasks/{task_id}/workbench")
                )
                assert _command_state(recovered) == failed_state
            api.stop()
            assert _read_calls(api.command_calls_path) == (
                scenario.command_profile_ids[0],
            )
            assert len(_read_calls(api.provider_calls_path)) == 1

        api.start()
        with api.client() as client:
            repaired = _response_json(
                client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            )
            assert repaired["task_loop"]["repair_count"] == 1
            assert _command_state(repaired)[0][1:3] == ("ready", 1)
        api.stop()

        api.start()
        with api.client() as client:
            first_passed = _response_json(
                client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            )
            first_state = _command_state(first_passed)
            assert first_state[0] == (
                scenario.command_profile_ids[0],
                "verified",
                scenario.expected_repaired_attempt,
                True,
                scenario.known_failure_count,
            )
            assert first_state[1][1] == "ready"
            second_passed = _response_json(
                client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            )
            delivered = _advance_to_execution_status(
                client,
                task_id,
                second_passed,
                "succeeded",
                scenario.max_advances,
            )
            assert _command_state(delivered)[1][1:4] == ("verified", 1, True)
        assert _read_calls(api.command_calls_path) == (
            scenario.command_profile_ids[0],
            scenario.command_profile_ids[0],
            scenario.command_profile_ids[1],
        )
        assert len(_read_calls(api.provider_calls_path)) == 1
    finally:
        api.stop()


@pytest.mark.parametrize("scope", ("catalog", "profile", "project_path", "node", "input"))
def test_command_plan_proof_drift_is_rejected_before_runtime(
    tmp_path: Path,
    scope: str,
) -> None:
    scenario = _scenario()
    workspace_root = tmp_path / f"{scope}-workspace"
    project = _prepare_command_workspace(
        workspace_root,
        scenario.command_project_path,
    )
    api = _CommandApiProcess(
        tmp_path,
        workspace_root,
        project_path=scenario.command_project_path,
        profile_ids=scenario.command_profile_ids,
        fault_mode="pass",
    )
    drifted_project = workspace_root / "backend-drifted"
    try:
        api.start()
        with api.client() as client:
            task_id, ready = _create_ready_command_task(
                client,
                max_advances=scenario.max_advances,
            )
            ready_state = _command_state(ready)
        api.stop()

        if scope in {"catalog", "profile"}:
            api.profile_drift = scope
        elif scope == "project_path":
            project.rename(drifted_project)
        else:
            asyncio.run(_tamper_ready_command_binding(api.database_url, scope))

        api.start()
        with api.client() as client:
            rejected = client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            assert rejected.status_code == 409, rejected.text
        assert _read_calls(api.command_calls_path) == ()
        assert len(_read_calls(api.provider_calls_path)) == 1
        if scope in {"catalog", "profile", "project_path"}:
            with api.client() as client:
                observed = client.get(f"/api/v1/tasks/{task_id}/workbench")
                if observed.status_code == 200:
                    assert _command_state(_response_json(observed)) == ready_state
    finally:
        api.stop()
        if drifted_project.exists() and not project.exists():
            drifted_project.rename(project)


def test_interrupted_command_becomes_outcome_unknown_without_replay(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    workspace_root = tmp_path / "unknown-workspace"
    _prepare_command_workspace(workspace_root, scenario.command_project_path)
    api = _CommandApiProcess(
        tmp_path,
        workspace_root,
        project_path=scenario.command_project_path,
        profile_ids=scenario.command_profile_ids,
        fault_mode="block_once",
    )
    request_result: dict[str, object] = {}
    try:
        api.start()
        with api.client() as client:
            task_id, _ready = _create_ready_command_task(
                client,
                max_advances=scenario.max_advances,
            )

        def dispatch() -> None:
            try:
                with api.client() as client:
                    request_result["response"] = client.post(
                        f"/api/v1/tasks/{task_id}/workbench:advance"
                    )
            except httpx.HTTPError as error:
                request_result["error"] = error

        thread = threading.Thread(target=dispatch, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not api.command_started_path.exists():
            time.sleep(0.05)
        assert api.command_started_path.exists()
        assert _read_calls(api.command_calls_path) == (
            scenario.interrupted_profile_id,
        )
        api.crash()
        thread.join(timeout=10)
        assert not thread.is_alive()

        asyncio.run(_expire_running_attempt(api.database_url))
        api.start()
        with api.client() as client:
            failed = _response_json(
                client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            )
            assert failed["task_loop"]["execution_status"] == "failed"
            assert _command_state(failed)[0][1:3] == ("failed", 1)
        assert _read_calls(api.command_calls_path) == (
            scenario.interrupted_profile_id,
        )
        outcomes = asyncio.run(_attempt_outcomes(api.database_url))
        assert ("outcome_unknown", scenario.expected_unknown_error_code) in outcomes

        terminal_state = _command_state(failed)
        for _ in range(scenario.stable_restart_cycles):
            api.stop()
            api.start()
            with api.client() as client:
                recovered = _response_json(
                    client.get(f"/api/v1/tasks/{task_id}/workbench")
                )
                assert recovered["task_loop"]["execution_status"] == "failed"
                assert _command_state(recovered) == terminal_state
            assert _read_calls(api.command_calls_path) == (
                scenario.interrupted_profile_id,
            )
            assert len(_read_calls(api.provider_calls_path)) == 1
    finally:
        api.stop()
