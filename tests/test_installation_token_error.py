"""Onboarding adapters report a failed token mint as one typed, installation-wide error (api#73).

The reviewer timeline adapter is not wired by ``github_installation_delivery_dispatcher``,
so no test here covers it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationEventProjector,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    GitHubInstallationTreeProvider,
    InMemoryInstallationAccessTokenCache,
    TokenErrorClassifyingProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_labels import (
    GitHubRepositoryLabelProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositoryReference,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)
from app.modules.reviews.application.try_enqueue_webhook_run import RunMessagePublisher


@dataclass
class _Inner:
    """A token provider that answers, or raises ``error`` when one is set."""

    error: BaseException | None = None
    calls: list[int] = field(default_factory=list)

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls.append(installation_external_id)
        if self.error is not None:
            raise self.error
        return "installation-token"


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        "https://api.github.com/app/installations/17/access_tokens?jwt=SENTINEL-jwt",
    )
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request,
        response=httpx.Response(status, request=request, text="SENTINEL-body"),
    )


def test_a_token_is_passed_through_unchanged() -> None:
    inner = _Inner()

    token = asyncio.run(TokenErrorClassifyingProvider(inner).get_installation_access_token(17))

    assert token == "installation-token"
    assert inner.calls == [17]


@pytest.mark.parametrize(
    "original",
    [
        _status_error(401),
        _status_error(404),
        _status_error(502),
        httpx.ConnectTimeout("SENTINEL-timeout"),
        ValueError("SENTINEL-bad-response"),
    ],
    ids=["401", "404", "502", "transport", "bad-response"],
)
def test_any_failure_becomes_the_typed_error_with_the_original_as_its_cause(
    original: Exception,
) -> None:
    with pytest.raises(InstallationAccessTokenError) as raised:
        asyncio.run(
            TokenErrorClassifyingProvider(_Inner(original)).get_installation_access_token(17)
        )

    assert raised.value.__cause__ is original
    assert "SENTINEL" not in str(raised.value)
    assert "api.github.com" not in str(raised.value)
    assert "access_tokens" not in str(raised.value)


@pytest.mark.parametrize(
    ("original", "transient"),
    [
        (_status_error(401), True),
        (_status_error(502), True),
        (httpx.ConnectTimeout("SENTINEL-timeout"), True),
        (httpx.ConnectError("SENTINEL-refused"), True),
        (ValueError("SENTINEL-bad-response"), False),
    ],
    ids=["401", "502", "timeout", "refused", "malformed-response"],
)
def test_only_a_failure_github_could_not_answer_is_transient(
    original: Exception, transient: bool
) -> None:
    """An HTTP error of the mint can heal on a later attempt; a malformed response cannot."""
    with pytest.raises(InstallationAccessTokenError) as raised:
        asyncio.run(
            TokenErrorClassifyingProvider(_Inner(original)).get_installation_access_token(17)
        )

    assert raised.value.transient is transient


def test_an_app_key_that_cannot_sign_is_a_permanent_token_error() -> None:
    """The real JWT encoder rejects the key before any request: no retry can heal that."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("no token request without a signed App JWT")

    async def get_token() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id="123",
                private_key="SENTINEL-not-a-private-key",
                cache=InMemoryInstallationAccessTokenCache(now=lambda: 1_800_000_000),
                now=lambda: 1_800_000_000,
            )
            await TokenErrorClassifyingProvider(provider).get_installation_access_token(17)

    with pytest.raises(InstallationAccessTokenError) as raised:
        asyncio.run(get_token())

    assert raised.value.transient is False
    assert raised.value.__cause__ is not None
    assert not isinstance(raised.value.__cause__, httpx.HTTPError)
    assert requests == []
    assert "SENTINEL" not in str(raised.value)


def test_cancellation_is_not_converted() -> None:
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            TokenErrorClassifyingProvider(
                _Inner(asyncio.CancelledError())
            ).get_installation_access_token(17)
        )


def test_the_real_provider_failure_keeps_its_status_as_the_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request, json={"message": "Bad credentials"})

    async def get_token() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id="123",
                private_key="test-private-key",
                jwt_encoder=lambda claims, private_key: "app-jwt",
                cache=InMemoryInstallationAccessTokenCache(now=lambda: 1_800_000_000),
                now=lambda: 1_800_000_000,
            )
            await TokenErrorClassifyingProvider(provider).get_installation_access_token(17)

    with pytest.raises(InstallationAccessTokenError) as raised:
        asyncio.run(get_token())

    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)
    assert raised.value.__cause__.response.status_code == 401


