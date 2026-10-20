"""SQL conditions of the repositories the graph index serves (#143, ADR 0001 §6.1).

Over `repositories` joined with `provider_installations`; the leader's enumeration and
the requests that write the index share them, so they agree on which repository has
an index.
"""

from __future__ import annotations

from sqlalchemy import and_

from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository

# The effective RAG of a repository (ADR §6.1): the toggle is on and the App can read it.
RAG_ON = and_(
    Repository.rag_enabled,
    Repository.installation_available,
    ProviderInstallation.deleted_at.is_(None),
)
# The indexer has a snapshot source for GitHub only (GitLab is #97).
GITHUB_PROVIDER = ProviderInstallation.provider == "github"
