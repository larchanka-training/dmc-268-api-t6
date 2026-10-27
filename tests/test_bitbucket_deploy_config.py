"""Bitbucket runtime configuration reaches every local Compose recipient."""

from __future__ import annotations

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
