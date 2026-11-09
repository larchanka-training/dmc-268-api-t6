"""The diff side a finding is anchored to; the canon is ``Side`` in contracts/openapi.yaml.

The name is the schema name ``GET /openapi.json`` serves, so the type is not ``DiffSide`` or
``FindingSide`` (the PG ``finding_side`` ENUM). A change to the set changes those two as well.
"""

from __future__ import annotations

from typing import Literal

type Side = Literal["LEFT", "RIGHT"]
