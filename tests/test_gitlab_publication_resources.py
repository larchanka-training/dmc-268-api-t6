"""Public factory configuration/lazy lifetime without admitted SQL or network."""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestCredential,
    GitLabTargetIdentity,
)
from app.modules.reviews.application.publish_run_review import PublishContext
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.gitlab_publication_resources import (
    GitLabInstalledPublicationResources,
)
from tests.test_gitlab_run_http import INPUT, POLICY, WORKSPACE
from tests.test_publish_run_review import context


class NoAdmission:
    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        raise AssertionError("context entry must not admit SQL or credentials")

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        raise AssertionError("unexpected authority fence")

    async def credential_for(self, target: BoundGitLabTarget) -> GitLabRequestCredential:
        raise AssertionError("unexpected credential")


async def no_checkpoint() -> None:
    raise AssertionError("opening resources must be lazy")


def trusted() -> PublishContext:
    return context(
        provider="gitlab",
        workspace_id=WORKSPACE,
        vcs_input=INPUT,
        installation_id=71,
        repository_full_name="team/project",
        pr_number=12,
        project_id=17,
    )


def test_open_without_submit_allocates_no_transport_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "process-PAT-must-not-be-read")
    monkeypatch.setenv("GITLAB_BASE_URL", "https://untrusted.example")

    def transport() -> httpx.AsyncBaseTransport:
        raise AssertionError("transport construction must be lazy")

    resources = GitLabInstalledPublicationResources(
        session_factory=async_sessionmaker[AsyncSession](),
        authority=NoAdmission(),
        origin_policy=POLICY,
        transport_factory=transport,
    )

    async def scenario() -> None:
        async with resources.open(trusted(), checkpoint=no_checkpoint):
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", None),
        ("vcs_input", None),
        ("project_id", 29),
        ("pr_number", 13),
        ("installation_id", 99),
        ("repository_full_name", "other/project"),
        ("provider", "github"),
        ("origin_active", False),
    ],
)
def test_inconsistent_context_is_refused_before_admission(field: str, value: Any) -> None:
    resources = GitLabInstalledPublicationResources(
        session_factory=async_sessionmaker[AsyncSession](),
        authority=NoAdmission(),
        origin_policy=POLICY,
    )

    async def scenario() -> None:
        with pytest.raises(RunCancelled, match="installation_unavailable"):
            async with resources.open(
                replace(trusted(), **{field: value}), checkpoint=no_checkpoint
            ):
                raise AssertionError("untrusted context yielded a gateway")

    asyncio.run(scenario())


def test_unverified_tls_context_is_rejected() -> None:
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified TLS"):
        GitLabInstalledPublicationResources(
            session_factory=async_sessionmaker[AsyncSession](),
            authority=NoAdmission(),
            origin_policy=POLICY,
            ssl_context=insecure,
        )
