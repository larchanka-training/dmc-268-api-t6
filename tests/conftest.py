"""Suite-wide fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

# Shared modules that test files import before pytest loads them through `pytest_plugins`:
# registered here, they keep the assertion rewriting and pytest does not warn that the module
# was already imported.
pytest.register_assert_rewrite(
    "tests.worker_queue_postgres", "tests.rules_md_e2e", "tests.monitoring_deploy_host"
)

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
