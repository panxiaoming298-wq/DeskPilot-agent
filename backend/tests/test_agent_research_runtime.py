from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_execution_runtime import AgentLeaseRejectedError
from deskpilot.application.plan_compiler import (
    research_to_html_contract,
    research_to_html_draft,
)
from deskpilot.application.web_research import PageReadRejectedError, SafePageReader
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.research import (
    PageSnapshot,
    SearchHit,
    SearchProviderResult,
    SearchRequest,
)
from deskpilot.infrastructure.models import (
    AgentDecisionRecord,
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentObservationRecord,
    AgentResultRecord,
    ModelDispatchAttemptRecord,
    ResearchSearchCallRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskRecord,
)
from deskpilot.main import create_app
from deskpilot.model_providers.fake import FakeModelProvider

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "stage-70-session-token-with-at-least-32-chars"


def _hit(rank: int, hostname: str) -> SearchHit:
    hit_id = f"sht_{sha256_digest({'rank': rank, 'hostname': hostname})}"
    material = {
        "hit_id": hit_id,
        "rank": rank,
        "title": f"Source {rank}",
        "url": f"https://{hostname}/article",
        "snippet": "Public research snippet.",
        "origin": "external_untrusted",
    }
    return SearchHit.model_validate({**material, "hit_digest": sha256_digest(material)})


class FakeSearchProvider:
    provider_id = "test-search"

    async def search(self, request: SearchRequest) -> SearchProviderResult:
        assert request.max_results >= 2
        return SearchProviderResult(
            provider_id=self.provider_id,
            hits=(_hit(1, "one.example"), _hit(2, "two.example")),
        )


