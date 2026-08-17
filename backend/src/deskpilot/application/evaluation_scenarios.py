"""Fixed, isolated scenarios available to packaged golden evaluation suites."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

from deskpilot.api.routes.websocket import _stream_live_events
from deskpilot.application.knowledge_base import LocalKnowledgeBase
from deskpilot.application.mcp_stdio import (
    McpBundleRejectedError,
    McpStdioError,
    McpStdioHost,
)
from deskpilot.application.model_gateway import ModelGateway, ModelRateLimitError
from deskpilot.application.runner_client import RunnerClientError, RunnerExitedError
from deskpilot.application.runner_supervisor import (
    RunnerClientPort,
    RunnerSupervisor,
)
from deskpilot.domain.evaluations import GoldenCase
from deskpilot.domain.model_contracts import (
    ModelExecutionBudget,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from deskpilot.domain.model_routing import ModelGatewayPolicy
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.infrastructure.database import Database
from deskpilot.mcp_servers.readonly_text_server import TOOL_NAME
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.runner.ipc_protocol import ToolCallResult


class _RateLimitedProvider(FakeModelProvider):
    def __init__(self, failures: int) -> None:
        super().__init__(provider_id="eval-rate-limited")
        self.failures = failures
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelRateLimitError(
                "Injected HTTP 429",
                provider_id=self.descriptor.provider_id,
                retry_after_seconds=0,
            )
        return await super().complete(request)


class _EvaluationRunnerClient:
    def __init__(self, runner_id: str) -> None:
        self._runner_id = runner_id
        self._running = False
        self._failure: asyncio.Future[RunnerClientError] = (
            asyncio.get_running_loop().create_future()
        )

    @property
    def runner_id(self) -> str | None:
        return self._runner_id if self._running else None

    @property
    def process_id(self) -> int | None:
        return int(self._runner_id.rsplit("-", 1)[-1]) if self._running else None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def wait_for_failure(self) -> RunnerClientError:
        return await self._failure

    def crash(self) -> None:
        self._running = False
        if not self._failure.done():
            self._failure.set_result(RunnerExitedError("Injected Runner crash"))

    async def cancel_call(self, call_id: str, reason: str) -> None:
        del call_id, reason

    async def call_tool(
        self,
        *,
        task_id: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        arguments: dict[str, object],
        actor: str,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        expected_resource_versions: dict[str, str] | None = None,
        authorization: ToolAuthorizationGrant,
        progress_callback: Any = None,
    ) -> ToolCallResult:
        del (
            task_id,
            step_id,
            tool_name,
            tool_version,
            arguments,
            actor,
            call_id,
            idempotency_key,
            expected_resource_versions,
            authorization,
            progress_callback,
        )
        raise RunnerExitedError("Evaluation client does not execute tools")

    async def get_commit_receipt(
        self,
        call_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ToolCommitReceipt | None:
        del call_id, timeout_seconds
        return None


class _DisconnectingWebSocket:
    def __init__(self) -> None:
        self.receives = 0
        self.sent = 0
        self.closed = 0

    async def receive(self) -> dict[str, str]:
        self.receives += 1
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload: object) -> None:
        del payload
        self.sent += 1

    async def close(self, code: int = 1000) -> None:
        del code
        self.closed += 1


class EvaluationScenarioRunner:
    """Dispatch only the finite scenario vocabulary accepted by ``GoldenCase``."""

    def __init__(self) -> None:
        self._package_root = Path(__file__).parents[1]

    async def execute(self, case: GoldenCase) -> dict[str, Any]:
        if case.scenario == "mcp.text_metrics":
            return await self._text_metrics(case.input)
        if case.scenario == "mcp.invalid_input_rejected":
            return await self._invalid_mcp_input(case.input)
        if case.scenario == "security.mcp_bundle_tamper":
            return await asyncio.to_thread(self._bundle_tamper)
        if case.scenario == "knowledge.source_stale":
            return await self._knowledge_stale(str(case.input["query"]))
        if case.scenario == "fault.model_rate_limit":
            return await self._model_rate_limit(case.input)
        if case.scenario == "fault.runner_crash_recovery":
            return await self._runner_crash_recovery(int(case.input["crashes"]))
        if case.scenario == "fault.websocket_disconnect":
            return await self._websocket_disconnect()
        return await self._mcp_protocol_anomaly(str(case.input["mode"]))

    async def _text_metrics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await McpStdioHost(self._readonly_server()).invoke(TOOL_NAME, arguments)
        return result.structured_content

    async def _invalid_mcp_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        arguments = cast(dict[str, Any], payload["arguments"])
        try:
            await McpStdioHost(self._readonly_server()).invoke(TOOL_NAME, arguments)
        except McpStdioError as error:
            return {"error_code": error.code, "rejected": True}
        return {"error_code": "NOT_REJECTED", "rejected": False}

    def _bundle_tamper(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="deskpilot-eval-bundle-") as directory:
            copied = Path(directory) / "server.py"
            shutil.copyfile(self._readonly_server(), copied)
            host = McpStdioHost(copied)
            copied.write_bytes(copied.read_bytes() + b"\n# changed\n")
            try:
                asyncio.run(host.invoke(TOOL_NAME, {"text": "must reject"}))
            except McpBundleRejectedError as error:
                return {"error_code": error.code, "rejected": True}
        return {"error_code": "NOT_REJECTED", "rejected": False}

    async def _knowledge_stale(self, query: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="deskpilot-eval-knowledge-") as directory:
            root = Path(directory)
            source = root / "source.md"
            source.write_text("recovery evidence is local\n", encoding="utf-8")
            database = Database(f"sqlite+aiosqlite:///{(root / 'eval.db').as_posix()}")
            try:
                await database.migrate()
                knowledge = LocalKnowledgeBase(database)
                await knowledge.import_file(str(source))
                initial = await knowledge.search(query, 10)
                source.write_text("changed content\n", encoding="utf-8")
                stale = await knowledge.search(query, 10)
                return {
                    "initial_citations": len(initial.citations),
                    "stale_sources": len(stale.stale_source_ids),
                    "leaked_old_citations": len(stale.citations),
                }
            finally:
                await database.dispose()

    @staticmethod
    async def _model_rate_limit(payload: dict[str, Any]) -> dict[str, Any]:
        failures = int(payload["failures"])
        attempts = int(payload["max_attempts"])
        provider = _RateLimitedProvider(failures)
        gateway = ModelGateway(
            default_provider_id=provider.descriptor.provider_id,
            policy=ModelGatewayPolicy(
                default_max_attempts=attempts,
                default_retry_delay_budget_seconds=0,
                retry_base_delay_seconds=0,
                retry_max_delay_seconds=0,
                circuit_failure_threshold=100,
            ),
        )
        gateway.register(provider)
        request = ModelRequest(
            request_id="eval-rate-limit",
            task_id="eval-rate-limit",
            role=ModelRole.INTENT,
            messages=(ModelMessage(role="user", content="fixed evaluation request"),),
            privacy_mode="local_only",
            timeout_seconds=2,
            execution_budget=ModelExecutionBudget(
                max_attempts=attempts,
                max_retry_delay_seconds=0,
            ),
        )
        try:
            await gateway.complete(request)
        except ModelRateLimitError as error:
            return {
                "attempts": provider.calls,
                "recovered": False,
                "error_code": error.code,
            }
        return {"attempts": provider.calls, "recovered": True, "error_code": None}

    @staticmethod
    async def _runner_crash_recovery(crashes: int) -> dict[str, Any]:
        clients = [_EvaluationRunnerClient(f"runner-{index}") for index in range(1, crashes + 2)]
        next_client = 0

        def factory() -> RunnerClientPort:
            nonlocal next_client
            client = clients[next_client]
            next_client += 1
            return client

        supervisor = RunnerSupervisor(
            client_factory=factory,
            restart_base_delay_seconds=0,
            restart_max_delay_seconds=0,
            circuit_failure_threshold=10,
            circuit_recovery_timeout_seconds=0,
            stable_window_seconds=60,
        )
        try:
            await supervisor.start()
            for index in range(crashes):
                clients[index].crash()
                target_generation = index + 2
                for _ in range(500):
                    if supervisor.snapshot().generation >= target_generation:
                        break
                    await asyncio.sleep(0)
                else:
                    raise RuntimeError("Runner recovery did not advance its generation")
            snapshot = supervisor.snapshot()
            return {
                "crashes_observed": snapshot.total_failures,
                "final_generation": snapshot.generation,
                "last_failure_code": snapshot.last_failure_code,
                "recovered": supervisor.is_running,
            }
        finally:
            await supervisor.stop()

    @staticmethod
    async def _websocket_disconnect() -> dict[str, Any]:
        socket = _DisconnectingWebSocket()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        await _stream_live_events(cast(Any, socket), queue, 0)
        return {
            "disconnect_handled": True,
            "receive_count": socket.receives,
            "events_sent_after_disconnect": socket.sent,
        }

    async def _mcp_protocol_anomaly(self, mode: str) -> dict[str, Any]:
        fixture_names = {
            "bad_version": "mcp_bad_version_server.py",
            "bad_id": "mcp_bad_id_server.py",
            "invalid_json": "mcp_invalid_json_server.py",
        }
        fixture_name = fixture_names.get(mode)
        if fixture_name is None:
            raise ValueError("Unknown fixed MCP anomaly mode")
        script = self._package_root / "evaluations" / "fixtures" / fixture_name
        try:
            await McpStdioHost(script).invoke(TOOL_NAME, {"text": "protocol probe"})
        except McpStdioError as error:
            return {"error_code": error.code, "rejected": True, "mode": mode}
        return {"error_code": "NOT_REJECTED", "rejected": False, "mode": mode}

    def _readonly_server(self) -> Path:
        return self._package_root / "mcp_servers" / "readonly_text_server.py"
