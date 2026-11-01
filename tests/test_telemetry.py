"""Traces and JSON logs with ``run_id`` (#395, docs/MONITORING.md §11).

The data boundary is the one the monitoring roles keep (#186 AC4): request and response bodies of
``run_actions``, ``runs.review_body`` and diffs never reach a span or a log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import app.modules.reviews.infrastructure.amqp as amqp
from app.common.infrastructure import telemetry
from app.common.infrastructure.telemetry import (
    JsonFormatter,
    RedactingSpanExporter,
    TracingMiddleware,
    bind_run_id,
    configure_logging,
    configure_tracing,
    record_step_span,
)
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.run_delivery import RunDeliveryPointer
from app.modules.reviews.infrastructure.run_action_records import insert_run_action
from tests.test_queue_messages import RUN, Delivery, pending
from tests.test_review_run_worker import FakeRun, handler

DIFF = "@@ -1 +1 @@\n-secret_old\n+secret_new"
REVIEW_BODY = "Review body: the token leaks in line 3"
STARTED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _reset_global_provider() -> None:
    # The API allows one global provider per process; tests swap it.
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._TRACER_PROVIDER = None


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """The exporter behind the redaction, as production wires it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(exporter)))
    _reset_global_provider()
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()
    _reset_global_provider()


def _texts(span: ReadableSpan) -> str:
    values = [span.name, *map(str, (span.attributes or {}).values())]
    for event in span.events:
        values.extend(map(str, (event.attributes or {}).values()))
    return "\n".join(values)


# --- export switch ----------------------------------------------------------------------------


def test_without_an_endpoint_tracing_stays_off_and_nothing_is_exported() -> None:
    _reset_global_provider()
    try:
        assert configure_tracing("worker", {}) is None
        assert configure_tracing("worker", {"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}) is None
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)
        with telemetry.tracer().start_as_current_span("noop") as span:
            assert not span.get_span_context().is_valid
    finally:
        _reset_global_provider()


def test_with_an_endpoint_spans_go_to_the_otlp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Exporter(InMemorySpanExporter):
        def __init__(self, *, endpoint: str, timeout: int) -> None:
            super().__init__()
            seen.append(endpoint)

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", Exporter
    )
    _reset_global_provider()
    try:
        provider = configure_tracing("api", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://tempo:4318/"})
        assert provider is not None
        assert seen == ["http://tempo:4318/v1/traces"]
        assert provider.resource.attributes["service.name"] == "api"
        telemetry.shutdown_tracing(provider)
    finally:
        _reset_global_provider()


# --- data boundary ----------------------------------------------------------------------------


def test_the_exporter_drops_every_attribute_outside_the_allowlist(
    spans: InMemorySpanExporter,
) -> None:
    with telemetry.tracer().start_as_current_span(
        "llm.call",
        attributes={
            "run_id": str(RUN),
            "dmc.tool": "llm.call",
            "request": DIFF,
            "http.url": "https://api.github.com/repos/o/r/pulls/7?token=x",
            "db.statement": f"UPDATE runs SET review_body = '{REVIEW_BODY}'",
        },
    ) as span:
        span.add_event("log", {"body": REVIEW_BODY, "exception.type": "ValueError"})
        span.record_exception(ValueError(REVIEW_BODY))

    [exported] = spans.get_finished_spans()
    assert dict(exported.attributes or {}) == {"run_id": str(RUN), "dmc.tool": "llm.call"}
    assert dict(exported.events[0].attributes or {}) == {"exception.type": "ValueError"}
    exception = exported.events[1].attributes or {}
    assert exception["exception.type"] == "ValueError"
    assert "exception.message" not in exception
    text = _texts(exported)
    for leaked in (DIFF, REVIEW_BODY, "token=x"):
        assert leaked not in text


@dataclass
class FakeSession:
    """Just enough of ``AsyncSession`` for ``insert_run_action``: attempt 2, no earlier steps."""

    scalars: list[Any] = field(default_factory=lambda: [2, 4])
    added: list[Any] = field(default_factory=list)

    async def scalar(self, _statement: Any) -> Any:
        return self.scalars.pop(0)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: Any) -> None:
        return None


