"""Public current-MR transport boundary; synthetic HTTP only, no socket/TLS proof."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.gitlab.application.manage_connection import (
    CredentialEnvelope,
    GitLabOriginPolicy,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabCredentialUnavailable,
    GitLabRequestAuthority,
    GitLabRequestCredential,
    GitLabRequestCredentials,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.current_merge_request import (
    HttpGitLabCurrentMergeRequestProvider,
)
from app.modules.reviews.application.project_gitlab_merge_request import (
    CanonicalMergeRequestAccessDenied,
    CanonicalMergeRequestIdentityMismatch,
    CanonicalMergeRequestInvalidResponse,
    CanonicalMergeRequestNotReady,
    CanonicalMergeRequestRateLimited,
    CanonicalMergeRequestUnavailable,
    GitLabMergeRequestCommand,
)

_TARGET = BoundGitLabTarget(
    UUID("11111111-1111-1111-1111-111111111111"),
    UUID("22222222-2222-2222-2222-222222222222"),
    UUID("33333333-3333-3333-3333-333333333333"),
    "https://gitlab.example.com",
    17,
    1,
)
_COMMAND = GitLabMergeRequestCommand(_TARGET.origin_provider_installation_id, 17, 901, 12, "open")


def payload() -> dict[str, Any]:
    return {
        "id": 901,
        "iid": 12,
        "project_id": 17,
        "target_project_id": 17,
        "source_project_id": 29,
        "state": "opened",
        "title": "Canonical title",
        "description": "Canonical description",
        "author": {"username": "reviewer"},
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/12",
        "source_branch": "feature",
        "target_branch": "main",
        "updated_at": "2026-10-19T08:30:00+00:00",
        "diff_refs": {"head_sha": "a" * 40, "start_sha": "b" * 40, "base_sha": "c" * 40},
    }


class Authority:
    def __init__(self) -> None:
        self.credential_reads = 0
        self.checks = 0

    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        raise AssertionError("Caller resolves the target exactly once")

    async def credential_for(self, target: BoundGitLabTarget) -> GitLabRequestCredential:
        self.credential_reads += 1
        return GitLabRequestCredential(target, "synthetic-origin-PAT")

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        assert target == _TARGET
        self.checks += 1


async def checkpoint() -> None:
    return None


def test_open_reads_numeric_canonical_endpoint_and_preserves_three_literal_refs() -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return response()

        authority = Authority()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = HttpGitLabCurrentMergeRequestProvider(
                client=client,
                authority=authority,
                origin_policy=GitLabOriginPolicy(frozenset({_TARGET.instance_origin})),
            )
            result = await provider.get_current(_COMMAND, target=_TARGET, checkpoint=checkpoint)
        assert len(seen) == 1
        assert str(seen[0].url) == "https://gitlab.example.com/api/v4/projects/17/merge_requests/12"
        assert seen[0].headers["PRIVATE-TOKEN"] == "synthetic-origin-PAT"
        assert result.target == _TARGET
        assert result.diff_refs.head_sha == "a" * 40
        assert result.diff_refs.start_sha == "b" * 40
        assert result.diff_refs.base_sha == "c" * 40
        assert result.title == "Canonical title"
        assert result.source_project_id == 29
        assert authority.credential_reads == 1

    asyncio.run(scenario())


class Body(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def response(data: dict[str, Any] | None = None, *, raw: bytes | None = None) -> httpx.Response:
    body = json.dumps(payload() if data is None else data).encode() if raw is None else raw
    return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Body(body))


async def read(
    handle: Callable[[httpx.Request], httpx.Response],
    *,
    command: GitLabMergeRequestCommand = _COMMAND,
    authority: GitLabRequestAuthority | None = None,
    target: BoundGitLabTarget = _TARGET,
    caller_checkpoint: Callable[[], Awaitable[None]] = checkpoint,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    policy: GitLabOriginPolicy | None = None,
) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = HttpGitLabCurrentMergeRequestProvider(
            client=client,
            authority=authority or Authority(),
            origin_policy=policy or GitLabOriginPolicy(frozenset({target.instance_origin})),
            sleeper=sleeper or no_wait,
        )
        return await provider.get_current(command, target=target, checkpoint=caller_checkpoint)


@pytest.mark.parametrize("action", ["open", "reopen", "update", "close", "merge"])
@pytest.mark.parametrize("state", ["opened", "closed", "merged"])
def test_all_actions_fetch_current_state_instead_of_projecting_hook_state(
    action: str,
    state: str,
) -> None:
    data = payload()
    data["state"] = state
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return response(data)

    result = asyncio.run(read(handle, command=replace(_COMMAND, action=action)))
    assert result.state == state
    assert seen == ["https://gitlab.example.com/api/v4/projects/17/merge_requests/12"]
    assert set(vars(_COMMAND)) == {
        "origin_provider_installation_id",
        "project_id",
        "external_id",
        "iid",
        "action",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_id", True),
        ("external_id", 0),
        ("external_id", 9223372036854775808),
        ("project_id", False),
        ("project_id", 0),
        ("project_id", 9223372036854775808),
        ("iid", True),
        ("iid", 0),
        ("iid", 2147483648),
        ("iid", 1.0),
        ("origin_provider_installation_id", str(_TARGET.origin_provider_installation_id)),
        ("action", "approved"),
        ("action", None),
    ],
)
def test_command_rejects_nonstorage_safe_identity(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(_COMMAND, **{field: value})


@pytest.mark.parametrize("field", ["id", "iid", "project_id", "target_project_id"])
@pytest.mark.parametrize("value", [999, True, 12.0, "12", None])
def test_canonical_identity_must_exactly_match_command(field: str, value: Any) -> None:
    data = payload()
    data[field] = value
    with pytest.raises(
        CanonicalMergeRequestIdentityMismatch, match="^gitlab_merge_request_identity_mismatch$"
    ):
        asyncio.run(read(lambda request: response(data)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x" * 501),
        ("title", None),
        ("title", "bad\0title"),
        ("description", "bad\ud800text"),
        ("source_branch", "x" * 256),
        ("target_branch", ""),
        ("target_branch", "bad\0branch"),
        ("author", {"username": "x" * 256}),
        ("author", None),
        ("state", "unknown"),
        ("updated_at", "2026-10-19T08:30:00"),
        ("updated_at", 1),
        ("updated_at", "nonsense"),
        ("source_project_id", None),
        ("source_project_id", 0),
        ("source_project_id", True),
        ("source_project_id", 9223372036854775808),
        ("web_url", "https://evil.example.com/group/project/12"),
        ("web_url", "https://user:secret@gitlab.example.com/project/12"),
        ("web_url", "https://gitlab.example.com/project/12#fragment"),
        ("web_url", "https://gitlab.example.com/project/12?token=secret"),
    ],
)
def test_invalid_canonical_metadata_is_sanitized_before_projection(field: str, value: Any) -> None:
    data = payload()
    data[field] = value
    with pytest.raises(
        CanonicalMergeRequestInvalidResponse, match="^gitlab_merge_request_invalid_response$"
    ):
        asyncio.run(read(lambda request: response(data)))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":901,"id":901}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": 1e9999}',
        b'{"value": ' + b"9" * 5000 + b"}",
        b"[" + b"[]," * 3 + b"[]]",
        b'{"value": "\\ud800"}',
        b'{"value": "\\u0000"}',
        b'{"x":' + b"[" * 130 + b"0" + b"]" * 130 + b"}",
        b"not-json-secret-canary",
        b'{"id":901,',
        b"\xff",
    ],
)
def test_strict_bounded_json_rejects_unsafe_body_without_echo(raw: bytes) -> None:
    with pytest.raises(
        CanonicalMergeRequestInvalidResponse, match="^gitlab_merge_request_invalid_response$"
    ):
        asyncio.run(read(lambda request: response(raw=raw)))


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (301, CanonicalMergeRequestInvalidResponse),
        (302, CanonicalMergeRequestInvalidResponse),
        (307, CanonicalMergeRequestInvalidResponse),
        (401, CanonicalMergeRequestAccessDenied),
        (403, CanonicalMergeRequestAccessDenied),
        (429, CanonicalMergeRequestRateLimited),
        (404, CanonicalMergeRequestUnavailable),
        (500, CanonicalMergeRequestUnavailable),
        (503, CanonicalMergeRequestUnavailable),
    ],
)
def test_status_faults_never_follow_redirects_or_expose_upstream_content(
    status: int,
    error: type[Exception],
) -> None:
    seen: list[str] = []
    body = Body(b"<html>secret-response-PAT-canary</html>")

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.url.host == "gitlab.example.com"
        return httpx.Response(
            status,
            headers={
                "Location": "https://evil.example.com/token",
                "Secret": "secret-header-canary",
            },
            stream=body,
        )

    with pytest.raises(error) as caught:
        asyncio.run(read(handle))
    assert len(seen) == 1
    assert body.closed
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/html"},
        {"Content-Type": "application/json; charset=latin1"},
        {"Content-Type": "application/json; charset=utf-8; charset=latin1"},
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "identity, identity"},
        {"Content-Length": "1048577"},
        {"Content-Length": "-1"},
        {"Content-Length": "+1"},
        {"Content-Length": "1.0"},
        {"Content-Length": "9" * 10000},
        {"Content-Length": "1, 1"},
        {"Content-Length": "0"},
        {"Content-Length": "9999"},
        {"Content-Length": "10", "Transfer-Encoding": "chunked"},
        {"Transfer-Encoding": "gzip"},
    ],
)
def test_unsupported_encoding_or_strict_length_declaration_closes_stream(
    headers: dict[str, str],
) -> None:
    body = Body(json.dumps(payload()).encode())

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "application/json", **headers}, stream=body
        )

    with pytest.raises(CanonicalMergeRequestInvalidResponse):
        asyncio.run(read(handle))
    assert body.closed


@pytest.mark.parametrize("chunked", [False, True])
def test_actual_body_cap_applies_without_content_length_and_to_chunked(chunked: bool) -> None:
    body = Body(b" " * 524288, b" " * 524288, b"secret-canary")
    headers = {"Content-Type": "application/json"}
    if chunked:
        headers["Transfer-Encoding"] = "chunked"
    with pytest.raises(CanonicalMergeRequestInvalidResponse):
        asyncio.run(read(lambda request: httpx.Response(200, headers=headers, stream=body)))
    assert body.closed


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
def test_transport_fault_is_static_without_raw_cause(error: type[httpx.HTTPError]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error("secret-PAT-header-body-DSN-canary")

    with pytest.raises(CanonicalMergeRequestUnavailable) as caught:
        asyncio.run(read(handle))
    assert str(caught.value) == "gitlab_merge_request_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_standalone_request_excludes_ambient_headers_cookies_auth_and_query() -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return response()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            timeout=None,
            follow_redirects=True,
            headers={
                "Authorization": "Bearer ambient",
                "PRIVATE-TOKEN": "ambient",
                "Cookie": "ambient=true",
                "Other-Secret": "ambient",
            },
            cookies={"session": "ambient"},
            auth=("ambient", "password"),
            params={"ambient": "query"},
        ) as client:
            provider = HttpGitLabCurrentMergeRequestProvider(
                client=client,
                authority=Authority(),
                origin_policy=GitLabOriginPolicy(frozenset({_TARGET.instance_origin})),
            )
            await provider.get_current(_COMMAND, target=_TARGET, checkpoint=checkpoint)
        assert len(seen) == 1
        assert set(seen[0].headers) == {"host", "private-token", "accept", "accept-encoding"}
        assert seen[0].headers["private-token"] == "synthetic-origin-PAT"
        assert seen[0].headers["accept-encoding"] == "identity"
        assert seen[0].url.query == b""
        assert seen[0].extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }

    asyncio.run(scenario())


async def no_wait(delay: float) -> None:
    return None


@pytest.mark.parametrize(
    ("state", "unprepared"),
    [
        ("opened", None),
        ("closed", {}),
        ("merged", {"head_sha": "a" * 40}),
        ("opened", {"head_sha": "", "start_sha": "b" * 40, "base_sha": "c" * 40}),
    ],
)
def test_missing_refs_exhaust_exact_three_gets_with_finite_waits(
    state: str,
    unprepared: Any,
) -> None:
    data = payload()
    data.update(state=state, diff_refs=unprepared)
    seen: list[str] = []
    sleeps: list[float] = []
    bodies: list[Body] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        body = Body(json.dumps(data).encode())
        bodies.append(body)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=body)

    with pytest.raises(CanonicalMergeRequestNotReady, match="^gitlab_merge_request_not_ready$"):
        asyncio.run(read(handle, sleeper=sleep))
    assert len(seen) == 3
    assert sleeps == [0.1, 0.25]
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("transient", ["missing_refs", "locked"])
def test_unprepared_current_mr_then_ready_returns_only_ready_snapshot(transient: str) -> None:
    attempts = 0
    authority = Authority()

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        data = payload()
        if attempts == 1:
            if transient == "locked":
                data["state"] = "locked"
            else:
                data["diff_refs"] = None
        return response(data)

    result = asyncio.run(read(handle, authority=authority))
    assert result.diff_refs.head_sha == "a" * 40
    assert attempts == 2
    assert authority.credential_reads == 1


@pytest.mark.parametrize("value", ["not-a-ref", "a" * 39, "a" * 65, "z" * 40, 123, True])
@pytest.mark.parametrize("name", ["head_sha", "start_sha", "base_sha"])
def test_present_but_invalid_refs_are_static_invalid_response(name: str, value: Any) -> None:
    data = payload()
    data["diff_refs"][name] = value
    with pytest.raises(CanonicalMergeRequestInvalidResponse):
        asyncio.run(read(lambda request: response(data)))


class GateAuthority(Authority):
    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure
        self.refused = False
        self.wrong_credential = False
        self.refuse_after_credential = False

    async def credential_for(self, target: BoundGitLabTarget) -> GitLabRequestCredential:
        credential = await super().credential_for(target)
        self.refused = self.refuse_after_credential
        if self.wrong_credential:
            return GitLabRequestCredential(
                replace(target, credential_generation=2), credential.token
            )
        return credential

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        if self.refused:
            raise self.failure
        await super().checkpoint(target)


@pytest.mark.parametrize(
    "failure",
    [
        GitLabTargetInactive(),
        GitLabCredentialGenerationChanged(),
        GitLabCredentialUnavailable(),
        GitLabRequestUnavailable(),
    ],
)
@pytest.mark.parametrize("boundary", ["credential", "prerequest", "sleep", "inflight"])
def test_authority_refusal_stops_next_request_and_canonical_return(
    failure: Exception,
    boundary: str,
) -> None:
    authority = GateAuthority(failure)
    authority.refuse_after_credential = boundary == "credential"
    requests = 0
    caller_checks = 0

    async def caller() -> None:
        nonlocal caller_checks
        caller_checks += 1
        if boundary == "prerequest" and caller_checks == 2:
            authority.refused = True

    async def sleep(delay: float) -> None:
        if boundary == "sleep":
            authority.refused = True

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if boundary == "inflight":
            authority.refused = True
        data = payload()
        if boundary == "sleep":
            data["diff_refs"] = None
        return response(data)

    with pytest.raises(type(failure)) as caught:
        asyncio.run(read(handle, authority=authority, caller_checkpoint=caller, sleeper=sleep))
    assert caught.value is failure
    assert requests == (1 if boundary in {"sleep", "inflight"} else 0)


def test_credential_returning_another_pinned_generation_is_refused_before_wire() -> None:
    authority = GateAuthority(GitLabCredentialGenerationChanged())
    authority.wrong_credential = True

    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Wrong-generation token must never reach HTTP")

    with pytest.raises(GitLabCredentialGenerationChanged):
        asyncio.run(read(handle, authority=authority))


@pytest.mark.parametrize("field", ["origin_provider_installation_id", "project_id"])
def test_command_target_disagreement_is_refused_before_credential(field: str) -> None:
    authority = Authority()
    changes: dict[str, Any] = {field: UUID(int=99) if field.startswith("origin") else 18}
    command = replace(_COMMAND, **changes)
    with pytest.raises(CanonicalMergeRequestIdentityMismatch):
        asyncio.run(read(lambda request: response(), command=command, authority=authority))
    assert authority.credential_reads == 0


@pytest.mark.parametrize(
    "target,policy",
    [
        (_TARGET, GitLabOriginPolicy(frozenset())),
        (
            replace(_TARGET, instance_origin="https://gitlab.internal"),
            GitLabOriginPolicy(frozenset({"https://gitlab.internal"})),
        ),
    ],
)
def test_runtime_origin_policy_precedes_credential_access(
    target: BoundGitLabTarget,
    policy: GitLabOriginPolicy,
) -> None:
    authority = Authority()
    with pytest.raises(ValueError):
        asyncio.run(
            read(lambda request: response(), authority=authority, target=target, policy=policy)
        )
    assert authority.credential_reads == 0


def test_success_calls_final_checkpoint_after_closed_response() -> None:
    body = Body(json.dumps(payload()).encode())
    final_checks = 0

    async def caller() -> None:
        nonlocal final_checks
        if body.closed:
            final_checks += 1

    asyncio.run(
        read(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/json"}, stream=body
            ),
            caller_checkpoint=caller,
        )
    )
    assert body.closed
    assert final_checks == 1


def test_two_instances_with_same_numeric_project_and_iid_keep_tokens_disjoint() -> None:
    other = replace(
        _TARGET,
        origin_provider_installation_id=UUID(int=91),
        workspace_id=UUID(int=92),
        repository_id=UUID(int=93),
        instance_origin="https://another.example.com",
    )
    seen: list[tuple[str, str]] = []

    class InstanceAuthority(Authority):
        async def credential_for(self, target: BoundGitLabTarget) -> GitLabRequestCredential:
            return GitLabRequestCredential(
                target, "instance-A" if target == _TARGET else "instance-B"
            )

        async def checkpoint(self, target: BoundGitLabTarget) -> None:
            assert target in (_TARGET, other)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["PRIVATE-TOKEN"]))
        data = payload()
        data["web_url"] = f"https://{request.url.host}/group/project/-/merge_requests/12"
        return response(data)

    async def scenario() -> None:
        for target in (_TARGET, other):
            result = await read(
                handle,
                authority=InstanceAuthority(),
                target=target,
                command=replace(
                    _COMMAND, origin_provider_installation_id=target.origin_provider_installation_id
                ),
            )
            assert result.target == target

    asyncio.run(scenario())
    assert seen == [
        ("https://gitlab.example.com/api/v4/projects/17/merge_requests/12", "instance-A"),
        ("https://another.example.com/api/v4/projects/17/merge_requests/12", "instance-B"),
    ]


@pytest.mark.parametrize("during_stream", ["transport", "cancel", "programming"])
def test_failure_midstream_always_closes_and_preserves_cancellation(during_stream: str) -> None:
    class BrokenBody(Body):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"partial":'
            if during_stream == "transport":
                raise httpx.ReadError("secret-body-canary")
            if during_stream == "cancel":
                raise asyncio.CancelledError()
            raise AssertionError("programming failure")

    body = BrokenBody()
    error = (
        CanonicalMergeRequestUnavailable
        if during_stream == "transport"
        else asyncio.CancelledError
        if during_stream == "cancel"
        else AssertionError
    )
    with pytest.raises(error):
        asyncio.run(
            read(
                lambda request: httpx.Response(
                    200, headers={"Content-Type": "application/json"}, stream=body
                )
            )
        )
    assert body.closed


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, 61])
def test_timeout_configuration_is_finite_and_bounded(timeout: float) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response())
        ) as client:
            with pytest.raises(ValueError, match="Invalid GitLab request timeout"):
                HttpGitLabCurrentMergeRequestProvider(
                    client=client,
                    authority=Authority(),
                    timeout_seconds=timeout,
                    origin_policy=GitLabOriginPolicy(frozenset({_TARGET.instance_origin})),
                )

    asyncio.run(scenario())


def test_exact_body_budget_and_persisted_metadata_limits_accept_valid_snapshot() -> None:
    data = payload()
    data.update(
        title="x" * 500,
        source_branch="s" * 255,
        target_branch="t" * 255,
        author={"username": "u" * 255},
        diff_refs={
            "head_sha": "A" * 64,
            "start_sha": "B" * 64,
            "base_sha": "C" * 64,
        },
    )
    raw = json.dumps(data).encode()
    raw += b" " * (1048576 - len(raw))
    body = Body(raw[:500000], raw[500000:])
    result = asyncio.run(
        read(
            lambda request: httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": "1048576",
                    "Content-Encoding": "identity",
                },
                stream=body,
            )
        )
    )
    assert len(result.title) == 500
    assert result.diff_refs.head_sha == "A" * 64
    assert result.diff_refs.start_sha == "B" * 64
    assert result.diff_refs.base_sha == "C" * 64
    assert body.closed


@pytest.mark.parametrize("phase", ["before_decrypt", "during_decrypt"])
def test_public_shared_authority_composition_blocks_rotated_attempt_before_http(phase: str) -> None:
    class Reader:
        changed = phase == "before_decrypt"

        async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
            raise AssertionError("Already resolved target")

        async def checkpoint(self, target: BoundGitLabTarget) -> None:
            if self.changed:
                raise GitLabCredentialGenerationChanged()

        async def credential_envelope_for(self, target: BoundGitLabTarget) -> CredentialEnvelope:
            await self.checkpoint(target)
            return CredentialEnvelope("synthetic-key", b"synthetic-envelope")

    reader = Reader()

    class Codec:
        def webhook_digest(self, secret: str) -> str:
            raise AssertionError("Read-only adapter")

        def decrypt(
            self,
            credential: CredentialEnvelope,
            *,
            installation_id: UUID,
            workspace_id: UUID,
            instance_origin: str,
        ) -> str:
            assert installation_id == _TARGET.origin_provider_installation_id
            assert workspace_id == _TARGET.workspace_id
            assert instance_origin == "https://gitlab.example.com"
            reader.changed = True
            return "synthetic-decrypted-PAT"

        def encrypt(
            self, token: str, *, installation_id: UUID, workspace_id: UUID, instance_origin: str
        ) -> CredentialEnvelope:
            raise AssertionError("Read-only adapter")

    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Rotation must stop HTTP")

    with pytest.raises(GitLabCredentialGenerationChanged):
        asyncio.run(read(handle, authority=GitLabRequestCredentials(reader, Codec())))


def test_total_attempt_deadline_closes_slow_stream_even_with_finite_chunks() -> None:
    class HangingBody(Body):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"partial":'
            await asyncio.Event().wait()

    body = HangingBody()

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    stream=body,
                )
            ),
            timeout=None,
        ) as client:
            provider = HttpGitLabCurrentMergeRequestProvider(
                client=client,
                authority=Authority(),
                timeout_seconds=0.01,
                origin_policy=GitLabOriginPolicy(frozenset({_TARGET.instance_origin})),
            )
            with pytest.raises(CanonicalMergeRequestUnavailable):
                await asyncio.wait_for(
                    provider.get_current(
                        _COMMAND,
                        target=_TARGET,
                        checkpoint=checkpoint,
                    ),
                    timeout=0.2,
                )

    asyncio.run(scenario())
    assert body.closed


def test_network_fixture_canonical_template_is_accepted_by_public_provider() -> None:
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).parent / "fixtures/gitlab_network/manifest.json").read_text()
    )
    data = manifest["merge_request"]
    target = replace(_TARGET, instance_origin="https://localhost:49443")
    command = replace(_COMMAND, external_id=912)

    class FixtureAuthority(Authority):
        async def checkpoint(self, actual: BoundGitLabTarget) -> None:
            assert actual == target
            self.checks += 1

    result = asyncio.run(
        read(
            lambda request: response(data),
            command=command,
            target=target,
            authority=FixtureAuthority(),
            policy=GitLabOriginPolicy(
                frozenset({target.instance_origin}), frozenset({target.instance_origin})
            ),
        )
    )
    assert result.web_url == "https://localhost:49443/team/project/-/merge_requests/12"
    assert result.provider_updated_at.isoformat() == "2026-10-19T08:30:00+00:00"
    assert result.diff_refs.head_sha == "a" * 40
    assert result.diff_refs.start_sha == "b" * 40
    assert result.diff_refs.base_sha == "c" * 40


@pytest.mark.parametrize("fault", ["header-bytes", "header-count", "duplicate", "control"])
def test_current_mr_rejects_unsafe_header_envelope_and_closes(fault: str) -> None:
    headers = [("Content-Type", "application/json")]
    if fault == "header-bytes":
        headers.append(("X-Fill", "a" * 16384))
    elif fault == "header-count":
        headers.extend((f"X-Fill-{n}", "a") for n in range(100))
    elif fault == "duplicate":
        headers.extend([("X-Scope", "first"), ("x-scope", "second")])
    else:
        headers.append(("X-Scope", "first\r\nsecond"))
    body = Body(json.dumps(payload()).encode())
    with pytest.raises(CanonicalMergeRequestInvalidResponse):
        asyncio.run(read(lambda request: httpx.Response(200, headers=headers, stream=body)))
    assert body.closed


@pytest.mark.parametrize("framing", ["length", "chunked", "close", "http2"])
def test_current_mr_accepts_identity_json_with_supported_body_framing(framing: str) -> None:
    raw = json.dumps(payload()).encode()
    body = Body(raw[:40], raw[40:])
    headers = {"Content-Type": 'application/json; charset="UTF-8"'}
    if framing == "length":
        headers["Content-Length"] = str(len(raw))
    elif framing == "chunked":
        headers["Transfer-Encoding"] = "chunked"
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers=headers,
            stream=body,
            extensions={"http_version": b"HTTP/2" if framing == "http2" else b"HTTP/1.1"},
        )

    result = asyncio.run(read(handle))
    assert result.external_id == 901
    assert body.closed
    assert seen[0].headers["Accept-Encoding"] == "identity"
