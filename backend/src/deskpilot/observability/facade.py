"""Failure-isolated OpenTelemetry SDK adapter with a bounded safe local store."""

import asyncio
import time
from collections import Counter, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from deskpilot.observability.attributes import (
    TELEMETRY_SCHEMA_VERSION,
    TelemetryAttributeRegistry,
)
from deskpilot.observability.schema import (
    TelemetryMetricPoint,
    TelemetryMetricsRead,
    TelemetrySpanRead,
    TelemetryTracePage,
)

_SPAN_NAMES = frozenset(
    {
        "deskpilot.task.accept",
        "deskpilot.model.dispatch",
        "deskpilot.tool.execute",
        "deskpilot.mcp.request",
        "deskpilot.evaluation.run",
        "deskpilot.evaluation.case",
    }
)
_OUTCOMES = frozenset(
    {"accepted", "succeeded", "failed", "denied", "cancelled", "unknown", "other"}
)


class SafeLocalSpanExporter(SpanExporter):
    """Stores only normalized, registry-approved projections in a bounded deque."""

    def __init__(self, registry: TelemetryAttributeRegistry, capacity: int) -> None:
        self._registry = registry
        self._spans: deque[TelemetrySpanRead] = deque(maxlen=capacity)
        self._lock = RLock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            projections = [self._project(span) for span in spans]
            with self._lock:
                self._spans.extend(projections)
        except BaseException:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def query(
        self,
        *,
        trace_id: str | None,
        task_correlation_id: str | None,
        limit: int,
    ) -> tuple[TelemetrySpanRead, ...]:
        with self._lock:
            snapshot = tuple(self._spans)
        result = [
            span
            for span in reversed(snapshot)
            if (trace_id is None or span.trace_id == trace_id)
            and (
                task_correlation_id is None
                or span.attributes.get("deskpilot.task.correlation_id") == task_correlation_id
            )
        ]
        return tuple(reversed(result[:limit]))

    def _project(self, span: ReadableSpan) -> TelemetrySpanRead:
        context = span.context
        if context is None:
            raise ValueError("Readable span has no context")
        parent_span_id = None
        if span.parent is not None and span.parent.is_valid:
            parent_span_id = format(span.parent.span_id, "016x")
        start_ns = span.start_time or 0
        end_ns = span.end_time or start_ns
        attributes: Mapping[str, object] = cast(Mapping[str, object], span.attributes or {})
        return TelemetrySpanRead(
            trace_id=format(context.trace_id, "032x"),
            span_id=format(context.span_id, "016x"),
            parent_span_id=parent_span_id,
            name=span.name if span.name in _SPAN_NAMES else "deskpilot.telemetry.unknown",
            kind=span.kind.name.lower(),
            status="error" if span.status.status_code is StatusCode.ERROR else "unset",
            attributes=self._registry.sanitize(attributes, local=True),
            started_at=datetime.fromtimestamp(start_ns / 1_000_000_000, UTC),
            completed_at=datetime.fromtimestamp(end_ns / 1_000_000_000, UTC),
            duration_ms=max(0.0, (end_ns - start_ns) / 1_000_000),
        )


