"""SQLAlchemy storage of staged policy documents and the policy snapshot of a run."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import RuleVersion
from app.modules.reviews.application.policy_documents import LoadedPolicyDocument
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    DocumentPresence,
    PolicyDocument,
    PolicySnapshot,
)
from app.modules.reviews.application.prepare_policy_snapshot import RunPolicyPins
from app.modules.reviews.infrastructure.models import (
    CodeChange,
    PromptVersion,
    Run,
    RunPolicyDocument,
    RunPolicySnapshot,
)

_REVIEW_PROMPT_BINDING = "review.system"


def _document(row: RunPolicyDocument) -> LoadedPolicyDocument:
    document = PolicyDocument(
        path=row.path,
        presence=DocumentPresence(row.presence),
        blob_oid=row.blob_oid,
        raw_sha256=row.raw_sha256,
        text_sha256=row.text_sha256,
        text=row.text,
        byte_count=row.byte_count,
        line_count=row.line_count,
    )
    return LoadedPolicyDocument(document, row.raw_bytes)


class SqlAlchemyPolicySnapshotStore:
    """Session-bound adapter; it only flushes and never owns a commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_pins(self, run_id: UUID) -> RunPolicyPins | None:
        row = (
            await self._session.execute(
                select(Run, CodeChange.repository_id, RuleVersion.checksum, PromptVersion.checksum)
                .join(CodeChange, CodeChange.id == Run.code_change_id)
                .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
                .join(PromptVersion, PromptVersion.id == Run.prompt_version_id)
                .where(Run.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        run, repository_id, defaults_checksum, prompt_checksum = row
        return RunPolicyPins(
            run_id=run.id,
            repository_id=repository_id,
            policy_base_sha=run.base_sha,
            service_defaults_rule_version_id=run.rule_version_id,
            defaults_checksum=defaults_checksum,
            # The run pins only the review prompt today; the other bindings and the
            # execution profile arrive with the LLM pipeline contract (#120).
            prompt_bindings={
                _REVIEW_PROMPT_BINDING: {
                    "id": str(run.prompt_version_id),
                    "checksum": prompt_checksum,
                }
            },
            execution_profile={},
        )

    async def get_staged_documents(self, run_id: UUID) -> tuple[LoadedPolicyDocument, ...]:
        rows = await self._session.scalars(
            select(RunPolicyDocument)
            .where(RunPolicyDocument.run_id == run_id)
            .order_by(RunPolicyDocument.path)
        )
        return tuple(_document(row) for row in rows)

    async def stage_documents(
        self, run_id: UUID, documents: tuple[LoadedPolicyDocument, ...]
    ) -> tuple[LoadedPolicyDocument, ...]:
        for item in documents:
            document = item.document
            await self._session.execute(
                insert(RunPolicyDocument)
                .values(
                    run_id=run_id,
                    path=document.path,
                    presence=document.presence.value,
                    blob_oid=document.blob_oid,
                    raw_sha256=document.raw_sha256,
                    text_sha256=document.text_sha256,
                    raw_bytes=item.raw,
                    text=document.text,
                    byte_count=document.byte_count,
                    line_count=document.line_count,
                )
                # A successful load of an earlier attempt or another worker is kept.
                .on_conflict_do_nothing(constraint="uq_run_policy_documents_run_path")
            )
        await self._session.flush()
        return await self.get_staged_documents(run_id)

    async def get_snapshot(self, run_id: UUID) -> PolicySnapshot | None:
        row = await self._session.get(RunPolicySnapshot, run_id)
        if row is None:
            return None
        staged = {
            item.document.path: item.document for item in await self.get_staged_documents(run_id)
        }
        if set(staged) != {RULES_DOCUMENT_PATH, AGENTS_DOCUMENT_PATH}:
            raise PolicyError(
                PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
                "The policy snapshot has lost a document",
                policy_base_sha=row.policy_base_sha,
            )
        return PolicySnapshot(
            schema_version=row.schema_version,
            snapshot_id=row.snapshot_id,
            run_id=row.run_id,
            repository_id=row.repository_id,
            policy_base_sha=row.policy_base_sha,
            rules=staged[RULES_DOCUMENT_PATH],
            agents=staged[AGENTS_DOCUMENT_PATH],
            ignore_patterns=tuple(row.ignore_patterns),
            ignore_digest=row.ignore_digest,
            parser_profile=row.parser_profile,
            parser_version=row.parser_version,
            matcher_version=row.matcher_version,
            limits_version=row.limits_version,
            builtin_exclusions_version=row.builtin_exclusions_version,
            priority_matcher_version=row.priority_matcher_version,
            service_defaults_rule_version_id=row.service_defaults_rule_version_id,
            defaults_checksum=row.defaults_checksum,
            prompt_bindings=dict(row.prompt_bindings),
            execution_profile=dict(row.execution_profile),
            policy_digest=row.policy_digest,
        )

    async def create_snapshot(self, snapshot: PolicySnapshot) -> PolicySnapshot:
        await self._session.execute(
            insert(RunPolicySnapshot)
            .values(
                run_id=snapshot.run_id,
                snapshot_id=snapshot.snapshot_id,
                repository_id=snapshot.repository_id,
                schema_version=snapshot.schema_version,
                policy_base_sha=snapshot.policy_base_sha,
                ignore_patterns=list(snapshot.ignore_patterns),
                ignore_digest=snapshot.ignore_digest,
                parser_profile=snapshot.parser_profile,
                parser_version=snapshot.parser_version,
                matcher_version=snapshot.matcher_version,
                limits_version=snapshot.limits_version,
                builtin_exclusions_version=snapshot.builtin_exclusions_version,
                priority_matcher_version=snapshot.priority_matcher_version,
                service_defaults_rule_version_id=snapshot.service_defaults_rule_version_id,
                defaults_checksum=snapshot.defaults_checksum,
                prompt_bindings=dict(snapshot.prompt_bindings),
                execution_profile=dict(snapshot.execution_profile),
                policy_digest=snapshot.policy_digest,
            )
            # A concurrent worker's snapshot wins; the caller compares the digests.
            .on_conflict_do_nothing(index_elements=[RunPolicySnapshot.run_id])
        )
        await self._session.flush()
        stored = await self.get_snapshot(snapshot.run_id)
        assert stored is not None
        return stored


class SqlAlchemyPolicySnapshotUnitOfWork(SqlAlchemyUnitOfWork):
    """Expose policy snapshot persistence in one explicit transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def policy(self) -> SqlAlchemyPolicySnapshotStore:
        return SqlAlchemyPolicySnapshotStore(self.session)
