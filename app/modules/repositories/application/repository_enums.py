"""Review settings of a repository as the spec names them in contracts/openapi.yaml.

Each name is the schema name ``GET /openapi.json`` serves. The stored copies are the PG ENUMs of
``common/infrastructure/db/enums.py`` (``WaitForCi``, ``SeverityThreshold``; ``ReviewEvent`` is
stored lowercase) and ``SeverityThresholdValue`` in ``reviews/application/verdict.py``.
"""

from __future__ import annotations

from typing import Literal

# deep (SandboxEngine) is phase 3 (SD §13): it has no worker in the MVP.
type RepositoryEngine = Literal["fast"]
type WaitForCi = Literal["auto", "always", "never"]
type ReviewEvent = Literal["COMMENT", "REQUEST_CHANGES"]
type SeverityThreshold = Literal["all", "warning", "critical"]
