"""A removed file the policy withholds reaches neither the review nor the conventions prompt."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from uuid import UUID

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    ConventionsFile,
    ConventionsRequest,
    GeneratedConventions,
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.conventions_prompt import build_conventions_prompt
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewPromptInput
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunConventionsInput,
    RunVcsInput,
)
from app.modules.reviews.application.prompt_builder import (
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
)
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from tests.test_prepare_policy_snapshot import Store, Uow
from tests.test_review_context_stage import (
    _BASE,
    _HEAD,
    _LOCATOR,
    _PATCH,
    _RUN,
    _SOURCE,
    Parser,
    Policy,
    Repository,
    Trace,
    Vcs,
    _checkpoint,
)
from tests.trigger_uow import processing_uow

_CODE_CHANGE = UUID(int=5)
_REPOSITORY = UUID(int=1)
_APP_BLOB = "d" * 40
# The default policy of the stage fakes ignores ``generated/**``. Neither removed file has
# a blob, and the second name holds the `` b/`` a diff header parser can cut at.
_FILES = (
    VcsFile("src/app.py", "modified", _APP_BLOB, None, 1, 0, 1, _PATCH),
    VcsFile("generated/gone.py", "removed", None, None, 0, 1, 1, "@@ -1 +0,0 @@\n-GONE_SECRET = 1"),
    VcsFile("generated/q b/r.py", "removed", None, None, 0, 1, 1, "@@ -1 +0,0 @@\n-QBR_SECRET = 2"),
)
# The base of the change still holds the removed files.
_CONTENT = {
    "src/app.py": _SOURCE,
    "generated/gone.py": "GONE_SECRET = 1\n",
    "generated/q b/r.py": "QBR_SECRET = 2\n",
}


@dataclass
class GitHub(Vcs):
    files_changed: int = 3

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return _FILES

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        return _SOURCE.encode()


@dataclass
class Runs:
    """One run: the processor stores its snapshot and the stage reads it back."""

    stored: list[DiffSnapshot] | None = None

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("the VCS path reads the run locator")

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(_CODE_CHANGE, _REPOSITORY, _HEAD, _BASE, _LOCATOR)

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return self.stored

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        self.stored = snapshots
        return snapshots

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(_REPOSITORY, ActiveConventionsPrompt(UUID(int=7), "conventions"))


class Source:
    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return RepositorySnapshot(content=None, sha=None)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return tuple(RepositoryFile(path, len(text)) for path, text in _CONTENT.items())

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return tuple(RepositoryFile(path, len(_CONTENT[path]), _CONTENT[path]) for path in paths)


class ConventionsStore:
    async def get(
        self,
        repository_id: UUID,
        agents_md_sha: str | None,
        prompt_version_id: UUID,
        policy_key: str | None = None,
    ) -> CachedConventions | None:
        return None

    async def save_and_record_trace(
        self,
        run_id: UUID,
        conventions: CachedConventions,
        trace_files: tuple[ConventionsFile, ...],
    ) -> CachedConventions:
        return conventions


@dataclass
class ConventionsUow:
    conventions: ConventionsStore = field(default_factory=ConventionsStore)

    async def __aenter__(self) -> ConventionsUow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class ConventionsModel:
    requests: list[ConventionsRequest] = field(default_factory=list)

    async def draft_conventions(self, *, request: ConventionsRequest) -> dict[str, object]:
        self.requests.append(request)
        return {
            # The draft traces exactly the paths it was given, whatever they are.
            "files": [{"path": path, "relevance": "Changed."} for path in request.traced_files],
            "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
            "recommendations": [
                f"Recommendation {number} (from: standard/correctness)" for number in "12345"
            ],
        }


@dataclass
class Prompts:
    runs: Runs

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput:
        # As ``SqlAlchemyReviewPromptRepository``: the stored rows by name, projected.
        assert self.runs.stored is not None
        changed, omitted = review_files_from_snapshots(
            sorted(self.runs.stored, key=lambda row: row.filename)
        )
        return ReviewPromptInput(
            system="system",
            rules=(),
            agents_md=conventions.agents_md,
            conventions=RepoConventions(
                conventions.conventions.key_patterns, conventions.conventions.recommendations
            ),
            changed_files=changed,
            omitted_files=omitted,
        )


@dataclass
class Model:
    meta: PullRequestMeta
    contexts: list[ReviewContext] = field(default_factory=list)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return self.meta

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        self.contexts.append(context)
        return {}


class Publisher:
    async def execute(self, run_id: UUID, model_output: object) -> bool:
        return True


def test_removed_files_behind_a_mask_reach_neither_prompt_by_name_or_content() -> None:
    runs = Runs()
    github = GitHub()
    stage = ReviewContextStage(
        checkpoint=_checkpoint,
        runs=runs,
        vcs=github,
        merge_base=github,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(Store()), source_factory=lambda pins: Policy()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=Repository(), parser=Parser(), counter=EstimatedTokenCounter()
        ),
        trace=Trace(),
    )
    conventions_model = ConventionsModel()
    processor = ReviewRunProcessor(
        runs,
        None,
        conventions=GenerateRepoConventions(
            PolicyFilteredConventionsSource(Source(), stage.excluded),
            conventions_model,
            ConventionsUow,
        ),
        vcs_provider=github,
        uow_factory=processing_uow(runs),
        context_stage=stage,
    )
    meta = asyncio.run(github.get_pull_request(_LOCATOR, checkpoint=_checkpoint)).meta
    model = Model(meta)
    pipeline = ExecuteReviewRun(processor, Prompts(runs), model, Publisher(), context_filter=stage)

    assert asyncio.run(pipeline.execute(_RUN)) is True

    [context] = model.contexts
    [request] = conventions_model.requests
    review_prompt = PromptBuilder().build(context)
    conventions_prompt = build_conventions_prompt(request).user
    for text in (review_prompt, conventions_prompt):
        for withheld in ("gone.py", "q b/r.py", "GONE_SECRET", "QBR_SECRET"):
            assert withheld not in text
        assert "src/app.py" in text
        assert "extra = 1" in text
    assert [file.path for file in context.changed_files] == ["src/app.py"]
    assert context.omitted_files == ()
    assert request.changed_files == ("src/app.py",)
