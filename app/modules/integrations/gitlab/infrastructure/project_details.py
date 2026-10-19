"""Authenticated GitLab GETs for onboarding, outside database transactions.

Only /user, /projects/:id and /projects/:id/languages are read. Read access does
not establish permission to publish; operators need an API-scoped token and an
appropriate project role for later publication. Never probe by posting a discussion.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabManagementError,
    GitLabOriginPolicy,
    validate_project_snapshot,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)


class GitLabProjectDetails:
    def __init__(self, client: httpx.AsyncClient, *, origin_policy: GitLabOriginPolicy) -> None:
        self._client = client
        self._origins = origin_policy

    async def authenticate(self, *, origin: str, token: str) -> None:
        identity = await self._get(origin, token, "/user", "invalid_token")
        if (
            type(identity.get("id")) is not int
            or identity["id"] <= 0
            or identity.get("state") != "active"
        ):
            raise GitLabManagementError("invalid_token")

    async def fetch_project(
        self, *, origin: str, token: str, project_id: int
    ) -> RepositoryOnboardingInput:
        if type(project_id) is not int or not 0 < project_id <= 9223372036854775807:
            raise GitLabManagementError("invalid_identity")
        await self.authenticate(origin=origin, token=token)
        project = await self._get(origin, token, f"/projects/{project_id}", "invalid_project")
        if project.get("archived") is not False or project.get("empty_repo") is not False:
            raise GitLabManagementError("project_unavailable")
        project_id_value = project.get("id")
        path = project.get("path_with_namespace")
        branch = project.get("default_branch")
        web_url = project.get("web_url")
        if type(project_id_value) is not int or project_id_value != project_id:
            raise GitLabManagementError("project_identity_mismatch")
        if not isinstance(path, str) or not isinstance(branch, str) or not isinstance(web_url, str):
            raise GitLabManagementError("invalid_project")
        snapshot = RepositorySnapshot(project_id_value, path, branch, web_url)
        validate_project_snapshot(snapshot, origin=origin, project_id=project_id)
        languages = await self._get(
            origin, token, f"/projects/{project_id}/languages", "invalid_languages"
        )
        shares: dict[str, Decimal] = {}
        for name, value in languages.items():
            if not name or len(name) > 100 or type(value) not in (int, Decimal):
                raise GitLabManagementError("invalid_languages")
            share = Decimal(value)
            exponent = share.as_tuple().exponent
            if (
                not share.is_finite()
                or not isinstance(exponent, int)
                or not 0 <= share <= 100
                or exponent < -18
            ):
                raise GitLabManagementError("invalid_languages")
            if share > 0:
                shares[name] = share
        # Exact common decimal scale preserves ordering (including 49.9/50.1).
        # These are percentage weights, never fabricated Git tree byte sizes.
        places = max(
            (max(0, -int(share.as_tuple().exponent)) for share in shares.values()), default=0
        )
        scale = 10 ** max(2, places)
        weights = {name: int(share * scale) for name, share in shares.items()}
        return RepositoryOnboardingInput(snapshot, weights)

    async def _get(self, origin: str, token: str, path: str, invalid_code: str) -> dict[str, Any]:
        self._origins.validate(origin)
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in token)
        ):
            raise GitLabManagementError("invalid_token")
        try:
            async with self._client.stream(
                "GET",
                f"{origin}/api/v4{path}",
                headers={"PRIVATE-TOKEN": token},
                follow_redirects=False,
            ) as response:
                if response.status_code in (401, 403):
                    raise GitLabManagementError("invalid_token")
                if response.status_code != 200:
                    raise GitLabManagementError("project_access_failed")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > 1048576:
                        raise GitLabManagementError(invalid_code)
                    body.extend(chunk)
        except httpx.HTTPError:
            raise GitLabManagementError("project_access_failed") from None
        try:
            value = json.loads(body, parse_float=Decimal)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise GitLabManagementError(invalid_code) from None
