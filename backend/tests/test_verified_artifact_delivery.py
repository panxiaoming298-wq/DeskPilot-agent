import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from deskpilot.application.artifact_delivery_runtime import (
    ArtifactDeliveryProofRejectedError,
)
from deskpilot.application.browser_verifier import (
    BrowserEvidence,
    IsolatedChromiumVerifier,
    audit_static_html,
)
from deskpilot.application.pdf_artifact_renderer import RenderedPdf
from deskpilot.application.plan_compiler import research_to_html_contract, research_to_html_draft
from deskpilot.application.web_research import SafePageReader
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.artifact_runtime import PdfRenderVerificationRead, digested
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.research import PageSnapshot, SearchHit, SearchProviderResult, SearchRequest
from deskpilot.domain.task_plans import DraftPlan, PlanProducer
from deskpilot.infrastructure.models import (
    ArtifactPatchReceiptRecord,
    ClaimVerdictRecord,
    ResearchCitationRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskRecord,
    VerificationEvidenceSnapshotRecord,
)
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "stage-71-session-token-with-at-least-32-chars"


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


class FakePageReader(SafePageReader):
    async def read(self, *, task_id: str, research_session_id: str, hit: SearchHit) -> PageSnapshot:
        text = f"Controlled public evidence from {hit.url}."
        fetched_at = datetime.now(UTC)
        identity = {
            "task_id": task_id,
            "research_session_id": research_session_id,
            "hit_id": hit.hit_id,
        }
        material = {
            "schema_version": "deskpilot.page-snapshot.v1",
            "page_snapshot_id": f"snp_{sha256_digest(identity)}",
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
            "fetched_at": fetched_at,
        }
        return PageSnapshot.model_validate({**material, "snapshot_digest": sha256_digest(material)})


class FakeIsolatedBrowser:
    async def verify(self, entry_path: Path, html: str) -> BrowserEvidence:
        assert entry_path.name.endswith(".html")
        parser, title, issues = audit_static_html(html)
        return BrowserEvidence(
            passed=not issues,
            engine="fake-isolated-chromium-v1",
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


class FakePdfRenderer:
    async def render(self, entry_path: Path) -> RenderedPdf:
        assert entry_path.name.endswith(".html")
        content = b"%PDF-1.4\n" + b"1" * 512 + b"\n%%EOF\n"
        source_digest = sha256_digest({"bytes_hex": content.hex()})
        material: dict[str, object] = {
            "profile_id": "deskpilot.pdf-render.v1",
            "status": "passed",
            "engine": "fake-pdf-renderer-v1",
            "source_digest": source_digest,
            "page_count": 1,
            "page_width_points": 595.0,
            "page_height_points": 842.0,
            "render_dpi": 144,
            "rendered_page_digests": (sha256_digest({"page": 1}),),
            "rendered_page_dimensions": ((1190, 1684),),
            "issue_codes": (),
        }
        return RenderedPdf(
            content=content,
            verification=PdfRenderVerificationRead.model_validate(
                digested(material, "evidence_digest")
            ),
        )


@pytest.fixture
def delivery_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'delivery.db').as_posix()}",
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
        search_provider=FakeSearchProvider(),
        page_reader=FakePageReader(),
        browser_verifier=FakeIsolatedBrowser(),
        pdf_artifact_renderer=FakePdfRenderer(),
    )
    with TestClient(app, headers=headers) as client:
        yield client


def _run_research(client: TestClient, suffix: str) -> tuple[str, str]:
    app = cast(FastAPI, client.app)
    assert client.portal is not None
    task_id = f"tsk_{suffix * 32}"

    async def insert_task() -> None:
        async with app.state.database.session() as session, session.begin():
            session.add(
                TaskRecord(
                    task_id=task_id,
                    goal="研究公开主题并形成带引用的静态页面",
                    status="submitted",
                    privacy_mode="balanced",
                    constraints=[],
                )
            )

    client.portal.call(insert_task)
    contract = research_to_html_contract(task_id, app.state.capability_catalog)
    client.portal.call(
        app.state.plan_compilation_service.activate,
        contract,
        research_to_html_draft(task_id),
    )
    started = client.post(f"/api/v1/tasks/{task_id}/execution-runs")
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    researched = client.post(f"/api/v1/execution-runs/{run_id}/research:run")
    assert researched.status_code == 200, researched.text
    return task_id, run_id


