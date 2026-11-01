"""Traces (OpenTelemetry → Grafana Tempo) and JSON logs with ``run_id`` (docs/MONITORING.md §11).

Export is off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set: then spans go over OTLP/HTTP to
``<endpoint>/v1/traces``; without it the OpenTelemetry API stays a no-op and nothing is sent.

Data boundary (the one the monitoring roles keep, docs/MONITORING.md §3): spans carry only the
attributes of ``ALLOWED_ATTRIBUTES``. Request and response bodies of ``run_actions``, diffs and
review texts never reach a span, and the exporter drops any other attribute before export, so a
library or a later span cannot widen the boundary by accident. Log records carry the formatted
message, never their ``extra`` fields.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOGGER = logging.getLogger(__name__)

TRACER_NAME = "dmc-268"
ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

RUN_ID = "run_id"
TOOL = "dmc.tool"
STEP_INDEX = "dmc.step.index"
ATTEMPT = "dmc.attempt"
ENGINE = "dmc.engine"
TRIGGER = "dmc.trigger"
OUTCOME = "dmc.outcome"
ERROR_TYPE = "error.type"

# Everything a span may carry. Bodies, URLs, SQL and message payloads are not here.
ALLOWED_ATTRIBUTES = frozenset(
    {
        RUN_ID,
        TOOL,
        STEP_INDEX,
        ATTEMPT,
        ENGINE,
        TRIGGER,
        OUTCOME,
        ERROR_TYPE,
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "messaging.system",
        "messaging.operation.type",
        "messaging.destination.name",
    }
)
# The ``exception`` event of ``Span.record_exception``: the type and the frames of the stack. The
# message stays out: it can quote a payload (it is ``runs.error_message``, closed to Grafana, §3).
ALLOWED_EVENT_ATTRIBUTES = frozenset(
    {"exception.type", "exception.stacktrace", "exception.escaped"}
)

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def current_run_id() -> str | None:
    return _run_id.get()


@contextmanager
def bind_run_id(run_id: UUID | str) -> Iterator[None]:
    """Log records and spans started inside carry this ``run_id``."""
    token = _run_id.set(str(run_id))
    try:
        yield
    finally:
        _run_id.reset(token)


# --- logs -----------------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, ``run_id``, trace ids, error."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # A delivery binds run_id; elsewhere a call site passes extra={"run_id": ...}. These two
        # extras are the only ones read: any other extra may hold a body and stays out.
        run_id = current_run_id() or record.__dict__.get(RUN_ID)
        if run_id is not None:
            entry[RUN_ID] = str(run_id)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            entry["trace_id"] = format(context.trace_id, "032x")
            entry["span_id"] = format(context.span_id, "016x")
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
            entry["exc_info"] = self.formatException(record.exc_info)
        elif isinstance(record.__dict__.get("error_type"), str):
            entry["error_type"] = record.__dict__["error_type"]
        return json.dumps(entry, ensure_ascii=False, default=str)


class SpanErrorHandler(logging.Handler):
    """Error tracking: an ERROR record with an exception marks the current span.

    Most failures are caught and classified (a failed run, a redelivery) rather than raised, and
    only logged; this puts each of them on the trace of the run, with the type and the stack.
    """

    def __init__(self) -> None:
        super().__init__(logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if not record.exc_info or record.exc_info[1] is None:
            return
        span = trace.get_current_span()
        if span.is_recording():
            mark_error(span, record.exc_info[1])


_HANDLER_NAME = "dmc-json"
_SPAN_HANDLER_NAME = "dmc-span-errors"
# uvicorn installs its own text handlers on these; they go through the root handler instead.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(service: str, *, level: int = logging.INFO) -> None:
    """JSON lines on stderr from the root logger; idempotent, keeps foreign handlers (pytest)."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        name = handler.get_name()
        if name in (_HANDLER_NAME, _SPAN_HANDLER_NAME) or type(handler) is logging.StreamHandler:
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    span_errors = SpanErrorHandler()
    span_errors.set_name(_SPAN_HANDLER_NAME)
    root.addHandler(span_errors)
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


# --- traces ---------------------------------------------------------------------------------


def frames_only(stacktrace: str) -> str:
    """The frames of a formatted traceback, without the ``Type: message`` lines.

    The message of an exception (and of each chained cause) can quote a payload: a pydantic
    ``input_value`` of the model output, a URL with its query. Frames are code locations.
    """
    lines = [
        line
        for line in stacktrace.splitlines()
        if line.startswith(("Traceback (most recent call last)", "  "))
    ]
    return "\n".join(lines)


def _allowed(
    attributes: Mapping[str, AttributeValue] | None, allowed: frozenset[str]
) -> dict[str, AttributeValue]:
    kept: dict[str, AttributeValue] = {}
    for key, value in (attributes or {}).items():
        if key not in allowed:
            continue
        if key == "exception.stacktrace" and isinstance(value, str):
            value = frames_only(value)
        kept[key] = value
    return kept


