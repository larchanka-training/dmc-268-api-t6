"""What created a Run; the canon is ``RunTrigger`` in contracts/openapi.yaml."""

from __future__ import annotations

from typing import Literal

type RunTrigger = Literal["webhook", "manual", "rerun", "dry_run"]
