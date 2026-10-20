"""Canonical same-instance GitLab MR read; client trust is injected by composition."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabRequestAuthority,
)
from app.modules.integrations.gitlab.infrastructure.response_body import (
    GitLabResponseBodyError,
    read_response_body,
    validate_response_headers,
)
from app.modules.reviews.application.project_gitlab_merge_request import (
    CanonicalGitLabMergeRequest,
    CanonicalMergeRequestAccessDenied,
    CanonicalMergeRequestIdentityMismatch,
    CanonicalMergeRequestInvalidResponse,
    CanonicalMergeRequestNotReady,
    CanonicalMergeRequestRateLimited,
    CanonicalMergeRequestUnavailable,
    GitLabDiffRefs,
    GitLabMergeRequestCheckpoint,
    GitLabMergeRequestCommand,
    GitLabMergeRequestState,
)


class HttpGitLabCurrentMergeRequestProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        authority: GitLabRequestAuthority,
        origin_policy: GitLabOriginPolicy,
        timeout_seconds: float = 10.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("Invalid GitLab request timeout")
        self._timeout_seconds = timeout_seconds
        self._sleep = sleeper
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client
        self._authority = authority
        self._origin_policy = origin_policy

    async def get_current(
        self,
        command: GitLabMergeRequestCommand,
        *,
        target: BoundGitLabTarget,
        checkpoint: GitLabMergeRequestCheckpoint,
    ) -> CanonicalGitLabMergeRequest:
        if (
            command.origin_provider_installation_id != target.origin_provider_installation_id
            or command.project_id != target.project_id
        ):
            raise CanonicalMergeRequestIdentityMismatch()
        origin = self._origin_policy.validate(target.instance_origin)
        credential = await self._authority.credential_for(target)
        if credential.target != target:
            raise GitLabCredentialGenerationChanged()
        await self._checkpoint(target, checkpoint)
        waits = (0.1, 0.25)
        for attempt in range(3):
            # A standalone Request bypasses ambient client headers, cookies, auth and query params.
            # Composition owns the injected client's verified transport and clean event hooks.
            request = httpx.Request(
                "GET",
                f"{origin}/api/v4/projects/{target.project_id}/merge_requests/{command.iid}",
                headers={
                    "PRIVATE-TOKEN": credential.token,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                extensions={"timeout": self._timeout.as_dict()},
            )
            await self._checkpoint(target, checkpoint)
            try:
                # Bound the whole streamed read too: a slow trickle can evade per-read timeouts.
                async with asyncio.timeout(self._timeout_seconds):
                    response = await self._client.send(
                        request,
                        auth=None,
                        follow_redirects=False,
                        stream=True,
                    )
                    try:
                        data = _decode(await _body(response))
                    finally:
                        await response.aclose()
            except (httpx.HTTPError, TimeoutError):
                raise CanonicalMergeRequestUnavailable() from None
            await self._checkpoint(target, checkpoint)
            try:
                return _canonical(data, command, target)
            except CanonicalMergeRequestNotReady:
                if attempt == 2:
                    raise CanonicalMergeRequestNotReady() from None
            await self._sleep(waits[attempt])
            await self._checkpoint(target, checkpoint)
        raise AssertionError("Unreachable bounded GitLab attempt")

    async def _checkpoint(
        self,
        target: BoundGitLabTarget,
        checkpoint: GitLabMergeRequestCheckpoint,
    ) -> None:
        await checkpoint()
        await self._authority.checkpoint(target)


async def _body(response: httpx.Response) -> bytes:
    if response.status_code in (401, 403):
        raise CanonicalMergeRequestAccessDenied() from None
    if response.status_code == 429:
        raise CanonicalMergeRequestRateLimited() from None
    if 300 <= response.status_code < 400:
        raise CanonicalMergeRequestInvalidResponse() from None
    if response.status_code != 200:
        raise CanonicalMergeRequestUnavailable() from None
    try:
        validate_response_headers(response)
        return await read_response_body(response, 1048576, json_content=True)
    except GitLabResponseBodyError:
        raise CanonicalMergeRequestInvalidResponse() from None


def _decode(body: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def integer(value: str) -> int:
        if len(value) > 64:
            raise ValueError
        return int(value)

    def finite(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError

    try:
        data = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_int=integer,
            parse_float=finite,
            parse_constant=invalid_constant,
        )
        if not isinstance(data, dict):
            raise ValueError
        pending: list[tuple[Any, int]] = [(data, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 128:
                raise ValueError
            if isinstance(value, dict):
                pending.extend((key, depth + 1) for key in value)
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
            elif isinstance(value, str) and any(
                ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value
            ):
                raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        raise CanonicalMergeRequestInvalidResponse() from None
    return data


def _text(data: dict[str, Any], key: str, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise CanonicalMergeRequestInvalidResponse()
    return value


def _canonical(
    data: dict[str, Any],
    command: GitLabMergeRequestCommand,
    target: BoundGitLabTarget,
) -> CanonicalGitLabMergeRequest:
    for key, expected in (
        ("id", command.external_id),
        ("iid", command.iid),
        ("project_id", target.project_id),
        ("target_project_id", target.project_id),
    ):
        value = data.get(key)
        if type(value) is not int or value != expected:
            raise CanonicalMergeRequestIdentityMismatch()
    source = data.get("source_project_id")
    if type(source) is not int or not 0 < source <= 9223372036854775807:
        raise CanonicalMergeRequestInvalidResponse()
    if data.get("state") == "locked":
        # GitLab documents this merge-in-progress state; retry finitely, never project it as open.
        raise CanonicalMergeRequestNotReady()
    try:
        state = GitLabMergeRequestState(_text(data, "state", 16))
        updated = datetime.fromisoformat(_text(data, "updated_at", 64))
        if updated.tzinfo is None or updated.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise CanonicalMergeRequestInvalidResponse() from None
    web_url = _text(data, "web_url", 2048)
    try:
        parsed = urlsplit(web_url)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != target.instance_origin
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
            or any(ord(char) < 33 or ord(char) == 127 for char in web_url)
        ):
            raise ValueError
    except ValueError:
        raise CanonicalMergeRequestInvalidResponse() from None
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise CanonicalMergeRequestInvalidResponse()
    author = data.get("author")
    if not isinstance(author, dict):
        raise CanonicalMergeRequestInvalidResponse()
    refs = data.get("diff_refs")
    if refs is None:
        raise CanonicalMergeRequestNotReady()
    if not isinstance(refs, dict):
        raise CanonicalMergeRequestInvalidResponse()
    validated_refs: list[str] = []
    for name in ("head_sha", "start_sha", "base_sha"):
        value = refs.get(name)
        if value is None or value == "":
            raise CanonicalMergeRequestNotReady()
        if (
            not isinstance(value, str)
            or re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value) is None
        ):
            raise CanonicalMergeRequestInvalidResponse()
        validated_refs.append(value)
    return CanonicalGitLabMergeRequest(
        target=target,
        external_id=command.external_id,
        project_id=target.project_id,
        iid=command.iid,
        target_project_id=target.project_id,
        source_project_id=source,
        state=state,
        title=_text(data, "title", 500),
        description=description,
        author_login=_text(author, "username", 255),
        web_url=web_url,
        source_branch=_text(data, "source_branch", 255),
        target_branch=_text(data, "target_branch", 255),
        provider_updated_at=updated,
        diff_refs=GitLabDiffRefs(*validated_refs),
    )
