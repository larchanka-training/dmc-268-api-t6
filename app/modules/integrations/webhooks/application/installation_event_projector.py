"""Project durable installation events into repository onboarding work."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositoryReference,
    RepositorySnapshot,
    RepositoryTreeBlob,
    classify_tree_languages,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)

_LOGGER = logging.getLogger(__name__)

# Per-repository work is up to three sequential GitHub calls; four repositories at a
# time keep a few hundred of them inside the 240 s delivery budget (while the latency of
# one repository stays small, see the class docstring) and stay far from GitHub's
# concurrent-request limit (100 per token).
_MAX_CONCURRENT_REPOSITORIES = 4


class _Skipped:
    """A repository that never started: the installation token failed before its turn."""


_SKIPPED = _Skipped()


@dataclass(frozen=True)
class RepositoryDetails:
    """The repository fields installation events usually omit."""

    default_branch: str
    web_url: str


class RepositoryDetailsUnavailableError(Exception):
    """GitHub cannot answer a repository details read right now.

    A provider raises it when the HTTP exchange itself failed: a transport-level error,
    a timeout, a redirect or decoding failure, or a non-2xx status from the token mint
    or from ``GET /repos/{full_name}``. The delivery is deferred and replayed. A response
    that arrives but is unusable (200 without branch or URL, a body that is not JSON, a
    malformed token response) is a permanent fault and propagates unchanged.
    """


class InstallationRepositoryDetailsProvider(Protocol):
    """Read a repository's default branch and web URL using the installation's token."""

    async def fetch_repository_details(
        self, *, installation_external_id: int, full_name: str
    ) -> RepositoryDetails: ...


class InstallationRepositoryTreeProvider(Protocol):
    """Fetch one repository's recursive tree at its declared default branch."""

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]: ...


class InstallationRepositoryLabelProvider(Protocol):
    """Create the review trigger label using the installation's GitHub token."""

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None: ...


class InstallationRepositoriesSync(Protocol):
    """Transactional repository and initial-rule synchronization boundary."""

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]: ...

    async def disable(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryReference, ...],
    ) -> None: ...