def _node_statuses(client: TestClient, run_id: str) -> dict[str, str]:
    body = client.get(f"/api/v1/execution-runs/{run_id}").json()
    return {item["local_key"]: item["status"] for item in body["nodes"]}


def _namespaced_research_draft(task_id: str) -> DraftPlan:
    source = research_to_html_draft(task_id)
    key_map = {
        "research": "s01_research",
        "build_html": "s01_build_html",
        "browser_verify": "s01_browser_verify",
    }
    return DraftPlan(
        task_id=task_id,
        contract_version=source.contract_version,
        producer=PlanProducer(
            kind="model_planner",
            producer_ref="deskpilot.offer-composer.v1",
        ),
        nodes=tuple(
            node.model_copy(
                update={
                    "local_key": key_map.get(node.local_key, node.local_key),
                    "depends_on": tuple(key_map.get(item, item) for item in node.depends_on),
                }
            )
            for node in source.nodes
        ),
    )


def test_only_verified_edges_unlock_artifact_browser_and_delivery(
    delivery_client: TestClient,
) -> None:
    app = cast(FastAPI, delivery_client.app)
    assert delivery_client.portal is not None
    task_id, run_id = _run_research(delivery_client, "a")

    blocked_build = delivery_client.post(f"/api/v1/execution-runs/{run_id}/artifacts:build")
    assert blocked_build.status_code == 409
    assert _node_statuses(delivery_client, run_id)["build_html"] == "pending"

    verified = delivery_client.post(f"/api/v1/execution-runs/{run_id}/claims:verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["outcome"] == "verified"
    assert verified.json()["grader_provider_id"] == "fake-local"
    assert verified.json()["grader_model"] == "deskpilot-fake-v1"
    statuses = _node_statuses(delivery_client, run_id)
    assert statuses["research"] == "verified"
    assert statuses["build_html"] == "ready"
    assert statuses["browser_verify"] == "pending"
    replay = delivery_client.post(f"/api/v1/execution-runs/{run_id}/claims:verify")
    assert replay.status_code == 200
    assert replay.json()["verification_run_id"] == verified.json()["verification_run_id"]

    built = delivery_client.post(f"/api/v1/execution-runs/{run_id}/artifacts:build")
    assert built.status_code == 200, built.text
    workspace = built.json()
    assert [item["relative_path"] for item in workspace["artifacts"]] == [
        "index.html",
        "report.md",
        "report.pdf",
    ]
    revision = workspace["artifacts"][0]["active_revision"]
    receipt = delivery_client.get(f"/api/v1/patch-receipts/{revision['patch_receipt_id']}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["new_digest"] == revision["content_digest"]
    statuses = _node_statuses(delivery_client, run_id)
    assert statuses["build_html"] == "verified"
    assert statuses["browser_verify"] == "ready"
    assert statuses["final_acceptance"] == "pending"

    blocked_final = delivery_client.post(f"/api/v1/execution-runs/{run_id}/final-acceptance:run")
    assert blocked_final.status_code == 409

    browser = delivery_client.post(f"/api/v1/execution-runs/{run_id}/browser:verify")
    assert browser.status_code == 200, browser.text
    assert browser.json()["status"] == "passed"
    assert browser.json()["external_request_count"] == 0
    statuses = _node_statuses(delivery_client, run_id)
    assert statuses["browser_verify"] == "verified"
    assert statuses["final_acceptance"] == "ready"

    delivered = delivery_client.post(f"/api/v1/execution-runs/{run_id}/final-acceptance:run")
    assert delivered.status_code == 200, delivered.text
    manifest = delivered.json()
    assert manifest["revision_id"] == revision["revision_id"]
    assert manifest["verified_claim_ids"]
    statuses = _node_statuses(delivery_client, run_id)
    assert set(statuses.values()) == {"verified"}
    assert delivery_client.get(f"/api/v1/execution-runs/{run_id}").json()["status"] == "succeeded"
    assert (
        delivery_client.get(f"/api/v1/task-workspaces/{workspace['workspace_id']}").json()["status"]
        == "delivered"
    )

    async def inspect_truth() -> tuple[str, int, int, int]:
        async with app.state.database.session() as session:
            task = await session.get(TaskRecord, task_id)
            verdicts = tuple((await session.scalars(select(ClaimVerdictRecord))).all())
            snapshots = tuple(
                (await session.scalars(select(VerificationEvidenceSnapshotRecord))).all()
            )
            receipts = tuple((await session.scalars(select(ArtifactPatchReceiptRecord))).all())
            assert task is not None
            return task.status, len(verdicts), len(snapshots), len(receipts)

    task_status, verdict_count, snapshot_count, receipt_count = delivery_client.portal.call(
        inspect_truth
    )
    assert task_status == "succeeded"
    assert verdict_count >= 1
    assert snapshot_count == 1
    assert receipt_count == 3


def test_namespaced_research_artifact_and_browser_nodes_use_exact_internal_api(
    delivery_client: TestClient,
) -> None:
    app = cast(FastAPI, delivery_client.app)
    assert delivery_client.portal is not None
    task_id = f"tsk_{'c' * 32}"

    async def insert_and_activate() -> None:
        async with app.state.database.session() as session, session.begin():
            session.add(
                TaskRecord(
                    task_id=task_id,
                    goal="namespaced research goal",
                    status="submitted",
                    privacy_mode="balanced",
                    constraints=[],
                )
            )
        contract = research_to_html_contract(task_id, app.state.capability_catalog)
        await app.state.plan_compilation_service.activate(
            contract,
            _namespaced_research_draft(task_id),
        )

    delivery_client.portal.call(insert_and_activate)
    started = delivery_client.post(f"/api/v1/tasks/{task_id}/execution-runs")
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    researched = delivery_client.post(f"/api/v1/execution-runs/{run_id}/research:run")
    assert researched.status_code == 200, researched.text

    async def exact_nodes() -> dict[str, str]:
        async with app.state.database.session() as session:
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).where(
                            TaskExecutionNodeRecord.run_id == run_id
                        )
                    )
                ).all()
            )
            return {item.local_key: item.node_id for item in nodes}

    nodes = delivery_client.portal.call(exact_nodes)

    async def run_exact_pipeline() -> tuple[str, str, str]:
        verified = await app.state.artifact_delivery_runtime.verify_research_node(
            run_id,
            node_id=nodes["s01_research"],
            local_key="s01_research",
        )
        workspace = await app.state.artifact_delivery_runtime.build_html_node(
            run_id,
            node_id=nodes["s01_build_html"],
            local_key="s01_build_html",
        )
        browser = await app.state.artifact_delivery_runtime.verify_browser_node(
            run_id,
            node_id=nodes["s01_browser_verify"],
            local_key="s01_browser_verify",
        )
        return verified.node_id, workspace.workspace_id, browser.node_id

    research_node_id, workspace_id, browser_node_id = delivery_client.portal.call(
        run_exact_pipeline
    )
    assert research_node_id == nodes["s01_research"]
    assert workspace_id
    assert browser_node_id == nodes["s01_browser_verify"]
    statuses = _node_statuses(delivery_client, run_id)
    assert statuses["s01_research"] == "verified"
    assert statuses["s01_build_html"] == "verified"
    assert statuses["s01_browser_verify"] == "verified"
    assert statuses["final_acceptance"] == "ready"

    async def replay_with_wrong_node() -> None:
        await app.state.artifact_delivery_runtime.verify_research_node(
            run_id,
            node_id=nodes["s01_build_html"],
            local_key="s01_research",
        )

    with pytest.raises(ArtifactDeliveryProofRejectedError, match="another execution node"):
        delivery_client.portal.call(replay_with_wrong_node)


