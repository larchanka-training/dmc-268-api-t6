"""GitHub device flow adapter follows the published OAuth wire contract."""

from __future__ import annotations

import asyncio

import httpx

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
