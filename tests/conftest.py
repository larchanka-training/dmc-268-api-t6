"""Suite-wide fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

# Shared test modules, rewritten before their first import to keep the assertion operand diff.
# Two reasons: a module loaded with `pytest_plugins` is imported by its test files before pytest
# loads it as a plugin (and pytest warns that it is already imported), and the deploy and
# monitoring fakes are plain modules whose helpers assert.
pytest.register_assert_rewrite(
    "tests.worker_queue_postgres",
    "tests.webhook_notifications_postgres",
    "tests.rules_md_e2e",
    "tests.deploy_host_fakes",
    "tests.deploy_prod_workflow_fakes",
    "tests.monitoring_deploy_host",
    "tests.code_index_postgres",
    "tests.portal_postgres",
    "tests.code_index_indexer_support",
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