@pytest.mark.parametrize(
    ("response", "status", "error_type"),
    [
        ({"files": [{"filename": "a.py", "patch": DIFF}]}, StatusCode.UNSET, None),
        ({"error": {"type": "VcsTimeout", "message": DIFF}}, StatusCode.ERROR, "VcsTimeout"),
        ({"error": {"class": "llm_timeout", "http_status": 504}}, StatusCode.ERROR, "llm_timeout"),
    ],
)
def test_each_run_action_row_is_a_step_span_without_its_request_or_response(
    spans: InMemorySpanExporter,
    response: dict[str, Any],
    status: StatusCode,
    error_type: str | None,
) -> None:
    session = FakeSession()

    asyncio.run(
        insert_run_action(
            cast(Any, session),
            run_id=RUN,
            tool="vcs.fetch_diff",
            request={"pr_number": 7, "patch": DIFF, "review_body": REVIEW_BODY},
            response=response,
            started_at=STARTED,
            duration_ms=1500,
        )
    )

    [span] = spans.get_finished_spans()
    assert span.name == "vcs.fetch_diff"
    expected: dict[str, Any] = {
        "run_id": str(RUN),
        "dmc.tool": "vcs.fetch_diff",
        "dmc.step.index": 5,
        "dmc.attempt": 2,
    }
    if error_type is not None:
        expected["error.type"] = error_type
    assert dict(span.attributes or {}) == expected
    assert span.status.status_code is status
    assert span.end_time is not None and span.start_time is not None
    assert span.end_time - span.start_time == 1_500_000_000
    assert DIFF not in _texts(span) and REVIEW_BODY not in _texts(span)


# --- RabbitMQ: one trace from publisher to worker ----------------------------------------------


@dataclass
class Exchange:
    published: list[aio_pika.Message] = field(default_factory=list)

    async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
        self.published.append(message)


def test_the_run_message_carries_the_trace_and_the_worker_continues_it(
    spans: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    exchange = Exchange()
    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, exchange), cast(AbstractExchange, exchange)
    )
    asyncio.run(publisher.publish_confirmed(pending()))
    [message] = exchange.published
    assert message.headers is not None
    assert str(message.headers["traceparent"]).startswith("00-")

    run_ids: list[str | None] = []

    async def handler(pointer: RunDeliveryPointer) -> DeliveryOutcome:
        run_ids.append(telemetry.current_run_id())
        record_step_span(
            run_id=pointer.run_id,
            tool="context.build",
            index=0,
            attempt=1,
            error_type=None,
            started_at=STARTED,
            duration_ms=10,
        )
        return DeliveryOutcome.ACK

    delivery = Delivery(message.body, headers=dict(message.headers))
    asyncio.run(
        amqp.handle_run_delivery(cast(AbstractIncomingMessage, delivery), handler, publisher)
    )

    assert delivery.acked
    assert run_ids == [str(RUN)]
    assert telemetry.current_run_id() is None
    by_name = {span.name: span for span in spans.get_finished_spans()}
    produced, consumed, step = (
        by_name["review.run.fast publish"],
        by_name["review.run"],
        by_name["context.build"],
    )
    assert produced.kind is SpanKind.PRODUCER and consumed.kind is SpanKind.CONSUMER
    assert {produced.context.trace_id, step.context.trace_id} == {consumed.context.trace_id}
    assert consumed.parent is not None and consumed.parent.span_id == produced.context.span_id
    assert step.parent is not None and step.parent.span_id == consumed.context.span_id
    assert (consumed.attributes or {})["run_id"] == str(RUN)
    assert (consumed.attributes or {})["dmc.outcome"] == "ack"


