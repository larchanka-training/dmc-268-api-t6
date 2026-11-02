"""Records the runner keeps."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    """One queued unit of work."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
