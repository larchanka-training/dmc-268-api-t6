"""Shared OpenTelemetry test fakes and fixtures (#468)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.common.infrastructure.telemetry import RedactingSpanExporter


def reset_global_tracer_provider() -> None:
    """Reset the global OpenTelemetry tracer provider for tests."""
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._TRACER_PROVIDER = None


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """The exporter behind redaction, as production wires it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(exporter)))
    reset_global_tracer_provider()
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()
    reset_global_tracer_provider()
