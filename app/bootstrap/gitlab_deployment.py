"""Pure GitLab configuration preflight for deployment tooling."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from app.modules.integrations.gitlab.infrastructure.settings import (
    GitLabApiSettings,
    GitLabRuntimeSettings,
    GitLabSettingsError,
)


def validate_environment(env: Mapping[str, str]) -> None:
    """Validate admission and worker settings without using runtime resources.

    The DSN only satisfies the shared settings parser; deployment preflight neither
    validates the deployment's database connection nor connects to a database.
    """
    runtime = GitLabRuntimeSettings.optional_from_environment(
        {**env, "DATABASE_URL": "postgresql+psycopg://localhost/deployment_preflight"}
    )
    admission = GitLabApiSettings.from_environment(env)
    if admission.intake_enabled and runtime is None:
        raise GitLabSettingsError("partial")


def main() -> int:
    try:
        validate_environment(os.environ)
    except GitLabSettingsError:
        print("configuration_invalid", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
