"""The HTTP clients on a fake opener: App authentication, paging, errors without secrets."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from review_bot.github import GitHubAppClient
from review_bot.ports import GitHubError, ReviewerRejected, TelegramError, TelegramRejected
from review_bot.telegram import TelegramClient

BOT_TOKEN = "123456:SECRET-bot-token"


class Response:
    def __init__(self, body: Any, link: str = "") -> None:
        self._raw = json.dumps(body).encode()
        self.headers = Message()
        if link:
            self.headers["Link"] = link

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class Opener:
    def __init__(self, routes: dict[str, list[Any]]) -> None:
        self.routes = routes
        self.calls: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Response:
        self.calls.append(request)
        key = f"{request.get_method()} {request.full_url}"
        answer = self.routes[key].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, Response) else Response(answer)


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(b"{}"))


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


API = "https://api.github.com"


def test_github_app_authenticates_once_and_pages_open_prs(key_pair: tuple[str, Any]) -> None:
    pem, public = key_pair
    pr = {
        "number": 5,
        "title": "T",
        "user": {"login": "author-x"},
        "html_url": "https://github.com/acme/api/pull/5",
        "draft": False,
        "labels": [{"name": "urgent"}],
        "requested_reviewers": [{"login": "bob-example"}],
        "requested_teams": [],
    }
    page2 = f"{API}/repos/acme/api/pulls?state=open&per_page=100&page=2"
    opener = Opener(
        {
            f"GET {API}/repos/acme/api/installation": [{"id": 77}],
            f"POST {API}/app/installations/77/access_tokens": [{"token": "inst-token"}],
            f"GET {API}/repos/acme/api/pulls?state=open&per_page=100": [
                Response([pr], link=f'<{page2}>; rel="next", <{page2}>; rel="last"')
            ],
            f"GET {page2}": [[{**pr, "number": 6, "draft": True}]],
            f"GET {API}/repos/acme/ui/pulls?state=open&per_page=100": [[]],
        }
    )
    client = GitHubAppClient("4242", pem, opener=opener, clock=lambda: 1_000_000.0)
    prs = client.open_pull_requests("acme", "api")
    assert [(p.number, p.draft) for p in prs] == [(5, False), (6, True)]
    assert prs[0].labels == frozenset({"urgent"})
    assert prs[0].requested_reviewers == ("bob-example",)
    assert client.open_pull_requests("acme", "ui") == []
    app_jwt = str(opener.calls[0].get_header("Authorization")).removeprefix("Bearer ")
    claims = jwt.decode(app_jwt, public, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "4242"
    # One installation token for both repos of the owner.
    urls = [c.full_url for c in opener.calls]
    assert urls.count(f"{API}/app/installations/77/access_tokens") == 1
    assert opener.calls[-1].get_header("Authorization") == "Bearer inst-token"


def test_github_request_reviewer_and_its_errors(key_pair: tuple[str, Any]) -> None:
    pem, _ = key_pair
    url = f"{API}/repos/acme/api/pulls/5/requested_reviewers"
    opener = Opener(
        {
            f"GET {API}/repos/acme/api/installation": [{"id": 1}],
            f"POST {API}/app/installations/1/access_tokens": [{"token": "inst-token"}],
            f"POST {url}": [{}, http_error(url, 422), http_error(url, 500)],
        }
    )
    client = GitHubAppClient("1", pem, opener=opener)
    client.request_reviewer("acme", "api", 5, "bob-example")
    assert json.loads(opener.calls[-1].data or b"") == {"reviewers": ["bob-example"]}  # type: ignore[arg-type]
    with pytest.raises(ReviewerRejected):
        client.request_reviewer("acme", "api", 5, "bob-example")
    with pytest.raises(GitHubError) as error:
        client.request_reviewer("acme", "api", 5, "bob-example")
    assert "inst-token" not in str(error.value)


def test_github_reviews_skip_pending_ones(key_pair: tuple[str, Any]) -> None:
    pem, _ = key_pair
    opener = Opener(
        {
            f"GET {API}/repos/acme/api/installation": [{"id": 1}],
            f"POST {API}/app/installations/1/access_tokens": [{"token": "t"}],
            f"GET {API}/repos/acme/api/pulls/5/reviews?per_page=100": [
                [
                    {"user": {"login": "a"}, "state": "APPROVED", "submitted_at": "2026-11-16"},
                    {"user": {"login": "b"}, "state": "PENDING"},
                ]
            ],
        }
    )
    reviews = GitHubAppClient("1", pem, opener=opener).reviews("acme", "api", 5)
    assert [(r.login, r.submitted) for r in reviews] == [("a", True), ("b", False)]


def test_telegram_errors_never_carry_the_token(caplog: pytest.LogCaptureFixture) -> None:
    base = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    opener = Opener(
        {
            f"POST {base}sendMessage": [
                {"ok": True, "result": {}},
                http_error(base + "sendMessage", 400),
                http_error(base + "sendMessage", 502),
                urllib.error.URLError(f"cannot reach {base}"),
            ],
            f"POST {base}getUpdates": [
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 9,
                            "message": {
                                "message_id": 3,
                                "chat": {"id": -100},
                                "from": {"username": "bob_tg"},
                                "text": "/mine",
                            },
                        }
                    ],
                }
            ],
        }
    )
    client = TelegramClient(BOT_TOKEN, opener=opener)
    caplog.set_level(logging.DEBUG)
    client.send_message("-100", "hi", reply_to=4)
    sent = json.loads(opener.calls[0].data or b"")  # type: ignore[arg-type]
    assert sent["parse_mode"] == "HTML" and sent["reply_parameters"]["message_id"] == 4
    with pytest.raises(TelegramRejected) as rejected:
        client.send_message("-100", "hi")
    with pytest.raises(TelegramError) as server_error:
        client.send_message("-100", "hi")
    assert not isinstance(server_error.value, TelegramRejected)
    with pytest.raises(TelegramError) as failed:
        client.send_message("-100", "hi")
    assert not isinstance(failed.value, TelegramRejected)
    (update,) = client.get_updates(offset=None, timeout=0)
    assert (update.update_id, update.chat_id, update.username) == (9, "-100", "bob_tg")
    for text in (str(rejected.value), str(server_error.value), str(failed.value), caplog.text):
        assert BOT_TOKEN not in text
