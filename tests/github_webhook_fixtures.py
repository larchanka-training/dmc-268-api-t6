"""Anonymized copies of real GitHub webhook deliveries (api#71).

The files under ``tests/fixtures/github_webhooks`` keep the exact key set and value
types of deliveries captured from GitHub; only identities and URLs are replaced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github_webhooks"


def load_github_webhook_fixture(name: str) -> dict[str, Any]:
    """Read one delivery body, for example ``installation_created``."""
    decoded: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text())
    return decoded


def removal_of(event_name: str, delivery: dict[str, Any]) -> dict[str, Any]:
    """Build the ``deleted`` / ``removed`` delivery that undoes ``delivery``.

    GitHub sends the same five-field repository items for both directions, so the
    removal reuses the items of the real added/created fixture.
    """
    if event_name == "installation":
        return {**delivery, "action": "deleted"}
    return {
        **delivery,
        "action": "removed",
        "repositories_added": [],
        "repositories_removed": delivery["repositories_added"],
    }
