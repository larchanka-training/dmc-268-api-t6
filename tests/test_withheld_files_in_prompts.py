"""A file the policy withholds reaches no model input, by its name or by its content."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import TracebackType
from uuid import UUID

import pytest

from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    ParseRequest,
)
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
    FileStatus,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import ReflectionPass, VerifierReply
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_path import ReviewBinding
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from tests.policy_snapshot_fakes import (
    make_complete_execution_profile,
    make_complete_prompt_bindings,
)
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
# A file renamed or copied to this name; the allowed file imports it, so AST and imports
# reach it unless it is withheld. Only the alias holds its text token. The module name stays
# in the allowed file's own import: RFS §5.1 removes no matching string from another file.
_ALIAS = "src/alias_target.py"
_ALIAS_BLOB = "8" * 40
_ALIAS_TEXT = "alias_secret_argument"
_REJECTED_OLD_NAME = "old_q9\nname.py"
_ALIAS_PATCH = f"@@ -1 +1 @@\n-def alias_helper(old):\n+def alias_helper({_ALIAS_TEXT}):"
_IMPORTER_PATCH = "@@ -4,1 +4,2 @@\n def greet(name):\n+    return alias_helper(name)"
_ALIAS_CONTENT = {
    "src/app.py": (
        "from .alias_target import alias_helper\n\n\n"
        "def greet(name):\n    return alias_helper(name)\n"
    ),
    _ALIAS: f"def alias_helper({_ALIAS_TEXT}):\n    return {_ALIAS_TEXT}\n",
}
# A copy keeps its source at the head, a rename does not; only the source holds this token.
_COPY_SOURCE_BLOB = "9" * 40
_COPY_SOURCE_TEXT = "copy_source_secret"
# The stored prompts the run pinned: the review binding and the pinned conventions prompt
# name the same rows as the pins.
_REVIEW_PROMPT = UUID(int=41)
_CONVENTIONS_PROMPT = UUID(int=43)


@dataclass
class GitHub(Vcs):
    files_changed: int = 3
    files: tuple[VcsFile, ...] = _FILES

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return self.files

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


@dataclass
class Source:
    content: Mapping[str, str] = field(default_factory=lambda: _CONTENT)

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return RepositorySnapshot(content=None, sha=None)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return tuple(RepositoryFile(path, len(text)) for path, text in self.content.items())

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return tuple(
            RepositoryFile(path, len(self.content[path]), self.content[path]) for path in paths
        )


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
    answer: dict[str, object] = field(default_factory=dict)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return self.meta

    async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
        self.contexts.append(context)
        return self.answer


class Publisher:
    async def execute(self, run_id: UUID, model_output: object) -> bool:
        return True


def test_removed_files_behind_a_mask_reach_neither_prompt_by_name_or_content() -> None:
    runs = Runs()
    github = GitHub()
    # One store for every unit of work: each policy document is staged in its own.
    store = Store()
    stage = ReviewContextStage(
        checkpoint=_checkpoint,
        runs=runs,
        vcs=github,
        merge_base=github,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Policy()
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


class PinnedRuns(Runs):
    """A run that pinned the v4 review prompt, so it takes the snapshot path and the verifier."""

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(
            _REPOSITORY,
            ActiveConventionsPrompt(UUID(int=7), "conventions"),
            review_binding=ReviewBinding(_REVIEW_PROMPT, "review.system", "v4", "github"),
            pinned_conventions_prompt=ActiveConventionsPrompt(
                _CONVENTIONS_PROMPT, "pinned conventions"
            ),
        )


@dataclass
class AliasRevision:
    """The revision the context build reads: every blob it asks for is recorded."""

    content: Mapping[str, str]
    blob_shas: Mapping[str, str]
    reads: list[str] = field(default_factory=list)

    async def tree(self, revision: str) -> RevisionTree:
        return RevisionTree(revision, self.blob_shas, complete=True)

    async def read_blob(self, blob_sha: str) -> bytes:
        self.reads.append(blob_sha)
        [path] = [path for path, sha in self.blob_shas.items() if sha == blob_sha]
        return self.content[path].encode()


@dataclass
class RecordingParser(Parser):
    sources: list[bytes] = field(default_factory=list)

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        self.sources.extend(item.source for item in requests)
        return await super().parse_many(requests)


@dataclass
class Verifier:
    """Abstains on every candidate; the prompts it was sent are recorded."""

    prompts: list[str] = field(default_factory=list)

    async def verify(self, *, system_prompt: str, user_prompt: str, **_: object) -> VerifierReply:
        self.prompts.append("\n".join((system_prompt, user_prompt)))
        decisions = [
            {
                "candidate_id": candidate,
                "status": "insufficient_context",
                "reason": "Not enough code.",
                "evidence_refs": [],
                "requirement_refs": [],
            }
            for candidate in re.findall(r'candidate candidate_id="([^"]+)"', user_prompt)
        ]
        return VerifierReply(output={"decisions": decisions})


def _finding(path: str, line: int) -> dict[str, object]:
    return {
        "path": path,
        "line": line,
        "start_line": None,
        "severity": "high",
        "category": "correctness",
        "title": f"Wrong value in {path}",
        "body": "The helper returns a constant.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
        "evidence_refs": [{"block_id": file_block_id(path), "start_line": line, "end_line": line}],
    }


@dataclass
class AliasRun:
    """What one attempt stored, read and sent, with every model call recorded."""

    stage: ReviewContextStage
    stored: list[DiffSnapshot]
    reads: list[str]
    parsed: list[bytes]
    conventions: ConventionsRequest
    context: ReviewContext
    verifier_prompts: list[str]

    @property
    def alias_row(self) -> DiffSnapshot:
        return next(row for row in self.stored if row.filename == _ALIAS)

    @property
    def prompts(self) -> tuple[str, ...]:
        """Every prompt a model is sent: conventions, review and the verifier call."""
        conventions = build_conventions_prompt(self.conventions)
        return (
            "\n".join((conventions.system, conventions.user)),
            PromptBuilder().build(self.context),
            *self.verifier_prompts,
        )

    def context_paths(self) -> set[str | None]:
        assert self.stage.payload is not None
        return {block.source.path for block in self.stage.payload.blocks}

    def import_resolutions(self) -> list[str]:
        assert self.stage.payload is not None
        return [ref.resolution.value for ref in self.stage.payload.imports]

    def parsed_text(self, token: str) -> bool:
        return any(token.encode() in source for source in self.parsed)


def _v4_store() -> Store:
    """Pins of the v4 review prompt, with the checksums of the prompts the pipeline reads."""
    pins = Store().pins
    assert pins is not None
    bindings = replace(
        make_complete_prompt_bindings(
            review_system_id=_REVIEW_PROMPT, conventions_id=_CONVENTIONS_PROMPT
        ),
        review_system_version="v4",
    )
    profile = make_complete_execution_profile()
    return Store(pins=replace(pins, prompt_bindings=bindings, execution_profile=profile))


def _alias_run(status: FileStatus, old_name: str) -> AliasRun:
    """One attempt through the worker's composition; only the ports are fakes."""
    files = (
        VcsFile("src/app.py", "modified", _APP_BLOB, None, 1, 0, 1, _IMPORTER_PATCH),
        VcsFile(_ALIAS, status, _ALIAS_BLOB, old_name, 1, 1, 2, _ALIAS_PATCH),
    )
    head = dict(_ALIAS_CONTENT)
    blob_shas = {"src/app.py": _APP_BLOB, _ALIAS: _ALIAS_BLOB}
    if status == "copied":
        head[old_name] = f"{_COPY_SOURCE_TEXT} = 1\n"
        blob_shas[old_name] = _COPY_SOURCE_BLOB
    runs = PinnedRuns()
    github = GitHub(files_changed=2, files=files)
    store = _v4_store()
    revision = AliasRevision(head, blob_shas)
    parser = RecordingParser()
    stage = ReviewContextStage(
        checkpoint=_checkpoint,
        runs=runs,
        vcs=github,
        merge_base=github,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Policy()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=revision, parser=parser, counter=EstimatedTokenCounter()
        ),
        trace=Trace(),
    )
    conventions_model = ConventionsModel()
    processor = ReviewRunProcessor(
        runs,
        None,
        conventions=GenerateRepoConventions(
            PolicyFilteredConventionsSource(Source(head), stage.excluded),
            conventions_model,
            ConventionsUow,
        ),
        vcs_provider=github,
        uow_factory=processing_uow(runs),
        context_stage=stage,
    )
    # The model cites both files: a candidate on a withheld file must not reach the verifier.
    answer: dict[str, object] = {"findings": [_finding("src/app.py", 5), _finding(_ALIAS, 1)]}
    meta = asyncio.run(github.get_pull_request(_LOCATOR, checkpoint=_checkpoint)).meta
    model = Model(meta, answer=answer)
    verifier = Verifier()
    pipeline = ExecuteReviewRun(
        processor,
        Prompts(runs),
        model,
        Publisher(),
        context_filter=stage,
        reflection_pass=ReflectionPass(verifier),
    )

    assert asyncio.run(pipeline.execute(_RUN)) is True

    assert runs.stored is not None
    [request] = conventions_model.requests
    [context] = model.contexts
    return AliasRun(
        stage, runs.stored, revision.reads, parser.sources, request, context, verifier.prompts
    )


