"""Validated GitHub request path segments: names and SHAs are untrusted input (api#73)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pydantic
import pytest

from app.common.infrastructure.github_repository_path import (
    InvalidGitHubPathSegment,
    commit_sha_segment,
    ref_segment,
    repository_path_from_full_name,
)
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.pull_request_dtos import parse_pull_request_event
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.infrastructure.github_current_pull_request import (
    HttpGitHubCurrentPullRequestProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationTreeProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_labels import (
    GitHubRepositoryLabelProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_reviewer_timeline import (
    HttpGitHubReviewerTimelineProvider,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.reviews.application.check_runs import CheckRunTarget, CheckRunView
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestState,
)
from app.modules.reviews.application.publish_run_review import ReviewSubmission
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsProvider
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubCheckRunGateway,
    GitHubPullRequestReviewGateway,
)
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider

_SHA_40 = "0123456789abcdef0123456789abcdef01234567"
_SHA_64 = "0123456789abcdef" * 4
_API = "https://api.github.com"


def _raw_path(path: str) -> bytes:
    """The path as httpx puts it on the wire: it collapses ``.`` and ``..`` segments."""
    return httpx.Request("GET", f"{_API}{path}").url.raw_path


@pytest.mark.parametrize(
    "full_name",
    [
        "example-owner/example-repo",
        "octo/.github",
        "octo_acme/repo",
        "owner/repo.js",
        "o/r_",
        "o/-x",
        "o/r.",
        "o/...x",
        "9/9",
    ],
)
def test_repository_path_keeps_a_valid_name_byte_for_byte(full_name: str) -> None:
    path = repository_path_from_full_name(full_name)

    assert path == f"/repos/{full_name}"
    assert _raw_path(path) == f"/repos/{full_name}".encode()


@pytest.mark.parametrize(
    "full_name",
    [
        "",
        "o",
        "/",
        "o/",
        "/r",
        "o/.",
        "o/..",
        "o/...",
        "../r",
        "./r",
        "o/../../pulls/1",
        "o/r/x",
        "a/b/c",
        "o//r",
        "o.x/r",
        "-o/r",
        "o /r",
        "o/r p",
        "o/r with space",
        "o/r\n",
        "o/r\t",
        "o/r\x00",
        "o/r?x",
        "o/r#x",
        "o/r%2Fx",
        "o/%2e%2e",
        "o/%2E",
        "o/r\\x",
        "o/r١",
        "o/ré",
        "é/r",
    ],
)
def test_repository_path_rejects_a_name_outside_owner_slash_repo(full_name: str) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        repository_path_from_full_name(full_name)

    assert isinstance(raised.value, ValueError)


def test_repository_path_accepts_a_name_of_512_characters_and_rejects_one_of_513() -> None:
    longest = "o/" + "r" * 510
    too_long = "o/" + "r" * 511

    assert repository_path_from_full_name(longest) == f"/repos/{longest}"
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        repository_path_from_full_name(too_long)
    assert too_long not in str(raised.value)


def test_repository_path_rejects_an_overlong_name_before_the_regex_can_backtrack() -> None:
    """``o/a.a.a....!`` costs seconds in the regex once it has tens of thousands of pieces."""
    crafted = "o/" + "a." * 20_000 + "!"
    started = time.perf_counter()

    with pytest.raises(InvalidGitHubPathSegment):
        repository_path_from_full_name(crafted)

    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("main", "main"),
        ("release/1.0", "release%2F1.0"),
        ("feat/a b", "feat%2Fa%20b"),
        ("u\u00fcber", "u%C3%BCber"),
        ("a?b", "a%3Fb"),
        ("a#b", "a%23b"),
        ("a..b", "a..b"),
        (".x", ".x"),
        ("...", "..."),
        ("%2e", "%252e"),
    ],
)
def test_ref_segment_percent_encodes_the_whole_name(name: str, encoded: str) -> None:
    segment = ref_segment(name)

    assert segment == encoded
    assert (
        _raw_path(f"/repos/o/r/git/trees/{segment}") == f"/repos/o/r/git/trees/{encoded}".encode()
    )


def test_ref_segment_keeps_a_slashed_branch_in_one_path_segment() -> None:
    assert _raw_path(f"/repos/o/r/git/trees/{ref_segment('a/b/c')}") == (
        b"/repos/o/r/git/trees/a%2Fb%2Fc"
    )


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_ref_segment_rejects_what_would_walk_the_request_path(name: str) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        ref_segment(name)

    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize("sha", [_SHA_40, _SHA_64, _SHA_40.upper(), "aB" * 20, "0" * 40, "f" * 64])
def test_commit_sha_segment_returns_a_full_hex_sha_unchanged(sha: str) -> None:
    assert commit_sha_segment(sha) == sha


@pytest.mark.parametrize(
    "sha",
    [
        "",
        "main",
        "..",
        "../../x",
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "a" * 128,
        "g" * 40,
        f"{_SHA_40}\n",
        f" {_SHA_40}",
        f"{_SHA_40[:-1]}/",
        f"{_SHA_40[:-3]}%2e.",
        "١" * 40,
    ],
)
def test_commit_sha_segment_rejects_anything_but_a_full_hex_sha(sha: str) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        commit_sha_segment(sha)

    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize(
    ("build", "value"),
    [
        (repository_path_from_full_name, "leak-marker/../../pulls/1"),
        (repository_path_from_full_name, "leak-marker/%2e%2e"),
        (ref_segment, ".."),
        (commit_sha_segment, "leak-marker/../.."),
    ],
)
def test_the_error_never_carries_the_rejected_value(
    build: Callable[[str], str], value: str
) -> None:
    with pytest.raises(InvalidGitHubPathSegment) as raised:
        build(value)

    assert "leak-marker" not in str(raised.value)
    assert "leak-marker" not in repr(raised.value)
    assert raised.value.args == (str(raised.value),)
    assert ".." not in str(raised.value)


# --- Webhook adapters: a bad name is rejected before a token is minted or a request is sent ---


@dataclass
class _Tokens:
    calls: list[int] = field(default_factory=list)

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls.append(installation_external_id)
        return "installation-token"


@dataclass
class _Wire:
    """Records what reaches the transport; ``answer`` is the body of every reply."""

    answer: object = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, request=request, json=self.answer)


def _run_on_wire(
    action: Callable[[httpx.AsyncClient, _Tokens], Awaitable[object]], wire: _Wire, tokens: _Tokens
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(wire.handler), base_url=_API
        ) as client:
            await action(client, tokens)

    asyncio.run(run())


def _pull_request_event(full_name: str) -> PullRequestEvent:
    return PullRequestEvent(
        action="synchronize",
        installation_external_id=17,
        repository_external_id=101,
        external_id=901,
        number=7,
        title="Add parser",
        description=None,
        author_login="alice",
        web_url="https://github.com/octo/repo/pull/7",
        source_branch="feature/parser",
        target_branch="main",
        base_sha="b" * 40,
        head_sha="a" * 40,
        state=PullRequestState.OPEN,
        provider_updated_at=datetime(2026, 9, 28, 11, 59, tzinfo=UTC),
        repository_full_name=full_name,
    )


def _snapshot(full_name: str, default_branch: str = "main") -> RepositorySnapshot:
    return RepositorySnapshot(
        external_id=101,
        full_name=full_name,
        default_branch=default_branch,
        web_url="https://github.com/octo/repo",
    )


_WEBHOOK_SINKS: dict[str, Callable[[httpx.AsyncClient, _Tokens, str], Awaitable[object]]] = {
    "reviewer-timeline": lambda client, tokens, full_name: HttpGitHubReviewerTimelineProvider(
        client=client, token_provider=tokens
    ).snapshot(_pull_request_event(full_name), "reviewer[bot]"),
    "current-pull-request": lambda client, tokens, full_name: HttpGitHubCurrentPullRequestProvider(
        client=client, token_provider=tokens
    ).get_current(_pull_request_event(full_name)),
    "repository-details": lambda client, tokens, full_name: (
        GitHubInstallationRepositoryDetailsProvider(
            client=client, token_provider=tokens
        ).fetch_repository_details(installation_external_id=17, full_name=full_name)
    ),
    "repository-label": lambda client, tokens, full_name: GitHubRepositoryLabelProvider(
        client=client, token_provider=tokens
    ).create_ai_review_label(installation_external_id=17, repository=_snapshot(full_name)),
    "installation-tree": lambda client, tokens, full_name: GitHubInstallationTreeProvider(
        client=client, token_provider=tokens
    ).fetch_default_branch_tree(installation_external_id=17, repository=_snapshot(full_name)),
}

_BAD_FULL_NAMES = ["o/..", "../r", "o/../../pulls/1", "o/r?x", "o/r#x", "o/r%2Fx", "a/b/c", "o /r"]


@pytest.mark.parametrize("sink", _WEBHOOK_SINKS)
@pytest.mark.parametrize("full_name", _BAD_FULL_NAMES)
def test_webhook_adapter_rejects_a_bad_repository_name_before_any_request(
    sink: str, full_name: str
) -> None:
    wire, tokens = _Wire(), _Tokens()

    with pytest.raises(InvalidGitHubPathSegment):
        _run_on_wire(lambda client, t: _WEBHOOK_SINKS[sink](client, t, full_name), wire, tokens)

    assert wire.requests == []
    assert tokens.calls == []


@pytest.mark.parametrize("default_branch", ["..", "."])
def test_tree_provider_rejects_a_default_branch_that_walks_the_path_before_any_request(
    default_branch: str,
) -> None:
    wire, tokens = _Wire(), _Tokens()

    with pytest.raises(InvalidGitHubPathSegment):
        _run_on_wire(
            lambda client, t: GitHubInstallationTreeProvider(
                client=client, token_provider=t
            ).fetch_default_branch_tree(
                installation_external_id=17, repository=_snapshot("octo/api", default_branch)
            ),
            wire,
            tokens,
        )

    assert wire.requests == []
    assert tokens.calls == []


@pytest.mark.parametrize(
    ("default_branch", "encoded"),
    [
        ("release/1.0", "release%2F1.0"),
        ("a/b/c", "a%2Fb%2Fc"),
        ("%2e%2e", "%252e%252e"),
        ("feat?x", "feat%3Fx"),
        ("a..b", "a..b"),
    ],
)
def test_tree_provider_sends_a_default_branch_as_one_encoded_path_segment(
    default_branch: str, encoded: str
) -> None:
    wire, tokens = _Wire(answer={"tree": []}), _Tokens()

    _run_on_wire(
        lambda client, t: GitHubInstallationTreeProvider(
            client=client, token_provider=t
        ).fetch_default_branch_tree(
            installation_external_id=17, repository=_snapshot("octo/api", default_branch)
        ),
        wire,
        tokens,
    )

    assert [request.url.raw_path for request in wire.requests] == [
        f"/repos/octo/api/git/trees/{encoded}?recursive=1".encode()
    ]


def _pull_request_payload(full_name: str) -> dict[str, Any]:
    return {
        "action": "opened",
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": full_name},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Add parser",
            "body": None,
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature/parser", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
            "state": "open",
            "merged": False,
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }


@pytest.mark.parametrize(
    "full_name",
    ["octo/repo", "octo/.github", "octo_acme/repo", "owner/repo.js", "example-owner/example-repo"],
)
def test_pull_request_parser_accepts_real_github_repository_names(full_name: str) -> None:
    event = parse_pull_request_event(_pull_request_payload(full_name))

    assert event.repository_full_name == full_name


_DISPATCH_LOGGER = "app.modules.integrations.webhooks.api.dispatch"


@dataclass
class _Dispatched:
    events: list[GitHubDispatchEvent] = field(default_factory=list)

    async def execute(self, delivery: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
        self.events.append(delivery)
        return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.PROJECTED_PR)


def _dispatch_pull_request(
    payload: dict[str, Any],
) -> tuple[InstallationDeliveryDispatchResult, _Dispatched]:
    dispatched = _Dispatched()
    result = asyncio.run(
        GitHubWebhookDispatchAdapter(dispatched).execute(
            VerifiedGitHubDelivery("delivery-pr", "pull_request", payload).to_receipt()
        )
    )
    return result, dispatched


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _DISPATCH_LOGGER and record.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("action", ["opened", "labeled"])
def test_a_pull_request_event_with_a_bad_repository_name_is_ignored_with_its_field_path_only(
    caplog: pytest.LogCaptureFixture, action: str
) -> None:
    """The outcome reason names the failing field (api#72), never the rejected value."""
    payload = _pull_request_payload("SENTINEL-owner/../SENTINEL-name")
    payload["action"] = action
    payload["label"] = {"name": "SENTINEL-label"}

    with caplog.at_level(logging.DEBUG):
        result, dispatched = _dispatch_pull_request(payload)

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert dispatched.events == []
    assert result.detail == f"action={action} invalid_payload fields=repository.full_name"
    assert "SENTINEL" not in caplog.text


def test_a_valid_pull_request_event_is_dispatched_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER):
        result, dispatched = _dispatch_pull_request(_pull_request_payload("octo/repo"))

    assert result.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert len(dispatched.events) == 1
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "full_name",
    [".", "..", "../..", "o/..", "../r", "./.", "o r/r", "o/r p", "o/r\n", "o/r/x", "o"],
)
def test_pull_request_parser_rejects_a_repository_name_outside_owner_slash_repo(
    full_name: str,
) -> None:
    with pytest.raises(pydantic.ValidationError) as raised:
        parse_pull_request_event(_pull_request_payload(full_name))

    assert [error["loc"] for error in raised.value.errors()] == [("repository", "full_name")]


