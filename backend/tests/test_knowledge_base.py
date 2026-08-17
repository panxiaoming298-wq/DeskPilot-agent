from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine


def test_import_search_and_stale_source_fail_closed(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "runbook.md"
    source.write_text(
        "# 磁盘压力手册\n\n超过阈值时先检查剩余空间，再推迟文件移动。\n",
        encoding="utf-8",
    )

    imported_response = client.post(
        "/api/v1/knowledge/sources:import",
        json={"path": str(source)},
    )
    assert imported_response.status_code == 200
    assert imported_response.headers["cache-control"] == "no-store"
    imported = imported_response.json()
    assert imported["artifact_id"] == f"art_{imported['content_digest']}"
    assert imported["chunk_count"] == 1

    repeated = client.post(
        "/api/v1/knowledge/sources:import",
        json={"path": str(source)},
    ).json()
    assert repeated["source_id"] == imported["source_id"]
    assert repeated["artifact_id"] == imported["artifact_id"]

    sources_response = client.get("/api/v1/knowledge/sources")
    assert sources_response.status_code == 200
    assert sources_response.headers["cache-control"] == "no-store"
    assert [item["source_id"] for item in sources_response.json()] == [imported["source_id"]]

    search_response = client.post(
        "/api/v1/knowledge/search",
        json={"query": "推迟文件移动", "limit": 5},
    )
    assert search_response.status_code == 200
    result = search_response.json()
    assert result["searched_sources"] == 1
    assert result["stale_source_ids"] == []
    assert len(result["citations"]) == 1
    citation = result["citations"][0]
    assert citation["source_id"] == imported["source_id"]
    assert citation["artifact_id"] == imported["artifact_id"]
    assert citation["locator"] == "L1-L3"
    assert len(citation["retrieval_proof_digest"]) == 64
    assert len(result["result_digest"]) == 64

    source.write_text("# 已变更\n\n旧内容不得继续命中。\n", encoding="utf-8")
    stale = client.post(
        "/api/v1/knowledge/search",
        json={"query": "推迟文件移动"},
    ).json()
    assert stale["citations"] == []
    assert stale["searched_sources"] == 0
    assert stale["stale_source_ids"] == [imported["source_id"]]


def test_import_rejects_unsupported_or_binary_source(
    client: TestClient,
    tmp_path: Path,
) -> None:
    unsupported = tmp_path / "notes.json"
    unsupported.write_text('{"message":"not admitted"}', encoding="utf-8")
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"ok\x00not-text")

    for source in (unsupported, binary):
        response = client.post(
            "/api/v1/knowledge/sources:import",
            json={"path": str(source)},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "KNOWLEDGE_SOURCE_INVALID"


def test_search_rejects_incomplete_chunk_proof_set(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "trusted.txt"
    source.write_text("trusted recovery procedure", encoding="utf-8")
    imported = client.post(
        "/api/v1/knowledge/sources:import",
        json={"path": str(source)},
    ).json()

    engine = create_engine(f"sqlite:///{(tmp_path / 'deskpilot-test.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM knowledge_chunks WHERE artifact_id = ?",
            (imported["artifact_id"],),
        )
    engine.dispose()

    response = client.post(
        "/api/v1/knowledge/search",
        json={"query": "trusted"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "KNOWLEDGE_PROOF_REJECTED"
