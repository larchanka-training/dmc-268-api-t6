"""Build and persist the immutable policy snapshot of one run (RULES_FORMAT_SPEC §7).

Documents are staged once per run and never overwritten, so a retry reads the same
bytes instead of the repository. VCS reads happen between short transactions; no model
call is allowed before the snapshot exists.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.policy_documents import (
    LoadedPolicyDocument,
    LoadPolicyDocuments,
    PolicyDocumentSource,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    POLICY_SNAPSHOT_SCHEMA_VERSION,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
    PolicyDocument,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.review_ignore import (
    BUILTIN_EXCLUSIONS_VERSION,
    LIMITS_VERSION,
    MATCHER_VERSION,
    PARSER_PROFILE,
    PARSER_VERSION,
    IgnorePolicy,
    parse_review_ignore,
)

PRIORITY_MATCHER_VERSION = "priority-matcher/v1"


@dataclass(frozen=True)
class RunPolicyPins:
    """Versions fixed when the run was created, before any VCS read."""

    run_id: UUID
    repository_id: UUID
    policy_base_sha: str
    service_defaults_rule_version_id: UUID
    defaults_checksum: str
    prompt_bindings: PromptBindings
    execution_profile: ExecutionProfile


class PolicySnapshotStore(Protocol):
    async def get_pins(self, run_id: UUID) -> RunPolicyPins | None: ...

    async def get_snapshot(self, run_id: UUID) -> PolicySnapshot | None: ...

    async def get_staged_documents(self, run_id: UUID) -> tuple[LoadedPolicyDocument, ...]: ...

    async def stage_documents(
        self, run_id: UUID, documents: tuple[LoadedPolicyDocument, ...]
    ) -> tuple[LoadedPolicyDocument, ...]:
        """Insert documents that are not staged yet and return every staged document."""
        ...

    async def create_snapshot(self, snapshot: PolicySnapshot) -> PolicySnapshot:
        """Insert the snapshot unless the run has one, and return the stored snapshot."""
        ...


class PolicySnapshotUnitOfWork(UnitOfWork, Protocol):
    @property
    def policy(self) -> PolicySnapshotStore: ...


class PolicySnapshotUnitOfWorkFactory(Protocol):
    def __call__(self) -> PolicySnapshotUnitOfWork: ...


def pinned_to_json(value: PromptBindings | ExecutionProfile) -> dict[str, str | int | None]:
    """A JSON form of pinned versions for the digest and for storage."""
    return {
        name: str(item) if isinstance(item, UUID) else item for name, item in asdict(value).items()
    }


def build_policy_snapshot(
    pins: RunPolicyPins,
    *,
    rules: PolicyDocument,
    agents: PolicyDocument,
    ignore: IgnorePolicy,
    snapshot_id: UUID,
) -> PolicySnapshot:
    """Assemble the snapshot; equal policies have equal ``policy_digest`` across runs."""
    payload = {
        "schema_version": POLICY_SNAPSHOT_SCHEMA_VERSION,
        "policy_base_sha": pins.policy_base_sha,
        "documents": [
            {
                "path": document.path,
                "presence": document.presence.value,
                "raw_sha256": document.raw_sha256,
                "text_sha256": document.text_sha256,
            }
            for document in (rules, agents)
        ],
        "ignore_digest": ignore.digest,
        "parser_profile": PARSER_PROFILE,
        "exact_parser_version": PARSER_VERSION,
        "limits_version": LIMITS_VERSION,
        "defaults_checksum": pins.defaults_checksum,
        "prompt_bindings": pinned_to_json(pins.prompt_bindings),
        "execution_profile": pinned_to_json(pins.execution_profile),
        "priority_matcher_version": PRIORITY_MATCHER_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return PolicySnapshot(
        schema_version=POLICY_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        run_id=pins.run_id,
        repository_id=pins.repository_id,
        policy_base_sha=pins.policy_base_sha,
        rules=rules,
        agents=agents,
        ignore_patterns=ignore.patterns,
        ignore_digest=ignore.digest,
        parser_profile=PARSER_PROFILE,
        parser_version=PARSER_VERSION,
        matcher_version=MATCHER_VERSION,
        limits_version=LIMITS_VERSION,
        builtin_exclusions_version=BUILTIN_EXCLUSIONS_VERSION,
        priority_matcher_version=PRIORITY_MATCHER_VERSION,
        service_defaults_rule_version_id=pins.service_defaults_rule_version_id,
        defaults_checksum=pins.defaults_checksum,
        prompt_bindings=pins.prompt_bindings,
        execution_profile=pins.execution_profile,
        policy_digest=sha256(canonical.encode("utf-8")).hexdigest(),
    )


class PreparePolicySnapshot:
    """Return the snapshot of a run, loading and storing it on the first attempt."""

    def __init__(
        self,
        *,
        uow_factory: PolicySnapshotUnitOfWorkFactory,
        source_factory: Callable[[RunPolicyPins], PolicyDocumentSource],
    ) -> None:
        self._uow_factory = uow_factory
        self._source_factory = source_factory

    async def execute(self, run_id: UUID) -> PolicySnapshot:
        async with self._uow_factory() as uow:
            snapshot = await uow.policy.get_snapshot(run_id)
            if snapshot is not None:
                return snapshot
            pins = await uow.policy.get_pins(run_id)
            if pins is None:
                raise LookupError(f"run {run_id} has no pinned policy versions")
            staged = await uow.policy.get_staged_documents(run_id)

        if {item.document.path for item in staged} != {RULES_DOCUMENT_PATH, AGENTS_DOCUMENT_PATH}:
            # The network read is outside any transaction; a failure stages nothing.
            loaded = await LoadPolicyDocuments(
                self._source_factory(pins), policy_base_sha=pins.policy_base_sha
            ).execute()
            async with self._uow_factory() as uow:
                staged = await uow.policy.stage_documents(run_id, (loaded.rules, loaded.agents))
                await uow.commit()

        by_path = {item.document.path: item.document for item in staged}
        rules, agents = by_path[RULES_DOCUMENT_PATH], by_path[AGENTS_DOCUMENT_PATH]
        # An invalid block fails the run here: the staged documents stay for diagnostics.
        ignore = parse_review_ignore(rules.text, policy_base_sha=pins.policy_base_sha)
        snapshot = build_policy_snapshot(
            pins, rules=rules, agents=agents, ignore=ignore, snapshot_id=uuid4()
        )
        async with self._uow_factory() as uow:
            stored = await uow.policy.create_snapshot(snapshot)
            await uow.commit()
        if stored.policy_digest != snapshot.policy_digest:
            raise PolicyError(
                PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                "The stored policy snapshot differs from the pinned sources",
                policy_base_sha=pins.policy_base_sha,
            )
        return stored
