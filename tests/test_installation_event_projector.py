"""Behavioural contracts for projecting durable installation events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest

from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationEventProjector,
    InstallationRepositoryDetailsProvider,
    InstallationRepositoryLabelProvider,
    InstallationRepositoryTreeProvider,
    RepositoryDetails,
    RepositoryDetailsUnavailableError,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    _DISPATCH_TIMEOUT_SECONDS,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationTreeProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_labels import (
    GitHubRepositoryLabelProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositoryReference,
    RepositorySnapshot,
    RepositoryTreeBlob,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)


def _reference(*, external_id: int = 101, branch: str = "main") -> RepositoryReference:
    return RepositoryReference(
        external_id=external_id,
        full_name=f"octo/repository-{external_id}",
        default_branch=branch,
        web_url=f"https://github.com/octo/repository-{external_id}",
    )


def _bare_reference(
    *, external_id: int, branch: str | None = None, url: str | None = None
) -> RepositoryReference:
    """A repository as GitHub really sends it: id and name, usually no branch or URL."""
    return RepositoryReference(
        external_id=external_id,
        full_name=f"example-owner/repo-{external_id}",
        default_branch=branch,
        web_url=url,
    )


@dataclass
class FakeTreeProvider(InstallationRepositoryTreeProvider):
    trees: dict[int, tuple[RepositoryTreeBlob, ...]] = field(default_factory=dict)
    error: Exception | None = None
    calls: list[tuple[int, int, str]] = field(default_factory=list)

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        self.calls.append(
            (installation_external_id, repository.external_id, repository.default_branch)
        )
        if self.error is not None:
            raise self.error
        return self.trees[repository.external_id]


@dataclass
class FakeSyncInstallationRepositories:
    calls: list[tuple[UUID, tuple[RepositoryOnboardingInput, ...]]] = field(default_factory=list)
    disable_calls: list[tuple[UUID, tuple[RepositoryReference, ...]]] = field(default_factory=list)

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]:
        self.calls.append((provider_installation_id, repositories))
        return ()

    async def disable(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryReference, ...],
        all_repositories: bool = False,
    ) -> None:
        self.disable_calls.append((provider_installation_id, repositories))


@dataclass
class FakeLabelProvider(InstallationRepositoryLabelProvider):
    calls: list[tuple[int, str]] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None:
        self.calls.append((installation_external_id, repository.full_name))
        if repository.full_name in self.fail_for:
            raise RuntimeError("GitHub label API unavailable")


@dataclass
class FakeDetailsProvider(InstallationRepositoryDetailsProvider):
    details: dict[str, RepositoryDetails] = field(default_factory=dict)
    error: Exception | None = None
    calls: list[tuple[int, str]] = field(default_factory=list)

    async def fetch_repository_details(
        self, *, installation_external_id: int, full_name: str
    ) -> RepositoryDetails:
        self.calls.append((installation_external_id, full_name))
        if self.error is not None:
            raise self.error
        return self.details[full_name]


@pytest.mark.parametrize("action", ["created", "added"])
def test_added_repositories_get_ai_review_label_before_database_sync(
    action: Literal["created", "added"],
) -> None:
    first = _reference(external_id=101)
    second = _reference(external_id=102)
    tree_provider = FakeTreeProvider(trees={101: (), 102: ()})
    labels = FakeLabelProvider()

    class Sync(FakeSyncInstallationRepositories):
        async def execute(
            self,
            *,
            provider_installation_id: UUID,
            repositories: tuple[RepositoryOnboardingInput, ...],
        ) -> tuple[OnboardingResult, ...]:
            assert labels.calls == [
                (17, "octo/repository-101"),
                (17, "octo/repository-102"),
            ]
            return await super().execute(
                provider_installation_id=provider_installation_id, repositories=repositories
            )

    sync = Sync()
    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree_provider,
            label_provider=labels,
            details_provider=FakeDetailsProvider(),
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(17, action, (first, second), ()),
        )
    )

    assert tree_provider.calls == [(17, 101, "main"), (17, 102, "main")]
    assert labels.calls == [(17, "octo/repository-101"), (17, "octo/repository-102")]
    assert len(sync.calls) == 1


def test_label_failure_is_logged_but_other_repositories_still_onboard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _reference(external_id=101)
    second = _reference(external_id=102)
    labels = FakeLabelProvider(fail_for={first.full_name})
    sync = FakeSyncInstallationRepositories()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=FakeTreeProvider(trees={101: (), 102: ()}),
            label_provider=labels,
            details_provider=FakeDetailsProvider(),
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(17, "added", (first, second), ()),
        )
    )

    assert labels.calls == [(17, first.full_name), (17, second.full_name)]
    assert len(sync.calls) == 1
    assert [item.snapshot.full_name for item in sync.calls[0][1]] == [
        first.full_name,
        second.full_name,
    ]
    assert "GitHub label API unavailable" in caplog.text


def test_projector_fetches_each_declared_default_branch_before_entering_sync() -> None:
    first = _reference(external_id=101, branch="main")
    second = _reference(external_id=102, branch="trunk")
    provider = FakeTreeProvider(
        trees={
            101: (RepositoryTreeBlob(path="api/main.py", size=3, entry_type="blob"),),
            102: (RepositoryTreeBlob(path="web/app.ts", size=6, entry_type="blob"),),
        }
    )
    sync = FakeSyncInstallationRepositories()
    provider_installation_id = uuid4()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=provider,
            label_provider=FakeLabelProvider(),
            details_provider=FakeDetailsProvider(),
            sync=sync,
        ).execute(
            provider_installation_id=provider_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(first, second),
                removed_repositories=(),
            ),
        )
    )

    assert provider.calls == [(17, 101, "main"), (17, 102, "trunk")]
    assert len(sync.calls) == 1
    sync_installation_id, inputs = sync.calls[0]
    assert sync_installation_id == provider_installation_id
    assert [(item.snapshot.external_id, dict(item.languages)) for item in inputs] == [
        (101, {"Python": 100}),
        (102, {"TypeScript": 100}),
    ]


def test_empty_repository_tree_onboards_the_repository_without_languages() -> None:
    """Guard, green before the fix too: an empty tree must reach ``sync`` with no languages."""
    sync = FakeSyncInstallationRepositories()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=FakeTreeProvider(trees={101: ()}),
            label_provider=FakeLabelProvider(),
            details_provider=FakeDetailsProvider(),
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(17, "added", (_reference(external_id=101),), ()),
        )
    )

    assert [(item.snapshot.external_id, dict(item.languages)) for item in sync.calls[0][1]] == [
        (101, {})
    ]


@pytest.mark.parametrize("action", ["deleted", "removed"])
def test_projector_soft_disables_removed_repositories_without_a_vcs_request(action: str) -> None:
    provider = FakeTreeProvider()
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()
    provider_installation_id = uuid4()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=provider,
            label_provider=labels,
            details_provider=FakeDetailsProvider(),
            sync=sync,
        ).execute(
            provider_installation_id=provider_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action=action,  # type: ignore[arg-type]
                added_repositories=(),
                removed_repositories=(_reference(),),
            ),
        )
    )

    assert provider.calls == []
    assert labels.calls == []
    assert sync.calls == []
    assert sync.disable_calls == [(provider_installation_id, (_reference(),))]


def test_provider_failure_is_propagated_before_the_database_sync_begins() -> None:
    provider = FakeTreeProvider(error=RuntimeError("GitHub unavailable"))
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=provider,
                label_provider=FakeLabelProvider(),
                details_provider=FakeDetailsProvider(),
                sync=sync,
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    installation_external_id=17,
                    action="created",
                    added_repositories=(_reference(),),
                    removed_repositories=(),
                ),
            )
        )

    assert provider.calls == [(17, 101, "main")]
    assert sync.calls == []


def test_missing_branch_and_url_are_fetched_once_and_feed_tree_label_and_sync() -> None:
    details = FakeDetailsProvider(
        details={
            "example-owner/repo-101": RepositoryDetails(
                default_branch="trunk", web_url="https://example.test/example-owner/repo-101"
            )
        }
    )
    tree = FakeTreeProvider(trees={101: ()})

    class Sync(FakeSyncInstallationRepositories):
        async def execute(
            self,
            *,
            provider_installation_id: UUID,
            repositories: tuple[RepositoryOnboardingInput, ...],
        ) -> tuple[OnboardingResult, ...]:
            assert details.calls == [(17, "example-owner/repo-101")]
            return await super().execute(
                provider_installation_id=provider_installation_id, repositories=repositories
            )

    sync = Sync()
    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree,
            label_provider=FakeLabelProvider(),
            details_provider=details,
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(
                17, "added", (_bare_reference(external_id=101),), ()
            ),
        )
    )

    assert details.calls == [(17, "example-owner/repo-101")]
    assert tree.calls == [(17, 101, "trunk")]
    assert [item.snapshot for item in sync.calls[0][1]] == [
        RepositorySnapshot(
            external_id=101,
            full_name="example-owner/repo-101",
            default_branch="trunk",
            web_url="https://example.test/example-owner/repo-101",
        )
    ]


def test_complete_repositories_need_no_details_request() -> None:
    details = FakeDetailsProvider()
    tree = FakeTreeProvider(trees={101: ()})
    sync = FakeSyncInstallationRepositories()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree,
            label_provider=FakeLabelProvider(),
            details_provider=details,
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(
                17,
                "added",
                (
                    _bare_reference(
                        external_id=101,
                        branch="develop",
                        url="https://example.test/example-owner/repo-101",
                    ),
                ),
                (),
            ),
        )
    )

    assert details.calls == []
    assert tree.calls == [(17, 101, "develop")]
    assert sync.calls[0][1][0].snapshot == RepositorySnapshot(
        external_id=101,
        full_name="example-owner/repo-101",
        default_branch="develop",
        web_url="https://example.test/example-owner/repo-101",
    )


@pytest.mark.parametrize(
    ("branch", "url"),
    [("develop", None), (None, "https://example.test/stale-url")],
)
def test_one_missing_field_takes_both_from_the_details_provider(
    branch: str | None, url: str | None
) -> None:
    details = FakeDetailsProvider(
        details={
            "example-owner/repo-101": RepositoryDetails(
                default_branch="trunk", web_url="https://example.test/example-owner/repo-101"
            )
        }
    )
    tree = FakeTreeProvider(trees={101: ()})
    sync = FakeSyncInstallationRepositories()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree,
            label_provider=FakeLabelProvider(),
            details_provider=details,
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(
                17, "added", (_bare_reference(external_id=101, branch=branch, url=url),), ()
            ),
        )
    )

    assert details.calls == [(17, "example-owner/repo-101")]
    assert tree.calls == [(17, 101, "trunk")]
    assert sync.calls[0][1][0].snapshot.web_url == "https://example.test/example-owner/repo-101"


def test_only_the_incomplete_repositories_of_a_batch_are_fetched() -> None:
    details = FakeDetailsProvider(
        details={
            "example-owner/repo-102": RepositoryDetails(
                default_branch="trunk", web_url="https://example.test/example-owner/repo-102"
            )
        }
    )
    tree = FakeTreeProvider(trees={101: (), 102: ()})
    sync = FakeSyncInstallationRepositories()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree,
            label_provider=FakeLabelProvider(),
            details_provider=details,
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(
                17,
                "created",
                (
                    _bare_reference(
                        external_id=101,
                        branch="main",
                        url="https://example.test/example-owner/repo-101",
                    ),
                    _bare_reference(external_id=102),
                ),
                (),
            ),
        )
    )

    assert details.calls == [(17, "example-owner/repo-102")]
    assert tree.calls == [(17, 101, "main"), (17, 102, "trunk")]
    assert [item.snapshot.default_branch for item in sync.calls[0][1]] == ["main", "trunk"]


def test_details_failure_is_propagated_before_tree_label_and_database_sync() -> None:
    details = FakeDetailsProvider(error=RuntimeError("GitHub unavailable"))
    tree = FakeTreeProvider(trees={101: ()})
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=tree, label_provider=labels, details_provider=details, sync=sync
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17, "added", (_bare_reference(external_id=101),), ()
                ),
            )
        )

    assert details.calls == [(17, "example-owner/repo-101")]
    assert tree.calls == []
    assert labels.calls == []
    assert sync.calls == []


def test_unavailable_details_propagate_as_the_typed_error_before_any_write() -> None:
    """The typed outage error is not swallowed: the dispatcher defers the delivery on it."""
    unavailable = RepositoryDetailsUnavailableError("GitHub repository details request failed")
    details = FakeDetailsProvider(error=unavailable)
    tree = FakeTreeProvider(trees={101: ()})
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(RepositoryDetailsUnavailableError) as raised:
        asyncio.run(
            InstallationEventProjector(
                tree_provider=tree, label_provider=labels, details_provider=details, sync=sync
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17, "added", (_bare_reference(external_id=101),), ()
                ),
            )
        )

    assert raised.value is unavailable
    assert details.calls == [(17, "example-owner/repo-101")]
    assert tree.calls == []
    assert labels.calls == []
    assert sync.calls == []


def test_details_failure_on_a_later_repository_still_persists_the_readable_ones() -> None:
    """The first repository resolves, the second fails: only the first reaches ``sync``.

    api#73 replaced the all-or-nothing batch (api#71); the failure still propagates, so
    the receipt is retried, but it no longer discards the repositories that were read.
    """

    @dataclass
    class FailingSecondDetails(FakeDetailsProvider):
        async def fetch_repository_details(
            self, *, installation_external_id: int, full_name: str
        ) -> RepositoryDetails:
            if full_name == "example-owner/repo-102":
                self.calls.append((installation_external_id, full_name))
                raise RuntimeError("GitHub unavailable for the second repository")
            return await super().fetch_repository_details(
                installation_external_id=installation_external_id, full_name=full_name
            )

    details = FailingSecondDetails(
        details={
            "example-owner/repo-101": RepositoryDetails(
                default_branch="trunk", web_url="https://example.test/example-owner/repo-101"
            )
        }
    )
    tree = FakeTreeProvider(trees={101: (), 102: ()})
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(RuntimeError, match="second repository"):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=tree,
                label_provider=FakeLabelProvider(),
                details_provider=details,
                sync=sync,
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17,
                    "added",
                    (_bare_reference(external_id=101), _bare_reference(external_id=102)),
                    (),
                ),
            )
        )

    assert details.calls == [(17, "example-owner/repo-101"), (17, "example-owner/repo-102")]
    assert tree.calls == [(17, 101, "trunk")]
    assert len(sync.calls) == 1
    assert [item.snapshot.external_id for item in sync.calls[0][1]] == [101]


_PROJECTOR_LOGGER = "app.modules.integrations.webhooks.application.installation_event_projector"


@dataclass
class FailingTree(FakeTreeProvider):
    """Reads the tree of every repository except the ones in ``errors``."""

    errors: dict[int, BaseException] = field(default_factory=dict)

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        if repository.external_id in self.errors:
            raise self.errors[repository.external_id]
        return await super().fetch_default_branch_tree(
            installation_external_id=installation_external_id, repository=repository
        )


def _not_found(repository_path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://api.github.com/repos/{repository_path}")
    return httpx.HTTPStatusError(
        f"Client error '404 Not Found' for url '{request.url}'",
        request=request,
        response=httpx.Response(404, request=request),
    )


def test_tree_failure_on_a_later_repository_still_persists_the_readable_ones(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 404 on the second repository (replication lag after creation) must not drop the first."""
    failure = _not_found("octo/SENTINEL-repository-102")
    tree = FailingTree(trees={101: ()}, errors={102: failure})
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(httpx.HTTPStatusError) as raised,
    ):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=tree,
                label_provider=labels,
                details_provider=FakeDetailsProvider(),
                sync=sync,
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17, "added", (_reference(external_id=101), _reference(external_id=102)), ()
                ),
            )
        )

    assert raised.value is failure
    assert len(sync.calls) == 1
    assert [item.snapshot.external_id for item in sync.calls[0][1]] == [101]
    warnings = [
        record
        for record in caplog.records
        if record.name == _PROJECTOR_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    logged = warnings[0].getMessage()
    assert "installation_id=17" in logged
    assert "repository_id=102" in logged
    assert "full_name=octo/repository-102" in logged
    assert "error_type=HTTPStatusError" in logged
    assert "status_code=404" in logged
    assert "SENTINEL" not in caplog.text
    assert warnings[0].exc_info is None


def test_unavailable_details_are_logged_with_the_type_and_status_of_their_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The typed details error has no response: the warning reports the error it wraps.

    The details adapter raises it ``from`` the HTTP error (api#71), so a 404 and a 500
    stay distinguishable per repository; the typed error itself still propagates.
    """
    unavailable = RepositoryDetailsUnavailableError("GitHub repository details request failed")
    unavailable.__cause__ = _not_found("example-owner/SENTINEL-repo-101")
    sync = FakeSyncInstallationRepositories()

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(RepositoryDetailsUnavailableError) as raised,
    ):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=FakeTreeProvider(),
                label_provider=FakeLabelProvider(),
                details_provider=FakeDetailsProvider(error=unavailable),
                sync=sync,
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17, "added", (_bare_reference(external_id=101),), ()
                ),
            )
        )

    assert raised.value is unavailable
    assert sync.calls == []
    logged = [
        record.getMessage()
        for record in caplog.records
        if record.name == _PROJECTOR_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(logged) == 1
    assert "repository_id=101" in logged[0]
    assert "error_type=HTTPStatusError" in logged[0]
    assert "status_code=404" in logged[0]
    assert "SENTINEL" not in caplog.text


def test_the_first_failure_in_event_order_is_raised_and_every_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_failure = RuntimeError("first failure")
    second_failure = ValueError("second failure")
    tree = FailingTree(trees={102: ()}, errors={101: first_failure, 103: second_failure})
    sync = FakeSyncInstallationRepositories()

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(RuntimeError) as raised,
    ):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=tree,
                label_provider=FakeLabelProvider(),
                details_provider=FakeDetailsProvider(),
                sync=sync,
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17,
                    "added",
                    tuple(_reference(external_id=item) for item in (101, 102, 103)),
                    (),
                ),
            )
        )

    assert raised.value is first_failure
    assert [item.snapshot.external_id for item in sync.calls[0][1]] == [102]
    logged = [
        record.getMessage()
        for record in caplog.records
        if record.name == _PROJECTOR_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(logged) == 2
    assert "repository_id=101" in logged[0] and "error_type=RuntimeError" in logged[0]
    assert "repository_id=103" in logged[1] and "error_type=ValueError" in logged[1]
    assert "status_code=None" in logged[0] and "status_code=None" in logged[1]


@dataclass
class FailingDetails(FakeDetailsProvider):
    """Reads the details of every repository except the ones in ``errors``."""

    errors: dict[str, Exception] = field(default_factory=dict)

    async def fetch_repository_details(
        self, *, installation_external_id: int, full_name: str
    ) -> RepositoryDetails:
        if full_name in self.errors:
            self.calls.append((installation_external_id, full_name))
            raise self.errors[full_name]
        return await super().fetch_repository_details(
            installation_external_id=installation_external_id, full_name=full_name
        )


@dataclass
class _LinkedInstallation:
    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        assert external_id == 17
        return UUID("00000000-0000-4000-8000-000000000017")


def _readable(*full_names: str) -> dict[str, RepositoryDetails]:
    return {
        name: RepositoryDetails(default_branch="trunk", web_url=f"https://example.test/{name}")
        for name in full_names
    }


def _dispatch_three_bare_repositories(
    details: InstallationRepositoryDetailsProvider,
    tree: InstallationRepositoryTreeProvider,
    sync: FakeSyncInstallationRepositories,
    *,
    labels: InstallationRepositoryLabelProvider | None = None,
) -> InstallationDeliveryDispatchResult:
    """The real dispatcher over the projector, for an event of repositories 101, 102, 103."""
    projector = InstallationEventProjector(
        tree_provider=tree,
        label_provider=labels or FakeLabelProvider(),
        details_provider=details,
        sync=sync,
    )
    return asyncio.run(
        GitHubInstallationDeliveryDispatcher(
            resolver=_LinkedInstallation(), onboarding=projector
        ).execute(
            GitHubDispatchEvent(
                "delivery-mixed",
                InstallationRepositoriesEvent(
                    17,
                    "added",
                    tuple(_bare_reference(external_id=item) for item in (101, 102, 103)),
                    (),
                ),
            )
        )
    )


def test_a_tree_failure_before_unavailable_details_takes_the_failed_dispatch_path() -> None:
    """The first failure in event order decides the path: here a tree 404, not a deferral."""
    tree_failure = _not_found("example-owner/repo-101/git/trees/trunk")
    details = FailingDetails(
        details=_readable("example-owner/repo-101", "example-owner/repo-103"),
        errors={
            "example-owner/repo-102": RepositoryDetailsUnavailableError(
                "GitHub repository details request failed with HTTP 404"
            )
        },
    )
    tree = FailingTree(trees={103: ()}, errors={101: tree_failure})
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(httpx.HTTPStatusError) as raised:
        _dispatch_three_bare_repositories(details, tree, sync)

    assert raised.value is tree_failure
    assert [[item.snapshot.external_id for item in call[1]] for call in sync.calls] == [[103]]


def test_unavailable_details_before_a_tree_failure_defer_the_delivery() -> None:
    """The first failure in event order decides the path: here unreadable details, deferred."""
    details = FailingDetails(
        details=_readable("example-owner/repo-102", "example-owner/repo-103"),
        errors={
            "example-owner/repo-101": RepositoryDetailsUnavailableError(
                "GitHub repository details request failed with HTTP 404"
            )
        },
    )
    tree = FailingTree(
        trees={103: ()}, errors={102: _not_found("example-owner/repo-102/git/trees/trunk")}
    )
    sync = FakeSyncInstallationRepositories()

    result = _dispatch_three_bare_repositories(details, tree, sync)

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    assert [[item.snapshot.external_id for item in call[1]] for call in sync.calls] == [[103]]


def test_an_earlier_repository_that_fails_later_still_decides_the_dispatch_path() -> None:
    """Event order, not completion order: 101's details fail only after 103's tree did."""
    tree_failed = asyncio.Event()
    finished: list[int] = []

    class DetailsAfterTheTreeFailure(FakeDetailsProvider):
        async def fetch_repository_details(
            self, *, installation_external_id: int, full_name: str
        ) -> RepositoryDetails:
            if full_name != "example-owner/repo-101":
                return await super().fetch_repository_details(
                    installation_external_id=installation_external_id, full_name=full_name
                )
            await tree_failed.wait()
            finished.append(101)
            raise RepositoryDetailsUnavailableError(
                "GitHub repository details request failed with HTTP 404"
            )

    class TreeThatFailsFirst(FakeTreeProvider):
        async def fetch_default_branch_tree(
            self,
            *,
            installation_external_id: int,
            repository: RepositorySnapshot,
        ) -> tuple[RepositoryTreeBlob, ...]:
            if repository.external_id != 103:
                return await super().fetch_default_branch_tree(
                    installation_external_id=installation_external_id, repository=repository
                )
            tree_failed.set()
            finished.append(103)
            raise _not_found("example-owner/repo-103/git/trees/trunk")

    sync = FakeSyncInstallationRepositories()

    result = _dispatch_three_bare_repositories(
        DetailsAfterTheTreeFailure(
            details=_readable("example-owner/repo-102", "example-owner/repo-103")
        ),
        TreeThatFailsFirst(trees={102: ()}),
        sync,
    )

    assert finished == [103, 101]
    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    assert [[item.snapshot.external_id for item in call[1]] for call in sync.calls] == [[102]]


def test_a_cancelled_repository_aborts_the_event_before_the_database_sync() -> None:
    """Cancellation is not a repository failure: it is re-raised before sync runs."""
    tree = FailingTree(trees={101: ()}, errors={102: asyncio.CancelledError()})
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=tree,
                label_provider=FakeLabelProvider(),
                details_provider=FakeDetailsProvider(),
                sync=sync,
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17, "added", (_reference(external_id=101), _reference(external_id=102)), ()
                ),
            )
        )

    assert sync.calls == []


@dataclass
class HangingTree(FakeTreeProvider):
    """Never answers; counts the repositories that entered and the ones it saw cancelled."""

    entered: int = 0
    cancelled: int = 0

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        self.entered += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("the hanging tree provider must never answer")


def test_the_dispatch_timeout_cancels_every_running_repository_and_never_syncs() -> None:
    """The worker wraps ``execute`` in ``wait_for(240 s)``: a timeout must reach the children."""
    tree = HangingTree()
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()
    projector = InstallationEventProjector(
        tree_provider=tree,
        label_provider=labels,
        details_provider=FakeDetailsProvider(),
        sync=sync,
        max_concurrent_repositories=4,
    )

    async def dispatch() -> None:
        await asyncio.wait_for(
            projector.execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    17,
                    "added",
                    tuple(_reference(external_id=item) for item in range(101, 107)),
                    (),
                ),
            ),
            timeout=0.05,
        )

    with pytest.raises(TimeoutError):
        asyncio.run(dispatch())

    assert tree.entered == 4
    assert tree.cancelled == 4
    assert labels.calls == []
    assert sync.calls == []


@pytest.mark.parametrize("limit", [0, -1])
def test_projector_rejects_a_concurrency_limit_below_one(limit: int) -> None:
    with pytest.raises(ValueError, match="max_concurrent_repositories must be at least 1"):
        InstallationEventProjector(
            tree_provider=FakeTreeProvider(),
            label_provider=FakeLabelProvider(),
            details_provider=FakeDetailsProvider(),
            sync=FakeSyncInstallationRepositories(),
            max_concurrent_repositories=limit,
        )


@pytest.mark.parametrize("action", ["created", "added"])
def test_two_hundred_bare_repositories_fit_the_delivery_budget(
    action: Literal["created", "added"],
) -> None:
    """api#73 AC 1: the real adapters onboard 200 repositories inside the dispatch budget.

    The whole event must end within ``_DISPATCH_TIMEOUT_SECONDS`` of virtual time: the
    worker's timeout is a budget to stay under, not an expected value. Each details and
    tree GET takes 0.1 s of that time, and the label POSTs are spaced 0.8 s apart by the
    pacer on the same clock. The clock is one counter that every sleep advances, so GETs
    of up to four repositories that run at the same time add up instead of overlapping:
    the virtual time is a pessimistic upper bound of the real one. The event needs at least
    199 gaps of 0.8 s, 159.2 s, plus the part of the 200 x 0.2 s of GETs that does not fall
    inside the pacer's waits, so a timeout of 240 s holds it and one of 100 s does not.

    It also proves the label lane (POST starts at least 0.8 s apart) and the GET
    concurrency (peak of four). A semaphore slot stays held while its repository waits for
    the label pacer, so the throughput is min(1 / 0.8 s, 4 / latency): the pacer stays the
    limit only while the latency of one repository (details, tree and label request) is
    under about 3.2 s.
    """
    total = 200
    get_latency_seconds = 0.1
    virtual_now = 0.0
    in_flight = 0
    peak_in_flight = 0
    label_posts: list[str] = []
    label_post_starts: list[float] = []

    class TokenProvider:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "test-installation-token"

    def clock() -> float:
        return virtual_now

    async def virtual_sleep(seconds: float) -> None:
        nonlocal virtual_now
        await asyncio.sleep(0)
        virtual_now += seconds

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak_in_flight
        if request.method == "POST":
            label_posts.append(request.url.path)
            label_post_starts.append(virtual_now)
            return httpx.Response(201, json={})
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        try:
            await virtual_sleep(get_latency_seconds)
        finally:
            in_flight -= 1
        if request.url.path.endswith("/git/trees/trunk"):
            return httpx.Response(
                200, json={"tree": [{"path": "src/app.py", "type": "blob", "size": 10}]}
            )
        return httpx.Response(
            200,
            json={"default_branch": "trunk", "html_url": f"https://example.test{request.url.path}"},
        )

    sync = FakeSyncInstallationRepositories()

    async def onboard() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            tokens = TokenProvider()
            projector = InstallationEventProjector(
                tree_provider=GitHubInstallationTreeProvider(client=client, token_provider=tokens),
                label_provider=GitHubRepositoryLabelProvider(
                    client=client, token_provider=tokens, clock=clock, sleep=virtual_sleep
                ),
                details_provider=GitHubInstallationRepositoryDetailsProvider(
                    client=client, token_provider=tokens
                ),
                sync=sync,
            )
            await asyncio.wait_for(
                projector.execute(
                    provider_installation_id=uuid4(),
                    event=InstallationRepositoriesEvent(
                        17,
                        action,
                        tuple(_bare_reference(external_id=item) for item in range(1, total + 1)),
                        (),
                    ),
                ),
                timeout=10,
            )

    asyncio.run(onboard())

    assert len(sync.calls) == 1
    assert [item.snapshot.external_id for item in sync.calls[0][1]] == list(range(1, total + 1))
    assert len(label_posts) == total
    assert peak_in_flight == 4
    gaps = [later - earlier for earlier, later in pairwise(label_post_starts)]
    assert min(gaps) == pytest.approx(0.8)
    # The first POST follows its repository's details and tree GETs, so their time shows.
    assert label_post_starts[0] >= 2 * get_latency_seconds
    assert virtual_now <= _DISPATCH_TIMEOUT_SECONDS


@pytest.mark.parametrize("action", ["deleted", "removed"])
def test_removal_of_bare_repositories_makes_no_github_request(action: str) -> None:
    details = FakeDetailsProvider()
    tree = FakeTreeProvider()
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()
    provider_installation_id = uuid4()
    removed = (_bare_reference(external_id=101), _bare_reference(external_id=102))

    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree, label_provider=labels, details_provider=details, sync=sync
        ).execute(
            provider_installation_id=provider_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action=action,  # type: ignore[arg-type]
                added_repositories=(),
                removed_repositories=removed,
            ),
        )
    )

    assert details.calls == []
    assert tree.calls == []
    assert labels.calls == []
    assert sync.calls == []
    assert sync.disable_calls == [(provider_installation_id, removed)]


def _token_failure(status: int = 401) -> InstallationAccessTokenError:
    request = httpx.Request("POST", "https://api.github.com/app/installations/17/access_tokens")
    original = httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request,
        response=httpx.Response(status, request=request),
    )
    error = InstallationAccessTokenError(
        "GitHub installation access token unavailable", transient=True
    )
    error.__cause__ = original
    return error


@dataclass
class StartRecordingTree(FakeTreeProvider):
    """Records every repository it is asked about; ``errors`` fail, the rest wait for them.

    A repository without an error answers only once every failing one has failed, so it is
    still in flight when the failure happens.
    """

    errors: dict[int, Exception] = field(default_factory=dict)
    started: list[int] = field(default_factory=list)
    failed: asyncio.Event = field(default_factory=asyncio.Event)

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        self.started.append(repository.external_id)
        if repository.external_id in self.errors:
            self.failed.set()
            raise self.errors[repository.external_id]
        if self.errors:
            await self.failed.wait()
        return ()


def _execute(
    tree: InstallationRepositoryTreeProvider,
    sync: FakeSyncInstallationRepositories,
    references: tuple[RepositoryReference, ...],
    *,
    max_concurrent_repositories: int,
    labels: InstallationRepositoryLabelProvider | None = None,
    details: InstallationRepositoryDetailsProvider | None = None,
) -> None:
    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree,
            label_provider=labels or FakeLabelProvider(),
            details_provider=details or FakeDetailsProvider(),
            sync=sync,
            max_concurrent_repositories=max_concurrent_repositories,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(17, "added", references, ()),
        )
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _PROJECTOR_LOGGER and record.levelno == logging.WARNING
    ]


def test_a_token_failure_skips_the_repositories_that_have_not_started(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = _token_failure()
    tree = StartRecordingTree(errors={101: failure})
    sync = FakeSyncInstallationRepositories()

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(InstallationAccessTokenError) as raised,
    ):
        _execute(
            tree,
            sync,
            tuple(_reference(external_id=item) for item in (101, 102, 103, 104)),
            max_concurrent_repositories=1,
        )

    assert raised.value is failure
    assert tree.started == [101]
    assert sync.calls == []
    logged = _warnings(caplog)
    assert len(logged) == 2
    assert "repository_id=101" in logged[0]
    assert "repository_id=" not in logged[1]
    assert "full_name=" not in logged[1]
    assert "installation_id=17" in logged[1] and "skipped=3" in logged[1]


def test_a_token_failure_lets_the_running_repositories_finish_and_syncs_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = StartRecordingTree(errors={102: _token_failure()})
    sync = FakeSyncInstallationRepositories()

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(InstallationAccessTokenError),
    ):
        _execute(
            tree,
            sync,
            tuple(_reference(external_id=item) for item in (101, 102, 103, 104)),
            max_concurrent_repositories=2,
        )

    assert tree.started == [101, 102]
    assert len(sync.calls) == 1
    assert [item.snapshot.external_id for item in sync.calls[0][1]] == [101]
    logged = _warnings(caplog)
    assert len(logged) == 2 and "skipped=2" in logged[1]


def test_a_token_failure_of_the_details_request_also_skips_the_rest() -> None:
    @dataclass
    class TokenFailingDetails(FakeDetailsProvider):
        async def fetch_repository_details(
            self, *, installation_external_id: int, full_name: str
        ) -> RepositoryDetails:
            self.calls.append((installation_external_id, full_name))
            raise _token_failure()

    details = TokenFailingDetails()
    tree = StartRecordingTree()
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(InstallationAccessTokenError):
        _execute(
            tree,
            sync,
            tuple(_bare_reference(external_id=item) for item in (101, 102, 103)),
            max_concurrent_repositories=1,
            details=details,
        )

    assert details.calls == [(17, "example-owner/repo-101")]
    assert tree.started == []
    assert sync.calls == []


def test_a_token_failure_does_not_hide_an_earlier_failure_in_event_order() -> None:
    earlier = RuntimeError("earlier failure")
    tree = StartRecordingTree(errors={101: earlier, 102: _token_failure()})
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(RuntimeError) as raised:
        _execute(
            tree,
            sync,
            tuple(_reference(external_id=item) for item in (101, 102, 103)),
            max_concurrent_repositories=2,
        )

    assert raised.value is earlier
    assert tree.started == [101, 102]


def test_a_failure_that_is_not_a_token_failure_does_not_skip_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: a repository-specific error (a 404, say) never stops the event."""
    failure = _not_found("octo/repository-101")
    tree = StartRecordingTree(errors={101: failure})
    sync = FakeSyncInstallationRepositories()

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(httpx.HTTPStatusError) as raised,
    ):
        _execute(
            tree,
            sync,
            tuple(_reference(external_id=item) for item in (101, 102, 103)),
            max_concurrent_repositories=1,
        )

    assert raised.value is failure
    assert tree.started == [101, 102, 103]
    assert [item.snapshot.external_id for item in sync.calls[0][1]] == [102, 103]
    assert len(_warnings(caplog)) == 1


@dataclass
class FailingLabels(FakeLabelProvider):
    """Creates the label of every repository except the ones in ``errors``."""

    errors: dict[str, Exception] = field(default_factory=dict)

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None:
        await super().create_ai_review_label(
            installation_external_id=installation_external_id, repository=repository
        )
        if repository.full_name in self.errors:
            raise self.errors[repository.full_name]


def test_a_token_failure_of_the_label_request_stops_the_event() -> None:
    """A label request without a token fails its repository like any token failure."""
    failure = _token_failure()
    labels = FailingLabels(errors={"octo/repository-102": failure})
    tree = StartRecordingTree()
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(InstallationAccessTokenError) as raised:
        _execute(
            tree,
            sync,
            tuple(_reference(external_id=item) for item in (101, 102, 103)),
            max_concurrent_repositories=1,
            labels=labels,
        )

    assert raised.value is failure
    assert tree.started == [101, 102]
    assert labels.calls == [(17, "octo/repository-101"), (17, "octo/repository-102")]
    assert [[item.snapshot.external_id for item in call[1]] for call in sync.calls] == [[101]]


def _three_readable_repositories() -> tuple[FakeDetailsProvider, FakeTreeProvider]:
    details = FakeDetailsProvider(
        details=_readable(
            "example-owner/repo-101", "example-owner/repo-102", "example-owner/repo-103"
        )
    )
    return details, FakeTreeProvider(trees={101: (), 102: (), 103: ()})


def test_a_token_failure_at_the_last_label_request_defers_the_delivery() -> None:
    """Deferred, not onboarded: the token fails only at the label of the last repository."""
    details, tree = _three_readable_repositories()
    labels = FailingLabels(errors={"example-owner/repo-103": _token_failure()})
    sync = FakeSyncInstallationRepositories()

    result = _dispatch_three_bare_repositories(details, tree, sync, labels=labels)

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    assert [[item.snapshot.external_id for item in call[1]] for call in sync.calls] == [[101, 102]]


def test_an_ordinary_label_error_still_onboards_every_repository(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: a label POST answered 403 is only logged, as before."""
    request = httpx.Request("POST", "https://api.github.com/repos/example-owner/repo-103/labels")
    forbidden = httpx.HTTPStatusError(
        f"Client error '403 Forbidden' for url '{request.url}'",
        request=request,
        response=httpx.Response(403, request=request),
    )
    details, tree = _three_readable_repositories()
    labels = FailingLabels(errors={"example-owner/repo-103": forbidden})
    sync = FakeSyncInstallationRepositories()

    with caplog.at_level(logging.ERROR, logger=_PROJECTOR_LOGGER):
        result = _dispatch_three_bare_repositories(details, tree, sync, labels=labels)

    assert result.status is InstallationDeliveryDispatchStatus.ONBOARDED
    assert [[item.snapshot.external_id for item in call[1]] for call in sync.calls] == [
        [101, 102, 103]
    ]
    assert "Failed to create ai-review label for example-owner/repo-103" in caplog.text


def test_a_token_failure_is_logged_with_the_type_and_status_of_its_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = StartRecordingTree(errors={101: _token_failure(status=404)})

    with (
        caplog.at_level(logging.WARNING, logger=_PROJECTOR_LOGGER),
        pytest.raises(InstallationAccessTokenError),
    ):
        _execute(
            tree,
            FakeSyncInstallationRepositories(),
            (_reference(external_id=101),),
            max_concurrent_repositories=1,
        )

    logged = _warnings(caplog)
    assert len(logged) == 1
    assert "error_type=HTTPStatusError" in logged[0]
    assert "status_code=404" in logged[0]
    assert "access_tokens" not in caplog.text
