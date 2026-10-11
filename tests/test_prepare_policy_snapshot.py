"""Policy snapshot of a run: staged once, immutable, and reused by a retry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.modules.reviews.application.policy_documents import (
    LoadedPolicyDocument,
    PolicyEntryKind,
    PolicyTreeEntry,
    missing_document,
    normalize_document,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    PolicySnapshot,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.review_ignore import parse_review_ignore

_RUN = UUID("00000000-0000-0000-0000-000000000002")
_BASE = "b" * 40
_RULES_TEXT = b"# Style\n\n```review-ignore\ngenerated/**\n```\n"


def _pins(**changes: object) -> RunPolicyPins:
    values: dict[str, object] = {
        "run_id": _RUN,
        "repository_id": UUID("00000000-0000-0000-0000-000000000001"),
        "policy_base_sha": _BASE,
        "service_defaults_rule_version_id": UUID("00000000-0000-0000-0000-000000000004"),
        "defaults_checksum": "2" * 64,
        "prompt_bindings": {"review.system": {"id": "p1", "checksum": "3" * 64}},
        "execution_profile": {},
    }
    return RunPolicyPins(**{**values, **changes})  # type: ignore[arg-type]


@dataclass
class Store:
    pins: RunPolicyPins | None = field(default_factory=_pins)
    documents: dict[str, LoadedPolicyDocument] = field(default_factory=dict)
    snapshot: PolicySnapshot | None = None
    commits: int = 0

    async def get_pins(self, run_id: UUID) -> RunPolicyPins | None:
        return self.pins

    async def get_snapshot(self, run_id: UUID) -> PolicySnapshot | None:
        return self.snapshot

    async def get_staged_documents(self, run_id: UUID) -> tuple[LoadedPolicyDocument, ...]:
        return tuple(self.documents.values())

    async def stage_documents(
        self, run_id: UUID, documents: tuple[LoadedPolicyDocument, ...]
    ) -> tuple[LoadedPolicyDocument, ...]:
        for item in documents:
            self.documents.setdefault(item.document.path, item)
        return tuple(self.documents.values())

    async def create_snapshot(self, snapshot: PolicySnapshot) -> PolicySnapshot:
        if self.snapshot is None:
            self.snapshot = snapshot
        return self.snapshot


@dataclass
class Uow:
    store: Store

    @property
    def policy(self) -> Store:
        return self.store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.store.commits += 1

    async def rollback(self) -> None:
        return None


@dataclass
class Source:
    rules: bytes | None = _RULES_TEXT
    agents: bytes | None = None
    error: PolicyError | None = None
    reads: int = 0

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if tree_oid is not None:
            return {"rules.md": PolicyTreeEntry(PolicyEntryKind.FILE, "3" * 40, 5)}
        entries = {}
        if self.rules is not None:
            entries[".review"] = PolicyTreeEntry(PolicyEntryKind.DIRECTORY, "1" * 40, None)
        if self.agents is not None:
            entries["AGENTS.md"] = PolicyTreeEntry(PolicyEntryKind.FILE, "2" * 40, 5)
        return entries

    async def read_blob(self, oid: str) -> bytes:
        content = self.rules if oid == "3" * 40 else self.agents
        assert content is not None
        return content


def _prepare(store: Store, source: Source) -> PolicySnapshot:
    use_case = PreparePolicySnapshot(
        uow_factory=lambda: Uow(store), source_factory=lambda pins: source
    )
    return asyncio.run(use_case.execute(_RUN))


def test_first_attempt_stages_documents_and_stores_the_snapshot() -> None:
    store, source = Store(), Source()

    snapshot = _prepare(store, source)

    assert snapshot.run_id == _RUN
    assert snapshot.policy_base_sha == _BASE
    assert snapshot.rules.text == _RULES_TEXT.decode()
    assert snapshot.agents.presence is DocumentPresence.MISSING
    assert snapshot.ignore_patterns == ("generated/**",)
    assert snapshot.parser_profile == "rules-markdown/v1"
    assert snapshot.matcher_version == "review-ignore/v1"
    assert snapshot.limits_version == "policy-limits/v1"
    assert store.documents[RULES_DOCUMENT_PATH].raw == _RULES_TEXT
    assert store.documents[AGENTS_DOCUMENT_PATH].raw is None
    assert store.snapshot is snapshot
    assert store.commits == 2


def test_retry_returns_the_stored_snapshot_without_reading_the_repository() -> None:
    store = Store()
    first = _prepare(store, Source())
    changed = Source(rules=b"# Other rules\n")

    assert _prepare(store, changed) is first
    assert changed.reads == 0


def test_retry_after_an_invalid_block_reuses_the_staged_documents() -> None:
    store = Store()
    broken = Source(rules=b"```review-ignore\na?.py\n```\n")

    with pytest.raises(PolicyError) as caught:
        _prepare(store, broken)
    assert caught.value.code is PolicyErrorCode.IGNORE_PATTERN_INVALID
    assert store.snapshot is None
    assert set(store.documents) == {RULES_DOCUMENT_PATH, AGENTS_DOCUMENT_PATH}

    fixed_later = Source(rules=_RULES_TEXT)
    with pytest.raises(PolicyError):
        _prepare(store, fixed_later)
    assert fixed_later.reads == 0


def test_read_failure_stages_nothing_and_is_not_a_missing_document() -> None:
    store = Store()
    failing = Source(error=PolicyError(PolicyErrorCode.FETCH_TRANSIENT, "GitHub answered 502"))

    with pytest.raises(PolicyError) as caught:
        _prepare(store, failing)
    assert caught.value.retryable is True
    assert store.documents == {}
    assert store.snapshot is None

    assert _prepare(store, Source()).rules.presence is DocumentPresence.PRESENT


def test_concurrent_worker_snapshot_with_another_digest_is_an_integrity_error() -> None:
    store = Store()
    other = build_policy_snapshot(
        _pins(),
        rules=missing_document(RULES_DOCUMENT_PATH),
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=parse_review_ignore(None),
        snapshot_id=UUID("00000000-0000-0000-0000-000000000009"),
    )

    class Racing(Store):
        async def create_snapshot(self, snapshot: PolicySnapshot) -> PolicySnapshot:
            return other

    with pytest.raises(PolicyError) as caught:
        _prepare(Racing(), Source())
    assert caught.value.code is PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR
    assert store.snapshot is None


def test_run_without_pins_is_a_lookup_error() -> None:
    with pytest.raises(LookupError, match="pinned policy versions"):
        _prepare(Store(pins=None), Source())


def _snapshot(rules: bytes | None, **pins: object) -> PolicySnapshot:
    document = (
        missing_document(RULES_DOCUMENT_PATH)
        if rules is None
        else normalize_document(RULES_DOCUMENT_PATH, rules, blob_oid="3" * 40)
    )
    return build_policy_snapshot(
        _pins(**pins),
        rules=document,
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=parse_review_ignore(document.text),
        snapshot_id=UUID("00000000-0000-0000-0000-000000000003"),
    )


def test_policy_digest_is_content_only_and_changes_with_any_policy_input() -> None:
    base = _snapshot(_RULES_TEXT)

    assert len(base.policy_digest) == 64
    assert (
        base.policy_digest
        == _snapshot(_RULES_TEXT, run_id=UUID("00000000-0000-0000-0000-00000000000a")).policy_digest
    )
    assert base.policy_digest != _snapshot(_RULES_TEXT + b"More prose.\n").policy_digest
    assert base.ignore_digest == _snapshot(_RULES_TEXT + b"More prose.\n").ignore_digest
    assert base.policy_digest != _snapshot(_RULES_TEXT, defaults_checksum="9" * 64).policy_digest
    assert base.policy_digest != _snapshot(_RULES_TEXT, policy_base_sha="c" * 40).policy_digest
    assert (
        base.policy_digest
        != _snapshot(
            _RULES_TEXT, prompt_bindings={"review.system": {"id": "p2", "checksum": "3" * 64}}
        ).policy_digest
    )


def test_empty_and_missing_rules_have_different_snapshots() -> None:
    empty, missing = _snapshot(b""), _snapshot(None)

    assert empty.ignore_patterns == missing.ignore_patterns == ()
    assert empty.policy_digest != missing.policy_digest
