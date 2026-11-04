"""Suite-wide fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

# A shared module loaded with `pytest_plugins` is imported by its test files before pytest
# loads it as a plugin: registered here, it keeps the assertion rewriting of a test file.
pytest.register_assert_rewrite("tests.worker_queue_postgres")

# ``configure_logging`` (the app lifespan, worker mains) changes these process-wide.
_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    saved = [
        (logger, list(logger.handlers), logger.level, logger.propagate)
        for logger in map(logging.getLogger, _LOGGERS)
    ]
    yield
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
