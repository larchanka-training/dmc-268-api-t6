from __future__ import annotations

from uuid import UUID

import pytest

from app.worker import WorkerSettings, run_url_factory


@pytest.mark.parametrize(
    "portal_url", ["https://ui.test", "https://ui.test/", "https://ui.test///"]
)
def test_run_url_links_to_the_run_and_normalizes_trailing_slashes(portal_url: str) -> None:
    run_url = run_url_factory(portal_url)

    assert (
        run_url(UUID("11111111-2222-3333-4444-555555555555"))
        == "https://ui.test/runs/11111111-2222-3333-4444-555555555555"
    )


@pytest.mark.parametrize("portal_url", [None, ""])
def test_run_url_is_absent_without_a_portal_url(portal_url: str | None) -> None:
    run_url = run_url_factory(portal_url)

    assert run_url(UUID("11111111-2222-3333-4444-555555555555")) is None


def test_worker_settings_reads_the_portal_url_from_environment() -> None:
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://app:app@db.test/app",
            "RABBITMQ_URL": "amqp://app:app@broker.test/",
            "PORTAL_URL": "https://ui.test",
        }
    )

    assert settings.portal_url == "https://ui.test"


@pytest.mark.parametrize("portal_env", [{}, {"PORTAL_URL": ""}], ids=["unset", "empty"])
def test_worker_settings_has_no_portal_url_when_unset_or_empty(portal_env: dict[str, str]) -> None:
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://app:app@db.test/app",
            "RABBITMQ_URL": "amqp://app:app@broker.test/",
            **portal_env,
        }
    )

    assert settings.portal_url is None
