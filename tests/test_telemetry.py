"""Traces and JSON logs with ``run_id`` (#395, docs/MONITORING.md §11).

The data boundary is the one the monitoring roles keep (#186 AC4): request and response bodies of
``run_actions``, ``runs.review_body`` and diffs never reach a span or a log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import TypeAdapter, ValidationError

import app.modules.reviews.infrastructure.amqp as amqp
import app.webhook_worker as webhook_worker
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
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReplayBatchResult,
)
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.rerun_run import RerunOutcome, RerunResult, RerunRun
from app.modules.reviews.application.run_delivery import RunDeliveryPointer
from app.modules.reviews.application.try_enqueue_webhook_run import TryEnqueueWebhookRun
from app.modules.reviews.infrastructure.run_action_records import insert_run_action
from app.webhook_worker import WebhookReplayProvider
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
    values = [span.name, span.status.description or "", *map(str, (span.attributes or {}).values())]
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
    # ``record_exception`` of the SDK: its stack is text with messages, dropped at export.
    exception = exported.events[1].attributes or {}
    assert exception["exception.type"] == "ValueError"
    assert "exception.message" not in exception and "exception.stacktrace" not in exception
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

    async def deliver() -> None:
        # One task: the binding of the delivery is reset when it ends.
        assert telemetry.current_run_id() is None
        await amqp.handle_run_delivery(cast(AbstractIncomingMessage, delivery), handler, publisher)
        assert telemetry.current_run_id() is None

    asyncio.run(deliver())

    assert delivery.acked
    assert run_ids == [str(RUN)]
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
    """Only ``extra`` and the text of the record are checked here; exception texts below."""
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


# --- the boundary through the production wiring (R1-01…R1-05) ----------------------------------

SECRET = "DIFFSECRET-7f3a"


def _pydantic_error() -> None:
    TypeAdapter(str).validate_python({"diff": SECRET})


def _pydantic_cause() -> None:
    try:
        _pydantic_error()
    except ValidationError as error:
        raise ValueError("the review output is invalid") from error


def _multiline_message() -> None:
    raise RuntimeError(f"hunk failed:\n  +{SECRET} added line")


def _implicit_context() -> None:
    try:
        raise KeyError(SECRET)
    except KeyError:
        raise ValueError(SECRET)  # noqa: B904 - the implicit __context__ is the case under test


def _task_group() -> None:
    async def fail() -> None:
        raise ValueError(SECRET)

    async def run() -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(fail())

    asyncio.run(run())


def _raised(raise_it: Callable[[], None]) -> BaseException:
    try:
        raise_it()
    except BaseException as error:
        return error
    raise AssertionError("did not raise")


ERRORS = pytest.mark.parametrize(
    "raise_it",
    [_pydantic_error, _pydantic_cause, _multiline_message, _implicit_context, _task_group],
    ids=["pydantic", "pydantic-cause", "multiline", "context", "exception-group"],
)


@pytest.fixture
def wired() -> Iterator[Callable[[], list[ReadableSpan]]]:
    """``configure_tracing`` as the services call it, with an in-memory exporter behind it."""
    exporter = InMemorySpanExporter()
    _reset_global_provider()
    provider = configure_tracing("worker", {}, exporter=exporter)
    assert provider is not None

    def exported() -> list[ReadableSpan]:
        provider.force_flush()
        return list(exporter.get_finished_spans())

    yield exported
    provider.shutdown()
    _reset_global_provider()


def test_the_production_exporter_drops_attributes_outside_the_allowlist(
    wired: Callable[[], list[ReadableSpan]],
) -> None:
    with telemetry.tracer().start_as_current_span("llm.call", attributes={"request": SECRET}):
        pass

    [span] = wired()
    assert dict(span.attributes or {}) == {}


@ERRORS
def test_an_error_on_a_span_leaves_only_its_types_and_frames(
    wired: Callable[[], list[ReadableSpan]], raise_it: Callable[[], None]
) -> None:
    error = _raised(raise_it)
    with telemetry.tracer().start_as_current_span("review.run") as span:
        telemetry.mark_error(span, error)

    [exported] = wired()
    assert SECRET not in _texts(exported)
    assert exported.status.description == type(error).__name__
    [event] = exported.events
    stack = str((event.attributes or {})["exception.stacktrace"])
    assert 'File "' in stack and type(error).__qualname__ in stack


@ERRORS
def test_an_error_in_a_json_log_line_leaves_only_its_types_and_frames(
    raise_it: Callable[[], None],
) -> None:
    error = _raised(raise_it)
    record = _record("Run %s attempt failed: %s", RUN, "internal_error")
    record.exc_info = (type(error), error, error.__traceback__)

    line = JsonFormatter("worker").format(record)
    assert SECRET not in line
    entry = json.loads(line)
    assert entry["error_type"] == type(error).__name__
    assert 'File "' in entry["exc_info"]


def test_a_status_description_leaves_only_as_a_type_name(
    wired: Callable[[], list[ReadableSpan]],
) -> None:
    exchange = Exchange()

    class Failing(Exchange):
        async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
            raise ConnectionError(f"confirm failed for body: {SECRET}")

    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, Failing()), cast(AbstractExchange, exchange)
    )
    with pytest.raises(ConnectionError):
        asyncio.run(publisher.publish_confirmed(pending()))
    with telemetry.tracer().start_as_current_span("direct") as span:
        span.set_status(Status(StatusCode.ERROR, f"failed: {SECRET}"))

    published, direct = wired()
    assert published.status.status_code is StatusCode.ERROR
    assert published.status.description == "ConnectionError"
    assert direct.status.status_code is StatusCode.ERROR and direct.status.description is None
    assert SECRET not in _texts(published) + _texts(direct)


def test_configure_logging_installs_the_span_error_handler_and_quiets_httpx() -> None:
    configure_logging("worker")

    handlers = {handler.get_name(): handler for handler in logging.getLogger().handlers}
    assert isinstance(handlers["dmc-span-errors"], telemetry.SpanErrorHandler)
    assert handlers["dmc-span-errors"].level == logging.ERROR
    assert logging.getLogger("httpx").level == logging.WARNING


def test_a_failure_without_an_exception_marks_the_span_by_its_error_type(
    spans: InMemorySpanExporter, span_errors: None
) -> None:
    log = logging.getLogger("app.test")
    with telemetry.tracer().start_as_current_span("review.run"):
        log.error("Run %s attempt failed", RUN, extra={"error_type": "deadline_exceeded"})

    [exported] = spans.get_finished_spans()
    assert exported.status.status_code is StatusCode.ERROR
    assert (exported.attributes or {})["error.type"] == "deadline_exceeded"


def test_a_5xx_answer_without_an_exception_is_an_error_span(spans: InMemorySpanExporter) -> None:
    api = FastAPI()

    @api.get("/busy")
    def busy() -> JSONResponse:
        return JSONResponse({}, status_code=503)

    api.add_middleware(TracingMiddleware)
    assert TestClient(api).request("PURGE", "/busy").status_code == 405
    assert TestClient(api).get("/busy").status_code == 503

    other, busy_span = spans.get_finished_spans()
    assert other.name == "_OTHER /busy"
    assert (other.attributes or {})["http.request.method"] == "_OTHER"
    assert busy_span.status.status_code is StatusCode.ERROR


def test_the_publish_message_carries_the_trace_and_its_consumer_continues_it(
    spans: InMemorySpanExporter,
) -> None:
    exchange = Exchange()
    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, exchange), cast(AbstractExchange, exchange)
    )
    pointer = ReviewPublishPointer(RUN, "a" * 40, "b" * 64, "COMMENT")
    asyncio.run(publisher.publish_review(pointer))
    [message] = exchange.published
    seen: list[ReviewPublishPointer] = []

    async def handle(received: ReviewPublishPointer) -> None:
        seen.append(received)

    delivery = Delivery(message.body, headers=dict(message.headers or {}))
    asyncio.run(
        amqp.handle_publish_delivery(cast(AbstractIncomingMessage, delivery), handle, publisher)
    )

    assert seen == [pointer] and delivery.acked
    by_name = {span.name: span for span in spans.get_finished_spans()}
    produced, consumed = by_name["review.publish publish"], by_name["review.publish"]
    assert consumed.parent is not None and consumed.parent.span_id == produced.context.span_id


# --- entrypoints ----------------------------------------------------------------------------


def test_the_api_lifespan_configures_logs_and_traces_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as api_main

    calls: list[str] = []
    monkeypatch.setattr(api_main, "configure_logging", lambda service: calls.append(service))
    monkeypatch.setattr(api_main, "configure_tracing", lambda service: calls.append(service))
    monkeypatch.setattr(api_main, "shutdown_tracing", lambda _: calls.append("flush"))

    with TestClient(api_main.app):
        assert calls == ["api", "api"]
    assert calls[-1] == "flush"
    assert TracingMiddleware in [cast(object, entry.cls) for entry in api_main.app.user_middleware]


def test_worker_main_configures_logs_and_traces_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.worker as worker

    calls: list[str] = []
    monkeypatch.setattr(
        worker, "configure_logging", lambda service: calls.append(f"logs:{service}")
    )
    monkeypatch.setattr(worker, "configure_tracing", lambda service: calls.append(service))
    monkeypatch.setattr(worker, "shutdown_tracing", lambda _: calls.append("flush"))
    monkeypatch.setattr(worker, "run_with_signals", lambda _: calls.append("run"))

    worker.main()

    assert calls == ["logs:worker", "worker", "run", "flush"]


def test_a_failed_webhook_pass_is_an_error_span_without_its_message(
    spans: InMemorySpanExporter,
) -> None:
    class Failing:
        async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
            raise RuntimeError(SECRET)

        async def purge_finished(self) -> int:
            raise RuntimeError(SECRET)

    receiver = Failing()
    assert asyncio.run(webhook_worker._scan_once(WebhookReplayProvider("gitlab", receiver))) is None
    assert asyncio.run(webhook_worker.purge_once(receiver, provider="gitlab")) == 0

    replay, purge = spans.get_finished_spans()
    for span in (replay, purge):
        assert span.status.status_code is StatusCode.ERROR
        assert (span.attributes or {})["error.type"] == "RuntimeError"
        assert (span.attributes or {})["dmc.provider"] == "gitlab"
        assert SECRET not in _texts(span)
    assert (replay.name, purge.name) == ("webhook.replay", "webhook.purge")


def test_sigterm_stops_the_webhook_worker_and_flushes_the_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def forever() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.Event().wait()

    monkeypatch.setattr(webhook_worker, "configure_logging", lambda _: None)
    monkeypatch.setattr(webhook_worker, "configure_tracing", lambda _: "provider")
    monkeypatch.setattr(webhook_worker, "shutdown_tracing", calls.append)
    monkeypatch.setattr(webhook_worker, "run_forever", forever)

    webhook_worker.main()

    assert calls == ["provider"]


# --- run_id of the real call sites outside a delivery ------------------------------------------


def _pending_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    formatter = JsonFormatter("api")
    return [
        json.loads(formatter.format(record))
        for record in caplog.records
        if "remains pending" in record.getMessage()
    ]


def test_a_rerun_whose_publication_failed_logs_its_run_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests import test_run_detail as detail

    repository = detail.Reruns(RerunResult(RerunOutcome.CREATED, detail.pending(detail.OTHER)))
    with caplog.at_level(logging.ERROR):
        asyncio.run(
            RerunRun(
                lambda: repository, repository, detail.Publisher(fail=True), lambda: detail.NOW
            ).execute(detail.RUN)
        )

    [entry] = _pending_lines(caplog)
    assert entry["run_id"] == str(detail.OTHER)
    assert "broker down" not in json.dumps(entry)


def test_a_webhook_run_whose_publication_failed_logs_its_run_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests import test_github_run_trigger as trigger

    uow = trigger.Uow()
    use_case = TryEnqueueWebhookRun(
        eligibility=trigger.Eligibility(),
        uow_factory=lambda: uow,
        publisher=trigger.Publisher(uow, fail=True),
        now=lambda: trigger._NOW,
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(use_case.execute(trigger._PR, trigger._HEAD))

    [entry] = _pending_lines(caplog)
    assert entry["run_id"] == str(trigger._RUN)
