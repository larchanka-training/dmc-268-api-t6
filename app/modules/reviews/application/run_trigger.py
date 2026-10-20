"""What created a Run; the canon is ``RunTrigger`` in contracts/openapi.yaml.

A change to the set also needs a new Alembic revision replacing ``ck_runs_trigger`` and
the same condition in ``Run.__table_args__`` (reviews/infrastructure/models.py).
"""

from __future__ import annotations

from typing import Literal

type RunTrigger = Literal["webhook", "manual", "rerun", "dry_run"]