def test_only_the_three_onboarding_adapters_receive_the_classifying_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pull request and CI adapters keep the raw provider, as the reviews worker does."""
    received: dict[str, object] = {}

    def recording(name: str) -> Callable[..., object]:
        def build(**kwargs: object) -> object:
            received[name] = kwargs["token_provider"]
            return object()

        return build

    for name, target in {
        "tree": "GitHubInstallationTreeProvider",
        "label": "GitHubRepositoryLabelProvider",
        "details": "GitHubInstallationRepositoryDetailsProvider",
        "current-pull-request": "HttpGitHubCurrentPullRequestProvider",
        "ci": "HttpGitHubCurrentHeadCiProvider",
    }.items():
        monkeypatch.setattr(f"app.bootstrap.reviews_api.{target}", recording(name))
    raw = _Inner()

    async def compose() -> None:
        async with httpx.AsyncClient(base_url="https://api.github.com") as client:
            ReviewsApiResources(
                cast(AsyncEngine, object()), cast(async_sessionmaker[AsyncSession], object())
            ).github_installation_delivery_dispatcher(
                client=client,
                token_provider=raw,
                bot_login="reviewer[bot]",
                run_publisher=cast(RunMessagePublisher, object()),
                app_id=1,
            )

    asyncio.run(compose())

    assert set(received) == {"tree", "label", "details", "current-pull-request", "ci"}
    for name in ("tree", "label", "details"):
        assert isinstance(received[name], TokenErrorClassifyingProvider)
    assert received["current-pull-request"] is raw
    assert received["ci"] is raw


@dataclass
class _Sync:
    calls: list[tuple[RepositoryOnboardingInput, ...]] = field(default_factory=list)

    async def execute(
        self, *, provider_installation_id: UUID, repositories: tuple[RepositoryOnboardingInput, ...]
    ) -> tuple[()]:
        self.calls.append(repositories)
        return ()

    async def disable(self, *, provider_installation_id: UUID, repositories: object) -> None:
        raise AssertionError("an added event disables nothing")


_TWENTY_BARE_REPOSITORIES = tuple(
    RepositoryReference(
        external_id=item,
        full_name=f"example-owner/repo-{item}",
        default_branch=None,
        web_url=None,
    )
    for item in range(1, 21)
)


def _revoked_app_key(requests: list[httpx.Request]) -> httpx.MockTransport:
    def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, request=request, json={"message": "Bad credentials"})

    return httpx.MockTransport(github)


def _real_projector(
    client: httpx.AsyncClient, sync: _Sync, slots: int
) -> InstallationEventProjector:
    """Real adapters over the real single-flight provider and a cold token cache."""
    tokens = TokenErrorClassifyingProvider(
        GitHubAppInstallationAccessTokenProvider(
            client=client,
            app_id="123",
            private_key="test-private-key",
            jwt_encoder=lambda claims, private_key: "app-jwt",
            cache=InMemoryInstallationAccessTokenCache(now=lambda: 1_800_000_000),
            now=lambda: 1_800_000_000,
        )
    )
    return InstallationEventProjector(
        tree_provider=GitHubInstallationTreeProvider(client=client, token_provider=tokens),
        label_provider=GitHubRepositoryLabelProvider(client=client, token_provider=tokens),
        details_provider=GitHubInstallationRepositoryDetailsProvider(
            client=client, token_provider=tokens
        ),
        sync=sync,
        max_concurrent_repositories=slots,
    )


@pytest.mark.parametrize("slots", [1, 4])
def test_a_revoked_app_key_costs_one_token_request_for_twenty_repositories(
    caplog: pytest.LogCaptureFixture, slots: int
) -> None:
    """Real adapters over the real single-flight provider and a cold cache: one mint, not N."""
    requests: list[httpx.Request] = []
    sync = _Sync()

    async def onboard() -> None:
        async with httpx.AsyncClient(
            transport=_revoked_app_key(requests), base_url="https://api.github.com"
        ) as client:
            await _real_projector(client, sync, slots).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(17, "added", _TWENTY_BARE_REPOSITORIES, ()),
            )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(InstallationAccessTokenError) as raised,
    ):
        asyncio.run(onboard())

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/app/installations/17/access_tokens")
    ]
    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)
    assert raised.value.__cause__.response.status_code == 401
    assert sync.calls == []
    assert f"skipped={20 - slots}" in caplog.text


@dataclass
class _Linked:
    """The installation is linked to a workspace."""

    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        assert external_id == 17
        return uuid4()


@pytest.mark.parametrize("slots", [1, 4])
def test_a_revoked_app_key_defers_the_delivery_after_one_token_request(
    caplog: pytest.LogCaptureFixture, slots: int
) -> None:
    """Through the dispatcher the token failure defers the delivery like unreadable details."""
    requests: list[httpx.Request] = []
    sync = _Sync()

    async def dispatch() -> InstallationDeliveryDispatchResult:
        async with httpx.AsyncClient(
            transport=_revoked_app_key(requests), base_url="https://api.github.com"
        ) as client:
            return await GitHubInstallationDeliveryDispatcher(
                resolver=_Linked(), onboarding=_real_projector(client, sync, slots)
            ).execute(
                GitHubDispatchEvent(
                    "delivery-revoked-key",
                    InstallationRepositoriesEvent(17, "added", _TWENTY_BARE_REPOSITORIES, ()),
                )
            )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(dispatch())

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/app/installations/17/access_tokens")
    ]
    assert sync.calls == []
    assert f"skipped={20 - slots}" in caplog.text
    assert (
        "Repository details unavailable for installation 17: "
        "GitHub installation access token unavailable"
    ) in caplog.text


@pytest.mark.parametrize("slots", [1, 4])
def test_a_malformed_token_response_fails_the_delivery_after_one_token_request(
    caplog: pytest.LogCaptureFixture, slots: int
) -> None:
    """A malformed token response is permanent (api#71): the dispatcher does not defer it."""
    requests: list[httpx.Request] = []

    def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, request=request, json={"SENTINEL": "no token"})

    sync = _Sync()

    async def dispatch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(github), base_url="https://api.github.com"
        ) as client:
            await GitHubInstallationDeliveryDispatcher(
                resolver=_Linked(), onboarding=_real_projector(client, sync, slots)
            ).execute(
                GitHubDispatchEvent(
                    "delivery-malformed-token",
                    InstallationRepositoriesEvent(17, "added", _TWENTY_BARE_REPOSITORIES, ()),
                )
            )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(InstallationAccessTokenError) as raised,
    ):
        asyncio.run(dispatch())

    assert raised.value.transient is False
    assert isinstance(raised.value.__cause__, ValueError)
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/app/installations/17/access_tokens")
    ]
    assert sync.calls == []
    assert f"skipped={20 - slots}" in caplog.text
    assert "Repository details unavailable" not in caplog.text
    assert "SENTINEL" not in caplog.text