def test_task_loop_artifact_api_persists_evidence_without_unlocking_edges(
    delivery_client: TestClient,
) -> None:
    app = cast(FastAPI, delivery_client.app)
    assert delivery_client.portal is not None
    task_id = f"tsk_{'d' * 32}"

    async def insert_and_activate() -> None:
        async with app.state.database.session() as session, session.begin():
            session.add(
                TaskRecord(
                    task_id=task_id,
                    goal="deferred edge research goal",
                    status="submitted",
                    privacy_mode="balanced",
                    constraints=[],
                )
            )
        contract = research_to_html_contract(task_id, app.state.capability_catalog)
        await app.state.plan_compilation_service.activate(
            contract,
            _namespaced_research_draft(task_id),
        )

    delivery_client.portal.call(insert_and_activate)
    started = delivery_client.post(f"/api/v1/tasks/{task_id}/execution-runs")
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    researched = delivery_client.post(f"/api/v1/execution-runs/{run_id}/research:run")
    assert researched.status_code == 200, researched.text

    async def exact_nodes() -> dict[str, str]:
        async with app.state.database.session() as session:
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord).where(
                            TaskExecutionNodeRecord.run_id == run_id
                        )
                    )
                ).all()
            )
            return {item.local_key: item.node_id for item in nodes}

    nodes = delivery_client.portal.call(exact_nodes)

    async def run_deferred_pipeline() -> tuple[str, str]:
        await app.state.artifact_delivery_runtime.verify_research_node(
            run_id,
            node_id=nodes["s01_research"],
            defer_task_loop_edge=True,
        )
        async with app.state.database.session() as session, session.begin():
            run = await session.get(TaskExecutionRunRecord, run_id)
            research = await session.get(
                TaskExecutionNodeRecord,
                nodes["s01_research"],
            )
            builder = await session.get(
                TaskExecutionNodeRecord,
                nodes["s01_build_html"],
            )
            assert run is not None and research is not None and builder is not None
            assert research.status == "awaiting_verification"
            assert builder.status == "pending"
            research.status = "verified"
            builder.status = "running"
            run.status = "active"
        workspace = await app.state.artifact_delivery_runtime.build_html_node(
            run_id,
            node_id=nodes["s01_build_html"],
            defer_task_loop_edge=True,
        )
        async with app.state.database.session() as session, session.begin():
            builder = await session.get(
                TaskExecutionNodeRecord,
                nodes["s01_build_html"],
            )
            browser = await session.get(
                TaskExecutionNodeRecord,
                nodes["s01_browser_verify"],
            )
            assert builder is not None and browser is not None
            assert builder.status == "running"
            assert browser.status == "pending"
            builder.status = "verified"
            browser.status = "running"
        rendered = await app.state.artifact_delivery_runtime.verify_browser_node(
            run_id,
            node_id=nodes["s01_browser_verify"],
            defer_task_loop_edge=True,
        )
        return workspace.workspace_id, rendered.status

    workspace_id, browser_status = delivery_client.portal.call(run_deferred_pipeline)
    assert workspace_id
    assert browser_status == "passed"
    statuses = _node_statuses(delivery_client, run_id)
    assert statuses["s01_browser_verify"] == "running"
    assert statuses["final_acceptance"] == "pending"


