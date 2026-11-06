"""The run data retention setting and its schedule on the worker leader (#457)."""

from __future__ import annotations

import pytest

from app.bootstrap.run_data_retention import RunDataRetentionSettings
from app.worker import WorkerSettings

_WORKER_ENV = {
    "DATABASE_URL": "postgresql+psycopg://unused/unused",
    "RABBITMQ_URL": "amqp://unused/",
}


@pytest.mark.parametrize(
    ("env", "days"),
    [
        pytest.param({}, 90, id="unset"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": ""}, 90, id="empty"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": "1"}, 1, id="lowest"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": " 30 "}, 30, id="spaces"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": "3650"}, 3650, id="highest"),
    ],
)
def test_the_retention_period_comes_from_the_environment(env: dict[str, str], days: int) -> None:
    assert RunDataRetentionSettings.from_environment(env).days == days


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0", id="zero"),
        pytest.param("3651", id="above-ten-years"),
        pytest.param("-1", id="negative"),
        pytest.param("1.5", id="fraction"),
        pytest.param("three months", id="words"),
        pytest.param("٩٠", id="non-ascii-digits"),
        pytest.param("1" * 19, id="too-many-digits"),
        pytest.param(" ", id="blank"),
    ],
)
def test_an_invalid_retention_period_stops_the_worker_start(value: str) -> None:
    with pytest.raises(ValueError, match="RUN_DATA_RETENTION_DAYS"):
        WorkerSettings.from_environment({**_WORKER_ENV, "RUN_DATA_RETENTION_DAYS": value})


def test_the_worker_settings_read_the_retention_period() -> None:
    assert WorkerSettings.from_environment(_WORKER_ENV).run_data_retention.days == 90
    settings = WorkerSettings.from_environment({**_WORKER_ENV, "RUN_DATA_RETENTION_DAYS": "45"})
    assert settings.run_data_retention.days == 45
