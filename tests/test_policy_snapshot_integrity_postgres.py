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
from tests.portal_postgres import RUN_DONE, Env, portal_schema
from tests.test_policy_snapshot_postgres import _RAW, _documents

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
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("sql", "params", "checksum"),
    _ROW_TAMPERS,
    ids=["raw-bytes", "text", "raw-sha256", "ignore-patterns", "policy-base-sha"],
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
def test_a_key_a_later_version_stored_in_the_profile_json_passes_on_read(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            stored = await _store(factory)
            profile = pinned_to_json(stored.execution_profile) | {"future_knob": "on"}
            canonical = canonical_policy_payload(
                schema_version=stored.schema_version,
                policy_base_sha=stored.policy_base_sha,
                documents=(stored.rules, stored.agents),
                ignore_digest=stored.ignore_digest,
                parser_profile=stored.parser_profile,
                parser_version=stored.parser_version,
                limits_version=stored.limits_version,
                defaults_checksum=stored.defaults_checksum,
                prompt_bindings=pinned_to_json(stored.prompt_bindings),
                execution_profile=profile,
                priority_matcher_version=stored.priority_matcher_version,
            )
            digest = sha256(canonical.encode("utf-8")).hexdigest()
            await _tamper(
                factory,
                "UPDATE run_policy_snapshots SET execution_profile = CAST(:profile AS jsonb), "
                "policy_digest = :digest WHERE run_id = :id",
                {"id": RUN_DONE, "profile": json.dumps(profile), "digest": digest},
            )

            async with _uow(factory) as work:
                read = await work.policy.get_snapshot(RUN_DONE)

            # The typed profile has no such field; the digest still covers the stored key.
            assert read == replace(stored, policy_digest=digest)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
