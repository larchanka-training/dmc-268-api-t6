"""A tampered policy snapshot row fails the run on read, on PostgreSQL (#168, part 3)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
    PolicySnapshot,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    RunPolicyPins,
    build_policy_snapshot,
    canonical_policy_payload,
    pinned_to_json,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from tests.portal_postgres import PR_B, PROMPT, RULE_A, RULE_B, RUN_DONE, Env, portal_schema
from tests.test_policy_snapshot_postgres import _RAW, _documents
from tests.test_prepare_policy_snapshot import Source
from tests.test_run_prompt_pins_postgres import _newer, _packaged, _prompt_id, _seed
from tests.test_run_publication_settings_postgres import _claim, _insert, _ready

_PROFILE = ExecutionProfile.fast("model-a", "", "model-c")
_RULES: dict[str, Any] = {"id": RUN_DONE, "path": RULES_DOCUMENT_PATH}


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test policy snapshot integrity")
    with portal_schema(url, None) as schema:
        yield schema


def _uow(factory: async_sessionmaker[Any]) -> SqlAlchemyPolicySnapshotUnitOfWork:
    return SqlAlchemyPolicySnapshotUnitOfWork(factory, execution_profile=_PROFILE)


async def _stage(factory: async_sessionmaker[Any]) -> RunPolicyPins:
    """Stage both documents of the running ``RUN_DONE`` and return its pins."""
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET state='running' WHERE id=:id"), {"id": RUN_DONE}
        )
    async with _uow(factory) as work:
        pins = await work.policy.get_pins(RUN_DONE)
        await work.policy.stage_documents(RUN_DONE, _documents())
        await work.commit()
    assert pins is not None
    return pins


async def _store(factory: async_sessionmaker[Any]) -> PolicySnapshot:
    """Store the snapshot of ``RUN_DONE`` through the real unit of work."""
    pins = await _stage(factory)
    rules, agents = _documents()
    async with _uow(factory) as work:
        stored = await work.policy.create_snapshot(
            build_policy_snapshot(
                pins,
                rules=rules.document,
                agents=agents.document,
                ignore=parse_review_ignore(rules.document.text),
                snapshot_id=UUID(int=11),
            )
        )
        await work.commit()
    return stored


async def _tamper(factory: async_sessionmaker[Any], sql: str, params: dict[str, Any]) -> None:
    async with factory.begin() as session:
        await session.execute(text(sql), params)


async def _read_error(factory: async_sessionmaker[Any]) -> PolicyError:
    async with _uow(factory) as work:
        with pytest.raises(PolicyError) as caught:
            await work.policy.get_snapshot(RUN_DONE)
    return caught.value


def _assert_integrity_error(error: PolicyError, checksum: str) -> None:
    assert error.error_code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR.value
    assert checksum in error.message
    assert "generated" not in error.message


_RAW_SHA256_TAMPER = (
    "UPDATE run_policy_documents SET raw_sha256 = repeat('0', 64) "
    "WHERE run_id = :id AND path = :path",
    _RULES,
)
_BASE_TAMPER = (
    "UPDATE run_policy_snapshots SET policy_base_sha = repeat('c', 40) WHERE run_id = :id",
    {"id": RUN_DONE},
)
_ROW_TAMPERS: list[tuple[str, dict[str, Any], str]] = [
    (
        "UPDATE run_policy_documents SET raw_bytes = :raw WHERE run_id = :id AND path = :path",
        {**_RULES, "raw": _RAW + b"x"},
        "raw_sha256",
    ),
    (
        "UPDATE run_policy_documents SET text = text || 'x' WHERE run_id = :id AND path = :path",
        _RULES,
        "text_sha256",
    ),
    (*_RAW_SHA256_TAMPER, "raw_sha256"),
    (
        "UPDATE run_policy_snapshots SET ignore_patterns = '[]'::jsonb WHERE run_id = :id",
        {"id": RUN_DONE},
        "ignore_digest",
    ),
    (*_BASE_TAMPER, "policy_digest"),
    (
        "UPDATE run_policy_documents SET line_count = 99 WHERE run_id = :id AND path = :path",
        _RULES,
        "line_count",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("sql", "params", "checksum"),
    _ROW_TAMPERS,
    ids=["raw-bytes", "text", "raw-sha256", "ignore-patterns", "policy-base-sha", "line-count"],
)
def test_a_tampered_snapshot_row_fails_on_read(
    env: Env, sql: str, params: dict[str, Any], checksum: str
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            stored = await _store(factory)
            async with _uow(factory) as work:
                assert await work.policy.get_snapshot(RUN_DONE) == stored

            await _tamper(factory, sql, params)

            _assert_integrity_error(await _read_error(factory), checksum)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _no_source(pins: RunPolicyPins) -> Any:
    raise AssertionError("a stored policy is never read from the repository again")


@pytest.mark.integration
def test_a_retry_fails_on_a_tampered_stored_snapshot(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            await _store(factory)
            await _tamper(factory, *_BASE_TAMPER)
            prepare = PreparePolicySnapshot(
                uow_factory=lambda: _uow(factory), source_factory=_no_source
            )

            with pytest.raises(PolicyError) as caught:
                await prepare.execute(RUN_DONE)

            _assert_integrity_error(caught.value, "policy_digest")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_retry_fails_on_a_tampered_staged_document_before_any_snapshot(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            await _stage(factory)
            await _tamper(factory, *_RAW_SHA256_TAMPER)
            prepare = PreparePolicySnapshot(
                uow_factory=lambda: _uow(factory), source_factory=_no_source
            )

            with pytest.raises(PolicyError) as caught:
                await prepare.execute(RUN_DONE)

            _assert_integrity_error(caught.value, "raw_sha256")
            async with factory() as session:
                snapshots = await session.scalar(
                    text("SELECT count(*) FROM run_policy_snapshots WHERE run_id = :id"),
                    {"id": RUN_DONE},
                )
            assert snapshots == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_an_untouched_retry_reuses_the_stored_snapshot(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            stored = await _store(factory)
            prepare = PreparePolicySnapshot(
                uow_factory=lambda: _uow(factory), source_factory=_no_source
            )

            again = await prepare.execute(RUN_DONE)

            assert again == stored
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("column", ["prompt_bindings", "execution_profile"])
def test_a_key_a_later_version_stored_in_the_pins_json_passes_on_read(
    env: Env, column: str
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            stored = await _store(factory)
            typed = {
                "prompt_bindings": pinned_to_json(stored.prompt_bindings),
                "execution_profile": pinned_to_json(stored.execution_profile),
            }
            value = typed[column] | {"future_knob": "on"}
            stored_json = typed | {column: value}
            canonical = canonical_policy_payload(
                schema_version=stored.schema_version,
                policy_base_sha=stored.policy_base_sha,
                documents=(stored.rules, stored.agents),
                ignore_digest=stored.ignore_digest,
                parser_profile=stored.parser_profile,
                parser_version=stored.parser_version,
                limits_version=stored.limits_version,
                defaults_checksum=stored.defaults_checksum,
                prompt_bindings=stored_json["prompt_bindings"],
                execution_profile=stored_json["execution_profile"],
                priority_matcher_version=stored.priority_matcher_version,
            )
            digest = sha256(canonical.encode("utf-8")).hexdigest()
            await _tamper(
                factory,
                f"UPDATE run_policy_snapshots SET {column} = CAST(:value AS jsonb), "
                "policy_digest = :digest WHERE run_id = :id",
                {"id": RUN_DONE, "value": json.dumps(value), "digest": digest},
            )

            async with _uow(factory) as work:
                read = await work.policy.get_snapshot(RUN_DONE)

            # The typed value has no such field; the digest still covers the stored key.
            assert read == replace(stored, policy_digest=digest)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("sql", "params", "field"),
    [
        (
            "UPDATE rule_versions SET checksum = repeat('0', 64) WHERE id = :id",
            {"id": RULE_A},
            "defaults_checksum",
        ),
        (
            "UPDATE prompt_versions SET checksum = repeat('0', 64) WHERE id = :id",
            {"id": PROMPT},
            "prompt_bindings",
        ),
        (
            "UPDATE runs SET rule_version_id = :rule WHERE id = :id",
            {"id": RUN_DONE, "rule": RULE_B},
            "service_defaults_rule_version_id",
        ),
        (
            "UPDATE runs SET base_sha = repeat('d', 40) WHERE id = :id",
            {"id": RUN_DONE},
            "policy_base_sha",
        ),
        (
            # Another head too: one webhook run per change and head.
            "UPDATE runs SET code_change_id = :change, head_sha = repeat('9', 40) WHERE id = :id",
            {"id": RUN_DONE, "change": PR_B},
            "repository_id",
        ),
    ],
    ids=["rule-checksum", "prompt-checksum", "run-rule-version", "run-base", "run-repository"],
)
def test_a_pinned_version_that_changed_after_the_snapshot_fails_on_read(
    env: Env, sql: str, params: dict[str, Any], field: str
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            stored = await _store(factory)
            async with _uow(factory) as work:
                assert await work.policy.get_snapshot(RUN_DONE) == stored

            await _tamper(factory, sql, params)

            _assert_integrity_error(await _read_error(factory), field)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("sql", "path", "field"),
    [
        (
            "UPDATE run_policy_documents SET byte_count = NULL WHERE run_id = :id AND path = :path",
            RULES_DOCUMENT_PATH,
            "byte_count",
        ),
        (
            "UPDATE run_policy_documents SET line_count = NULL WHERE run_id = :id AND path = :path",
            RULES_DOCUMENT_PATH,
            "line_count",
        ),
        (
            "UPDATE run_policy_documents SET byte_count = 7 WHERE run_id = :id AND path = :path",
            AGENTS_DOCUMENT_PATH,
            "byte_count",
        ),
    ],
    ids=["present-without-byte-count", "present-without-line-count", "missing-with-byte-count"],
)
def test_a_document_row_that_disagrees_with_its_presence_is_an_integrity_error(
    env: Env, sql: str, path: str, field: str
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            await _store(factory)
            # The CHECK of migration 0029 does not cover byte_count and line_count.
            await _tamper(factory, sql, {"id": RUN_DONE, "path": path})

            error = await _read_error(factory)

            # Terminal, not the retryable internal_error of a plain ValueError.
            assert error.error_code == PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR.value
            assert error.source_path == path
            assert error.message == (
                f"The stored fields of a policy document do not match its presence: {field}"
                f" | файл `{path}`"
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("key", "column"),
    [
        ("review.conventions", "conventions_prompt_version_id"),
        ("review.verifier", "verifier_prompt_version_id"),
    ],
    ids=["conventions", "verifier"],
)
def test_a_run_prompt_pin_that_changed_after_the_snapshot_fails_on_read(
    env: Env, key: str, column: str
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _seed(factory, _packaged())
            async with factory.begin() as session:
                run_id = await _insert(session)
            await _claim(factory, run_id, 1)
            prepare = PreparePolicySnapshot(
                uow_factory=lambda: _uow(factory), source_factory=lambda pins: Source()
            )
            stored = await prepare.execute(run_id)
            # The run pinned both prompts at creation (#280): the snapshot binds all three.
            assert stored.prompt_bindings.is_complete is True
            async with _uow(factory) as work:
                assert await work.policy.get_snapshot(run_id) == stored
            other = _newer(key)
            await _seed(factory, (other,))
            other_id = await _prompt_id(factory, key, other.version)

            await _tamper(
                factory,
                f"UPDATE runs SET {column} = :prompt WHERE id = :id",
                {"id": run_id, "prompt": other_id},
            )

            async with _uow(factory) as work:
                with pytest.raises(PolicyError) as caught:
                    await work.policy.get_snapshot(run_id)
            _assert_integrity_error(caught.value, "prompt_bindings")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
