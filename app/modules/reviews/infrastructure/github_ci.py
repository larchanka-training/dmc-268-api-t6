"""Fetch check suites and combined commit status for one immutable GitHub head."""

from __future__ import annotations

from typing import Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.modules.reviews.application.determine_ci_eligibility import CheckSuite, CiSnapshot

_SHA_PATTERN = r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"


class InstallationTokenProvider(Protocol):
    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class _AppDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)


class _SuiteDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    app: _AppDto
    status: str = Field(min_length=1)
    conclusion: str | None
    # Absent means "runs may exist": the gate keeps blocking on the suite (fail-safe).
    latest_check_runs_count: int = Field(default=1, ge=0)


class _SuitesPageDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    total_count: int = Field(ge=0)
    check_suites: list[_SuiteDto]


class _CombinedStatusDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    sha: str = Field(pattern=_SHA_PATTERN)
    state: Literal["success", "failure", "pending"]
    total_count: int = Field(ge=0)


class HttpGitHubCurrentHeadCiProvider:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def get_current_head_ci(
        self, installation_external_id: int, repository_full_name: str, head_sha: str
    ) -> CiSnapshot:
        if repository_full_name.count("/") != 1:
            raise ValueError("GitHub repository name must be owner/repo")
        owner, repo = repository_full_name.split("/", 1)
        if (
            not owner
            or not repo
            or len(head_sha) not in {40, 64}
            or (any(character not in "0123456789abcdefABCDEF" for character in head_sha))
        ):
            raise ValueError("Invalid GitHub repository or head SHA")
        token = await self._tokens.get_installation_access_token(installation_external_id)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/commits/{head_sha}"
        suites: list[CheckSuite] = []
        expected_count: int | None = None
        for page in range(1, 1001):
            response = await self._client.get(
                f"{prefix}/check-suites",
                params={"per_page": 100, "page": page},
                headers=headers,
            )
            response.raise_for_status()
            parsed = _SuitesPageDto.model_validate(response.json())
            if expected_count is None:
                expected_count = parsed.total_count
            elif expected_count != parsed.total_count:
                raise ValueError("GitHub check suite count changed during pagination")
            for item in parsed.check_suites:
                if item.head_sha.casefold() != head_sha.casefold():
                    raise ValueError("GitHub check suite head SHA mismatch")
                suites.append(
                    CheckSuite(
                        app_id=item.app.id,
                        status=item.status,
                        conclusion=item.conclusion,
                        latest_check_runs_count=item.latest_check_runs_count,
                    )
                )
            if len(suites) >= expected_count:
                if len(suites) != expected_count:
                    raise ValueError("GitHub check suite page exceeded total count")
                break
            if len(parsed.check_suites) < 100:
                raise ValueError("GitHub check suite pagination ended early")
        else:
            raise ValueError("GitHub check suite pagination exceeded bound")

        response = await self._client.get(f"{prefix}/status", headers=headers)
        response.raise_for_status()
        status = _CombinedStatusDto.model_validate(response.json())
        if status.sha.casefold() != head_sha.casefold():
            raise ValueError("GitHub combined status SHA mismatch")
        return CiSnapshot(head_sha, tuple(suites), status.state, status.total_count)
