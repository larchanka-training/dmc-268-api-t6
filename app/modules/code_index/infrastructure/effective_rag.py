"""SQL conditions of the repositories the graph index serves (#143, ADR 0001 §6.1).

Over `repositories` joined with `provider_installations`; the leader's enumeration and
the requests that write the index share them, so they agree on which repository has
an index.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, or_, select

from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository

# The effective RAG of a repository (ADR §6.1): the toggle is on and the App can read it.
RAG_ON = and_(
    Repository.rag_enabled,
    Repository.installation_available,
    ProviderInstallation.deleted_at.is_(None),
)
# The indexer has a snapshot source for GitHub only (GitLab is #97).
GITHUB_PROVIDER = ProviderInstallation.provider == "github"
BITBUCKET_PROVIDER = ProviderInstallation.provider == "bitbucket"
# The subquery is correlated to one portal repository and installation. It deliberately
# does not require a confirmed webhook: a connected private repository may be indexed
# before its first signed delivery, while disconnect/token revocation closes access.
BITBUCKET_ACTIVE = exists(
    select(BitbucketRepositoryIdentity.repository_id)
    .join(
        BitbucketConnection,
        BitbucketConnection.provider_installation_id
        == BitbucketRepositoryIdentity.provider_installation_id,
    )
    .join(
        BitbucketRepositoryWebhookSecret,
        BitbucketRepositoryWebhookSecret.repository_id == BitbucketRepositoryIdentity.repository_id,
    )
    .where(
        BitbucketRepositoryIdentity.repository_id == Repository.id,
        BitbucketRepositoryIdentity.provider_installation_id == ProviderInstallation.id,
        BitbucketRepositoryIdentity.workspace_id == ProviderInstallation.workspace_id,
        BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
        BitbucketConnection.revoked_at.is_(None),
        BitbucketConnection.token_ciphertext.is_not(None),
        BitbucketRepositoryWebhookSecret.revoked_at.is_(None),
    )
)
INDEXABLE_PROVIDER = or_(GITHUB_PROVIDER, and_(BITBUCKET_PROVIDER, BITBUCKET_ACTIVE))
