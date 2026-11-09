"""GitHub device flow adapter follows the published OAuth wire contract."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.modules.auth.application.exchange_github_code import GitHubProviderUnavailable
from app.modules.auth.infrastructure.github_oauth import HttpGitHubDeviceOAuthClient


def test_github_device_flow_start_pending_and_authorized() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if len(requests) == 2:
            return httpx.Response(200, json={"error": "authorization_pending"})
        return httpx.Response(200, json={"access_token": "ghu_private", "token_type": "bearer"})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://github.com"
        ) as client:
            oauth = HttpGitHubDeviceOAuthClient(client, client_id="test-client")
            challenge = await oauth.start_device()
            assert challenge.device_code == "device-secret"
            assert (await oauth.poll_device(challenge.device_code)).status == "pending"
            authorized = await oauth.poll_device(challenge.device_code)
            assert authorized.status == "authorized"
            assert authorized.access_token == "ghu_private"

    asyncio.run(exercise())
    assert all(request.method == "POST" for request in requests)
    assert b"client_id=test-client" in requests[0].content
    assert b"device_code=device-secret" in requests[1].content
    assert b"client_secret" not in b"".join(request.content for request in requests)
    assert requests[0].headers["Accept"] == "application/json"


@pytest.mark.parametrize(
    ("payload", "status", "interval"),
    [
        ({"error": "slow_down", "interval": 11}, "slow_down", 11),
        ({"error": "access_denied"}, "denied", None),
        ({"error": "expired_token"}, "expired", None),
    ],
)
def test_github_device_poll_terminal_and_backoff_states(
    payload: dict[str, object], status: str, interval: int | None
) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
            base_url="https://github.com",
        ) as client:
            result = await HttpGitHubDeviceOAuthClient(client, client_id="test-client").poll_device(
                "device-secret"
            )
            assert result.status == status
            assert result.interval == interval

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, {"error": "incorrect_client_credentials"}),
        (200, {"error": "unknown_error"}),
        (200, {"access_token": ""}),
        (200, ["not-an-object"]),
    ],
)
def test_github_device_poll_rejects_provider_failures(status: int, payload: object) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)),
            base_url="https://github.com",
        ) as client:
            with pytest.raises(GitHubProviderUnavailable):
                await HttpGitHubDeviceOAuthClient(client, client_id="test-client").poll_device(
                    "device-secret"
                )

    asyncio.run(exercise())
