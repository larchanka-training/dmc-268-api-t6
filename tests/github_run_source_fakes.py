"""Shared fakes of the GitHub Run source tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.vcs_diff import PullRequestLocator

HEAD = "e" * 40
BASE = "b" * 40
LOCATOR = PullRequestLocator(
    installation_external_id=17, repository_full_name="octo/repo", number=7
)


@dataclass
class Runs:
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(
            code_change_id=uuid4(),
            repository_id=uuid4(),
            head_sha=HEAD,
            base_sha=BASE,
            locator=LOCATOR,
        )

    async def get_run_snapshots(self, run_id: UUID) -> None:
        return None


@dataclass
class Blobs:
    """Blob contents by sha; a missing sha answers like a GitHub error."""

    contents: dict[str, bytes] = field(default_factory=dict)

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        if sha not in self.contents:
            request = httpx.Request("GET", f"https://api.github.test/git/blobs/{sha}")
            raise httpx.HTTPStatusError(
                "server error", request=request, response=httpx.Response(502, request=request)
            )
        return self.contents[sha]
