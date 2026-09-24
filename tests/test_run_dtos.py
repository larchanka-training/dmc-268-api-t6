from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.api.dtos import PullRequestDto, RunSessionDto


def test_run_session_serializes_the_frontend_camel_case_contract() -> None:
    dto = RunSessionDto(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        status="succeeded",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        started_at=datetime(2026, 9, 24, tzinfo=UTC),
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        pull_request=PullRequestDto(
            repo="org/repo",
            number=1,
            title="Title",
            url="https://example.test/pr/1",
            head_sha="a" * 40,
        ),
    )

    assert dto.model_dump(by_alias=True)["cancelRequested"] is False
    assert dto.model_dump(by_alias=True)["pullRequest"]["headSha"] == "a" * 40
