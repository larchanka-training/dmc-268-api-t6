"""One deterministic Bitbucket build status for a pull-request head."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.modules.integrations.bitbucket.application.request_credentials import BoundBitbucketTarget

BitbucketBuildState = Literal["INPROGRESS", "SUCCESSFUL", "STOPPED"]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BitbucketStatusSnapshot:
    run_id: UUID
    target: BoundBitbucketTarget
    pr_number: int
    head_sha: str
    source_branch: str
    state: str
    run_order: int | None = None
    coverage_status: str | None = None
    target_branch: str | None = None


class BitbucketStatusGateway(Protocol):
    async def upsert(self, run_id: UUID, run_url: str | None) -> None: ...


async def update_bitbucket_status(
    gateway: BitbucketStatusGateway | None, run_id: UUID, run_url: str | None
) -> None:
    """Provider status failure does not change the persisted Run state."""
    if gateway is None:
        return
    try:
        await gateway.upsert(run_id, run_url)
    except Exception:
        _LOGGER.exception("Bitbucket status update failed for run %s", run_id)


def bitbucket_status_payload(
    *,
    workspace_id: UUID,
    repository_uuid: UUID,
    pr_number: int,
    head_sha: str,
    source_branch: str,
    run_url: str,
    state: str,
    coverage_status: str | None = None,
) -> dict[str, str]:
    """A rerun on the same PR/head overwrites the previous status key."""
    if (
        type(workspace_id) is not UUID
        or type(repository_uuid) is not UUID
        or type(pr_number) is not int
        or pr_number <= 0
        or len(head_sha) != 40
        or any(char not in "0123456789abcdef" for char in head_sha)
        or not source_branch
        or len(source_branch) > 255
        or not run_url.startswith("https://")
        or state
        not in {"queued", "running", "publishing", "succeeded", "failed", "cancelled", "skipped"}
    ):
        raise ValueError("invalid Bitbucket build status")
    digest = hashlib.sha256(
        f"{workspace_id}:{repository_uuid}:{pr_number}:{head_sha}".encode()
    ).hexdigest()
    remote_state: BitbucketBuildState
    if state in {"queued", "running", "publishing"}:
        remote_state = "INPROGRESS"
        description = "AI review running"
    elif state == "succeeded":
        remote_state = "SUCCESSFUL"
        if coverage_status == "none":
            description = "AI review: nothing reviewable"
        elif coverage_status == "partial":
            description = "AI review incomplete"
        else:
            description = "AI review complete"
    else:
        # The review reports failures without failing the repository's merge check.
        remote_state = "STOPPED"
        description = {
            "failed": "AI review did not complete",
            "cancelled": "AI review cancelled",
            "skipped": "AI review skipped",
        }[state]
    return {
        "key": f"AI-REVIEW-{digest[:30]}",
        "state": remote_state,
        "name": "AI Code Reviewer",
        "description": description,
        "url": run_url,
        "refname": source_branch,
    }
