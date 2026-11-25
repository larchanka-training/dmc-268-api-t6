"""The prod GitHub App manifest is the public contract and holds no secrets."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "github-app-manifest.json"
PAGE = ROOT / "docs" / "GITHUB_APP.md"

# One call or event per repository permission, independent of the prose around it.
PERMISSION_REASONS = {
    "pull_requests: write": "POST /pulls/{n}/reviews",
    "checks: write": "upsert_check_run",
    "contents: read": "GET /git/blobs/{sha}",
    "metadata: read": "GET /repos/{owner}/{repo}",
    "issues: write": "POST /repos/{owner}/{repo}/labels",
    "actions: read": "workflow_run",
    "statuses: read": "combined status",
}

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
    assert manifest["request_oauth_on_install"] is False
    assert manifest["hook_attributes"] == {
        "url": "https://api.dmc268-t6.axyi.ru/webhooks/github",
        "active": True,
    }
    assert manifest["default_permissions"] == PROD_PERMISSIONS
    assert manifest["default_events"] == PROD_EVENTS
    text = MANIFEST.read_text(encoding="utf-8")
    for marker in SECRET_MARKERS:
        assert marker not in text


def test_github_app_page_records_owners_transfer_and_each_permission() -> None:
    text = PAGE.read_text(encoding="utf-8")

    assert "Владелец staging App — личный аккаунт `axyi`" in text
    assert "slug `dmc268-t6-reviewer`" in text
    assert "Владелец prod App — организация `dmc-268`" in text
    assert "slug `ai-codereviewer-bot`" in text
    assert "В организацию `larchanka-training` staging App не передан" in text
    assert "Роль `axyi` в `larchanka-training` — `member`" in text
    assert "роль в этом изменении не перечитывалась" in text
    assert "https://github.com/apps/ai-codereviewer-bot" in text
    assert "`installation` и `installation_repositories` приходят без отдельной подписки" in text
    assert "`pull_request_review_thread` в MVP не подписан" in text
    for event in PROD_EVENTS:
        assert f"`{event}`" in text
    for permission, reason in PERMISSION_REASONS.items():
        assert f"`{permission}`" in text
        assert reason in text
    for marker in SECRET_MARKERS:
        assert marker not in text


def test_readme_explains_install_and_taking_the_page_down() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Установка" in text
    assert "### Снятие страницы" in text
    assert "https://github.com/apps/ai-codereviewer-bot/installations/new" in text
    assert "`installation.created`" in text
    assert "лейбл `ai-review`" in text
    assert "Only on this account" in text
    assert "docs/GITHUB_APP.md" in text
