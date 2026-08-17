import json

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from deskpilot.observability import TelemetryFacade
from deskpilot.observability.facade import IsolatingSpanExporter
from deskpilot.observability.safe_logging import SafeTelemetryLoggingFilter


class _FailingExporter(SpanExporter):
    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        del spans
        raise RuntimeError("credential=canary-secret")


def test_default_deny_exporter_drops_content_secret_and_exception_text() -> None:
    telemetry = TelemetryFacade(capacity=100)
    try:
        with telemetry.tracer.start_as_current_span(
            "deskpilot.model.dispatch",
            attributes={
                "deskpilot.operation.category": "model",
                "deskpilot.outcome": "succeeded",
                "prompt": "CANARY_PROMPT_NEVER_EXPORT",
                "authorization": "Bearer CANARY_SECRET_NEVER_EXPORT",
                "url.full": "https://example.invalid/private?q=secret",
            },
            record_exception=False,
            set_status_on_exception=False,
        ):
            pass
        projection = telemetry.query(limit=10)
        assert len(projection.spans) == 1
        serialized = json.dumps(projection.model_dump(mode="json"), sort_keys=True)
        assert "CANARY" not in serialized
        assert "example.invalid" not in serialized
        assert projection.spans[0].attributes == {
            "deskpilot.operation.category": "model",
            "deskpilot.outcome": "succeeded",
        }

        try:
            with telemetry.operation("deskpilot.model.dispatch", "model"):
                raise RuntimeError("CANARY_EXCEPTION_BODY")
        except RuntimeError:
            pass
        failed = telemetry.query(limit=10).spans[-1]
        assert failed.status == "error"
        assert failed.attributes["deskpilot.error.code"] == "UNCLASSIFIED_FAILURE"
        assert "CANARY" not in json.dumps(failed.model_dump(mode="json"))
    finally:
        telemetry.shutdown()


def test_exporter_failure_is_isolated() -> None:
    exporter = IsolatingSpanExporter(_FailingExporter())
    assert exporter.export([]) is SpanExportResult.FAILURE
    assert exporter.force_flush() is False
    exporter.shutdown()


def test_all_minimum_span_categories_and_safe_logging_contract() -> None:
    telemetry = TelemetryFacade(capacity=100)
    operations = (
        ("deskpilot.task.accept", "task"),
        ("deskpilot.model.dispatch", "model"),
        ("deskpilot.tool.execute", "tool"),
        ("deskpilot.mcp.request", "mcp"),
        ("deskpilot.evaluation.run", "evaluation"),
    )
    try:
        for name, category in operations:
            with telemetry.operation(name, category):
                pass
        spans = telemetry.query(limit=100).spans
        observed = {
            (span.name, span.attributes["deskpilot.operation.category"])
            for span in spans
        }
        assert observed == set(operations)
        assert {point.category for point in telemetry.metrics().points} == {
            "task",
            "model",
            "tool",
            "mcp",
            "evaluation",
        }
    finally:
        telemetry.shutdown()

    logging_filter = SafeTelemetryLoggingFilter()
    unsafe = __import__("logging").LogRecord("test", 20, "", 0, "secret", (), None)
    safe = __import__("logging").LogRecord("test", 20, "", 0, "safe", (), None)
    safe.telemetry_safe = True
    safe.error_code = "MODEL_TIMEOUT"
    assert logging_filter.filter(unsafe) is False
    assert logging_filter.filter(safe) is True


def test_task_correlation_trace_query_and_low_cardinality_metrics(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/tasks",
        json={"goal": "telemetry canary goal must never be exported"},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()
    correlation_id = events[0]["trace_id"]

    response = client.get(
        "/api/v1/telemetry/traces",
        params={"task_correlation_id": correlation_id},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    spans = response.json()["spans"]
    assert [span["name"] for span in spans] == ["deskpilot.task.accept"]
    assert spans[0]["attributes"]["deskpilot.task.correlation_id"] == correlation_id
    serialized = json.dumps(spans)
    assert "telemetry canary goal" not in serialized

    by_trace = client.get(
        "/api/v1/telemetry/traces",
        params={"trace_id": spans[0]["trace_id"]},
    )
    assert by_trace.status_code == 200
    assert by_trace.json()["spans"][0]["span_id"] == spans[0]["span_id"]
    assert client.get("/api/v1/telemetry/traces").status_code == 422

    metrics = client.get("/api/v1/telemetry/metrics")
    assert metrics.status_code == 200
    task_points = [point for point in metrics.json()["points"] if point["category"] == "task"]
    assert task_points[0]["outcome"] == "accepted"
    assert task_points[0]["operation_count"] == 1
    assert task_id not in json.dumps(metrics.json())


def test_evaluation_emits_one_run_and_twenty_case_spans(client: TestClient) -> None:
    run = client.post("/api/v1/evaluations/golden:run").json()
    projection = client.app.state.telemetry.query(limit=100)
    evaluation_spans = [
        span
        for span in projection.spans
        if span.attributes.get("deskpilot.operation.category") == "evaluation"
    ]
    assert len(evaluation_spans) == 21
    roots = [span for span in evaluation_spans if span.name == "deskpilot.evaluation.run"]
    cases = [span for span in evaluation_spans if span.name == "deskpilot.evaluation.case"]
    assert roots[0].attributes["deskpilot.evaluation.run_id"] == run["run_id"]
    assert len(cases) == 20
    assert all(case.trace_id == roots[0].trace_id for case in cases)
    assert all(case.parent_span_id == roots[0].span_id for case in cases)
    serialized = json.dumps([span.model_dump(mode="json") for span in evaluation_spans])
    assert "input_digest" not in serialized
    assert "output_digest" not in serialized