def test_an_exception_in_the_worker_is_an_error_span_with_the_run_id(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(amqp, "REQUEUE_ERROR_DELAY_SECONDS", 0)
    exchange = Exchange()
    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, exchange), cast(AbstractExchange, exchange)
    )

    async def handler(pointer: RunDeliveryPointer) -> DeliveryOutcome:
        raise RuntimeError("provider exploded")

    body = json.dumps(amqp.run_message_body(pending())).encode()
    asyncio.run(
        amqp.handle_run_delivery(cast(AbstractIncomingMessage, Delivery(body)), handler, publisher)
    )

    [consumed] = [span for span in spans.get_finished_spans() if span.name == "review.run"]
    assert consumed.status.status_code is StatusCode.ERROR
    assert (consumed.attributes or {})["run_id"] == str(RUN)
    assert (consumed.attributes or {})["error.type"] == "RuntimeError"
    [event] = consumed.events
    assert event.name == "exception"
    assert (event.attributes or {})["exception.type"] == "RuntimeError"


def test_a_dead_lettered_run_is_an_error_span(spans: InMemorySpanExporter) -> None:
    async def handler(pointer: RunDeliveryPointer) -> DeliveryOutcome:
        return DeliveryOutcome.DEAD_LETTER

    exchange = Exchange()
    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, exchange), cast(AbstractExchange, exchange)
    )
    body = json.dumps(amqp.run_message_body(pending())).encode()
    asyncio.run(
        amqp.handle_run_delivery(cast(AbstractIncomingMessage, Delivery(body)), handler, publisher)
    )

    [consumed] = spans.get_finished_spans()
    assert consumed.status.status_code is StatusCode.ERROR
    assert (consumed.attributes or {})["dmc.outcome"] == "dead_letter"


# --- API ------------------------------------------------------------------------------------


def _api() -> FastAPI:
    api = FastAPI()
    api.add_middleware(TracingMiddleware)

    @api.get("/healthcheck")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/api/runs/{run_id}")
    def run(run_id: UUID) -> dict[str, str]:
        return {"id": str(run_id)}

    @api.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return api


def test_the_api_span_has_the_route_template_and_status_not_the_url(
    spans: InMemorySpanExporter,
) -> None:
    client = TestClient(_api(), raise_server_exceptions=False)
    assert client.get("/healthcheck").status_code == 200
    assert client.get(f"/api/runs/{RUN}?token=secret").status_code == 200
    assert client.get("/boom").status_code == 500

    ok, failed = spans.get_finished_spans()
    assert ok.name == "GET /api/runs/{run_id}"
    assert ok.kind is SpanKind.SERVER
    assert dict(ok.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/api/runs/{run_id}",
        "http.response.status_code": 200,
    }
    assert str(RUN) not in _texts(ok) and "secret" not in _texts(ok)
    assert failed.status.status_code is StatusCode.ERROR
    assert (failed.attributes or {})["error.type"] == "RuntimeError"
    assert (failed.attributes or {})["http.response.status_code"] == 500


# --- JSON logs ------------------------------------------------------------------------------


def _record(message: str, *args: object, **extra: Any) -> logging.LogRecord:
    record = logging.LogRecord("app.worker", logging.INFO, __file__, 1, message, args, None)
    record.__dict__.update(extra)
    return record


def test_a_log_line_is_json_with_the_run_id_of_the_delivery_and_no_extra_fields(
    spans: InMemorySpanExporter,
) -> None:
    formatter = JsonFormatter("worker")
    with bind_run_id(RUN), telemetry.tracer().start_as_current_span("review.run") as span:
        line = formatter.format(
            _record("Run %s is published", "r-1", request={"patch": DIFF}, body=REVIEW_BODY)
        )
        trace_id = format(span.get_span_context().trace_id, "032x")

    entry = json.loads(line)
    assert entry["run_id"] == str(RUN)
    assert entry["trace_id"] == trace_id
    assert entry["message"] == "Run r-1 is published"
    assert {"ts", "level", "service", "logger", "span_id"} <= entry.keys()
    assert DIFF not in line and REVIEW_BODY not in line
    assert "run_id" not in json.loads(formatter.format(_record("idle")))


