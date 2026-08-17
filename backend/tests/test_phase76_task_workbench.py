from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.browser_verifier import BrowserEvidence, audit_static_html
from deskpilot.application.web_research import SafePageReader
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.research import PageSnapshot, SearchHit, SearchProviderResult, SearchRequest
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "stage-76-session-token-with-at-least-32-chars"


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


class RecordedSearchProvider:
    provider_id = "recorded-search"

    async def search(self, request: SearchRequest) -> SearchProviderResult:
        assert request.max_results >= 2
        return SearchProviderResult(
            provider_id=self.provider_id,
            hits=(_hit(1, "one.example"), _hit(2, "two.example")),
        )


class RecordedPageReader(SafePageReader):
    async def read(
        self,
        *,
        task_id: str,
        research_session_id: str,
        hit: SearchHit,
    ) -> PageSnapshot:
        text = f"Controlled public evidence from {hit.url}."
        material = {
            "schema_version": "deskpilot.page-snapshot.v1",
            "page_snapshot_id": f"snp_{sha256_digest({'task': task_id, 'hit': hit.hit_id})}",
            "task_id": task_id,
            "research_session_id": research_session_id,
            "search_hit_id": hit.hit_id,
            "requested_url": hit.url,
            "final_url": hit.url,
            "status_code": 200,
            "media_type": "text/html",
            "title": hit.title,
            "extracted_text": text,
            "content_digest": sha256_digest({"text": text}),
            "extractor_version": "deskpilot.html-text.v1",
            "origin": "external_untrusted",
            "fetched_at": datetime.now(UTC),
        }
        return PageSnapshot.model_validate(
            {**material, "snapshot_digest": sha256_digest(material)}
        )


class RecordedBrowser:
    async def verify(self, entry_path: Path, html: str) -> BrowserEvidence:
        parser, title, issues = audit_static_html(html)
        return BrowserEvidence(
            passed=not issues,
            engine="recorded-isolated-browser-v1",
            title=title,
            heading_count=parser.heading_count,
            link_count=parser.link_count,
            external_request_count=0,
            console_error_count=0,
            page_error_count=0,
            issue_codes=issues,
            dom_digest=sha256_digest({"dom": html}),
            screenshot_digest=sha256_digest({"screenshot": html}),
        )


@pytest.fixture
def workbench_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'workbench.db').as_posix()}",
        artifact_workspace_root=str(tmp_path / "workspaces"),
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
        search_provider=RecordedSearchProvider(),
        page_reader=RecordedPageReader(),
        browser_verifier=RecordedBrowser(),
    )
    with TestClient(app, headers=headers) as client:
        yield client


def _enabled(body: dict[str, object], action: str) -> bool:
    actions = body["actions"]
    assert isinstance(actions, list)
    return any(
        isinstance(item, dict) and item.get("action") == action and item.get("enabled") is True
        for item in actions
    )