# --- Review adapters: names and SHAs read from the database are validated the same way ---

_HEAD = "e" * 40
_BASE = "b" * 40
_BAD_SHAS = ["", "short", "..", "../../x", "e" * 39, "e" * 40 + "/", "e" * 40 + "\n", "g" * 40]


def _check_run(
    client: httpx.AsyncClient, tokens: _Tokens, full_name: str, sha: str
) -> Awaitable[object]:
    return GitHubCheckRunGateway(client=client, token_provider=tokens).upsert(
        CheckRunTarget(17, full_name, sha, uuid4()),
        CheckRunView(status="in_progress", conclusion=None, title="AI Review", summary="s"),
    )


def _review(client: httpx.AsyncClient, tokens: _Tokens, full_name: str) -> Awaitable[object]:
    return GitHubPullRequestReviewGateway(client=client, token_provider=tokens).submit_review(
        ReviewSubmission(17, full_name, 7, _HEAD, "COMMENT", "body", (), "hash")
    )


def _vcs_pull_request(
    client: httpx.AsyncClient, tokens: _Tokens, full_name: str
) -> Awaitable[object]:
    return HttpGitHubVcsProvider(client=client, token_provider=tokens).get_pull_request(
        PullRequestLocator(17, full_name, 7)
    )


def _vcs_blob(client: httpx.AsyncClient, tokens: _Tokens, full_name: str) -> Awaitable[object]:
    return HttpGitHubVcsProvider(client=client, token_provider=tokens).get_blob(
        PullRequestLocator(17, full_name, 7), _HEAD
    )


