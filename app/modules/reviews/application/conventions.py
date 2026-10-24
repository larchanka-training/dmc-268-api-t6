"""Generate, validate, cache, and trace repository conventions."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.application.languages import LANGUAGE_BY_SUFFIX, classify_languages
from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
    PolicySnapshot,
    RecoveryKind,
    SourceRole,
)
from app.modules.reviews.application.prompt_budget import verify_provenance_pre_send_guard
from app.modules.reviews.application.prompt_builder import ReviewRule

_STRICT = ConfigDict(extra="forbid", strict=True)
_MAX_CONTEXT_FILES = 10
_MAX_CONTEXT_BYTES = 128 * 1024
_MAX_CONTEXT_LINES = 300
# The model describes at most this many changed paths (``RepoConventionsDraft.files``);
# the rest of a large pull request gets the generic trace entry of a cache hit.
MAX_TRACED_FILES = 100
_RECOMMENDATION_SOURCE_SUFFIX = re.compile(
    r".*\(from: (standard/(security|correctness|performance|readability)|(?!standard/)[^()]+)\)"
)


@dataclass(frozen=True)
class RepositorySnapshot:
    """One revision-pinned repository text artifact."""

    content: str | None
    sha: str | None


@dataclass(frozen=True)
class RepositoryFile:
    """A repository tree entry, with content when it was selected for context."""

    path: str
    size: int
    content: str | None = None
    omitted_lines: int = 0


@dataclass(frozen=True)
class ActiveConventionsPrompt:
    """The deployed conventions contract selected independently of a review run."""

    id: UUID
    content: str


@dataclass(frozen=True)
class ConventionsRequest:
    """The complete immutable input envelope for one conventions-model call."""

    system: str
    rules: tuple[ReviewRule, ...]
    agents_md: str | None
    repo_tree: tuple[str, ...]
    repo_files: tuple[RepositoryFile, ...]
    languages: dict[str, int]
    changed_files: tuple[str, ...]
    omitted_tree_paths: int = 0
    snapshot: PolicySnapshot | None = field(default=None, compare=False, repr=False)
    agents_md_sha: str | None = field(default=None, compare=False, repr=False)

    @property
    def traced_files(self) -> tuple[str, ...]:
        """The changed paths the model must describe, in order."""
        return self.changed_files[:MAX_TRACED_FILES]


def build_conventions_manifest(
    request: ConventionsRequest,
    *,
    run_id: UUID | None = None,
    snapshot: PolicySnapshot | None = None,
    agents_md_sha: str | None = None,
    recovery_kind: RecoveryKind = "none",
) -> CallManifest:
    """Build the exact CallManifest for a conventions request (before or after fitting)."""
    effective_snapshot = snapshot if snapshot is not None else request.snapshot
    effective_sha = agents_md_sha if agents_md_sha is not None else request.agents_md_sha
    conventions_blocks: list[ManifestBlock] = []
    if request.agents_md:
        if (
            effective_snapshot is not None
            and effective_snapshot.agents.is_present
            and effective_snapshot.agents.text is not None
        ):
            agents_text = effective_snapshot.agents.text
        else:
            agents_text = (
                request.agents_md.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
            )
        agents_rev = (
            effective_snapshot.policy_base_sha if effective_snapshot is not None else effective_sha
        )
        agents_hash = sha256(agents_text.encode("utf-8")).hexdigest()
        lines_count = len(agents_text.splitlines()) or 1
        conventions_blocks.append(
            ManifestBlock(
                block_id="agents-md",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="project_conventions_document",
                path="AGENTS.md",
                revision=agents_rev,
                content_sha256=agents_hash,
                start_line=1,
                end_line=lines_count,
                shown_line_count=lines_count,
            )
        )
    if request.rules:
        defaults_lines = sum(len(r.checks) for r in request.rules) or 1
        conventions_blocks.append(
            ManifestBlock(
                block_id="service-defaults",
                source_role=SourceRole.SERVICE_DEFAULTS,
                source_kind="service_defaults",
                start_line=1,
                end_line=defaults_lines,
                shown_line_count=defaults_lines,
            )
        )
    for idx, cf in enumerate(request.repo_files):
        lines_count = len((cf.content or "").splitlines()) or 1
        conventions_blocks.append(
            ManifestBlock(
                block_id=f"file-{idx}",
                source_role=SourceRole.SOURCE_EXCERPT,
                source_kind="source_code",
                path=cf.path,
                start_line=1,
                end_line=lines_count,
                shown_line_count=lines_count,
            )
        )
    for idx, tree_path in enumerate(request.repo_tree):
        conventions_blocks.append(
            ManifestBlock(
                block_id=f"tree-{idx}",
                source_role=SourceRole.SOURCE_EXCERPT,
                source_kind="source_code",
                path=tree_path,
            )
        )
    for idx, cf_path in enumerate(request.changed_files):
        conventions_blocks.append(
            ManifestBlock(
                block_id=f"changed-{idx}",
                source_role=SourceRole.DIFF,
                source_kind="source_code",
                path=cf_path,
            )
        )
    return CallManifest(
        call_id=uuid4(),
        run_id=run_id,
        stage="conventions",
        recovery_kind=recovery_kind,
        blocks=tuple(conventions_blocks),
    )


class ConventionsFile(BaseModel):
    """One pull-request file that explains a generated convention."""

    model_config = _STRICT

    path: Annotated[str, Field(min_length=1, max_length=512)]
    relevance: Annotated[str, Field(min_length=1, max_length=1200)]


class ConventionsDraft(BaseModel):
    """The exact closed model contract before its cacheable parts are persisted."""

    model_config = _STRICT

    files: Annotated[list[ConventionsFile], Field(max_length=MAX_TRACED_FILES)]
    key_patterns: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=160)]],
        Field(min_length=3, max_length=10),
    ]
    recommendations: Annotated[
        list[Annotated[str, Field(min_length=1)]], Field(min_length=5, max_length=12)
    ]

    @field_validator("recommendations")
    @classmethod
    def recommendations_must_end_with_a_valid_source(cls, values: list[str]) -> list[str]:
        """Keep every cacheable recommendation attributable to a stable source."""
        if any(_RECOMMENDATION_SOURCE_SUFFIX.fullmatch(value.rstrip()) is None for value in values):
            raise ValueError(
                "recommendations must end with '(from: standard/<category>)' or "
                "'(from: <rule name>)'"
            )
        return values


@dataclass(frozen=True)
class CachedConventions:
    """The stable cache value, keyed by repository, AGENTS revision and prompt version."""

    repository_id: UUID
    agents_md_sha: str | None
    prompt_version_id: UUID
    key_patterns: tuple[str, ...]
    recommendations: tuple[str, ...]
    languages: dict[str, int]
    # Identity of the ignore policy the entry was generated under; ``None`` without one.
    policy_key: str | None = None


@dataclass(frozen=True)
class GeneratedConventions:
    """Outcome for one run, including whether a provider call was avoided."""

    conventions: CachedConventions
    agents_md: str | None
    cache_hit: bool


class RepositoryConventionsSource(Protocol):
    """Network boundary for immutable repository data."""

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot: ...

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]: ...

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]: ...


class PolicyFilteredConventionsSource:
    """A conventions source that never lists or loads a file the policy withholds.

    The masks are applied before files are selected and loaded, so an ignored file
    reaches the conventions model neither by its name nor by its content.
    """

    def __init__(self, inner: RepositoryConventionsSource, excluded: Callable[[str], bool]) -> None:
        self._inner = inner
        self._excluded = excluded

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return await self._inner.fetch_agents_md(repository_id)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        tree = await self._inner.fetch_tree(repository_id)
        return tuple(file for file in tree if not self._excluded(file.path))

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        allowed = tuple(path for path in paths if not self._excluded(path))
        files = await self._inner.fetch_files(repository_id, allowed)
        return tuple(file for file in files if not self._excluded(file.path))


class ConventionsModel(Protocol):
    """Network boundary for the constrained conventions draft."""

    async def draft_conventions(
        self,
        *,
        request: ConventionsRequest,
    ) -> Mapping[str, object]: ...


class RepositoryConventionsStore(Protocol):
    """Short database operations; implementations must not perform network calls."""

    async def get(
        self,
        repository_id: UUID,
        agents_md_sha: str | None,
        prompt_version_id: UUID,
        policy_key: str | None = None,
    ) -> CachedConventions | None: ...

    async def save_and_record_trace(
        self,
        run_id: UUID,
        conventions: CachedConventions,
        trace_files: tuple[ConventionsFile, ...],
    ) -> CachedConventions: ...


class RepositoryConventionsUnitOfWork(UnitOfWork, Protocol):
    """One caller-owned transaction for cache and trace writes."""

    @property
    def conventions(self) -> RepositoryConventionsStore: ...


class RepositoryConventionsUnitOfWorkFactory(Protocol):
    def __call__(self) -> RepositoryConventionsUnitOfWork: ...


class GenerateRepoConventions:
    """Generate a revision-qualified conventions cache entry for one review run."""

    def __init__(
        self,
        source: RepositoryConventionsSource,
        model: ConventionsModel,
        uow_factory: RepositoryConventionsUnitOfWorkFactory,
    ) -> None:
        self._source = source
        self._model = model
        self._uow_factory = uow_factory

    async def execute(
        self,
        *,
        repository_id: UUID,
        conventions_prompt: ActiveConventionsPrompt,
        run_id: UUID,
        changed_files: tuple[str, ...],
        rules: tuple[ReviewRule, ...] = (),
        policy_key: str | None = None,
        snapshot: PolicySnapshot | None = None,
    ) -> GeneratedConventions:
        # Fetching the revision happens outside every database operation; it is the
        # exact cache key and therefore cannot be inferred from a stale repository row.
        agents_md = await self._source.fetch_agents_md(repository_id)
        async with self._uow_factory() as uow:
            # Conventions generated under another ignore policy may describe files that
            # are withheld now, so the policy is part of the cache identity.
            cached = await uow.conventions.get(
                repository_id,
                agents_md.sha,
                conventions_prompt.id,
                **({} if policy_key is None else {"policy_key": policy_key}),
            )
        if cached is not None:
            async with self._uow_factory() as uow:
                await uow.conventions.save_and_record_trace(
                    run_id,
                    cached,
                    _trace_files_for_changed_paths(changed_files),
                )
                await uow.commit()
            return GeneratedConventions(cached, agents_md.content, cache_hit=True)

        tree = await self._source.fetch_tree(repository_id)
        languages = classify_languages(tree)
        selected = select_context_files(tree)
        fetched_files = await self._source.fetch_files(repository_id, selected)
        files = _bound_context_files(selected, fetched_files)

        request = ConventionsRequest(
            system=conventions_prompt.content,
            rules=rules,
            agents_md=agents_md.content,
            repo_tree=tuple(file.path for file in tree),
            repo_files=files,
            languages=languages,
            changed_files=changed_files,
            snapshot=snapshot,
            agents_md_sha=agents_md.sha,
        )
        conventions_manifest = build_conventions_manifest(
            request,
            run_id=run_id,
            snapshot=snapshot,
            agents_md_sha=agents_md.sha,
        )
        verify_provenance_pre_send_guard(conventions_manifest, snapshot=snapshot)

        raw_draft = await self._model.draft_conventions(request=request)
        draft = ConventionsDraft.model_validate(raw_draft)
        _validate_trace_files(draft.files, changed_files[:MAX_TRACED_FILES])
        conventions = CachedConventions(
            repository_id=repository_id,
            agents_md_sha=agents_md.sha,
            prompt_version_id=conventions_prompt.id,
            key_patterns=tuple(draft.key_patterns),
            recommendations=tuple(draft.recommendations),
            languages=languages,
            policy_key=policy_key,
        )
        async with self._uow_factory() as uow:
            saved = await uow.conventions.save_and_record_trace(
                run_id,
                conventions,
                (
                    *draft.files,
                    *_trace_files_for_changed_paths(changed_files[MAX_TRACED_FILES:]),
                ),
            )
            await uow.commit()
        return GeneratedConventions(saved, agents_md.content, cache_hit=False)


def derive_languages(files: tuple[RepositoryFile, ...]) -> dict[str, int]:
    """Compatibility wrapper for callers of the former reviews-local helper."""
    return classify_languages(files)


def select_context_files(files: tuple[RepositoryFile, ...]) -> tuple[str, ...]:
    """Select deterministic, bounded source files without fetching their contents yet."""
    selected: list[str] = []
    total = 0
    for file in sorted(files, key=lambda item: item.path):
        suffix = PurePosixPath(file.path).suffix.lower()
        if suffix not in LANGUAGE_BY_SUFFIX or file.size <= 0:
            continue
        if len(selected) >= _MAX_CONTEXT_FILES or total + file.size > _MAX_CONTEXT_BYTES:
            continue
        selected.append(file.path)
        total += file.size
    return tuple(selected)


def _bound_context_files(
    selected_paths: tuple[str, ...], fetched_files: tuple[RepositoryFile, ...]
) -> tuple[RepositoryFile, ...]:
    """Keep the model input to the selected files and its per-file line budget."""
    files_by_path = {file.path: file for file in fetched_files}
    bounded: list[RepositoryFile] = []
    for path in selected_paths:
        file = files_by_path.get(path)
        if file is None:
            continue
        if file.content is None:
            bounded.append(file)
            continue
        lines = file.content.splitlines(keepends=True)
        bounded.append(
            RepositoryFile(
                path=file.path,
                size=file.size,
                content="".join(lines[:_MAX_CONTEXT_LINES]),
                omitted_lines=file.omitted_lines + max(0, len(lines) - _MAX_CONTEXT_LINES),
            )
        )
    return tuple(bounded)


def _trace_files_for_changed_paths(paths: tuple[str, ...]) -> tuple[ConventionsFile, ...]:
    """Record this run's changed paths when conventions themselves are cached."""
    return tuple(
        ConventionsFile(
            path=path,
            relevance="Changed in this pull request; apply cached repository conventions.",
        )
        for path in paths
    )


def _validate_trace_files(files: list[ConventionsFile], changed_paths: tuple[str, ...]) -> None:
    """Reject a model trace that does not describe exactly this pull request."""
    if changed_paths and not files:
        raise ValueError(
            "conventions draft must trace at least one file for a changed pull request"
        )
    if tuple(file.path for file in files) != changed_paths:
        raise ValueError("conventions draft files must match the current pull request paths")