class WrongBindingModelProvider(FakeModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        if request.metadata.get("agent_loop_phase") != "request_route":
            return response
        assert response.structured_output is not None
        return response.model_copy(
            update={
                "structured_output": {
                    **response.structured_output,
                    "route_binding_id": f"rbn_{'0' * 64}",
                }
            }
        )


class FakePageReader(SafePageReader):
    async def read(
        self,
        *,
        task_id: str,
        research_session_id: str,
        hit: SearchHit,
    ) -> PageSnapshot:
        text = f"Controlled public evidence from {hit.url}."
        fetched_at = datetime.now(UTC)
        content_digest = sha256_digest({"text": text})
        identity = {
            "task_id": task_id,
            "research_session_id": research_session_id,
            "hit_id": hit.hit_id,
        }
        snapshot_id = f"snp_{sha256_digest(identity)}"
        material = {
            "schema_version": "deskpilot.page-snapshot.v1",
            "page_snapshot_id": snapshot_id,
            "task_id": task_id,
            "research_session_id": research_session_id,
            "search_hit_id": hit.hit_id,
            "requested_url": hit.url,
            "final_url": hit.url,
            "status_code": 200,
            "media_type": "text/html",
            "title": hit.title,
            "extracted_text": text,
            "content_digest": content_digest,
            "extractor_version": "deskpilot.html-text.v1",
            "origin": "external_untrusted",
            "fetched_at": fetched_at,
        }
        return PageSnapshot.model_validate({**material, "snapshot_digest": sha256_digest(material)})


@pytest.fixture
def research_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'research.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "receipts.db"),
        research_runtime_enabled=True,
        model_gateway_policy=ModelGatewayPolicy(
            provider_pricing=(ModelProviderPricing(provider_id="fake-local"),),
        ),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    app = create_app(
        settings,
        search_provider=FakeSearchProvider(),
        page_reader=FakePageReader(),
    )
    with TestClient(app, headers=headers) as client:
        yield client


def _activate_research_task(client: TestClient, suffix: str) -> str:
    app = cast(FastAPI, client.app)
    assert client.portal is not None
    task_id = f"tsk_{suffix * 32}"

    async def insert_task() -> None:
        async with app.state.database.session() as session, session.begin():
            session.add(
                TaskRecord(
                    task_id=task_id,
                    goal="研究公开主题并形成带引用的事实",
                    status="submitted",
                    privacy_mode="balanced",
                    constraints=[],
                )
            )

    client.portal.call(insert_task)
    contract = research_to_html_contract(task_id, app.state.capability_catalog)
    draft = research_to_html_draft(task_id)
    client.portal.call(app.state.plan_compilation_service.activate, contract, draft)
    return task_id


def test_research_front_half_persists_candidates_and_stops_at_verification(
    research_client: TestClient,
) -> None:
    app = cast(FastAPI, research_client.app)
    assert research_client.portal is not None
    task_id = _activate_research_task(research_client, "7")
    capabilities = research_client.get("/api/v1/capabilities").json()["capabilities"]
    research_versions = [
        item for item in capabilities if item["capability_id"] == "research.read.v1"
    ]
    assert [(item["version"], item["runtime_enabled"]) for item in research_versions] == [
        ("1.0.0", False),
        ("1.1.0", True),
    ]

    started = research_client.post(f"/api/v1/tasks/{task_id}/execution-runs")
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    nodes = {item["local_key"]: item for item in started.json()["nodes"]}
    assert nodes["research"]["status"] == "ready"
    assert nodes["research"]["runtime_enabled"] is True
    assert nodes["build_html"]["status"] == "pending"

    researched = research_client.post(f"/api/v1/execution-runs/{run_id}/research:run")
    assert researched.status_code == 200, researched.text
    body = researched.json()
    assert body["status"] == "awaiting_verification"
    assert len(body["page_snapshots"]) == 2
    assert body["claims"] and body["citations"]
    assert {item["status"] for item in body["claims"]} == {"awaiting_verification"}
    assert {item["status"] for item in body["citations"]} == {"awaiting_verification"}

    run = research_client.get(f"/api/v1/execution-runs/{run_id}").json()
    current = {item["local_key"]: item for item in run["nodes"]}
    assert run["status"] == "awaiting_verification"
    assert current["research"]["status"] == "awaiting_verification"
    assert current["build_html"]["status"] == "pending"

    async def inspect_truth() -> tuple[str, tuple[str, ...], tuple[str, ...], str, str]:
        async with app.state.database.session() as session:
            task = await session.get(TaskRecord, task_id)
            turns = tuple(
                (
                    await session.scalars(
                        select(AgentModelTurnRecord).order_by(AgentModelTurnRecord.turn_no)
                    )
                ).all()
            )
            decisions = tuple(
                (
                    await session.scalars(
                        select(AgentDecisionRecord).order_by(AgentDecisionRecord.created_at)
                    )
                ).all()
            )
            attempts = tuple((await session.scalars(select(ModelDispatchAttemptRecord))).all())
            observation = await session.scalar(select(AgentObservationRecord))
            result = await session.scalar(select(AgentResultRecord))
            search = await session.scalar(select(ResearchSearchCallRecord))
            assert task is not None
            assert len(turns) == 2
            assert len(attempts) == 2
            assert observation is not None
            assert result is not None
            assert search is not None
            return (
                task.status,
                tuple(item.status for item in turns),
                tuple(item.kind for item in decisions),
                observation.observation_digest,
                result.manifest["disposition"],
            )

    task_status, turn_statuses, decision_kinds, observation_digest, disposition = (
        research_client.portal.call(inspect_truth)
    )
    assert task_status == "submitted"
    assert turn_statuses == ("succeeded", "succeeded")
    assert decision_kinds == ("request_route", "submit_result")
    assert len(observation_digest) == 64
    assert disposition == "candidate"

    projected = research_client.get(f"/api/v1/execution-runs/{run_id}")
    assert projected.status_code == 200, projected.text
    model_turns = projected.json()["model_turns"]
    assert [item["decision_kind"] for item in model_turns] == [
        "request_route",
        "submit_result",
    ]
    assert model_turns[0]["observation_digest"] == observation_digest

    async def tamper_observation() -> None:
        async with app.state.database.session() as session, session.begin():
            observation = await session.scalar(select(AgentObservationRecord))
            assert observation is not None
            observation.projection = {"page_snapshots": []}

    research_client.portal.call(tamper_observation)
    rejected = research_client.get(f"/api/v1/execution-runs/{run_id}")
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "AGENT_RUNTIME_PROOF_REJECTED"


def test_stale_fencing_token_cannot_start_invocation(
    research_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = cast(FastAPI, research_client.app)
    task_id = _activate_research_task(research_client, "8")
    assert research_client.portal is not None
    run = research_client.portal.call(app.state.agent_execution_runtime.start, task_id)
    claimed = research_client.portal.call(
        app.state.agent_execution_runtime.claim_next,
        run.run_id,
        "worker-a",
    )
    assert claimed is not None
    locked_entities: list[str] = []
    original_scalar = AsyncSession.scalar

    async def record_locked_entity(
        session: AsyncSession, statement: object, *args: object, **kwargs: object
    ) -> object:
        if getattr(statement, "_for_update_arg", None) is not None:
            descriptions = getattr(statement, "column_descriptions", ())
            if descriptions:
                entity = descriptions[0].get("entity")
                if entity is not None:
                    locked_entities.append(str(entity.__name__))
        return await original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", record_locked_entity)
    with pytest.raises(AgentLeaseRejectedError):
        research_client.portal.call(
            app.state.agent_execution_runtime.start_invocation,
            claimed.invocation.invocation_id,
            "worker-a",
            claimed.claim_fencing_token + 1,
        )
    assert locked_entities == [
        "TaskExecutionRunRecord",
        "TaskExecutionNodeRecord",
        "AgentInvocationRecord",
    ]

    async def expire_and_retry() -> tuple[object, ...]:
        async with app.state.database.session() as session, session.begin():
            node = await session.get(TaskExecutionNodeRecord, claimed.invocation.node_id)
            assert node is not None
            assert node.budget["retries"] == 2
            node.claim_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        retried = await app.state.agent_execution_runtime.claim_next(
            run.run_id, "worker-b"
        )
        assert retried is not None
        async with app.state.database.session() as session:
            first = await session.get(
                AgentInvocationRecord, claimed.invocation.invocation_id
            )
            current_run = await session.get(TaskExecutionRunRecord, run.run_id)
            assert first is not None and current_run is not None
            return (
                first.execution_status,
                first.finished_at is not None,
                first.revision,
                retried.invocation.attempt,
                retried.claim_fencing_token,
                current_run.status,
            )

    assert research_client.portal.call(expire_and_retry) == (
        "failed_retryable",
        True,
        2,
        2,
        claimed.claim_fencing_token + 1,
        "active",
    )


def test_model_cannot_escape_frozen_research_route_binding(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'wrong-binding.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / "receipts.db"),
        research_runtime_enabled=True,
        model_gateway_policy=ModelGatewayPolicy(
            provider_pricing=(ModelProviderPricing(provider_id="fake-local"),),
        ),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    app = create_app(
        settings,
        model_provider=WrongBindingModelProvider(),
        search_provider=FakeSearchProvider(),
        page_reader=FakePageReader(),
    )
    with TestClient(app, headers=headers) as client:
        task_id = _activate_research_task(client, "6")
        started = client.post(f"/api/v1/tasks/{task_id}/execution-runs")
        run_id = started.json()["run_id"]
        rejected = client.post(f"/api/v1/execution-runs/{run_id}/research:run")

        assert rejected.status_code == 409
        assert rejected.json()["code"] == "AGENT_ROUTE_BINDING_REJECTED"

        async def inspect_attempt() -> tuple[str, str, int]:
            async with app.state.database.session() as session:
                turn = await session.scalar(select(AgentModelTurnRecord))
                attempt = await session.scalar(select(ModelDispatchAttemptRecord))
                search_count = len(
                    tuple((await session.scalars(select(ResearchSearchCallRecord))).all())
                )
                assert turn is not None
                assert attempt is not None
                return turn.status, attempt.status, search_count

        assert client.portal is not None
        assert client.portal.call(inspect_attempt) == ("failed", "failed", 0)


@pytest.mark.parametrize(
    "url,address",
    [
        ("file:///etc/passwd", "93.184.216.34"),
        ("http://user:secret@example.com", "93.184.216.34"),
        ("http://localhost", "127.0.0.1"),
        ("http://metadata.example", "169.254.169.254"),
        ("http://private.example", "10.0.0.1"),
        ("http://ipv6.example", "::1"),
    ],
)
def test_page_reader_rejects_non_public_targets(url: str, address: str) -> None:
    reader = SafePageReader(resolver=lambda host, port: [address])
    with pytest.raises(PageReadRejectedError):
        reader._validated_target(url)


def test_page_reader_accepts_only_resolved_public_address() -> None:
    reader = SafePageReader(resolver=lambda host, port: ["93.184.216.34"])
    parsed, address, port = reader._validated_target("https://example.com/research")
    assert parsed.hostname == "example.com"
    assert address == "93.184.216.34"
    assert port == 443


def test_page_reader_revalidates_redirect_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RedirectResponse:
        status = 302

        @staticmethod
        def getheader(name: str, default: str | None = None) -> str | None:
            return "http://127.0.0.1/metadata" if name == "Location" else default

        @staticmethod
        def read(size: int = -1) -> bytes:
            del size
            return b""

    class RedirectConnection:
        def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
            del host, port, address, timeout

        @staticmethod
        def request(method: str, target: str, headers: dict[str, str]) -> None:
            del method, target, headers

        @staticmethod
        def getresponse() -> RedirectResponse:
            return RedirectResponse()

        @staticmethod
        def close() -> None:
            return None

    from deskpilot.application import web_research

    monkeypatch.setattr(web_research, "_PinnedHTTPConnection", RedirectConnection)
    reader = SafePageReader(
        resolver=lambda host, port: ["93.184.216.34"] if host == "public.example" else ["127.0.0.1"]
    )
    with pytest.raises(PageReadRejectedError):
        reader._read_sync(
            f"tsk_{'9' * 32}",
            f"rsr_{'a' * 64}",
            _hit(1, "public.example").model_copy(update={"url": "http://public.example/start"}),
        )


def test_page_reader_rejects_mixed_public_private_dns_answer() -> None:
    reader = SafePageReader(resolver=lambda host, port: ["93.184.216.34", "10.0.0.2"])
    with pytest.raises(PageReadRejectedError):
        reader._validated_target("https://mixed.example/")
