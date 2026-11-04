"""Bounded, resumable operator migration for stored provider credentials."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Never
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialPurpose,
    optional_bitbucket_cipher_from_environment,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    CredentialContext as BitbucketContext,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    EncryptedCredential as BitbucketEncrypted,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    CredentialCipher,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    CredentialContext as GitLabContext,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    EncryptedCredential as GitLabEncrypted,
)
from app.modules.integrations.gitlab.infrastructure.settings import DatabaseSettings, strict_json

Kind = Literal["bitbucket_tokens", "bitbucket_webhook_secrets", "gitlab_tokens"]
KINDS: tuple[Kind, ...] = ("bitbucket_tokens", "bitbucket_webhook_secrets", "gitlab_tokens")


@dataclass(frozen=True)
class _Row:
    id: UUID
    installation: UUID
    workspace: UUID
    origin: str | None
    generation: int
    binding_generation: int | None
    key_id: str
    ciphertext: bytes
    revoked_at: datetime | None


_SELECT: dict[Kind, str] = {
    "bitbucket_tokens": (
        "SELECT provider_installation_id AS id, provider_installation_id AS installation, "
        "workspace_id AS workspace, NULL AS origin, credential_generation AS generation, "
        "NULL AS binding_generation, token_key_id AS key_id, token_ciphertext AS ciphertext, "
        "revoked_at AS revoked_at "
        "FROM bitbucket_connections WHERE token_key_id IS NOT NULL "
        "AND token_key_id <> :active AND revoked_at IS NULL "
        "ORDER BY provider_installation_id LIMIT :limit"
    ),
    "bitbucket_webhook_secrets": (
        "SELECT s.repository_id AS id, i.provider_installation_id AS installation, "
        "i.workspace_id AS workspace, NULL AS origin, s.generation AS generation, "
        "s.binding_generation AS binding_generation, s.secret_key_id AS key_id, "
        "s.secret_ciphertext AS ciphertext, s.revoked_at AS revoked_at "
        "FROM bitbucket_repository_webhook_secrets s "
        "JOIN bitbucket_repository_identities i ON i.repository_id = s.repository_id "
        "WHERE s.secret_key_id IS NOT NULL AND s.secret_key_id <> :active "
        "AND s.revoked_at IS NULL ORDER BY s.repository_id LIMIT :limit"
    ),
    "gitlab_tokens": (
        "SELECT provider_installation_id AS id, provider_installation_id AS installation, "
        "workspace_id AS workspace, instance_origin AS origin, "
        "credential_generation AS generation, NULL AS binding_generation, "
        "credential_key_id AS key_id, token_ciphertext AS ciphertext, revoked_at AS revoked_at "
        "FROM gitlab_connections WHERE credential_key_id IS NOT NULL "
        "AND credential_key_id <> :active "
        "ORDER BY provider_installation_id LIMIT :limit"
    ),
}

_UPDATE: dict[Kind, str] = {
    "bitbucket_tokens": (
        "UPDATE bitbucket_connections SET token_ciphertext = :new_ciphertext, "
        "token_key_id = :active, updated_at = now() "
        "WHERE provider_installation_id = :id AND workspace_id = :workspace "
        "AND credential_generation = :generation AND token_key_id = :key_id "
        "AND token_ciphertext = :ciphertext AND revoked_at IS NULL RETURNING 1"
    ),
    "bitbucket_webhook_secrets": (
        "UPDATE bitbucket_repository_webhook_secrets s "
        "SET secret_ciphertext = :new_ciphertext, secret_key_id = :active, updated_at = now() "
        "WHERE s.repository_id = :id AND s.generation = :generation "
        "AND s.binding_generation = :binding_generation AND s.secret_key_id = :key_id "
        "AND s.secret_ciphertext = :ciphertext AND s.revoked_at IS NULL "
        "AND EXISTS (SELECT 1 FROM bitbucket_repository_identities i "
        "WHERE i.repository_id = s.repository_id AND i.provider_installation_id = :installation "
        "AND i.workspace_id = :workspace) RETURNING 1"
    ),
    "gitlab_tokens": (
        "UPDATE gitlab_connections SET token_ciphertext = :new_ciphertext, "
        "credential_key_id = :active, updated_at = now() "
        "WHERE provider_installation_id = :id AND workspace_id = :workspace "
        "AND instance_origin = :origin AND credential_generation = :generation "
        "AND credential_key_id = :key_id AND token_ciphertext = :ciphertext "
        "AND revoked_at IS NOT DISTINCT FROM :revoked_at RETURNING 1"
    ),
}

_COUNTS: dict[Kind, str] = {
    "bitbucket_tokens": "SELECT token_key_id, count(*) FROM bitbucket_connections "
    "WHERE token_key_id IS NOT NULL GROUP BY token_key_id",
    "bitbucket_webhook_secrets": "SELECT secret_key_id, count(*) "
    "FROM bitbucket_repository_webhook_secrets WHERE secret_key_id IS NOT NULL "
    "GROUP BY secret_key_id",
    "gitlab_tokens": "SELECT credential_key_id, count(*) FROM gitlab_connections "
    "WHERE credential_key_id IS NOT NULL GROUP BY credential_key_id",
}


class KeyRotation:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        bitbucket: BitbucketCredentialCipher,
        gitlab: CredentialCipher,
        bitbucket_active: str,
        gitlab_active: str,
    ) -> None:
        self._engine = engine
        self._bitbucket = bitbucket
        self._gitlab = gitlab
        self._active: dict[Kind, str] = {
            "bitbucket_tokens": bitbucket_active,
            "bitbucket_webhook_secrets": bitbucket_active,
            "gitlab_tokens": gitlab_active,
        }

    async def counts(self) -> dict[Kind, dict[str, int]]:
        """Read key-ID counts only; no envelope is selected or emitted."""
        result: dict[Kind, dict[str, int]] = {}
        async with self._engine.connect() as db:
            for kind in KINDS:
                rows = (await db.execute(text(_COUNTS[kind]))).all()
                result[kind] = {str(key_id): int(count) for key_id, count in rows}
        return result

    async def verify(self) -> bool:
        counts = await self.counts()
        return all(all(key_id == self._active[kind] for key_id in counts[kind]) for kind in KINDS)

    async def rotate_batch(self, kind: Kind, *, batch_size: int) -> int:
        """Read at most batch_size old envelopes, then CAS each in a short transaction."""
        if kind not in KINDS or not 1 <= batch_size <= 500:
            raise ValueError("rotation_parameters_invalid")
        active = self._active[kind]
        async with self._engine.connect() as db:
            selected = (
                (await db.execute(text(_SELECT[kind]), {"active": active, "limit": batch_size}))
                .mappings()
                .all()
            )
        rows = [_Row(**row) for row in selected]
        updated = 0
        for row in rows:
            if kind == "gitlab_tokens":
                if row.origin is None:
                    raise ValueError("credential_context_invalid")
                gl_context = GitLabContext(row.installation, row.workspace, row.origin)
                plaintext = self._gitlab.decrypt(
                    GitLabEncrypted(row.key_id, row.ciphertext), context=gl_context
                )
                gl_encrypted = self._gitlab.encrypt(plaintext, context=gl_context)
                new_ciphertext = gl_encrypted.token_ciphertext
                new_key_id = gl_encrypted.key_id
            else:
                bb_context = BitbucketContext(
                    row.installation,
                    row.workspace,
                    row.id if kind == "bitbucket_webhook_secrets" else None,
                )
                purpose = (
                    CredentialPurpose.WEBHOOK_SECRET
                    if kind == "bitbucket_webhook_secrets"
                    else CredentialPurpose.TOKEN
                )
                plaintext = self._bitbucket.decrypt(
                    BitbucketEncrypted(row.key_id, row.ciphertext),
                    context=bb_context,
                    purpose=purpose,
                )
                bb_encrypted = self._bitbucket.encrypt(
                    plaintext, context=bb_context, purpose=purpose
                )
                new_ciphertext = bb_encrypted.ciphertext
                new_key_id = bb_encrypted.key_id
            if new_key_id != active:
                raise ValueError("active_key_mismatch")
            async with self._engine.begin() as db:
                changed = await db.execute(
                    text(_UPDATE[kind]),
                    {**row.__dict__, "active": active, "new_ciphertext": new_ciphertext},
                )
                updated += int(changed.first() is not None)
        return updated


def _gitlab_cipher(env: Mapping[str, str]) -> CredentialCipher:
    try:
        raw, active = env["GITLAB_CREDENTIAL_KEYS"], env["GITLAB_ACTIVE_KEY_ID"]
        if len(raw) > 16384 or not 1 <= len(active) <= 64:
            raise ValueError
        parsed = strict_json(raw.encode("utf-8"), limit=16384)
        if not isinstance(parsed, dict):
            raise ValueError
        keys: dict[str, bytes] = {}
        for key_id, value in parsed.items():
            if (
                not isinstance(key_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key_id) is None
                or not isinstance(value, str)
            ):
                raise ValueError
            key = base64.b64decode(value, validate=True)
            if base64.b64encode(key).decode("ascii") != value:
                raise ValueError
            keys[key_id] = key
        return CredentialCipher(active_key_id=active, keys=keys)
    except (KeyError, ValueError, TypeError, binascii.Error):
        raise ValueError("configuration_invalid") from None


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "arguments_invalid\n")


def _parser() -> _SafeParser:
    parser = _SafeParser(prog="python -m app.credential_key_rotation", allow_abbrev=False)
    parser.add_argument("command", choices=("dry-run", "run", "verify"))
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=1)
    return parser


async def execute(
    args: argparse.Namespace, env: Mapping[str, str]
) -> tuple[dict[str, object], bool]:
    if not 1 <= args.batch_size <= 500 or not 1 <= args.max_batches <= 10000:
        raise ValueError("rotation_parameters_invalid")
    database = DatabaseSettings.from_environment(env)
    bitbucket = optional_bitbucket_cipher_from_environment(env)
    if bitbucket is None:
        raise ValueError("configuration_invalid")
    gitlab = _gitlab_cipher(env)
    bb_active = env["BITBUCKET_ACTIVE_KEY_ID"]
    gl_active = env["GITLAB_ACTIVE_KEY_ID"]
    engine = create_async_engine(database.database_url, hide_parameters=True, echo=False)
    try:
        operator = KeyRotation(
            engine,
            bitbucket=bitbucket,
            gitlab=gitlab,
            bitbucket_active=bb_active,
            gitlab_active=gl_active,
        )
        updated: dict[Kind, int] = dict.fromkeys(KINDS, 0)
        if args.command == "run":
            for _ in range(args.max_batches):
                for kind in KINDS:
                    updated[kind] += await operator.rotate_batch(kind, batch_size=args.batch_size)
        counts = await operator.counts()
        ready = all(all(key == operator._active[kind] for key in counts[kind]) for kind in KINDS)
        if args.command == "run":
            return {"updated": updated, "counts": counts, "ready": ready}, True
        return {"counts": counts}, args.command != "verify" or ready
    finally:
        await engine.dispose()


def main() -> int:
    args = _parser().parse_args()
    try:
        result, success = asyncio.run(execute(args, os.environ))
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        return 0 if success else 1
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130
    except Exception:
        # This is the final confidentiality boundary: no DB/crypto exception text escapes.
        sys.stderr.write("operation_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
