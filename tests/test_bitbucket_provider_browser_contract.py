"""Browser projections expose provider identity instead of inferring it from a URL."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.main import to_repository_dto, to_run_session_dto
from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.repositories.application.repository_settings import RepositorySettings
from app.modules.reviews.application.list_runs import RunListItem


def test_bitbucket_repository_and_run_expose_provider_identity() -> None:
    now = datetime.now(UTC)
    repository = RepositorySettings(
        id=uuid4(),
        full_name="private-space/private-repo",
        url="https://bitbucket.org/private-space/private-repo",
        default_branch="main",
        enabled=True,
        default_engine="fast",
        wait_for_ci="auto",
        max_comments=10,
        review_event="COMMENT",
        branch_filter=(),
        severity_threshold="all",
        installation_available=True,
        rag_enabled=False,
        rag_index=RagIndex(RagIndexStatus.DISABLED, None, None, 0, 0, None),
        provider="bitbucket",
    )
    assert to_repository_dto(repository).model_dump(by_alias=True)["provider"] == "bitbucket"

    run = RunListItem(
        id=uuid4(),
        status="queued",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        started_at=None,
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        repo=repository.full_name,
        number=7,
        title="Review",
        url="https://bitbucket.org/private-space/private-repo/pull-requests/7",
        head_sha="a" * 40,
        created_at=now,
        trigger="webhook",
        provider="bitbucket",
    )
    dto = to_run_session_dto(run).model_dump(by_alias=True)
    assert dto["provider"] == "bitbucket"
    assert dto["pullRequest"]["provider"] == "bitbucket"
