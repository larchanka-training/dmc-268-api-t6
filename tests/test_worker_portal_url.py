from __future__ import annotations

from traceback import format_exception
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


_BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://app:app@db.test/app",
    "RABBITMQ_URL": "amqp://app:app@broker.test/",
}


@pytest.mark.parametrize(
    ("portal_url", "expected_run_url"),
    [
        (
            "https://staging-ui.dmc268-t6.axyi.ru",
            "https://staging-ui.dmc268-t6.axyi.ru/runs/11111111-2222-3333-4444-555555555555",
        ),
        ("https://ui.test", "https://ui.test/runs/11111111-2222-3333-4444-555555555555"),
        ("https://ui.test/", "https://ui.test/runs/11111111-2222-3333-4444-555555555555"),
        ("https://ui.test///", "https://ui.test/runs/11111111-2222-3333-4444-555555555555"),
        (
            "http://localhost:5173",
            "http://localhost:5173/runs/11111111-2222-3333-4444-555555555555",
        ),
        (
            "http://127.0.0.1:8080",
            "http://127.0.0.1:8080/runs/11111111-2222-3333-4444-555555555555",
        ),
        ("http://[::1]:5173", "http://[::1]:5173/runs/11111111-2222-3333-4444-555555555555"),
        ("http://[::1]", "http://[::1]/runs/11111111-2222-3333-4444-555555555555"),
        (
            "https://ui.test:65535",
            "https://ui.test:65535/runs/11111111-2222-3333-4444-555555555555",
        ),
        (
            "https://ui.test/portal/",
            "https://ui.test/portal/runs/11111111-2222-3333-4444-555555555555",
        ),
        ("HTTPS://ui.test", "HTTPS://ui.test/runs/11111111-2222-3333-4444-555555555555"),
        (
            "https://ui.test/portal%20path/%E2%9C%93/",
            "https://ui.test/portal%20path/%E2%9C%93/runs/11111111-2222-3333-4444-555555555555",
        ),
    ],
)
def test_worker_settings_preserve_valid_portal_bases_and_generated_links(
    portal_url: str, expected_run_url: str
) -> None:
    settings = WorkerSettings.from_environment({**_BASE_ENV, "PORTAL_URL": portal_url})

    assert settings.portal_url == portal_url
    assert (
        run_url_factory(settings.portal_url)(UUID("11111111-2222-3333-4444-555555555555"))
        == expected_run_url
    )


@pytest.mark.parametrize(
    "portal_url",
    [
        "ui.test",
        "/portal",
        "//ui.test",
        "ftp://ui.test",
        "https:///portal",
        "https://",
        "https://:443",
        " ",
        " https://ui.test",
        "https://ui.test ",
        "https://ui .test",
        "https://ui.test/\tpath",
        "https://ui.test/\npath",
        "https://ui.test/\rpath",
        "https://ui.test/\x00path",
        "https://ui.test/\x1fpath",
        "https://ui.test/\x7fpath",
        "https://ui.test/\x85path",
        "https://ui.test/?next=portal",
        "https://ui.test?",
        "https://ui.test/#run",
        "https://ui.test#",
        "https://user:secret@ui.test",
        "https://@ui.test",
        "https://[::1",
        "https://[::1]garbage",
        "https://[::1]garbage:80",
        "https://[::1]:",
        "https://ui.test:",
        "https://ui.test:bad",
        "https://ui.test:65536",
        "https://ui.test:0",
        "https://ui.test/\u200bpath",
        "https://ui.test\\portal",
    ],
)
def test_worker_settings_reject_malformed_nonempty_portal_urls(portal_url: str) -> None:
    with pytest.raises(RuntimeError, match="PORTAL_URL"):
        WorkerSettings.from_environment({**_BASE_ENV, "PORTAL_URL": portal_url})


@pytest.mark.parametrize(
    "portal_url",
    [
        "https://alice:do-not-log-this@ui.test:bad",
        "https://alice:do-not-log-this@ui.test／x",
    ],
    ids=["bad-port", "nfkc-netloc"],
)
def test_worker_settings_do_not_echo_a_credential_bearing_portal_url(portal_url: str) -> None:
    with pytest.raises(RuntimeError, match="PORTAL_URL") as raised:
        WorkerSettings.from_environment({**_BASE_ENV, "PORTAL_URL": portal_url})

    assert portal_url not in str(raised.value)
    diagnostic = "".join(format_exception(raised.value))
    assert "do-not-log-this" not in diagnostic
    assert portal_url not in diagnostic
