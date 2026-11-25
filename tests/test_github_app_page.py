"""The prod GitHub App manifest is the public contract and holds no secrets."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "github-app-manifest.json"

# SYSTEM_DESIGN §8.2 repository permissions, and the events that section subscribes
# for the current MVP. installation* events are delivered without a toggle;
# pull_request_review_thread stays unsubscribed until after MVP.
PROD_PERMISSIONS = {
    "actions": "read",
    "checks": "write",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "read",
}
PROD_EVENTS = [
    "check_suite",
    "pull_request",
    "push",
    "repository",
    "status",
    "workflow_run",
]
SECRET_MARKERS = (
    "client_secret",
    "private_key",
    "webhook_secret",
    "BEGIN PRIVATE",
    "BEGIN RSA",
    "GH_APP_PRIVATE_KEY",
)


def test_prod_manifest_matches_sd_permissions_and_carries_no_secret() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["name"] == "AI CodeReviewer Bot"
    assert manifest["description"] == "AI code reviewer bot"
    assert manifest["url"] == "https://www.dmc268-t6.axyi.ru/"
    assert manifest["public"] is True
    assert manifest["hook_attributes"] == {
        "url": "https://api.dmc268-t6.axyi.ru/webhooks/github",
        "active": True,
    }
    assert manifest["default_permissions"] == PROD_PERMISSIONS
    assert manifest["default_events"] == PROD_EVENTS
    text = MANIFEST.read_text(encoding="utf-8")
    for marker in SECRET_MARKERS:
        assert marker not in text
