"""SQL conditions of the repositories the graph index serves (#143, ADR 0001 §6.1).

Over `repositories` joined with `provider_installations`; the leader's enumeration and
the requests that write the index share them, so they agree on which repository has
an index.
"""

from __future__ import annotations

from app.modules.repositories.infrastructure.models import ProviderInstallation

# The indexer has a snapshot source for GitHub only (GitLab is #97).
GITHUB_PROVIDER = ProviderInstallation.provider == "github"
