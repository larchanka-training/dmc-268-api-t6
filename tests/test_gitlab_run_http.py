"""Public target-only HTTP controls; MockTransport is synthetic, not TLS proof."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Self
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestCredential,
)
from app.modules.integrations.gitlab.infrastructure.run_credentials import FetchCode
from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabMergeRequestResource,
    GitLabRunHttp,
    GitLabRunHttpError,
)
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.vcs_diff import PullRequestLocator

ORIGIN = "https://gitlab.com"
IDENTITY = UUID("10000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("10000000-0000-0000-0000-000000000002")
REPOSITORY = UUID("10000000-0000-0000-0000-000000000003")
INPUT = RunVcsInput(
    IDENTITY,
    REPOSITORY,
    "a" * 40,
    "b" * 40,
    PullRequestLocator(71, "team/project", 12, "gitlab", IDENTITY, 17),
)
TARGET = BoundGitLabTarget(IDENTITY, WORKSPACE, REPOSITORY, ORIGIN, 17, 1)
POLICY = GitLabOriginPolicy(frozenset({ORIGIN}))
TOKEN = "synthetic-run-PAT-canary"


@dataclass
class Credentials:
    expected_input: RunVcsInput = INPUT
    target: BoundGitLabTarget = TARGET
    admitted: int = 0
    checks: int = 0
    failure: Exception | None = None

    async def bind(self, *, error_code: FetchCode = "diff_fetch_failed") -> BoundGitLabTarget:
        return self.target

    async def credential_for_request(
        self, *, error_code: FetchCode = "diff_fetch_failed"
    ) -> GitLabRequestCredential:
        self.admitted += 1
        return GitLabRequestCredential(self.target, TOKEN)

    async def checkpoint(self, *, error_code: FetchCode = "diff_fetch_failed") -> None:
        self.checks += 1
        if self.failure is not None:
            raise self.failure


@dataclass
class Stream(httpx.AsyncByteStream):
    chunks: tuple[bytes, ...]
    closed: bool = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for value in self.chunks:
            yield value

    async def aclose(self) -> None:
        self.closed = True


def test_duplicate_json_is_rejected_and_response_closes() -> None:
    async def scenario() -> None:
        stream = Stream((b'{"id":1,"id":2}',))
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/json"}, stream=stream
            )
        )
        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=transport
        ) as http:
            with pytest.raises(GitLabRunHttpError):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "access_denied"),
        (403, "access_denied"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (302, "invalid_response"),
    ],
)
def test_http_fault_is_typed_safe_and_response_closed(status: int, kind: str) -> None:
    async def scenario() -> None:
        stream = Stream((b'{"secret":"upstream-body-canary"}',))
        credentials = Credentials()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                headers={
                    "Content-Type": "application/json",
                    "Retry-After": "120",
                    "Location": "https://evil.example",
                },
                stream=stream,
            )
        )
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=POLICY, transport=transport
        ) as http:
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert error.value.kind == kind
        assert "canary" not in str(error.value)
        assert stream.closed and credentials.checks == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 404, 429, 500])
def test_post_reply_retirement_wins_over_ordinary_result(status: int) -> None:
    from app.modules.reviews.application.run_failures import RunCancelled

    async def scenario() -> None:
        credentials = Credentials(failure=RunCancelled("installation_unavailable"))
        stream = Stream((b"{}",))
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers={"Content-Type": "application/json"}, stream=stream
            )
        )
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=POLICY, transport=transport
        ) as http:
            with pytest.raises(RunCancelled):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"NaN",
        b'{"x":1e9999}',
        b'{"x":"\\ud800"}',
        b'{"x":"\\u0000"}',
        b"\xff",
        b"[" * 66 + b"0" + b"]" * 66,
    ],
)
def test_strict_json_never_returns_unsafe_values(body: bytes) -> None:
    async def scenario() -> None:
        stream = Stream((body,))
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/json"}, stream=stream
            )
        )
        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=transport
        ) as http:
            with pytest.raises(GitLabRunHttpError):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=4096)
        assert stream.closed

    asyncio.run(scenario())


def test_stream_byte_limit_precedes_materialization() -> None:
    async def scenario() -> None:
        stream = Stream((b'"aaa', b'aaaaaaaa"'))
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/json"}, stream=stream
            )
        )
        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=transport
        ) as http:
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_json(GitLabMergeRequestResource(), max_bytes=5)
        assert error.value.kind == "body_too_large" and stream.closed

    asyncio.run(scenario())


def test_routes_raw_bytes_precision_and_metadata_are_public_and_bounded() -> None:
    from decimal import Decimal

    from app.modules.integrations.gitlab.infrastructure.run_http import (
        GitLabCommitsResource,
        GitLabDiffVersionResource,
        GitLabDiffVersionsResource,
        GitLabTreeResource,
    )

    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def external(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "HEAD":
                return httpx.Response(
                    200,
                    headers={
                        "X-Gitlab-Blob-Id": "c" * 40,
                        "X-Gitlab-Size": "4",
                        "X-Gitlab-Ref": "a" * 40,
                        "Content-Length": "4",
                    },
                    stream=Stream(()),
                )
            if request.url.path.endswith("/raw"):
                return httpx.Response(200, stream=Stream((b"\x00\xff\r\n",)))
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json", "X-Next-Page": "2"},
                stream=Stream((b'{"id":9007199254740993,"value":0.123456789012345678901}',)),
            )

        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            value = await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
            assert value.data == {
                "id": 9007199254740993,
                "value": Decimal("0.123456789012345678901"),
            }
            assert value.headers["x-next-page"] == "2"
            assert "9007199254740993" not in repr(value)
            await http.get_json(GitLabDiffVersionsResource(2), max_bytes=1048576)
            await http.get_json(GitLabDiffVersionResource(9), max_bytes=8388608)
            await http.get_json(
                GitLabCommitsResource(2),
                max_bytes=1048576,
                error_code="commit_messages_fetch_failed",
            )
            await http.get_json(
                GitLabTreeResource("a" * 40, path="dir/%file", page_token="cursor&secret=2"),
                max_bytes=1048576,
            )
            head = await http.head_file("dir/a%20 b.py", "a" * 40)
            assert head.headers["x-gitlab-blob-id"] == "c" * 40
            assert "cccc" not in repr(head)
            assert await http.get_blob("c" * 40, max_bytes=4) == b"\x00\xff\r\n"
        assert len(requests) == 7 and credentials.admitted == 7 and credentials.checks == 7
        assert requests[0].url.path == "/api/v4/projects/17/merge_requests/12"
        assert str(requests[1].url).endswith("/versions?page=2&per_page=100")
        assert requests[2].url.path.endswith("/versions/9")
        assert str(requests[3].url).endswith("/commits?page=2&per_page=10")
        assert requests[4].url.params["page_token"] == "cursor&secret=2"
        assert requests[5].url.raw_path.startswith(
            b"/api/v4/projects/17/repository/files/dir%2Fa%2520%20b.py?"
        )
        assert all(request.headers["PRIVATE-TOKEN"] == TOKEN for request in requests)
        assert all(
            request.extensions["timeout"] == {"connect": 10, "read": 10, "write": 10, "pool": 10}
            for request in requests
        )
        assert all(
            "authorization" not in request.headers
            and "cookie" not in request.headers
            and "user-agent" not in request.headers
            for request in requests
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../b", "a//b", "a\\b", "a\x00b"])
def test_unsafe_repository_path_fails_before_admission(path: str) -> None:
    async def scenario() -> None:
        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("no wire"))
            ),
        ) as http:
            with pytest.raises(ValueError):
                await http.head_file(path, "a" * 40)
        assert credentials.admitted == 0

    asyncio.run(scenario())


def test_unpinned_ref_untrusted_origin_and_invalid_limits_do_not_decrypt() -> None:
    from dataclasses import replace

    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabDiffVersionsResource
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable

    async def scenario() -> None:
        credentials = Credentials()
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("no wire"))
        )
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=POLICY, transport=transport
        ) as http:
            with pytest.raises(ValueError):
                await http.head_file("file.py", "c" * 40)
            with pytest.raises(ValueError):
                await http.get_json(GitLabDiffVersionsResource(6), max_bytes=1024)
            with pytest.raises(ValueError):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=8388608)
            for timeout in (0, float("nan"), 11, True):
                with pytest.raises(ValueError):
                    await http.get_blob("c" * 40, max_bytes=1, timeout_seconds=timeout)
            credentials.target = replace(TARGET, instance_origin="https://other.example")
            with pytest.raises(RequiredSourceUnavailable):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert credentials.admitted == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/html"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        {"Content-Type": "application/json", "Content-Length": "-1"},
        {"Content-Type": "application/json", "Content-Length": "9"},
        {"Content-Type": "application/json", "Content-Length": "2", "Transfer-Encoding": "chunked"},
    ],
)
def test_unsafe_response_framing_closes_and_rechecks(headers: dict[str, str]) -> None:
    async def scenario() -> None:
        credentials = Credentials()
        stream = Stream((b"{}",))
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=stream)
            ),
        ) as http:
            with pytest.raises(GitLabRunHttpError):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert stream.closed and credentials.checks == 1

    asyncio.run(scenario())


def test_slow_trickle_is_bounded_and_closes_without_late_checkpoint() -> None:
    class SlowStream(Stream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                await asyncio.sleep(0.02)
                yield chunk

    async def scenario() -> None:
        credentials = Credentials()
        stream = SlowStream((b'"', b"a", b"b", b"c", b'"'))
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"Content-Type": "application/json"}, stream=stream
                )
            ),
        ) as http:
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_json(
                    GitLabMergeRequestResource(), max_bytes=1024, timeout_seconds=0.05
                )
        assert error.value.kind == "unavailable" and stream.closed and credentials.checks == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["cancel", "deadline", "required", "task", "programmer"])
def test_body_primary_control_survives_response_close_fault(control: str) -> None:
    from app.modules.reviews.application.run_failures import (
        RequiredSourceUnavailable,
        RunCancelled,
        RunFailure,
    )

    primary: BaseException = {
        "cancel": RunCancelled("stop"),
        "deadline": RunFailure("deadline_exceeded"),
        "required": RequiredSourceUnavailable("diff_fetch_failed"),
        "task": asyncio.CancelledError(),
        "programmer": RuntimeError("programmer"),
    }[control]

    class BrokenStream(Stream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise primary
            yield b""

        async def aclose(self) -> None:
            self.closed = True
            raise OSError("close-canary")

    async def scenario() -> None:
        credentials = Credentials()
        stream = BrokenStream(())
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"Content-Type": "application/json"}, stream=stream
                )
            ),
        ) as http:
            with pytest.raises(type(primary)) as error:
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert error.value is primary and stream.closed and credentials.checks == 0

    asyncio.run(scenario())


class OwnedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        start_failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.start_failure = start_failure
        self.close_failure = close_failure
        self.closes = 0

    async def __aenter__(self) -> Self:
        if self.start_failure:
            raise self.start_failure
        return self

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, stream=Stream((b"{}",))
        )

    async def aclose(self) -> None:
        self.closes += 1
        if self.close_failure:
            raise self.close_failure


@pytest.mark.parametrize("stage", ["startup", "body", "cancel", "success"])
def test_context_owns_transport_and_preserves_primary_when_close_fails(stage: str) -> None:
    async def scenario() -> None:
        primary: BaseException = (
            asyncio.CancelledError() if stage == "cancel" else RuntimeError("primary")
        )
        transport = OwnedTransport(
            start_failure=primary if stage == "startup" else None, close_failure=OSError("close")
        )
        http = GitLabRunHttp(credentials=Credentials(), origin_policy=POLICY, transport=transport)
        expected = OSError if stage == "success" else type(primary)
        with pytest.raises(expected) as error:
            async with http:
                if stage in {"body", "cancel"}:
                    raise primary
        if stage != "success":
            assert error.value is primary
        assert transport.closes == 1
        with pytest.raises(RuntimeError):
            await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        with pytest.raises(RuntimeError):
            await http.__aenter__()

    asyncio.run(scenario())


def test_shared_attempt_budgets_are_finite_via_public_requests() -> None:
    async def scenario() -> None:
        calls = 0

        def external(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=Stream((b"x" * 1048576,)))

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            for _ in range(64):
                assert len(await http.get_blob("c" * 40, max_bytes=1048576)) == 1048576
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_blob("c" * 40, max_bytes=1048576)
            assert error.value.kind == "budget_exhausted"
        assert calls == 65
        calls = 0

        def tiny(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=Stream((b"x",)))

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(tiny)
        ) as http:
            for _ in range(512):
                await http.get_blob("c" * 40, max_bytes=1)
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_blob("c" * 40, max_bytes=1)
            assert error.value.kind == "budget_exhausted"
        assert calls == 512

    asyncio.run(scenario())


def test_concurrent_admission_cannot_exceed_shared_send_budget() -> None:
    class YieldingCredentials(Credentials):
        async def credential_for_request(
            self, *, error_code: FetchCode = "diff_fetch_failed"
        ) -> GitLabRequestCredential:
            await asyncio.sleep(0)
            return await super().credential_for_request(error_code=error_code)

    async def scenario() -> None:
        calls = 0

        def external(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=Stream((b"x",)))

        async with GitLabRunHttp(
            credentials=YieldingCredentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(external),
        ) as http:
            outcomes = await asyncio.gather(
                *(http.get_blob("c" * 40, max_bytes=1) for _ in range(513)), return_exceptions=True
            )
        assert calls == 512
        assert sum(isinstance(value, GitLabRunHttpError) for value in outcomes) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("retry", ["120", "9999999999", "bad-header-canary"])
def test_retry_after_is_bounded_and_never_echoes_untrusted_header(retry: str) -> None:
    from datetime import timedelta

    async def scenario() -> None:
        stream = Stream(())
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"Retry-After": retry}, stream=stream)
            ),
        ) as http:
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
        assert (
            error.value.retry_after
            == {
                "120": timedelta(seconds=120),
                "9999999999": timedelta(days=1),
                "bad-header-canary": None,
            }[retry]
        )
        assert "canary" not in str(error.value) and stream.closed

    asyncio.run(scenario())


def test_tls_and_client_environment_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified TLS"):
        GitLabRunHttp(credentials=Credentials(), origin_policy=POLICY, ssl_context=context)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-canary.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent-ca-canary")

    async def scenario() -> None:
        def external(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") is None
            assert request.headers.get("cookie") is None
            assert "canary" not in str(request.url)
            return httpx.Response(
                200, headers={"Content-Type": "application/json"}, stream=Stream((b"{}",))
            )

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            assert (await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)).data == {}

    asyncio.run(scenario())


def test_large_json_node_and_numeric_limits_are_public_failures() -> None:
    async def scenario() -> None:
        for body in (
            b"[" + b"0," * 200000 + b"0]",
            b'{"id":' + b"9" * 65 + b"}",
            b'{"x":0.' + b"1" * 129 + b"}",
        ):
            stream = Stream((body,))
            async with GitLabRunHttp(
                credentials=Credentials(),
                origin_policy=POLICY,
                transport=httpx.MockTransport(
                    lambda request, body_stream=stream: httpx.Response(
                        200, headers={"Content-Type": "application/json"}, stream=body_stream
                    )
                ),
            ) as http:
                with pytest.raises(GitLabRunHttpError) as error:
                    await http.get_json(GitLabMergeRequestResource(), max_bytes=1048576)
            assert error.value.kind == "invalid_response" and stream.closed

    asyncio.run(scenario())


def test_repeated_failed_reads_charge_delivered_chunks_and_retain_exhaustion() -> None:
    async def scenario() -> None:
        calls = 0
        streams: list[Stream] = []

        def external(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            stream = Stream((b"x" * 1048577,))
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(external),
        ) as http:
            # Every failed read delivers 1 MiB + 1. The 64th crosses 64 MiB.
            for _ in range(63):
                with pytest.raises(GitLabRunHttpError) as error:
                    await http.get_blob("c" * 40, max_bytes=1048576)
                assert error.value.kind == "body_too_large"
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_blob("c" * 40, max_bytes=1048576)
            assert error.value.kind == "budget_exhausted"
            assert calls == 64
            # A smaller later operation cannot reuse the exhausted attempt budget.
            with pytest.raises(GitLabRunHttpError) as error:
                await http.get_blob("c" * 40, max_bytes=1)
            assert error.value.kind == "budget_exhausted"
            assert calls == 64
        assert all(stream.closed for stream in streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["json", "head"])
def test_raw_utf8_header_octets_exceeding_budget_close_and_recheck(operation: str) -> None:
    async def scenario() -> None:
        credentials = Credentials()
        stream = Stream((b"{}",) if operation == "json" else ())
        headers = [(b"Content-Type", b"application/json"), (b"X-Extra", "é".encode() * 10000)]
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=stream)
            ),
        ) as http:
            with pytest.raises(GitLabRunHttpError) as error:
                if operation == "json":
                    await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
                else:
                    await http.head_file("file.py", "a" * 40)
            assert error.value.kind == "invalid_response"
        assert stream.closed and credentials.checks == 1

    asyncio.run(scenario())


def test_tree_page_registers_only_actual_pinned_fork_entries_for_raw_and_directory_reads() -> None:
    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabTreeResource
    from tests.test_gitlab_vcs import Router, reply

    async def scenario() -> None:
        router = Router()
        requests: list[httpx.Request] = []

        def external(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/repository/tree"):
                return reply(
                    [
                        {
                            "id": "d" * 40,
                            "name": "new.py",
                            "path": "src/new.py",
                            "type": "blob",
                            "mode": "100644",
                        },
                        {
                            "id": "f" * 40,
                            "name": "src",
                            "path": "src",
                            "type": "tree",
                            "mode": "040000",
                        },
                    ]
                )
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            await http.get_canonical_merge_request()
            result = await http.get_tree_page(GitLabTreeResource(INPUT.head_sha, side="head"))
            assert [(entry.path, entry.kind) for entry in result.entries] == [
                ("src/new.py", "file"),
                ("src", "directory"),
            ]
            assert result.terminal and result.next_page_token is None
            assert await http.tree_path("f" * 40, INPUT.head_sha, side="head") == "src"
            assert await http.get_blob("d" * 40, max_bytes=10) == b"new"
            metadata = await http.file_metadata("src/new.py", INPUT.head_sha, side="head")
            assert (metadata.blob_oid, metadata.size) == ("d" * 40, 3)
            before = len(requests)
            assert await http.file_metadata("src/new.py", INPUT.head_sha, side="head") == metadata
            assert len(requests) == before
            with pytest.raises(GitLabRunHttpError):
                await http.get_blob("e" * 40, max_bytes=10)
        assert any("/projects/29/repository/tree" in str(request.url) for request in requests)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    ["unknown_mode", "wrong_kind", "duplicate", "parent_symlink", "direct_child", "wrong_name"],
)
def test_owned_tree_rejects_invalid_entries_before_any_blob_authorization(fault: str) -> None:
    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabTreeResource
    from tests.test_gitlab_vcs import Router, reply

    async def scenario() -> None:
        router = Router()

        def external(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repository/tree"):
                entry = {
                    "id": "d" * 40,
                    "name": "file.py",
                    "path": "file.py",
                    "type": "blob",
                    "mode": "100644",
                }
                rows = [entry]
                if fault == "unknown_mode":
                    entry["mode"] = "100600"
                elif fault == "wrong_kind":
                    entry["type"] = "tree"
                elif fault == "duplicate":
                    rows = [entry, entry]
                elif fault == "parent_symlink":
                    entry["path"] = "link/file.py"
                    rows = [
                        {
                            "id": "e" * 40,
                            "name": "link",
                            "path": "link",
                            "type": "blob",
                            "mode": "120000",
                        },
                        entry,
                    ]
                elif fault == "direct_child":
                    entry["path"] = "src/file.py"
                else:
                    entry["name"] = "different.py"
                return reply(rows)
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            await http.get_canonical_merge_request()
            with pytest.raises(GitLabRunHttpError):
                await http.get_tree_page(
                    GitLabTreeResource(
                        INPUT.head_sha, side="head", recursive=fault != "direct_child"
                    )
                )
            with pytest.raises(GitLabRunHttpError):
                await http.get_blob("d" * 40, max_bytes=10)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["versions", "discussions"])
def test_publication_get_and_post_are_target_only_and_share_strict_decoder(kind: str) -> None:
    from typing import Literal, cast

    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabPublicationResponse

    async def scenario() -> None:
        seen: list[httpx.Request] = []
        streams: list[Stream] = []

        def external(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            stream = Stream((b'{"ok":true}',))
            streams.append(stream)
            return httpx.Response(
                201 if request.method == "POST" else 200,
                headers={"Content-Type": "application/json"},
                stream=stream,
            )

        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            response = await http.get_publication_json(
                cast(Literal["versions", "discussions"], kind), page=2, per_page=100
            )
            assert response.decode_json() == {"ok": True}
            created = await http.post_discussion({"body": "safe body", "position": {"new_line": 3}})
            assert created.status_code == 201
            assert "safe body" not in repr(created)
            assert isinstance(response, GitLabPublicationResponse)
        assert [
            (request.method, request.url.path, dict(request.url.params)) for request in seen
        ] == [
            (
                "GET",
                f"/api/v4/projects/17/merge_requests/12/{kind}",
                {"page": "2", "per_page": "100"},
            ),
            ("POST", "/api/v4/projects/17/merge_requests/12/discussions", {}),
        ]
        assert all(request.headers["PRIVATE-TOKEN"] == TOKEN for request in seen)
        assert all(stream.closed for stream in streams)
        assert credentials.checks == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload", [{"body": "\ud800"}, {"body": float("nan")}, {"body": "x" * 1048576}]
)
def test_outbound_publication_json_is_finite_and_safe_before_wire(
    payload: dict[str, object],
) -> None:
    from typing import cast

    from app.modules.integrations.gitlab.infrastructure.run_http import JsonValue

    async def scenario() -> None:
        def external(request: httpx.Request) -> httpx.Response:
            raise AssertionError("invalid outbound payload reached wire")

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            with pytest.raises(GitLabRunHttpError) as caught:
                await http.post_discussion(cast(dict[str, JsonValue], payload))
            assert str(caught.value) in {
                "GitLab read failed (invalid_response)",
                "GitLab read failed (body_too_large)",
            }
            assert caught.value.__cause__ is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_publication_ordinary_status_is_closed_and_fenced_before_classification(
    status: int,
) -> None:
    async def scenario() -> None:
        stream = Stream((b"provider-body-canary",))
        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, headers={"Retry-After": "7"}, stream=stream)
            ),
        ) as http:
            response = await http.get_publication_json("discussions")
            assert response.status_code == status
            assert response.retry_after is not None and response.retry_after.total_seconds() == 7
            assert "provider-body-canary" not in repr(response)
            assert stream.closed and credentials.checks == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("page,size", [(True, 100), (0, 100), (-1, 100), (1, 101), (1, False)])
def test_publication_pagination_rejects_invalid_values_before_wire(page: int, size: int) -> None:
    async def scenario() -> None:
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("wire"))
            ),
        ) as http:
            with pytest.raises(ValueError):
                await http.get_publication_json("discussions", page=page, per_page=size)

    asyncio.run(scenario())