def test_tampered_citation_cannot_unlock_verified_edge(
    delivery_client: TestClient,
) -> None:
    app = cast(FastAPI, delivery_client.app)
    assert delivery_client.portal is not None
    _, run_id = _run_research(delivery_client, "b")

    async def tamper() -> None:
        async with app.state.database.session() as session, session.begin():
            citation = await session.scalar(select(ResearchCitationRecord))
            assert citation is not None
            citation.manifest = {**citation.manifest, "locator_text": "forged evidence"}

    delivery_client.portal.call(tamper)
    rejected = delivery_client.post(f"/api/v1/execution-runs/{run_id}/claims:verify")
    assert rejected.status_code == 409
    statuses = _node_statuses(delivery_client, run_id)
    assert statuses["research"] == "awaiting_verification"
    assert statuses["build_html"] == "pending"


def test_static_browser_audit_rejects_remote_resources_and_scripts() -> None:
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'">
    <title>x</title><script src="https://cdn.example/x.js"></script></head>
    <body><h1>x</h1><img src="https://cdn.example/x.png"></body></html>"""
    _, _, issues = audit_static_html(html)
    assert "SCRIPT_FORBIDDEN" in issues
    assert "EXTERNAL_RESOURCE_FORBIDDEN" in issues


def test_real_isolated_browser_captures_render_evidence_when_available(
    tmp_path: Path,
) -> None:
    verifier = IsolatedChromiumVerifier(timeout_seconds=30)
    if not verifier.available:
        pytest.skip("No local Chromium-family browser")
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none';
    style-src 'unsafe-inline'; base-uri 'none'"><title>验收页</title>
    <style>body{font-family:sans-serif}</style></head><body><h1>验收页</h1></body></html>"""
    entry = tmp_path / "index.html"
    entry.write_text(html, encoding="utf-8")

    evidence = asyncio.run(verifier.verify(entry, html))

    assert evidence.passed is True
    assert evidence.external_request_count == 0
    assert evidence.dom_digest != evidence.screenshot_digest
