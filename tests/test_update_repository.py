"""`UpdateRepository` against fakes of its ports: only `ragEnabled` touches the index (#143).

The order is the update, the purge with `disabled`, the re-read for the response, then
the commit; a body without `ragEnabled=false` purges nothing, and a repository the scoped
store cannot find is neither purged nor committed. `ragEnabled=true` enables the index
the same way, but only when the re-read shows RAG in effect (an available repository).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.bootstrap.reviews_api import get_repository_settings
from app.main import app
from app.modules.code_index.domain.enums import PurgeReason, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.repositories.application.repository_settings import (
    RepositorySettings,
    RepositorySettingsChange,
    UpdateRepository,
)
from tests.portal_test_client import authenticated_test_client

REPOSITORY_ID = UUID("00000000-0000-4000-8000-0000000000a2")
UPDATED = RepositorySettings(
    id=REPOSITORY_ID,
    full_name="octo/repo",
    url="https://github.com/octo/repo",
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
)
# The read after the purge differs from the update's result, so the test sees which one
# the use case returns.
REREAD = replace(UPDATED, full_name="octo/repo-after-purge")


@dataclass
class FakeRepositories:
    """The scoped store, the purger and the transaction, recording every call in order."""

    found: bool = True
    # The effective `rag_enabled` the update reads back: the toggle AND the installation.
    rag_in_effect: bool = False
    provider: str = "github"
    calls: list[str] = field(default_factory=list)

    @property
    def repositories(self) -> Self:
        return self

    @property
    def index_purger(self) -> Self:
        return self

    @property
    def index_enabler(self) -> Self:
        return self

    async def list_repositories(self) -> list[RepositorySettings]:
        raise AssertionError("UpdateRepository does not list repositories")

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None:
        self.calls.append(f"get {repository_id == REPOSITORY_ID}")
        return replace(REREAD, provider=self.provider) if self.found else None

    async def update_repository(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None:
        self.calls.append(f"update {repository_id == REPOSITORY_ID}")
        if not self.found:
            return None
        return replace(UPDATED, rag_enabled=self.rag_in_effect, provider=self.provider)

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        self.calls.append(f"purge {repository_id == REPOSITORY_ID} {reason}")

    async def enable(self, repository_id: UUID) -> None:
        self.calls.append(f"enable {repository_id == REPOSITORY_ID}")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


def _execute(fake: FakeRepositories, change: RepositorySettingsChange) -> RepositorySettings | None:
    return asyncio.run(UpdateRepository(lambda: fake).execute(REPOSITORY_ID, change))


def test_rag_off_purges_with_disabled_rereads_and_then_commits() -> None:
    fake = FakeRepositories()

    item = _execute(fake, RepositorySettingsChange(rag_enabled=False))

    assert fake.calls == ["update True", "purge True disabled", "get True", "commit"]
    assert item == REREAD


def test_rag_on_in_effect_enables_rereads_and_then_commits() -> None:
    fake = FakeRepositories(rag_in_effect=True)

    item = _execute(fake, RepositorySettingsChange(rag_enabled=True))

    assert fake.calls == ["update True", "enable True", "get True", "commit"]
    assert item == REREAD


@pytest.mark.parametrize(
    ("change", "rag_in_effect"),
    [
        (RepositorySettingsChange(enabled=False), True),
        # The installation masks the toggle: an unavailable repository gets no build.
        (RepositorySettingsChange(rag_enabled=True), False),
    ],
    ids=["rag-enabled-absent", "rag-on-unavailable"],
)
def test_a_body_touches_the_index_only_when_rag_is_in_effect(
    change: RepositorySettingsChange, rag_in_effect: bool
) -> None:
    fake = FakeRepositories(rag_in_effect=rag_in_effect)

    item = _execute(fake, change)

    assert fake.calls == ["update True", "commit"]
    assert item == replace(UPDATED, rag_enabled=rag_in_effect)


def test_a_missing_repository_is_neither_purged_nor_committed() -> None:
    fake = FakeRepositories(found=False)

    item = _execute(fake, RepositorySettingsChange(rag_enabled=False))

    assert fake.calls == ["update True"]
    assert item is None


@pytest.mark.parametrize(
    "change",
    [
        RepositorySettingsChange(wait_for_ci="auto"),
        RepositorySettingsChange(wait_for_ci="always"),
        RepositorySettingsChange(review_event="REQUEST_CHANGES"),
    ],
    ids=["ci-auto", "ci-always", "request-changes"],
)
def test_bitbucket_rejects_settings_it_cannot_honor_before_writing(
    change: RepositorySettingsChange,
) -> None:
    fake = FakeRepositories(provider="bitbucket")

    with pytest.raises(ValueError, match="bitbucket_.*_unsupported"):
        _execute(fake, change)

    assert fake.calls == ["get True"]


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_other_providers_keep_ci_and_review_event_settings(provider: str) -> None:
    fake = FakeRepositories(provider=provider)

    item = _execute(
        fake,
        RepositorySettingsChange(wait_for_ci="always", review_event="REQUEST_CHANGES"),
    )

    assert item is not None and item.provider == provider
    assert fake.calls == ["get True", "update True", "commit"]


def test_unsupported_setting_on_missing_repository_does_not_expose_provider() -> None:
    fake = FakeRepositories(found=False)

    item = _execute(fake, RepositorySettingsChange(wait_for_ci="auto"))

    assert item is None
    assert fake.calls == ["get True"]


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"waitForCi": "auto"}, "waitForCi", "bitbucket_wait_for_ci_unsupported"),
        ({"waitForCi": "always"}, "waitForCi", "bitbucket_wait_for_ci_unsupported"),
        (
            {"reviewEvent": "REQUEST_CHANGES"},
            "reviewEvent",
            "bitbucket_review_event_unsupported",
        ),
    ],
)
def test_patch_bitbucket_rejects_unsupported_settings_with_422(
    body: dict[str, str], field: str, code: str
) -> None:
    fake = FakeRepositories(provider="bitbucket")
    app.dependency_overrides[get_repository_settings] = lambda: lambda: fake
    try:
        response = authenticated_test_client(app).patch(f"/api/repos/{REPOSITORY_ID}", json=body)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    assert response.json()["detail"][0]["msg"] == code
    assert fake.calls == ["get True"]


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_patch_other_providers_keeps_existing_review_settings(provider: str) -> None:
    fake = FakeRepositories(provider=provider)
    app.dependency_overrides[get_repository_settings] = lambda: lambda: fake
    try:
        response = authenticated_test_client(app).patch(
            f"/api/repos/{REPOSITORY_ID}",
            json={"waitForCi": "always", "reviewEvent": "REQUEST_CHANGES"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert fake.calls == ["get True", "update True", "commit"]