def redact(span: ReadableSpan) -> ReadableSpan:
    """A copy of ``span`` with only the allowed attributes on it and its events."""
    events = [
        Event(event.name, _allowed(event.attributes, ALLOWED_EVENT_ATTRIBUTES), event.timestamp)
        for event in span.events
    ]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_allowed(span.attributes, ALLOWED_ATTRIBUTES),
        events=events,
        links=(),
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Applies the data boundary right before export, whatever set the attributes."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._exporter.export([redact(span) for span in spans])

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


def configure_tracing(
    service: str,
    environ: Mapping[str, str] = os.environ,
    *,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Install the global tracer provider, or return None and leave tracing a no-op."""
    endpoint = environ.get(ENDPOINT_ENV, "").strip()
    if exporter is None:
        if not endpoint:
            _LOGGER.info("Tracing is off: %s is not set", ENDPOINT_ENV)
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", timeout=5)
    # OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=staging) is merged in by the SDK.
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter)))
    trace.set_tracer_provider(provider)
    return provider


def shutdown_tracing(provider: TracerProvider | None) -> None:
    """Flush what is queued; an unreachable collector costs at most the exporter timeout."""
    if provider is not None:
        provider.shutdown()


_RECORDED = "__dmc_span_recorded__"


def mark_error(span: Span, error: BaseException) -> None:
    """Status ``error``, ``error.type`` and one ``exception`` event per exception and span."""
    marker = f"{span.get_span_context().span_id:016x}"
    if getattr(error, _RECORDED, None) == marker:
        return
    with suppress(AttributeError):
        setattr(error, _RECORDED, marker)
    span.record_exception(error)
    span.set_attribute(ERROR_TYPE, type(error).__name__)
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))


def _ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def record_step_span(
    *,
    run_id: UUID,
    tool: str,
    index: int,
    attempt: int,
    error_type: str | None,
    started_at: datetime,
    duration_ms: int,
) -> None:
    """A finished span for one ``run_actions`` row, under the current span (the run).

    The row already holds the timing, so the span is written after the fact with the same start
    and duration. ``error_type`` (None: the step succeeded) is all that leaves the row: never its
    request or response body.
    """
    span = tracer().start_span(
        tool,
        start_time=_ns(started_at),
        attributes={
            RUN_ID: str(run_id),
            TOOL: tool,
            STEP_INDEX: index,
            ATTEMPT: attempt,
        },
    )
    if error_type is not None:
        span.set_attribute(ERROR_TYPE, error_type)
        span.set_status(Status(StatusCode.ERROR, error_type))
    span.end(end_time=_ns(started_at + timedelta(milliseconds=duration_ms)))


# --- context through RabbitMQ ----------------------------------------------------------------


def inject_headers(headers: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Add ``traceparent`` (W3C) of the current span to AMQP headers."""
    propagate.inject(headers)
    return headers


@contextmanager
def publish_span(queue: str, run_id: UUID) -> Iterator[None]:
    with (
        bind_run_id(run_id),
        tracer().start_as_current_span(
            f"{queue} publish",
            kind=SpanKind.PRODUCER,
            attributes={
                RUN_ID: str(run_id),
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "publish",
                "messaging.destination.name": queue,
            },
        ),
    ):
        yield


@contextmanager
def consume_span(
    name: str,
    headers: Mapping[str, Any] | None,
    run_id: UUID,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Iterator[Span]:
    """The span of one delivery, continuing the trace of the publisher; binds ``run_id``.

    The handler catches its own errors (redelivery, DLQ) and marks the span with ``mark_error``;
    an error that escapes is marked here.
    """
    carrier = {key: value for key, value in (headers or {}).items() if isinstance(value, str)}
    with (
        bind_run_id(run_id),
        tracer().start_as_current_span(
            name,
            context=propagate.extract(carrier),
            kind=SpanKind.CONSUMER,
            attributes={
                RUN_ID: str(run_id),
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "process",
                **(attributes or {}),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span,
    ):
        try:
            yield span
        except Exception as error:
            mark_error(span, error)
            raise


# --- HTTP (API) -------------------------------------------------------------------------------

_UNTRACED_PATHS = frozenset({"/healthcheck"})


class TracingMiddleware:
    """A server span per HTTP request: method, route template and status, never the URL."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _UNTRACED_PATHS:
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method", "GET"))
        with tracer().start_as_current_span(
            method,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            started = False

            async def traced_send(message: Message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    status = int(message["status"])
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                await send(message)

            try:
                await self._app(scope, receive, traced_send)
            except Exception as error:
                # ServerErrorMiddleware, outside this one, answers 500 through its own send.
                if not started:
                    span.set_attribute("http.response.status_code", 500)
                mark_error(span, error)
                raise
            finally:
                route = getattr(scope.get("route"), "path", None)
                if isinstance(route, str):
                    span.set_attribute("http.route", route)
                    span.update_name(f"{method} {route}")
