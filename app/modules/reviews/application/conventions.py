"""Generate, validate, cache, and trace repository conventions."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.prompt_builder import ReviewRule

_STRICT = ConfigDict(extra="forbid", strict=True)
_MAX_CONTEXT_FILES = 12
_MAX_CONTEXT_BYTES = 128 * 1024
_RECOMMENDATION_SOURCE_SUFFIX = re.compile(
    r".*\(from: (standard/(security|correctness|performance|readability)|(?!standard/)[^()]+)\)"
)

_LANGUAGE_BY_SUFFIX = {
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JSX",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".svelte": "Svelte",
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".vue": "Vue",
}


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


class ConventionsFile(BaseModel):
    """One pull-request file that explains a generated convention."""

    model_config = _STRICT

    path: Annotated[str, Field(min_length=1, max_length=512)]
    relevance: Annotated[str, Field(min_length=1, max_length=1200)]


class ConventionsDraft(BaseModel):
    """The exact closed model contract before its cacheable parts are persisted."""

    model_config = _STRICT

    files: Annotated[list[ConventionsFile], Field(max_length=100)]
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
        self, repository_id: UUID, agents_md_sha: str | None, prompt_version_id: UUID
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
    ) -> GeneratedConventions:
        # Fetching the revision happens outside every database operation; it is the
        # exact cache key and therefore cannot be inferred from a stale repository row.
        agents_md = await self._source.fetch_agents_md(repository_id)
        async with self._uow_factory() as uow:
            cached = await uow.conventions.get(repository_id, agents_md.sha, conventions_prompt.id)
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
        languages = derive_languages(tree)
        selected = select_context_files(tree)
        files = await self._source.fetch_files(repository_id, selected)
        raw_draft = await self._model.draft_conventions(
            request=ConventionsRequest(
                system=conventions_prompt.content,
                rules=rules,
                agents_md=agents_md.content,
                repo_tree=tuple(file.path for file in tree),
                repo_files=files,
                languages=languages,
                changed_files=changed_files,
            )
        )
        draft = ConventionsDraft.model_validate(raw_draft)
        _validate_trace_files(draft.files, changed_files)
        conventions = CachedConventions(
            repository_id=repository_id,
            agents_md_sha=agents_md.sha,
            prompt_version_id=conventions_prompt.id,
            key_patterns=tuple(draft.key_patterns),
            recommendations=tuple(draft.recommendations),
            languages=languages,
        )
        async with self._uow_factory() as uow:
            saved = await uow.conventions.save_and_record_trace(
                run_id,
                conventions,
                tuple(draft.files),
            )
            await uow.commit()
        return GeneratedConventions(saved, agents_md.content, cache_hit=False)


def derive_languages(files: tuple[RepositoryFile, ...]) -> dict[str, int]:
    """Return deterministic whole-percent language shares from repository byte sizes.

    Unknown extensions never affect the denominator.  Largest-remainder allocation
    sums to 100; exact ties use the language name, so two equivalent trees always
    produce the same cache value regardless of provider ordering.
    """
    totals: defaultdict[str, int] = defaultdict(int)
    for file in files:
        language = _LANGUAGE_BY_SUFFIX.get(PurePosixPath(file.path).suffix.lower())
        if language is not None and file.size > 0:
            totals[language] += file.size
    total = sum(totals.values())
    if total == 0:
        return {}

    percentages = {language: size * 100 // total for language, size in totals.items()}
    remainder = 100 - sum(percentages.values())
    ranked = sorted(
        totals,
        key=lambda language: (-(totals[language] * 100 % total), language),
    )
    for language in ranked[:remainder]:
        percentages[language] += 1
    return {language: percentages[language] for language in sorted(percentages)}


def select_context_files(files: tuple[RepositoryFile, ...]) -> tuple[str, ...]:
    """Select deterministic, bounded source files without fetching their contents yet."""
    selected: list[str] = []
    total = 0
    for file in sorted(files, key=lambda item: item.path):
        suffix = PurePosixPath(file.path).suffix.lower()
        if suffix not in _LANGUAGE_BY_SUFFIX or file.size <= 0:
            continue
        if len(selected) == _MAX_CONTEXT_FILES or total + file.size > _MAX_CONTEXT_BYTES:
            continue
        selected.append(file.path)
        total += file.size
    return tuple(selected)


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
