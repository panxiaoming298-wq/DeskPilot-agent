from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from deskpilot.application.evaluation_service import EvaluationService


def test_builtin_golden_run_records_trace_and_replays(client: TestClient) -> None:
    assert client.get("/api/v1/evaluations/runs").json() == {"runs": []}

    response = client.post("/api/v1/evaluations/golden:run")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    run = response.json()
    assert run["status"] == "passed"
    assert run["suite_version"] == 2
    assert run["case_count"] == 20
    assert run["passed_count"] == 20
    assert run["failed_count"] == 0
    assert run["safety_case_count"] == 11
    assert run["safety_passed_count"] == 11
    assert run["success_rate"] == 1.0
    assert run["safety_rate"] == 1.0
    assert len(run["traces"]) == 20
    assert run["traces"][0]["previous_event_digest"] is None
    assert run["traces"][1]["previous_event_digest"] == run["traces"][0]["event_digest"]
    assert run["traces"][19]["previous_event_digest"] == run["traces"][18]["event_digest"]
    assert {
        trace["scenario"] for trace in run["traces"] if trace["scenario"].startswith("fault.")
    } == {
        "fault.model_rate_limit",
        "fault.runner_crash_recovery",
        "fault.websocket_disconnect",
        "fault.mcp_protocol_anomaly",
    }

    replay_response = client.post(f"/api/v1/evaluations/runs/{run['run_id']}:replay")
    assert replay_response.status_code == 200
    replay = replay_response.json()
    assert replay["status"] == "passed"
    assert replay["replay_of_run_id"] == run["run_id"]
    assert replay["replay_match"] is True
    assert [item["output_digest"] for item in replay["traces"]] == [
        item["output_digest"] for item in run["traces"]
    ]

    listed = client.get("/api/v1/evaluations/runs").json()["runs"]
    assert [item["run_id"] for item in listed] == [replay["run_id"], run["run_id"]]

    report_response = client.get("/api/v1/evaluations/reports/latest")
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["schema_version"] == "deskpilot.evaluation-report.v1"
    assert report["run_count"] == 2
    assert report["passed_run_count"] == 2
    assert report["run_success_rate"] == 1.0
    assert report["run_duration_p50_ms"] is not None
    assert report["run_duration_p95_ms"] is not None
    assert report["case_duration_p50_ms"] is not None
    assert report["case_duration_p95_ms"] is not None
    assert report["failure_counts"] == {}
    assert [point["run_id"] for point in report["trend"]] == [run["run_id"], replay["run_id"]]
    assert len(report["report_digest"]) == 64

    exported = client.get("/api/v1/evaluations/reports/latest:export")
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == (
        'attachment; filename="deskpilot-evaluation-report-v1.json"'
    )
    assert exported.json() == report


def test_evaluation_trace_tamper_is_rejected(client: TestClient, tmp_path: Path) -> None:
    run = client.post("/api/v1/evaluations/golden:run").json()
    engine = create_engine(f"sqlite:///{(tmp_path / 'deskpilot-test.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE evaluation_trace_events SET output_digest = ? "
            "WHERE run_id = ? AND sequence = 1",
            ("0" * 64, run["run_id"]),
        )
    engine.dispose()

    response = client.get(f"/api/v1/evaluations/runs/{run['run_id']}")
    assert response.status_code == 409
    assert response.json()["code"] == "EVALUATION_PROOF_REJECTED"
    report = client.get("/api/v1/evaluations/reports/latest")
    assert report.status_code == 409
    assert report.json()["code"] == "EVALUATION_PROOF_REJECTED"


def test_expectation_mismatch_has_stable_failure_classification(
    client: TestClient,
    tmp_path: Path,
) -> None:
    suite = tmp_path / "mismatch.yaml"
    suite.write_text(
        """schema_version: deskpilot.golden-suite.v1
suite_id: deskpilot.mismatch
version: 1
cases:
  - case_id: mcp.expected-mismatch
    scenario: mcp.text_metrics
    input: {text: hello}
    expect: {character_count: 999}
""",
        encoding="utf-8",
    )
    client.app.state.evaluation_service = EvaluationService(
        client.app.state.database,
        suite,
    )

    response = client.post("/api/v1/evaluations/golden:run")
    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "failed"
    assert run["failed_count"] == 1
    assert run["traces"][0]["error_code"] == "EXPECTATION_MISMATCH"
    assert run["result_manifest"]["cases"][0]["error_code"] == "EXPECTATION_MISMATCH"
    report = client.get("/api/v1/evaluations/reports/latest").json()
    assert report["failed_run_count"] == 1
    assert report["failure_counts"] == {"EXPECTATION_MISMATCH": 1}


def test_replay_rejects_suite_version_drift(client: TestClient, tmp_path: Path) -> None:
    run = client.post("/api/v1/evaluations/golden:run").json()
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        """schema_version: deskpilot.golden-suite.v1
suite_id: deskpilot.changed
version: 2
cases:
  - case_id: security.bundle
    scenario: security.mcp_bundle_tamper
    safety_case: true
    input: {}
    expect: {error_code: MCP_SERVER_BUNDLE_REJECTED}
""",
        encoding="utf-8",
    )
    client.app.state.evaluation_service = EvaluationService(client.app.state.database, changed)

    response = client.post(f"/api/v1/evaluations/runs/{run['run_id']}:replay")
    assert response.status_code == 409
    assert response.json()["code"] == "EVALUATION_PROOF_REJECTED"