def _current_head_ci(
    client: httpx.AsyncClient, tokens: _Tokens, full_name: str
) -> Awaitable[object]:
    return HttpGitHubCurrentHeadCiProvider(
        client=client, token_provider=tokens
    ).get_current_head_ci(17, full_name, _HEAD)


@dataclass
class _Runs:
    full_name: str
    head_sha: str
    base_sha: str

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(
            code_change_id=uuid4(),
            repository_id=uuid4(),
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            locator=PullRequestLocator(17, self.full_name, 7),
        )


def _run_source(client: httpx.AsyncClient, tokens: _Tokens, runs: _Runs) -> GitHubRunSource:
    return GitHubRunSource(
        client=client,
        token_provider=tokens,
        vcs=cast(VcsProvider, None),
        runs=runs,  # type: ignore[arg-type]
        run_id=uuid4(),
    )


_REVIEW_SINKS: dict[str, Callable[[httpx.AsyncClient, _Tokens, str], Awaitable[object]]] = {
    "check-run": lambda client, tokens, full_name: _check_run(client, tokens, full_name, _HEAD),
    "pull-request-review": _review,
    "vcs-pull-request": _vcs_pull_request,
    "vcs-blob": _vcs_blob,
    "current-head-ci": _current_head_ci,
    "run-source-tree": lambda client, tokens, full_name: _run_source(
        client, tokens, _Runs(full_name, _HEAD, _BASE)
    ).fetch_tree(uuid4()),
    "run-source-agents-md": lambda client, tokens, full_name: _run_source(
        client, tokens, _Runs(full_name, _HEAD, _BASE)
    ).fetch_agents_md(uuid4()),
}