class IsolatingSpanExporter(SpanExporter):
    """Prevents an exporter outage from changing application behavior."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self._delegate.export(spans)
        except BaseException:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except BaseException:
            return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except BaseException:
            return False


class TelemetryOperation:
    def __init__(
        self,
        span: Span,
        category: str,
        registry: TelemetryAttributeRegistry,
    ) -> None:
        self._span = span
        self._registry = registry
        self.category = category
        self.outcome = "succeeded"

    def set_attribute(self, key: str, value: object) -> None:
        safe = self._registry.sanitize({key: value}, local=True)
        if key in safe:
            self._span.set_attribute(key, safe[key])

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome if outcome in _OUTCOMES else "other"
        self._span.set_attribute("deskpilot.outcome", self.outcome)


class TelemetryFacade:
    """The only application entry point for traces and low-cardinality metrics."""

    def __init__(self, *, capacity: int = 5_000, enabled: bool = True) -> None:
        self.registry = TelemetryAttributeRegistry()
        self._enabled = enabled
        resource = Resource.create(
            {
                "service.name": "deskpilot-backend",
                "service.version": "0.1.0",
                "telemetry.sdk.language": "python",
            }
        )
        self._tracer_provider = TracerProvider(resource=resource)
        self._local_exporter = SafeLocalSpanExporter(self.registry, capacity)
        self._tracer_provider.add_span_processor(
            SimpleSpanProcessor(IsolatingSpanExporter(self._local_exporter))
        )
        self._tracer = self._tracer_provider.get_tracer(
            "deskpilot.observability", TELEMETRY_SCHEMA_VERSION
        )
        self._meter_provider = MeterProvider(resource=resource)
        self._meter = self._meter_provider.get_meter(
            "deskpilot.observability", TELEMETRY_SCHEMA_VERSION
        )
        self._operation_counter = self._meter.create_counter(
            "deskpilot.operation.count", unit="{operation}"
        )
        self._duration_histogram = self._meter.create_histogram(
            "deskpilot.operation.duration", unit="ms"
        )
        self._metric_counts: Counter[tuple[str, str]] = Counter()
        self._duration_count: Counter[tuple[str, str]] = Counter()
        self._duration_sum: dict[tuple[str, str], float] = {}
        self._duration_max: dict[tuple[str, str], float] = {}
        self._metric_lock = RLock()

    @property
    def tracer(self) -> trace.Tracer:
        """Exposed only for contract tests of the redacting exporter boundary."""
        return self._tracer

    @contextmanager
    def operation(
        self,
        name: str,
        category: str,
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[TelemetryOperation]:
        category = (
            category if category in {"task", "model", "tool", "mcp", "evaluation"} else "other"
        )
        safe_attributes = self.registry.sanitize(
            {
                **(attributes or {}),
                "deskpilot.telemetry.schema.version": TELEMETRY_SCHEMA_VERSION,
                "deskpilot.operation.category": category,
                "deskpilot.outcome": "succeeded",
            },
            local=True,
        )
        started = time.perf_counter_ns()
        if not self._enabled:
            disabled_span = trace.INVALID_SPAN
            operation = TelemetryOperation(
                cast(Span, disabled_span), category, self.registry
            )
            try:
                yield operation
            finally:
                self._record_metrics(category, operation.outcome, started)
            return
        with self._tracer.start_as_current_span(
            name if name in _SPAN_NAMES else "deskpilot.telemetry.unknown",
            attributes=cast(dict[str, Any], safe_attributes),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            operation = TelemetryOperation(cast(Span, span), category, self.registry)
            try:
                yield operation
            except asyncio.CancelledError:
                operation.set_outcome("cancelled")
                raise
            except BaseException as error:
                operation.set_outcome("failed")
                code = getattr(error, "code", "UNCLASSIFIED_FAILURE")
                sanitized = self.registry.sanitize({"deskpilot.error.code": code}, local=True)
                for key, value in sanitized.items():
                    span.set_attribute(key, value)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                self._record_metrics(category, operation.outcome, started)

    def query(
        self,
        *,
        trace_id: str | None = None,
        task_correlation_id: str | None = None,
        limit: int = 100,
    ) -> TelemetryTracePage:
        return TelemetryTracePage(
            export_policy_digest=self.registry.policy_digest,
            spans=self._local_exporter.query(
                trace_id=trace_id,
                task_correlation_id=task_correlation_id,
                limit=limit,
            ),
        )

    def metrics(self) -> TelemetryMetricsRead:
        with self._metric_lock:
            keys = sorted(self._metric_counts)
            points = tuple(
                TelemetryMetricPoint(
                    category=cast(Any, category),
                    outcome=cast(Any, outcome),
                    operation_count=self._metric_counts[key],
                    duration_count=self._duration_count[key],
                    duration_sum_ms=float(self._duration_sum[key]),
                    duration_max_ms=self._duration_max.get(key, 0.0),
                )
                for key in keys
                for category, outcome in [key]
            )
        return TelemetryMetricsRead(points=points)

    def shutdown(self) -> None:
        for shutdown in (
            self._tracer_provider.shutdown,
            self._meter_provider.shutdown,
        ):
            try:
                shutdown()
            except BaseException:
                pass

    def _record_metrics(self, category: str, outcome: str, started: int) -> None:
        if not self._enabled:
            return
        duration_ms = max(0.0, (time.perf_counter_ns() - started) / 1_000_000)
        dimensions = {"category": category, "outcome": outcome}
        try:
            self._operation_counter.add(1, dimensions)
            self._duration_histogram.record(duration_ms, dimensions)
        except BaseException:
            pass
        key = (category, outcome)
        with self._metric_lock:
            self._metric_counts[key] += 1
            self._duration_count[key] += 1
            self._duration_sum[key] = self._duration_sum.get(key, 0.0) + duration_ms
            self._duration_max[key] = max(self._duration_max.get(key, 0.0), duration_ms)
