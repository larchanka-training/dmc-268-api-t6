"""GitHub App VCS adapter for complete PR file lists and SHA-addressed blobs."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)

_SHA = r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"
_MAX_FILES = 3000
_PAGE_SIZE = 100
_REQUEST_TIMEOUT = 10.0
_API_VERSION = "2022-11-28"


class InstallationTokenProvider(Protocol):
    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class _GitHubDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _User(_GitHubDto):
    login: str = Field(min_length=1, max_length=255)


class _Repo(_GitHubDto):
    full_name: str = Field(min_length=3, max_length=512)


class _Ref(_GitHubDto):
    ref: str = Field(min_length=1, max_length=255)
    sha: str = Field(pattern=_SHA)
    repo: _Repo | None


class _Label(_GitHubDto):
    name: str = Field(min_length=1)


class _PullRequest(_GitHubDto):
    id: int = Field(gt=0, le=2**63 - 1)
    number: int = Field(gt=0, le=2**31 - 1)
    title: str = Field(min_length=1, max_length=500)
    body: str | None = None
    user: _User
    head: _Ref
    base: _Ref
    labels: list[_Label] = Field(default_factory=list)
    draft: bool
    changed_files: int = Field(ge=0)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class _File(_GitHubDto):
    filename: str = Field(min_length=1, max_length=1024)
    status: Literal["added", "modified", "removed", "renamed"]
    sha: str | None = Field(default=None, pattern=_SHA)
    previous_filename: str | None = None
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changes: int = Field(ge=0)
    patch: str | None = None
    size: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_file(self) -> _File:
        if self.status == "renamed" and not self.previous_filename:
            raise ValueError("renamed GitHub file needs previous_filename")
        if self.status != "removed" and self.sha is None:
            raise ValueError("GitHub file needs an immutable blob SHA")
        if self.changes != self.additions + self.deletions:
            raise ValueError("GitHub file change counts disagree")
        return self


class _Blob(_GitHubDto):
    sha: str = Field(pattern=_SHA)
    encoding: Literal["base64"]
    content: str
    size: int = Field(ge=0)


_FILES = TypeAdapter(list[_File])


def _repository_prefix(locator: PullRequestLocator) -> str:
    if locator.installation_external_id <= 0 or locator.number <= 0:
        raise ValueError("GitHub installation and pull request number must be positive")
    if locator.repository_full_name.count("/") != 1:
        raise ValueError("GitHub repository must be owner/repo")
    owner, repo = locator.repository_full_name.split("/", 1)
    if not owner or not repo:
        raise ValueError("GitHub repository must be owner/repo")
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }


class HttpGitHubVcsProvider:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
        prefix = _repository_prefix(locator)
        token = await self._tokens.get_installation_access_token(locator.installation_external_id)
        response = await self._client.get(
            f"{prefix}/pulls/{locator.number}",
            headers=_headers(token),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        item = _PullRequest.model_validate(response.json())
        if item.number != locator.number:
            raise ValueError("GitHub pull request number changed")
        return VcsPullRequest(
            locator=locator,
            head_sha=item.head.sha,
            base_sha=item.base.sha,
            meta=PullRequestMeta(
                title=item.title,
                description=item.body,
                author=item.user.login,
                source_branch=item.head.ref,
                target_branch=item.base.ref,
                labels=tuple(label.name for label in item.labels),
                files_changed=item.changed_files,
                lines_added=item.additions,
                lines_removed=item.deletions,
                is_draft=item.draft,
                is_fork=item.head.repo is None
                or item.base.repo is None
                or item.head.repo.full_name != item.base.repo.full_name,
            ),
        )

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
        locator = pull_request.locator
        prefix = _repository_prefix(locator)
        expected = pull_request.meta.files_changed
        if expected > _MAX_FILES:
            raise ValueError("GitHub pull request exceeds the 3000-file API cap")
        token = await self._tokens.get_installation_access_token(locator.installation_external_id)
        files: list[VcsFile] = []
        for page in range(1, _MAX_FILES // _PAGE_SIZE + 1):
            response = await self._client.get(
                f"{prefix}/pulls/{locator.number}/files",
                params={"per_page": _PAGE_SIZE, "page": page},
                headers=_headers(token),
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            batch = _FILES.validate_python(response.json())
            if len(batch) > _PAGE_SIZE:
                raise ValueError("GitHub file page exceeded 100 entries")
            files.extend(
                VcsFile(
                    filename=item.filename,
                    status=item.status,
                    blob_sha=None if item.status == "removed" else item.sha,
                    previous_filename=item.previous_filename,
                    additions=item.additions,
                    deletions=item.deletions,
                    changes=item.changes,
                    patch=item.patch,
                    size=item.size,
                )
                for item in batch
            )
            if len(files) > expected:
                raise ValueError("GitHub file pagination exceeded the PR file count")
            if len(files) == expected:
                break
            if len(batch) < _PAGE_SIZE:
                raise ValueError("GitHub file pagination ended before the PR file count")
        else:
            raise ValueError("GitHub file pagination exceeded the 3000-file API cap")
        if len({file.filename for file in files}) != len(files):
            raise ValueError("GitHub file pagination repeated a filename")
        if await self.get_pull_request(locator) != pull_request:
            raise ValueError("GitHub pull request changed during file pagination")
        return tuple(files)

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
        prefix = _repository_prefix(locator)
        if re.fullmatch(_SHA, sha) is None:
            raise ValueError("GitHub blob SHA must be 40 or 64 hexadecimal characters")
        token = await self._tokens.get_installation_access_token(locator.installation_external_id)
        response = await self._client.get(
            f"{prefix}/git/blobs/{sha}",
            headers=_headers(token),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        item = _Blob.model_validate(response.json())
        if item.sha.casefold() != sha.casefold():
            raise ValueError("GitHub blob SHA differs from the requested SHA")
        try:
            data = base64.b64decode("".join(item.content.split()), validate=True)
        except binascii.Error as exc:
            raise ValueError("GitHub blob content is invalid base64") from exc
        if len(data) != item.size:
            raise ValueError("GitHub blob decoded size differs from declared size")
        return data
