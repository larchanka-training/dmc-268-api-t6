"""GitHub REST client acting as a GitHub App installation (JWT -> installation token).

Rate: one list call per repo and minute (two repos: 120 calls an hour) plus a reviews call only
when a request changes state, far below the installation limit of 5000 calls an hour.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import jwt

from review_bot.http import Opener, default_opener
from review_bot.models import PullRequest, Review
from review_bot.ports import GitHubError, ReviewerRejected

API = "https://api.github.com"
TIMEOUT_SECONDS = 20


class GitHubAppClient:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        opener: Opener = default_opener,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._opener = opener
        self._clock = clock
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}

    # authentication

    def _app_jwt(self) -> str:
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _token(self, owner: str, name: str) -> str:
        installation = self._installations.get(owner)
        if installation is None:
            data = self._call("GET", f"/repos/{owner}/{name}/installation", self._app_jwt())
            installation = int(data["id"])
            self._installations[owner] = installation
        cached = self._tokens.get(installation)
        if cached is not None and cached[1] - 300 > self._clock():
            return cached[0]
        data = self._call(
            "POST", f"/app/installations/{installation}/access_tokens", self._app_jwt()
        )
        token = str(data["token"])
        # Installation tokens live one hour; refresh five minutes before that.
        self._tokens[installation] = (token, self._clock() + 3600)
        return token

    # transport

    def _call(self, method: str, path: str, bearer: str, body: dict[str, Any] | None = None) -> Any:
        data, _ = self._request(method, API + path, bearer, body)
        return data

    def _request(
        self, method: str, url: str, bearer: str, body: dict[str, Any] | None = None
    ) -> tuple[Any, str]:
        request = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {bearer}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "dmc-268-review-bot",
                "Content-Type": "application/json",
            },
        )
        path = url.removeprefix(API).split("?")[0]
        try:
            with self._opener(request, TIMEOUT_SECONDS) as response:
                raw = response.read()
                link = response.headers.get("Link", "") or ""
        except urllib.error.HTTPError as error:
            if error.code == 422:
                raise ReviewerRejected(f"GitHub {method} {path}: HTTP 422") from None
            raise GitHubError(f"GitHub {method} {path}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GitHubError(f"GitHub {method} {path}: {type(error).__name__}") from None
        return (json.loads(raw) if raw else None), link

    def _paged(self, path: str, token: str) -> list[Any]:
        items: list[Any] = []
        url: str | None = API + path
        while url:
            data, link = self._request("GET", url, token)
            items.extend(data)
            url = _next_link(link)
        return items

    # GitHubPort

    def open_pull_requests(self, owner: str, name: str) -> list[PullRequest]:
        token = self._token(owner, name)
        raw = self._paged(f"/repos/{owner}/{name}/pulls?state=open&per_page=100", token)
        return [
            PullRequest(
                repo=f"{owner}/{name}",
                number=int(item["number"]),
                title=str(item["title"]),
                author=str(item["user"]["login"]),
                url=str(item["html_url"]),
                draft=bool(item.get("draft", False)),
                labels=frozenset(str(label["name"]) for label in item.get("labels", [])),
                requested_reviewers=tuple(
                    str(user["login"]) for user in item.get("requested_reviewers", [])
                ),
                requested_teams=tuple(
                    str(team["slug"]) for team in item.get("requested_teams", [])
                ),
            )
            for item in raw
        ]

    def reviews(self, owner: str, name: str, number: int) -> list[Review]:
        token = self._token(owner, name)
        raw = self._paged(f"/repos/{owner}/{name}/pulls/{number}/reviews?per_page=100", token)
        return [
            Review(
                login=str((item.get("user") or {}).get("login", "")),
                submitted=item.get("state") != "PENDING" and bool(item.get("submitted_at")),
            )
            for item in raw
        ]

    def request_reviewer(self, owner: str, name: str, number: int, login: str) -> None:
        token = self._token(owner, name)
        self._call(
            "POST",
            f"/repos/{owner}/{name}/pulls/{number}/requested_reviewers",
            token,
            {"reviewers": [login]},
        )


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        section = part.split(";")
        if len(section) >= 2 and section[1].strip() == 'rel="next"':
            return section[0].strip().strip("<>")
    return None
