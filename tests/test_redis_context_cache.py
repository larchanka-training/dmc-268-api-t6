"""Redis caches of context building: keys, lifetimes and silence on any failure."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

import pytest

from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import AstLanguage, facts_to_json
from app.modules.reviews.infrastructure.redis_context_cache import (
    RedisAstFactsCache,
    RedisTreeCache,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts, profile_version

_REPOSITORY = UUID(int=1)
_BLOB = "d" * 40


@dataclass
class FakeRedis:
    values: dict[str, str] = field(default_factory=dict)
    lifetimes: dict[str, int | None] = field(default_factory=dict)
    broken: bool = False

    async def get(self, name: str) -> str | None:
        if self.broken:
            raise ConnectionError("redis is down")
        return self.values.get(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> object:
        if self.broken:
            raise ConnectionError("redis is down")
        self.values[name] = value
        self.lifetimes[name] = ex
        return True


def test_facts_round_trip_under_a_key_of_repository_blob_language_and_versions() -> None:
    redis = FakeRedis()
    cache = RedisAstFactsCache(redis, repository_id=_REPOSITORY)
    facts = extract_facts(AstLanguage.PYTHON, b"def f():\n    return 1\n")

    async def scenario() -> None:
        assert await cache.get(_BLOB, AstLanguage.PYTHON) is None
        await cache.put(_BLOB, AstLanguage.PYTHON, facts)
        assert await cache.get(_BLOB, AstLanguage.PYTHON) == facts
        # Another language, blob or repository never reads this entry.
        assert await cache.get(_BLOB, AstLanguage.GO) is None
        assert await cache.get("e" * 40, AstLanguage.PYTHON) is None
        other = RedisAstFactsCache(redis, repository_id=UUID(int=2))
        assert await other.get(_BLOB, AstLanguage.PYTHON) is None

    asyncio.run(scenario())
    (key,) = redis.values
    assert key.startswith(f"ast:{_REPOSITORY}:{_BLOB}:python:")
    assert redis.lifetimes[key] == 7 * 24 * 3600


def test_facts_cached_without_comment_spans_are_never_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Before #498 the key ended at the profile digest and the facts had no "comments".
    redis = FakeRedis()
    profile = sha256(profile_version(AstLanguage.PYTHON).encode("utf-8")).hexdigest()[:16]
    stale = facts_to_json(extract_facts(AstLanguage.PYTHON, b"x = 1\n"))
    del stale["comments"]
    redis.values[f"ast:{_REPOSITORY}:{_BLOB}:python:{profile}"] = json.dumps(stale)
    cache = RedisAstFactsCache(redis, repository_id=_REPOSITORY)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(cache.get(_BLOB, AstLanguage.PYTHON)) is None

    assert caplog.records == []


def test_tree_is_cached_for_one_hour_by_repository_and_revision() -> None:
    redis = FakeRedis()
    cache = RedisTreeCache(redis, repository_id=_REPOSITORY)
    tree = RevisionTree("a" * 40, {"src/a.py": _BLOB}, complete=True)

    async def scenario() -> None:
        assert await cache.get("a" * 40) is None
        await cache.put(tree)
        assert await cache.get("a" * 40) == tree
        assert await cache.get("b" * 40) is None

    asyncio.run(scenario())
    assert redis.lifetimes == {f"tree:{_REPOSITORY}:{'a' * 40}": 3600}


def test_unreachable_redis_and_a_broken_entry_read_as_not_cached() -> None:
    redis = FakeRedis(broken=True)
    facts = extract_facts(AstLanguage.PYTHON, b"x = 1\n")

    async def scenario() -> None:
        ast = RedisAstFactsCache(redis, repository_id=_REPOSITORY)
        trees = RedisTreeCache(redis, repository_id=_REPOSITORY)
        await ast.put(_BLOB, AstLanguage.PYTHON, facts)
        await trees.put(RevisionTree("a" * 40, {}, complete=True))
        assert await ast.get(_BLOB, AstLanguage.PYTHON) is None
        assert await trees.get("a" * 40) is None

        redis.broken = False
        redis.values[ast._key(_BLOB, AstLanguage.PYTHON)] = "{not json"
        redis.values[trees._key("a" * 40)] = "[1, 2"
        assert await ast.get(_BLOB, AstLanguage.PYTHON) is None
        assert await trees.get("a" * 40) is None

    asyncio.run(scenario())