def test_unified_workbench_only_unlocks_verified_steps(
    workbench_client: TestClient,
) -> None:
    created = workbench_client.post(
        "/api/v1/research-workbench/tasks",
        json={"goal": "研究两个公开来源并制作可验证 HTML", "privacy_mode": "balanced"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    task_id = body["task"]["task_id"]
    run_id = body["executions"]["runs"][0]["run_id"]
    assert body["stage"] == "researching"
    assert len(body["conversation"]) == 1
    assert _enabled(body, "run_research")
    assert not _enabled(body, "build_artifact")

    researched = workbench_client.post(f"/api/v1/execution-runs/{run_id}/research:run")
    assert researched.status_code == 200, researched.text
    body = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert body["stage"] == "awaiting_verification"
    assert _enabled(body, "verify_claims")
    assert not _enabled(body, "build_artifact")

    assert (
        workbench_client.post(
            f"/api/v1/execution-runs/{run_id}/claims:verify"
        ).status_code
        == 200
    )
    body = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert body["stage"] == "building_artifact"
    assert _enabled(body, "build_artifact")

    assert (
        workbench_client.post(
            f"/api/v1/execution-runs/{run_id}/artifacts:build"
        ).status_code
        == 200
    )
    body = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert body["stage"] == "verifying_browser"
    assert _enabled(body, "verify_browser")

    assert (
        workbench_client.post(
            f"/api/v1/execution-runs/{run_id}/browser:verify"
        ).status_code
        == 200
    )
    body = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert body["stage"] == "ready_to_deliver"
    assert _enabled(body, "finalize_delivery")

    delivered = workbench_client.post(
        f"/api/v1/execution-runs/{run_id}/final-acceptance:run"
    )
    assert delivered.status_code == 200, delivered.text
    body = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert body["stage"] == "delivered"
    assert body["research"]["claims"]
    assert body["verification"]["outcome"] == "verified"
    assert body["workspace"]["artifacts"][0]["active_revision"]["patch_receipt_id"]
    assert body["browser"]["external_request_count"] == 0
    assert _enabled(body, "prepare_export")
    assert body["projection_digest"]


def test_exact_export_requires_preview_confirmation_and_never_overwrites(
    workbench_client: TestClient,
    tmp_path: Path,
) -> None:
    created = workbench_client.post(
        "/api/v1/research-workbench/tasks",
        json={"goal": "形成精确导出的研究页面", "privacy_mode": "balanced"},
    ).json()
    task_id = created["task"]["task_id"]
    run_id = created["executions"]["runs"][0]["run_id"]
    for endpoint in (
        f"/api/v1/execution-runs/{run_id}/research:run",
        f"/api/v1/execution-runs/{run_id}/claims:verify",
        f"/api/v1/execution-runs/{run_id}/artifacts:build",
        f"/api/v1/execution-runs/{run_id}/browser:verify",
        f"/api/v1/execution-runs/{run_id}/final-acceptance:run",
    ):
        response = workbench_client.post(endpoint)
        assert response.status_code == 200, response.text
    delivery_id = response.json()["delivery_id"]
    target = tmp_path / "exports" / "result.html"
    target.parent.mkdir()
    prepare_headers = {"Idempotency-Key": "prepare-export-0001"}
    prepared = workbench_client.post(
        f"/api/v1/deliveries/{delivery_id}/exports:prepare",
        json={"target_path": str(target)},
        headers=prepare_headers,
    )
    assert prepared.status_code == 201, prepared.text
    preview = prepared.json()
    assert preview["status"] == "prepared"
    assert not target.exists()

    stale = workbench_client.post(
        f"/api/v1/artifact-exports/{preview['export_id']}:commit",
        json={"confirmation_digest": "0" * 64},
        headers={"Idempotency-Key": "commit-export-0001"},
    )
    assert stale.status_code == 409
    assert not target.exists()

    committed = workbench_client.post(
        f"/api/v1/artifact-exports/{preview['export_id']}:commit",
        json={"confirmation_digest": preview["confirmation_digest"]},
        headers={"Idempotency-Key": "commit-export-0001"},
    )
    assert committed.status_code == 200, committed.text
    receipt = committed.json()
    assert receipt["status"] == "committed"
    assert receipt["receipt_digest"]
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    replay = workbench_client.post(
        f"/api/v1/artifact-exports/{preview['export_id']}:commit",
        json={"confirmation_digest": preview["confirmation_digest"]},
        headers={"Idempotency-Key": "commit-export-0001"},
    )
    assert replay.status_code == 200
    assert replay.json()["receipt_digest"] == receipt["receipt_digest"]
    workbench = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert workbench["stage"] == "exported"

    existing = workbench_client.post(
        f"/api/v1/deliveries/{delivery_id}/exports:prepare",
        json={"target_path": str(target)},
        headers={"Idempotency-Key": "prepare-export-0002"},
    )
    assert existing.status_code == 409

    target.write_text("tampered", encoding="utf-8")
    drifted = workbench_client.get(f"/api/v1/artifact-exports/{preview['export_id']}")
    assert drifted.status_code == 409
    drifted_workbench = workbench_client.get(
        f"/api/v1/tasks/{task_id}/workbench"
    )
    assert drifted_workbench.status_code == 409


def test_stop_fences_unfinished_execution(workbench_client: TestClient) -> None:
    created = workbench_client.post(
        "/api/v1/research-workbench/tasks",
        json={"goal": "停止尚未开始的研究执行", "privacy_mode": "balanced"},
    ).json()
    task_id = created["task"]["task_id"]
    run_id = created["executions"]["runs"][0]["run_id"]

    stopped = workbench_client.post(f"/api/v1/execution-runs/{run_id}:cancel")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "cancelled"
    assert {item["status"] for item in stopped.json()["nodes"]} == {"cancelled"}

    research = workbench_client.post(f"/api/v1/execution-runs/{run_id}/research:run")
    assert research.status_code == 409
    projection = workbench_client.get(f"/api/v1/tasks/{task_id}/workbench").json()
    assert projection["stage"] == "blocked"
    stop_action = next(
        item for item in projection["actions"] if item["action"] == "stop_execution"
    )
    assert stop_action["enabled"] is False


def test_disabled_research_rejects_before_creating_task(client: TestClient) -> None:
    response = client.post(
        "/api/v1/research-workbench/tasks",
        json={"goal": "未配置研究 Provider 时拒绝", "privacy_mode": "balanced"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "TASK_WORKBENCH_CONFLICT"
    assert client.get("/api/v1/tasks").json()["total"] == 0