# ``kept``: the head files besides the change; a copy keeps its source, a rename does not.
@pytest.mark.parametrize(
    ("status", "kept"),
    [("renamed", ()), ("copied", ("src/old_name.py",))],
    ids=["renamed", "copied"],
)
def test_new_alias_of_a_change_with_a_rejected_old_name_reaches_no_model_input(
    status: FileStatus, kept: tuple[str, ...]
) -> None:
    withheld = _alias_run(status, _REJECTED_OLD_NAME)
    shown = _alias_run(status, "src/old_name.py")

    # ``review_patch`` is the model's only patch. The store asks ``excludes_change`` before the
    # stage has seen the diff and later filters ask ``excluded``: only this row shows a lost
    # check of the old name.
    assert withheld.alias_row.review_patch is None
    assert shown.alias_row.review_patch == _ALIAS_PATCH
    assert withheld.stage.excluded(_ALIAS) is True
    assert shown.stage.excluded(_ALIAS) is False

    # AST and imports: the import of the alias is not followed and its blob is not read.
    assert withheld.import_resolutions() == ["excluded"]
    assert shown.import_resolutions() == ["resolved"]
    assert withheld.context_paths() == {"src/app.py"}
    assert shown.context_paths() == {"src/app.py", _ALIAS}
    assert _ALIAS_BLOB not in withheld.reads
    assert _ALIAS_BLOB in shown.reads
    assert not withheld.parsed_text(_ALIAS_TEXT)
    assert shown.parsed_text(_ALIAS_TEXT)

    # Conventions: neither listed nor loaded, and an entry cached while it was allowed is
    # not reused for the same masks. The source a copy keeps under the rejected name is not
    # listed either; under an allowed name it stays a file of its own.
    assert withheld.conventions.repo_tree == ("src/app.py",)
    assert shown.conventions.repo_tree == ("src/app.py", _ALIAS, *kept)
    assert [file.path for file in withheld.conventions.repo_files] == ["src/app.py"]
    assert [file.path for file in shown.conventions.repo_files] == [_ALIAS, "src/app.py", *kept]
    assert withheld.conventions.changed_files == ("src/app.py",)
    assert shown.conventions.changed_files == ("src/app.py", _ALIAS)
    assert withheld.stage.policy_key != shown.stage.policy_key

    # Review: dropped from both lists, not only moved to the omitted one.
    assert [file.path for file in withheld.context.changed_files] == ["src/app.py"]
    assert [file.path for file in shown.context.changed_files] == [_ALIAS, "src/app.py"]
    assert withheld.context.omitted_files == ()

    # Verifier: called once in both runs; what reaches it is checked with the prompts below.
    assert len(withheld.verifier_prompts) == len(shown.verifier_prompts) == 1

    # The same three prompts carry the alias when its old name is allowed, and the conventions
    # prompt carries the source a copy keeps, so the absence here is not an empty check.
    assert len(withheld.prompts) == len(shown.prompts) == 3
    for prompt in withheld.prompts:
        assert "src/app.py" in prompt
        for token in (_ALIAS, _ALIAS_TEXT, "old_q9", "name.py", _COPY_SOURCE_TEXT):
            assert token not in prompt
    for prompt in shown.prompts:
        assert _ALIAS in prompt
        assert _ALIAS_TEXT in prompt
    assert sum(_COPY_SOURCE_TEXT in prompt for prompt in shown.prompts) == len(kept)
