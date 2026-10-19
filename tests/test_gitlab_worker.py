"""Pure worker settings, route binding and installed resource boundary controls."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.gitlab_reviews import build_gitlab_review_worker_resources
from app.modules.integrations.gitlab.infrastructure.settings import (
    GitLabRuntimeSettings,
    GitLabSettingsError,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.worker import BoundVcsProviderSelector, WorkerSettings
from tests.test_gitlab_run_http import INPUT
from tests.test_gitlab_worker_postgres import runtime_env
from tests.test_review_context_stage import Vcs

BASE = {"DATABASE_URL": "postgresql+psycopg://localhost/test", "RABBITMQ_URL": "amqp://localhost"}


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"SSL_CERT_FILE": "/missing.pem"},
        {"GITLAB_TOKEN": "legacy", "GITLAB_BASE_URL": "https://untrusted.example"},
    ],
)
def test_optional_runtime_never_opts_in_from_ca_or_legacy_pat(extra: dict[str, str]) -> None:
    assert GitLabRuntimeSettings.optional_from_environment({**BASE, **extra}) is None
    assert WorkerSettings.from_environment({**BASE, **extra}).gitlab is None


@pytest.mark.parametrize(
    "name",
    [
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
    ],
)
def test_partial_runtime_has_safe_explicit_kind(name: str) -> None:
    with pytest.raises(GitLabSettingsError) as failure:
        GitLabRuntimeSettings.optional_from_environment({**BASE, name: "[]"})
    assert failure.value.kind == "partial"
    assert str(failure.value) == "configuration_invalid"
    with pytest.raises(RuntimeError, match="^GitLab review worker configuration is invalid$"):
        WorkerSettings.from_environment({**BASE, name: "secret-canary"})


def test_worker_installed_settings_hide_credentials_and_fail_safe_on_tls_startup() -> None:
    env = runtime_env(BASE["DATABASE_URL"], BASE["RABBITMQ_URL"])
    settings = WorkerSettings.from_environment(env)
    assert settings.gitlab is not None
    assert "test-key" not in repr(settings)
    assert "kkkkkkkk" not in repr(settings.gitlab)
    with pytest.raises(RuntimeError, match="^GitLab review worker configuration is invalid$"):
        build_gitlab_review_worker_resources(
            settings=replace(settings.gitlab, ssl_cert_file="/missing-task-ca-canary.pem"),
            session_factory=async_sessionmaker[AsyncSession](),
        )


@pytest.mark.parametrize(
    "locator",
    [
        replace(INPUT.locator, provider="github"),
        replace(INPUT.locator, number=99),
        replace(INPUT.locator, repository_full_name="other/project"),
        replace(INPUT.locator, repository_external_id=19),
        replace(INPUT.locator, installation_external_id=2),
        replace(INPUT.locator, origin_provider_installation_id=UUID(int=9)),
    ],
)
def test_selector_requires_the_entire_original_locator(locator: PullRequestLocator) -> None:
    vcs = Vcs()
    selector = BoundVcsProviderSelector(INPUT.locator, vcs)
    assert selector.select(INPUT.locator) is vcs
    assert selector.select(locator) is None


@pytest.mark.parametrize("phase", ["startup", "body", "cancel", "close", "normal"])
def test_attempt_owns_and_closes_transport_preserving_primary(phase: str) -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    import httpx

    from app.modules.reviews.application.handle_review_run import ClaimedAttempt

    class Transport(httpx.AsyncBaseTransport):
        closed = 0

        async def __aenter__(self) -> Transport:
            if phase == "startup":
                raise ValueError("startup-primary")
            return self

        async def aclose(self) -> None:
            self.closed += 1
            if phase in {"startup", "body", "cancel", "close"}:
                raise RuntimeError("close-secondary")

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("lifecycle test performs no HTTP")

    transport = Transport()
    settings = WorkerSettings.from_environment(
        runtime_env(BASE["DATABASE_URL"], BASE["RABBITMQ_URL"])
    )
    assert settings.gitlab is not None
    resources = build_gitlab_review_worker_resources(
        settings=settings.gitlab,
        session_factory=async_sessionmaker[AsyncSession](),
        transport_factory=lambda: transport,
    )
    claimed = ClaimedAttempt(
        INPUT.code_change_id,
        INPUT.repository_id,
        1,
        "fast",
        datetime.now(UTC) + timedelta(minutes=1),
        INPUT.code_change_id,
        INPUT.repository_id,
        "test",
    )

    async def checkpoint() -> None:
        pass

    async def scenario() -> None:
        async with resources.attempt(claimed=claimed, expected_input=INPUT, checkpoint=checkpoint):
            if phase == "body":
                raise ValueError("body-primary")
            if phase == "cancel":
                raise asyncio.CancelledError()

    if phase in {"startup", "body"}:
        with pytest.raises(ValueError, match=f"^{phase}-primary$"):
            asyncio.run(scenario())
    elif phase == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(scenario())
    elif phase == "close":
        with pytest.raises(RuntimeError, match="^close-secondary$"):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert transport.closed == 1


def test_concurrent_attempts_have_independent_sources_and_context_stages() -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    import httpx

    from app.bootstrap.gitlab_reviews import GitLabAttemptBundle
    from app.modules.reviews.application.handle_review_run import ClaimedAttempt

    settings = WorkerSettings.from_environment(
        runtime_env(BASE["DATABASE_URL"], BASE["RABBITMQ_URL"])
    )
    assert settings.gitlab is not None
    transports: list[httpx.MockTransport] = []

    def transport() -> httpx.AsyncBaseTransport:
        item = httpx.MockTransport(lambda request: httpx.Response(500))
        transports.append(item)
        return item

    resources = build_gitlab_review_worker_resources(
        settings=settings.gitlab,
        session_factory=async_sessionmaker[AsyncSession](),
        transport_factory=transport,
    )
    claimed = ClaimedAttempt(
        INPUT.code_change_id,
        INPUT.repository_id,
        1,
        "fast",
        datetime.now(UTC) + timedelta(minutes=1),
        INPUT.code_change_id,
        INPUT.repository_id,
        "test",
    )

    async def checkpoint() -> None:
        pass

    async def scenario() -> None:
        bundles: list[GitLabAttemptBundle] = []
        barrier = asyncio.Barrier(2)

        async def attempt() -> None:
            async with resources.attempt(
                claimed=claimed, expected_input=INPUT, checkpoint=checkpoint
            ) as bundle:
                bundles.append(bundle)
                await barrier.wait()

        await asyncio.gather(attempt(), attempt())
        assert bundles[0].source is not bundles[1].source
        assert bundles[0].vcs is not bundles[1].vcs
        assert bundles[0].context_stage is not bundles[1].context_stage
        assert len(transports) == 2 and transports[0] is not transports[1]

    asyncio.run(scenario())
