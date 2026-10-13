"""Redis caches of context building: AST facts by blob and the file tree by revision.

CONTEXT_AND_VERIFICATION_SPEC §4.3. Both are optional accelerators: a miss, a broken
entry or an unreachable Redis reads as "not cached" and never fails a run. Neither is a
permission to show a file: the caller applies the ignore policy before every use.
"""

from __future__ import annotations

import json
import logging
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstLanguage,
    facts_from_json,
    facts_to_json,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import profile_version

_LOGGER = logging.getLogger(__name__)
AST_TTL_SECONDS = 7 * 24 * 3600
TREE_TTL_SECONDS = 3600


class RedisLike(Protocol):
    async def get(self, name: str) -> bytes | str | None: ...

    async def set(self, name: str, value: str, ex: int | None = None) -> object: ...


class RedisAstFactsCache:
    """Facts by ``(repository_id, blob_sha, language, grammar and query versions)``."""

    def __init__(self, redis: RedisLike, *, repository_id: UUID) -> None:
        self._redis = redis
        self._repository_id = repository_id

    def _key(self, blob_sha: str, language: AstLanguage) -> str:
        profile = sha256(profile_version(language).encode("utf-8")).hexdigest()[:16]
        return f"ast:{self._repository_id}:{blob_sha}:{language.value}:{profile}"

    async def get(self, blob_sha: str, language: AstLanguage) -> AstFacts | None:
        try:
            value = await self._redis.get(self._key(blob_sha, language))
            return None if value is None else facts_from_json(json.loads(value))
        except Exception:
            _LOGGER.warning("AST cache read failed blob_sha=%s", blob_sha, exc_info=True)
            return None

    async def put(self, blob_sha: str, language: AstLanguage, facts: AstFacts) -> None:
        try:
            payload = json.dumps(facts_to_json(facts), separators=(",", ":"))
            await self._redis.set(self._key(blob_sha, language), payload, ex=AST_TTL_SECONDS)
        except Exception:
            _LOGGER.warning("AST cache write failed blob_sha=%s", blob_sha, exc_info=True)


class RedisTreeCache:
    """Complete file tree by ``(repository_id, revision)`` for one hour."""

    def __init__(self, redis: RedisLike, *, repository_id: UUID) -> None:
        self._redis = redis
        self._repository_id = repository_id

    def _key(self, revision: str) -> str:
        return f"tree:{self._repository_id}:{revision}"

    async def get(self, revision: str) -> RevisionTree | None:
        try:
            value = await self._redis.get(self._key(revision))
            if value is None:
                return None
            return RevisionTree(revision=revision, blob_shas=dict(json.loads(value)), complete=True)
        except Exception:
            _LOGGER.warning("Tree cache read failed revision=%s", revision, exc_info=True)
            return None

    async def put(self, tree: RevisionTree) -> None:
        try:
            payload = json.dumps(dict(tree.blob_shas), separators=(",", ":"))
            await self._redis.set(self._key(tree.revision), payload, ex=TREE_TTL_SECONDS)
        except Exception:
            _LOGGER.warning("Tree cache write failed revision=%s", tree.revision, exc_info=True)
