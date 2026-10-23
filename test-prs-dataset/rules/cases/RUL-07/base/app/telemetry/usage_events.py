"""Product telemetry events."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    name: str
    user_id: int


def encode(event: Event) -> str:
    return json.dumps(asdict(event), sort_keys=True)