def test_an_exception_log_line_names_the_error_type() -> None:
    formatter = JsonFormatter("api")
    try:
        raise ValueError("bad")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()

    entry = json.loads(formatter.format(record))
    assert entry["error_type"] == "ValueError"
    assert "Traceback" in entry["exc_info"]


def test_configure_logging_routes_uvicorn_through_one_json_handler() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    uvicorn = logging.getLogger("uvicorn.access")
    uvicorn.addHandler(logging.StreamHandler())
    uvicorn.propagate = False
    try:
        configure_logging("api")
        configure_logging("api")
        ours = [handler for handler in root.handlers if handler.get_name() == "dmc-json"]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, JsonFormatter)
        assert uvicorn.handlers == [] and uvicorn.propagate
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


# --- error tracking -------------------------------------------------------------------------


@pytest.fixture
def span_errors() -> Iterator[None]:
    root = logging.getLogger()
    tracked = telemetry.SpanErrorHandler()
    root.addHandler(tracked)
    yield
    root.removeHandler(tracked)


def test_a_caught_pipeline_exception_reaches_the_span_of_the_run(
    spans: InMemorySpanExporter, span_errors: None
) -> None:
    """The handler classifies a pipeline error into a failed or retried run instead of raising."""

    def failing(claimed: ClaimedAttempt) -> bool:
        raise RuntimeError(f"postprocess failed on {REVIEW_BODY}")

    async def deliver() -> None:
        with telemetry.consume_span("review.run", {}, RUN):
            await handler(FakeRun(), failing).execute(RUN)

    asyncio.run(deliver())

    [consumed] = spans.get_finished_spans()
    assert consumed.status.status_code is StatusCode.ERROR
    assert (consumed.attributes or {})["error.type"] == "RuntimeError"
    [event] = consumed.events
    exception = event.attributes or {}
    assert exception["exception.type"] == "RuntimeError"
    stack = str(exception["exception.stacktrace"])
    assert stack.startswith("Traceback") and "in failing" in stack
    assert "exception.message" not in exception
    assert REVIEW_BODY not in stack


def test_a_logged_exception_is_recorded_once_per_span(
    spans: InMemorySpanExporter, span_errors: None
) -> None:
    log = logging.getLogger("app.test")
    with telemetry.tracer().start_as_current_span("review.run") as span:
        try:
            raise ValueError("once")
        except ValueError as error:
            telemetry.mark_error(span, error)
            log.exception("failed")
        log.error("no exception here")

    [exported] = spans.get_finished_spans()
    assert [event.name for event in exported.events] == ["exception"]


def test_the_stack_keeps_frames_and_drops_every_message() -> None:
    try:
        try:
            raise KeyError(DIFF)
        except KeyError as cause:
            raise ValueError(REVIEW_BODY) from cause
    except ValueError:
        import traceback

        text = traceback.format_exc()

    frames = telemetry.frames_only(text)
    assert frames.startswith("Traceback (most recent call last)")
    assert 'File "' in frames
    assert DIFF.splitlines()[-1] not in frames and REVIEW_BODY not in frames


def test_run_id_from_the_call_site_reaches_api_and_webhook_worker_lines() -> None:
    formatter = JsonFormatter("webhook-worker")
    record = _record("Run publication remains pending", run_id=str(RUN), error_type="Timeout")

    entry = json.loads(formatter.format(record))
    assert entry["run_id"] == str(RUN)
    assert entry["error_type"] == "Timeout"


def test_a_publication_binds_the_run_id_for_its_log_lines(spans: InMemorySpanExporter) -> None:
    seen: list[str | None] = []

    class Recording(Exchange):
        async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
            seen.append(telemetry.current_run_id())

    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, Recording()), cast(AbstractExchange, Recording())
    )
    asyncio.run(publisher.publish_confirmed(pending()))

    assert seen == [str(RUN)]