class InstallationEventProjector:
    """Prepare GitHub repository data before entering the repository transaction.

    A durable-delivery runner calls this projector after parsing a stored
    installation event.  Real events name a repository without its default branch
    and web URL; those are read through ``details_provider`` before the tree is
    fetched, and not at all when the event already carries both.  Up to
    ``max_concurrent_repositories`` repositories are processed at a time.  A slot
    stays held while its repository waits for the label adapter's pacer (one POST
    every 0.8 s), so the throughput is ``min(1 / 0.8 s, slots / (G + P))``, where
    ``G + P`` is the details, tree and label-request latency of one repository.  The
    pacing, not the sum of all calls, bounds a large event only while ``G + P``
    stays below about 3.2 s per repository (200 repositories take about 160 s).  The
    label lane alone allows 240 s / 0.8 s = 300 repositories; the ceiling of about 280
    leaves about 16 s for the details and tree requests of the first repositories and
    for the commit.  Slow trees shrink that ceiling (the 10 s httpx timeout applies to
    each phase of a call, not to its total).  A repository whose details or tree
    cannot be read does not discard the others: the readable ones go to ``sync``
    (which therefore never opens its unit of work before every GitHub call is done),
    then the first failure in event order propagates unchanged: the dispatcher defers
    the delivery when it is ``RepositoryDetailsUnavailableError`` or a transient
    ``InstallationAccessTokenError``, any other error leaves it eligible for retry on
    the failed path.  Repositories are processed independently, except after a failure
    to obtain the installation token: it concerns the whole installation, so the
    repositories that have not started are skipped (one WARNING counts them), while the
    running ones finish and the readable ones are still saved.  Replaying the whole
    event is safe to repeat (replay upserts and re-enables saved repositories).
    Removed/deleted events enter the explicit transactional soft-disable path without
    making a VCS request.
    """

    def __init__(
        self,
        *,
        tree_provider: InstallationRepositoryTreeProvider,
        label_provider: InstallationRepositoryLabelProvider,
        details_provider: InstallationRepositoryDetailsProvider,
        sync: InstallationRepositoriesSync,
        max_concurrent_repositories: int = _MAX_CONCURRENT_REPOSITORIES,
    ) -> None:
        if max_concurrent_repositories < 1:
            raise ValueError("max_concurrent_repositories must be at least 1")
        self._tree_provider = tree_provider
        self._label_provider = label_provider
        self._details_provider = details_provider
        self._sync = sync
        self._max_concurrent_repositories = max_concurrent_repositories

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        event: InstallationRepositoriesEvent,
    ) -> tuple[OnboardingResult, ...]:
        if event.removed_repositories:
            await self._sync.disable(
                provider_installation_id=provider_installation_id,
                repositories=event.removed_repositories,
            )
            return ()

        slots = asyncio.Semaphore(self._max_concurrent_repositories)
        token_failed = False

        async def onboard(reference: RepositoryReference) -> RepositoryOnboardingInput | _Skipped:
            nonlocal token_failed
            async with slots:
                # Set before the slot is released, so the next repository in line sees it.
                if token_failed:
                    return _SKIPPED
                try:
                    return await self._onboard_repository(event.installation_external_id, reference)
                except InstallationAccessTokenError:
                    token_failed = True
                    raise

        # No fail-fast on a repository's own error: every repository runs to its end (a token
        # failure only skips the ones that have not started); results keep the event order.
        outcomes = await asyncio.gather(
            *(onboard(reference) for reference in event.added_repositories),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome

        inputs = tuple(item for item in outcomes if isinstance(item, RepositoryOnboardingInput))
        failures = [
            (reference, outcome)
            for reference, outcome in zip(event.added_repositories, outcomes, strict=True)
            if isinstance(outcome, Exception)
        ]
        for reference, failure in failures:
            # The type and the HTTP status only: an HTTP error's text carries the request
            # URL. The status is read by duck typing to keep httpx out of this layer.
            # A typed details or token error stands for the failure that caused it.
            cause = (
                failure.__cause__
                if isinstance(
                    failure, (RepositoryDetailsUnavailableError, InstallationAccessTokenError)
                )
                else None
            )
            reported = failure if cause is None else cause
            _LOGGER.warning(
                "Failed to onboard repository: installation_id=%s repository_id=%s "
                "full_name=%s error_type=%s status_code=%s",
                event.installation_external_id,
                reference.external_id,
                reference.full_name,
                type(reported).__name__,
                getattr(getattr(reported, "response", None), "status_code", None),
            )
        skipped = sum(1 for outcome in outcomes if outcome is _SKIPPED)
        if skipped:
            _LOGGER.warning(
                "Skipped repositories after an installation token failure: "
                "installation_id=%s skipped=%s",
                event.installation_external_id,
                skipped,
            )
        results: tuple[OnboardingResult, ...] = ()
        if inputs:
            results = await self._sync.execute(
                provider_installation_id=provider_installation_id, repositories=inputs
            )
        if failures:
            raise failures[0][1]
        return results

    async def _onboard_repository(
        self, installation_external_id: int, reference: RepositoryReference
    ) -> RepositoryOnboardingInput:
        repository = await self._resolve(installation_external_id, reference)
        tree = await self._tree_provider.fetch_default_branch_tree(
            installation_external_id=installation_external_id,
            repository=repository,
        )
        try:
            await self._label_provider.create_ai_review_label(
                installation_external_id=installation_external_id,
                repository=repository,
            )
        except Exception:
            _LOGGER.exception("Failed to create ai-review label for %s", repository.full_name)
        return RepositoryOnboardingInput(
            snapshot=repository,
            languages=classify_tree_languages(tree),
        )

    async def _resolve(
        self, installation_external_id: int, reference: RepositoryReference
    ) -> RepositorySnapshot:
        """Complete a reference; a missing branch or URL is read from GitHub for both."""
        if reference.default_branch is not None and reference.web_url is not None:
            return RepositorySnapshot(
                external_id=reference.external_id,
                full_name=reference.full_name,
                default_branch=reference.default_branch,
                web_url=reference.web_url,
            )
        details = await self._details_provider.fetch_repository_details(
            installation_external_id=installation_external_id, full_name=reference.full_name
        )
        return RepositorySnapshot(
            external_id=reference.external_id,
            full_name=reference.full_name,
            default_branch=details.default_branch,
            web_url=details.web_url,
        )
