"""Review context L1-L4: what is shown, what is withheld, and why."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from app.modules.reviews.application.ast_resolution import ImportResolution, RevisionTree
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    AstLanguage,
    ParseRequest,
)
from app.modules.reviews.application.context_provenance import LineRange, SourceRole
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.policy_documents import missing_document
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    ContextStatus,
    Representation,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_HEAD = "a" * 40
_REPOSITORY = UUID(int=1)

_SERVICE = '''from .models import User
from .secret import token


def unrelated():
    return 0


def greet(user_id):
    """Greet a user."""
    user = User(user_id)
    extra = 1
    return "hi " + user.name
'''
_MODELS = '''class User:
    """A user of the system."""

    def __init__(self, user_id):
        self.name = str(user_id)
'''
_GREET_PATCH = (
    "@@ -9,4 +9,5 @@\n def greet(user_id):\n"
    '     """Greet a user."""\n     user = User(user_id)\n+    extra = 1\n'
    '     return "hi " + user.name'
)


class Words:
    """One token per whitespace-separated word: easy to reason about in a test."""

    def count(self, text: str) -> int:
        return len(text.split())


@dataclass
class Repository:
    files: dict[str, str]
    complete: bool = True
    read: list[str] = field(default_factory=list)

    def sha(self, path: str) -> str:
        return f"{abs(hash(path)) % 10**12:040d}"

    async def tree(self, revision: str) -> RevisionTree:
        assert revision == _HEAD
        return RevisionTree(revision, {path: self.sha(path) for path in self.files}, self.complete)

    async def read_blob(self, blob_sha: str) -> bytes:
        path = next(item for item in self.files if self.sha(item) == blob_sha)
        self.read.append(path)
        return self.files[path].encode()


@dataclass
class InProcessParser:
    parsed: list[str] = field(default_factory=list)
    closed: int = 0

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        self.parsed.extend(request.key for request in requests)
        return {
            request.key: extract_facts(request.language, request.source) for request in requests
        }

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class MemoryCache:
    entries: dict[tuple[str, AstLanguage], AstFacts] = field(default_factory=dict)

    async def get(self, blob_sha: str, language: AstLanguage) -> AstFacts | None:
        return self.entries.get((blob_sha, language))

    async def put(self, blob_sha: str, language: AstLanguage, facts: AstFacts) -> None:
        self.entries[(blob_sha, language)] = facts


def _vcs_file(
    repository: Repository,
    path: str,
    patch: str | None,
    *,
    additions: int = 1,
    deletions: int = 0,
    status: FileStatus = "modified",
    previous: str | None = None,
) -> VcsFile:
    return VcsFile(
        filename=path,
        status=status,
        blob_sha=None if status == "removed" else repository.sha(path),
        previous_filename=previous,
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch=patch,
    )


def _diff_map(*files: VcsFile, listed: int | None = None) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha=_HEAD,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="t",
            description=None,
            author="octo",
            source_branch="f",
            target_branch="main",
            labels=(),
            files_changed=len(files) if listed is None else listed,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=_REPOSITORY,
        diff_old_sha="c" * 40,
    )


def _build(
    repository: Repository,
    diff_map: DiffMap,
    *,
    rules_text: str | None = None,
    budget: int = 100_000,
    parser: InProcessParser | None = None,
    cache: MemoryCache | None = None,
) -> ReviewContextPayload:
    policy = parse_review_ignore(rules_text)
    snapshot = build_policy_snapshot(
        RunPolicyPins(UUID(int=2), _REPOSITORY, "b" * 40, UUID(int=4), "2" * 64, {}, {}),
        rules=missing_document(RULES_DOCUMENT_PATH),
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=policy,
        snapshot_id=UUID(int=3),
    )
    matcher = IgnoreMatcher(policy)
    builder = BuildReviewContext(
        source=repository, parser=parser or InProcessParser(), counter=Words(), cache=cache
    )
    return asyncio.run(
        builder.execute(
            diff_map=diff_map,
            snapshot=snapshot,
            matcher=matcher,
            ignore=apply_ignore(matcher, diff_map),
            token_budget=budget,
        )
    )


def _service_repository(**extra: str) -> Repository:
    return Repository(
        {
            "pkg/service.py": _SERVICE,
            "pkg/models.py": _MODELS,
            "pkg/secret.py": "token = 1\n",
            **extra,
        }
    )


def test_changed_function_gets_diff_full_file_and_imported_signature() -> None:
    repository = _service_repository()
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert payload.status is ContextStatus.REVIEW
    assert payload.levels_present == (ContextLevel.L1, ContextLevel.L3, ContextLevel.L4)
    by_level = {block.level: block for block in payload.blocks}
    diff = by_level[ContextLevel.L1]
    assert diff.source.role is SourceRole.DIFF
    assert diff.source.ranges == (LineRange(9, 13),)
    assert "+    extra = 1" in diff.text
    full = by_level[ContextLevel.L3]
    assert (full.source.ranges, full.source.reason) == ((LineRange(1, 13),), "full_file")
    imported = by_level[ContextLevel.L4]
    assert imported.source.role is SourceRole.AST_EXCERPT
    assert (imported.source.path, imported.representation) == (
        "pkg/models.py",
        Representation.SIGNATURE,
    )
    # The signature and the docstring are shown, the body of the class is not.
    assert imported.text == 'class User:\n    """A user of the system."""'
    assert imported.source.ranges == (LineRange(1, 2),)
    assert payload.tokens_used == sum(block.tokens for block in payload.blocks)
    assert (payload.diff_digest, payload.policy_snapshot_id) == (diff_map.digest, UUID(int=3))


def test_import_not_used_by_the_changed_function_is_not_read() -> None:
    repository = _service_repository()
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert [ref.module for ref in payload.imports] == [".models"]
    assert "pkg/secret.py" not in repository.read


def test_ignored_file_is_counted_not_named_and_never_read_through_an_import() -> None:
    repository = _service_repository()
    repository.files["generated/client.py"] = "x = 1\n"
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/service.py", _GREET_PATCH),
        _vcs_file(repository, "generated/client.py", "@@ -1 +1 @@\n-x = 0\n+x = 1", deletions=1),
    )

    payload = _build(
        repository, diff_map, rules_text="```review-ignore\ngenerated/**\npkg/models.py\n```\n"
    )

    assert payload.ignored_files == 1
    assert [(item.reason, item.path, item.count) for item in payload.omissions] == [
        ("ignored", None, 1)
    ]
    assert all(block.source.path != "generated/client.py" for block in payload.blocks)
    assert [(ref.module, ref.resolution) for ref in payload.imports] == [
        (".models", ImportResolution.EXCLUDED)
    ]
    assert ContextLevel.L4 not in payload.levels_present
    assert "pkg/models.py" not in repository.read
    assert "generated/client.py" not in repository.read


def test_file_renamed_out_of_an_ignored_path_stays_excluded_everywhere() -> None:
    repository = _service_repository()
    repository.files["pkg/models.py"] = _MODELS
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/service.py", _GREET_PATCH),
        _vcs_file(
            repository,
            "pkg/models.py",
            "@@ -1 +1 @@\n-class Old:\n+class User:",
            deletions=1,
            status="renamed",
            previous="generated/models.py",
        ),
    )

    payload = _build(repository, diff_map, rules_text="```review-ignore\ngenerated/**\n```\n")

    assert payload.ignored_files == 1
    assert all(block.source.path != "pkg/models.py" for block in payload.blocks)
    assert payload.imports[0].resolution is ImportResolution.EXCLUDED
    assert "pkg/models.py" not in repository.read


def test_all_changes_ignored_and_an_empty_diff_are_their_own_outcomes() -> None:
    repository = _service_repository()
    only = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    ignored = _build(repository, only, rules_text="```review-ignore\n**\n```\n")
    assert (ignored.status, ignored.blocks, ignored.reviewable_lines) == (
        ContextStatus.ALL_CHANGES_IGNORED,
        (),
        0,
    )
    assert repository.read == []

    assert _build(repository, _diff_map()).status is ContextStatus.NO_CHANGES


def test_ignored_lines_do_not_count_toward_the_summary_only_threshold() -> None:
    repository = _service_repository()
    big = "@@ -0,0 +1,4000 @@\n" + "\n".join("+x" for _ in range(4000))
    edge = "@@ -0,0 +1,2999 @@\n" + "\n".join("+x" for _ in range(2999))
    small = _vcs_file(repository, "pkg/service.py", _GREET_PATCH)
    repository.files["generated/big.py"] = "x\n"
    repository.files["pkg/edge.txt"] = "x\n" * 2999
    generated = _vcs_file(repository, "generated/big.py", big, additions=4000, status="added")

    ignored = _build(
        repository,
        _diff_map(small, generated),
        rules_text="```review-ignore\ngenerated/**\n```\n",
    )
    assert (ignored.status, ignored.reviewable_lines) == (ContextStatus.REVIEW, 1)

    too_large = _build(repository, _diff_map(small, generated))
    assert (too_large.status, too_large.reason, too_large.blocks) == (
        ContextStatus.SUMMARY_ONLY,
        "diff_too_large",
        (),
    )

    exactly = _build(
        repository,
        _diff_map(
            small,
            _vcs_file(repository, "pkg/edge.txt", edge, additions=2999, status="added"),
        ),
    )
    assert (exactly.status, exactly.reviewable_lines) == (ContextStatus.REVIEW, 3000)


def test_incomplete_file_list_gives_summary_only_without_line_coverage() -> None:
    repository = _service_repository()
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH), listed=2)

    payload = _build(repository, diff_map)

    assert (payload.status, payload.reason, payload.blocks) == (
        ContextStatus.SUMMARY_ONLY,
        "diff_inventory_incomplete",
        (),
    )


def test_unusable_files_are_withheld_with_a_reason_and_the_rest_is_reviewed() -> None:
    repository = _service_repository()
    repository.files["src\\bad.py"] = "x = 1\n"
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/service.py", _GREET_PATCH),
        _vcs_file(repository, "src\\bad.py", "@@ -1 +1 @@\n-x = 0\n+x = 1", deletions=1),
        _vcs_file(repository, "pkg/secret.py", None, additions=3),
    )

    payload = _build(repository, diff_map)

    assert payload.status is ContextStatus.REVIEW
    assert {(item.reason, item.path) for item in payload.omissions} == {
        ("unsupported_path", "src\\bad.py"),
        ("patch_missing", "pkg/secret.py"),
    }
    assert {block.source.path for block in payload.blocks if block.level is ContextLevel.L1} == {
        "pkg/service.py"
    }


def test_tight_budget_keeps_the_diff_and_marks_a_cut_hunk() -> None:
    repository = _service_repository()
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    # The hunk is 21 words; nothing else fits after it.
    diff_only = _build(repository, diff_map, budget=21)
    assert diff_only.levels_present == (ContextLevel.L1,)
    assert diff_only.blocks[0].truncated is False
    assert {item.reason for item in diff_only.omissions} == {"ast_limit", "budget"}

    cut = _build(repository, diff_map, budget=12)
    assert [(block.level, block.truncated) for block in cut.blocks] == [(ContextLevel.L1, True)]
    assert cut.blocks[0].tokens <= 12
    assert repository.read.count("pkg/models.py") == 1


def test_large_file_gets_the_changed_function_instead_of_the_whole_file() -> None:
    filler = "".join(f"\n\ndef filler_{n}():\n    return {n}\n" for n in range(400))
    source = _SERVICE + filler
    repository = _service_repository(**{"pkg/service.py": source})
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert ContextLevel.L3 not in payload.levels_present
    window = next(block for block in payload.blocks if block.level is ContextLevel.L2)
    # The enclosing function is shown whole, not an arbitrary window around the hunk.
    assert window.source.ranges == (LineRange(9, 13),)
    assert window.text.startswith("def greet(user_id):")


def test_change_outside_any_declaration_gets_a_line_window() -> None:
    lines = [f"value_{n} = {n}" for n in range(1, 2001)]
    lines[999] = "print(value_1)"
    repository = Repository({"data.py": "\n".join(lines) + "\n"})
    patch = (
        "@@ -999,3 +999,3 @@\n value_999 = 999\n-value_1000 = 1000\n"
        "+print(value_1)\n value_1001 = 1001"
    )
    diff_map = _diff_map(_vcs_file(repository, "data.py", patch, deletions=1))

    payload = _build(repository, diff_map)

    window = next(block for block in payload.blocks if block.level is ContextLevel.L2)
    # The file is over 1500 lines: the window widens to 80 lines on each side.
    assert window.source.ranges == (LineRange(919, 1081),)


def test_language_without_a_grammar_keeps_line_based_context() -> None:
    repository = Repository({"notes.md": "# Title\n\ntext\n"})
    diff_map = _diff_map(_vcs_file(repository, "notes.md", "@@ -3 +3 @@\n-old\n+text", deletions=1))
    parser = InProcessParser()

    payload = _build(repository, diff_map, parser=parser)

    assert payload.levels_present == (ContextLevel.L1, ContextLevel.L3)
    assert parser.parsed == []
    assert payload.imports == ()


def test_deleted_file_is_shown_as_a_diff_of_the_old_side_only() -> None:
    repository = _service_repository()
    removed = _vcs_file(
        repository,
        "pkg/gone.py",
        "@@ -1,2 +0,0 @@\n-a = 1\n-b = 2",
        additions=0,
        deletions=2,
        status="removed",
    )

    payload = _build(repository, _diff_map(removed))

    assert payload.levels_present == (ContextLevel.L1,)
    assert payload.blocks[0].source.path == "pkg/gone.py"
    assert payload.blocks[0].source.revision == "c" * 40


def test_cached_facts_skip_the_parser_but_not_the_ignore_guard() -> None:
    repository = _service_repository()
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))
    cache = MemoryCache()

    first = InProcessParser()
    _build(repository, diff_map, parser=first, cache=cache)
    assert sorted(first.parsed) == ["pkg/models.py", "pkg/service.py"]
    assert first.closed == 1

    second = InProcessParser()
    again = _build(repository, diff_map, parser=second, cache=cache)
    assert second.parsed == []
    assert ContextLevel.L4 in again.levels_present

    guarded = _build(
        repository,
        diff_map,
        parser=InProcessParser(),
        cache=cache,
        rules_text="```review-ignore\npkg/models.py\n```\n",
    )
    assert ContextLevel.L4 not in guarded.levels_present
    assert guarded.imports[0].resolution is ImportResolution.EXCLUDED