@pytest.mark.parametrize("sink", _REVIEW_SINKS)
@pytest.mark.parametrize("full_name", _BAD_FULL_NAMES)
def test_review_adapter_rejects_a_bad_repository_name_before_any_request(
    sink: str, full_name: str
) -> None:
    wire, tokens = _Wire(), _Tokens()
    # The CI adapter keeps its own earlier slash count, so it raises a plain ValueError there.
    expected = (
        ValueError
        if sink == "current-head-ci" and full_name.count("/") != 1
        else InvalidGitHubPathSegment
    )

    with pytest.raises(expected):
        _run_on_wire(lambda client, t: _REVIEW_SINKS[sink](client, t, full_name), wire, tokens)

    assert wire.requests == []
    assert tokens.calls == []


@pytest.mark.parametrize("sha", _BAD_SHAS)
def test_check_run_gateway_rejects_a_bad_head_sha_before_any_request(sha: str) -> None:
    wire, tokens = _Wire(), _Tokens()

    with pytest.raises(InvalidGitHubPathSegment):
        _run_on_wire(lambda client, t: _check_run(client, t, "octo/repo", sha), wire, tokens)

    assert wire.requests == []
    assert tokens.calls == []


@pytest.mark.parametrize("sha", _BAD_SHAS)
@pytest.mark.parametrize("which", ["head", "base"])
def test_run_source_rejects_a_bad_tree_sha_before_any_request(sha: str, which: str) -> None:
    runs = _Runs("octo/repo", sha if which == "head" else _HEAD, sha if which == "base" else _BASE)
    wire, tokens = _Wire(), _Tokens()

    async def read_tree(client: httpx.AsyncClient, t: _Tokens) -> object:
        source = _run_source(client, t, runs)
        return await (
            source.fetch_tree(uuid4()) if which == "head" else source.fetch_agents_md(uuid4())
        )

    with pytest.raises(InvalidGitHubPathSegment):
        _run_on_wire(read_tree, wire, tokens)

    assert wire.requests == []
    assert tokens.calls == []
