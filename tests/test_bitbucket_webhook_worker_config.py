"""The shared webhook worker schedules Bitbucket only with a complete key ring."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import pytest

from app.modules.integrations.webhooks.application.receive_github_delivery import ReplayBatchResult
from app.webhook_worker import WebhookReplayProvider, WorkerConfig


@dataclass
class Receiver:
    async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
        return ReplayBatchResult(0, 0, 0, limit)

    async def purge_finished(self) -> int:
        return 0


def test_bitbucket_worker_config_is_optional_and_rejects_partial_keys() -> None:
    env = {"DATABASE_URL": "postgresql+psycopg://example/db", "RABBITMQ_URL": "amqp://example"}
    assert WorkerConfig.from_environment(env).bitbucket is None
    keys = json.dumps({"test": base64.b64encode(b"a" * 32).decode()})
    configured = WorkerConfig.from_environment(
        {
            **env,
            "BITBUCKET_CREDENTIAL_KEYS": keys,
            "BITBUCKET_ACTIVE_KEY_ID": "test",
        }
    )
    assert configured.bitbucket is not None
    assert WebhookReplayProvider("bitbucket", Receiver()).provider == "bitbucket"
    with pytest.raises(RuntimeError, match="BITBUCKET_CREDENTIAL_KEYS"):
        WorkerConfig.from_environment({**env, "BITBUCKET_CREDENTIAL_KEYS": keys})
