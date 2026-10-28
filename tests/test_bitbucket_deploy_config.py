"""Bitbucket runtime configuration reaches every local Compose recipient."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]


def test_local_compose_passes_bitbucket_keyring_to_api_and_both_workers() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for service in ("backend", "worker", "webhook-worker"):
        environment = services[service]["environment"]
        assert environment["BITBUCKET_CREDENTIAL_KEYS"] == "${BITBUCKET_CREDENTIAL_KEYS:-}"
        assert environment["BITBUCKET_ACTIVE_KEY_ID"] == "${BITBUCKET_ACTIVE_KEY_ID:-}"
    assert services["backend"]["environment"]["BITBUCKET_WEBHOOK_BASE_URL"] == (
        "${BITBUCKET_WEBHOOK_BASE_URL:-}"
    )
    for service in ("worker", "webhook-worker"):
        assert "BITBUCKET_WEBHOOK_BASE_URL" not in services[service]["environment"]


def test_rollback_verifies_the_bitbucket_keyring_env_file() -> None:
    tree = ast.parse((ROOT / "scripts/verify_rollback.py").read_text())
    host_files = next(
        ast.literal_eval(statement.value)
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "HOST_FILES"
            for target in statement.targets
        )
    )
    assert "bitbucket.env" in host_files


def test_browser_and_incoming_webhook_contracts_are_documented_separately() -> None:
    spec = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
    paths = spec["paths"]
    assert not any(path.startswith("/webhooks/bitbucket") for path in paths)
    rotate = paths[
        "/api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/credential"
    ]
    assert "put" in rotate and "post" not in rotate
    webhook_docs = (ROOT / "docs/BITBUCKET_WEBHOOK.md").read_text()
    assert "/webhooks/bitbucket/{installation_id}/{repository_id}" in webhook_docs
    assert "X-Hub-Signature" in webhook_docs
    assert "256 KiB" in webhook_docs


def test_webhook_setup_subscribes_to_close_events() -> None:
    spec = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
    events = spec["components"]["schemas"]["BitbucketWebhookSetup"]["properties"]["events"]
    assert events["items"]["enum"] == [
        "repo:push",
        "pullrequest:created",
        "pullrequest:updated",
        "pullrequest:fulfilled",
        "pullrequest:rejected",
    ]
    webhook_docs = (ROOT / "docs/BITBUCKET_WEBHOOK.md").read_text()
    assert "`pullrequest:fulfilled`" in webhook_docs
    assert "`pullrequest:rejected`" in webhook_docs
