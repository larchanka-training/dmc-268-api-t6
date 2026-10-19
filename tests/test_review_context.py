"""Review context L1-L4: what is shown, what is withheld, and why."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Literal
from uuid import UUID

import pytest

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
    ExecutionProfile,
    PromptBindings,
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
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    _is_generated,
)
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
    builtin_excluded: Callable[[str], bool] = lambda path: False,
) -> ReviewContextPayload:
    policy = parse_review_ignore(rules_text)
    snapshot = build_policy_snapshot(
        RunPolicyPins(
            UUID(int=2),
            _REPOSITORY,
            "b" * 40,
            UUID(int=4),
            "2" * 64,
            PromptBindings("3" * 64, "", ""),
            ExecutionProfile.fast("model-a", ""),
        ),
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
            ignore=apply_ignore(matcher, diff_map, builtin_excluded=builtin_excluded),
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


def test_file_renamed_out_of_a_builtin_exclusion_is_never_read_through_an_import() -> None:
    # No mask of the repository: only the built-in exclusion of ``dist/``.
    repository = _service_repository()
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/service.py", _GREET_PATCH),
        _vcs_file(
            repository,
            "pkg/models.py",
            "@@ -1 +1 @@\n-class Old:\n+class User:",
            deletions=1,
            status="renamed",
            previous="dist/models.py",
        ),
    )

    payload = _build(repository, diff_map, builtin_excluded=_is_generated)

    assert payload.ignored_files == 1
    assert all(block.source.path != "pkg/models.py" for block in payload.blocks)
    assert [ref.resolution for ref in payload.imports] == [ImportResolution.EXCLUDED]
    assert ContextLevel.L4 not in payload.levels_present
    assert "pkg/models.py" not in repository.read


def _go_repository(members: int) -> Repository:
    main = 'package main\n\nimport "example.com/m/pkg"\n\nfunc main() {\n\tpkg.Foo()\n}\n'
    files = {"go.mod": "module example.com/m\n\ngo 1.22\n", "cmd/main.go": main}
    for index in range(members):
        name = "Foo" if index == 0 else f"Other{index}"
        files[f"pkg/f{index:02d}.go"] = (
            f"package pkg\n\n// {name} does a thing.\nfunc {name}() {{}}\n"
        )
    return Repository(files)


def test_go_mod_counts_toward_the_limit_of_imported_files() -> None:
    patch = "@@ -5,2 +5,3 @@\n func main() {\n+\tpkg.Foo()\n }"

    def build(members: int) -> tuple[ReviewContextPayload, Repository]:
        repository = _go_repository(members)
        return _build(
            repository, _diff_map(_vcs_file(repository, "cmd/main.go", patch))
        ), repository

    payload, repository = build(19)
    # ``go.mod`` and 19 members of the package: exactly the limit of 20 files.
    assert [ref.resolution for ref in payload.imports] == [ImportResolution.RESOLVED]
    assert ContextLevel.L4 in payload.levels_present

    payload, repository = build(20)
    # One file over the limit: the package is not fully known, so nothing is resolved.
    assert [(ref.resolution, ref.reason) for ref in payload.imports] == [
        (ImportResolution.UNSUPPORTED, "ast_limit")
    ]
    assert ContextLevel.L4 not in payload.levels_present
    assert len([path for path in repository.read if path != "cmd/main.go"]) <= 20


def test_changed_file_past_the_ast_limit_is_not_parsed_through_an_import() -> None:
    # 51 changed files; the first one imports the last, which is past the limit of 50.
    files = {f"pkg/m{n:02d}.py": f"def f{n}():\n    return {n}\n" for n in range(1, 51)}
    files["pkg/m00.py"] = "from .m50 import f50\n\n\ndef f0():\n    return f50()\n"
    repository = Repository(dict(sorted(files.items())))
    patches = {
        path: "@@ -1,2 +1,2 @@\n def f{0}():\n-    return 0\n+    return {0}".format(int(path[5:7]))
        for path in repository.files
    }
    patches["pkg/m00.py"] = "@@ -4,2 +4,2 @@\n def f0():\n-    return 0\n+    return f50()"
    diff_map = _diff_map(
        *(_vcs_file(repository, path, patches[path], deletions=1) for path in repository.files)
    )
    parser = InProcessParser()

    payload = _build(repository, diff_map, parser=parser)

    assert "pkg/m50.py" not in parser.parsed
    assert len(parser.parsed) == 50
    assert all(ref.resolution is not ImportResolution.RESOLVED for ref in payload.imports)


def test_import_target_with_a_parse_error_keeps_the_parse_reason_not_an_l4_limit() -> None:
    # A syntax error over the imported class: the import is not resolved, so the target
    # is never cut from L4 as if it were too large.
    repository = _service_repository()
    repository.files["pkg/models.py"] = _MODELS.replace("class User:", "class User(:")
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert [(ref.resolution, ref.reason) for ref in payload.imports] == [
        (ImportResolution.UNSUPPORTED, "ast_failed")
    ]
    assert ("ast_limit", "pkg/models.py") not in [
        (item.reason, item.path) for item in payload.omissions
    ]
    assert ContextLevel.L4 not in payload.levels_present


def _docstring(words: int, *, lines: int = 1) -> str:
    """An indented docstring of ``words`` words (one per line when ``lines`` > 1)."""
    if lines == 1:
        return '    """' + " ".join(["w"] * words) + '"""\n'
    assert words == lines
    return '    """w\n' + "    w\n" * (lines - 2) + '    w"""\n'


def _sized(text: str, size: int) -> str:
    """``text`` and a trailing comment line, ``size`` bytes in UTF-8 in total."""
    padded = text + "#" * (size - len(text.encode()) - 1) + "\n"
    assert len(padded.encode()) == size
    return padded


@pytest.mark.parametrize(
    ("words", "shown"),
    [
        # 4 x 2000 fill the total exactly; the fifth fragment, 2 + 1 = 3 tokens, is cut.
        pytest.param((1998, 1998, 1998, 1998, 1), [2000, 2000, 2000, 2000], id="exact"),
        # 7999 are shown; the fifth, a bare 2-token signature, does not fit in the 1 left.
        pytest.param((1998, 1998, 1998, 1997, None), [2000, 2000, 2000, 1999], id="one-short"),
    ],
)
def test_imported_signatures_stop_at_the_l4_total_of_8000_tokens(
    words: tuple[int | None, ...], shown: list[int]
) -> None:
    # A signature `def fN():` is 2 words and a docstring of k words adds k: one token a word.
    library = "".join(
        f"def f{n}():\n{'' if count is None else _docstring(count)}    return {n}\n\n\n"
        for n, count in enumerate(words)
    )
    repository = Repository(
        {
            "pkg/app.py": (
                "from .lib import f0, f1, f2, f3, f4\n\n\n"
                "def run():\n    x = 1\n    return f0() + f1() + f2() + f3() + f4()\n"
            ),
            "pkg/lib.py": library,
        }
    )
    patch = "@@ -4,2 +4,3 @@\n def run():\n+    x = 1\n     return f0() + f1() + f2() + f3() + f4()"
    diff_map = _diff_map(_vcs_file(repository, "pkg/app.py", patch))

    payload = _build(repository, diff_map)

    imported = [block for block in payload.blocks if block.level is ContextLevel.L4]
    assert [block.source.ranges for block in imported] == [
        (LineRange(1, 2),),
        (LineRange(6, 7),),
        (LineRange(11, 12),),
        (LineRange(16, 17),),
    ]
    assert [block.tokens for block in imported] == shown
    assert [(item.reason, item.path) for item in payload.omissions] == [("ast_limit", "pkg/lib.py")]


@pytest.mark.parametrize(
    "docstring",
    [
        pytest.param(_docstring(200, lines=200), id="201-lines"),
        pytest.param(_docstring(1999), id="2001-tokens"),
    ],
)
def test_imported_signature_over_the_fragment_limit_is_not_shown(docstring: str) -> None:
    repository = _service_repository()
    repository.files["pkg/models.py"] = "class User:\n" + docstring
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert [ref.resolution for ref in payload.imports] == [ImportResolution.RESOLVED]
    assert ("ast_limit", "pkg/models.py") in [
        (item.reason, item.path) for item in payload.omissions
    ]
    assert ContextLevel.L4 not in payload.levels_present


@pytest.mark.parametrize(
    ("docstring", "ranges", "tokens"),
    [
        pytest.param(_docstring(199, lines=199), (LineRange(1, 200),), 201, id="200-lines"),
        pytest.param(_docstring(1998), (LineRange(1, 2),), 2000, id="2000-tokens"),
    ],
)
def test_imported_signature_at_the_fragment_limit_is_shown(
    docstring: str, ranges: tuple[LineRange, ...], tokens: int
) -> None:
    repository = _service_repository()
    repository.files["pkg/models.py"] = "class User:\n" + docstring
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert [
        (block.source.path, block.source.ranges, block.tokens)
        for block in payload.blocks
        if block.level is ContextLevel.L4
    ] == [("pkg/models.py", ranges, tokens)]


def test_changed_file_over_256_kib_keeps_its_diff_and_is_not_parsed() -> None:
    over = _service_repository()
    over.files["pkg/service.py"] = _sized(_SERVICE, 262_145)
    at = _service_repository()
    at.files["pkg/service.py"] = _sized(_SERVICE, 262_144)
    over_parser, at_parser = InProcessParser(), InProcessParser()

    over_payload = _build(
        over, _diff_map(_vcs_file(over, "pkg/service.py", _GREET_PATCH)), parser=over_parser
    )
    at_payload = _build(
        at, _diff_map(_vcs_file(at, "pkg/service.py", _GREET_PATCH)), parser=at_parser
    )

    assert ("source_too_large", "pkg/service.py") in [
        (item.reason, item.path) for item in over_payload.omissions
    ]
    assert [
        block.source.path for block in over_payload.blocks if block.level is ContextLevel.L1
    ] == ["pkg/service.py"]
    assert "pkg/service.py" not in over_parser.parsed
    assert "pkg/service.py" in at_parser.parsed
    assert "source_too_large" not in [item.reason for item in at_payload.omissions]


def test_imported_file_over_256_kib_is_not_resolved() -> None:
    repository = _service_repository()
    repository.files["pkg/models.py"] = _sized(_MODELS, 262_145)
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))

    payload = _build(repository, diff_map)

    assert [(ref.resolution, ref.reason) for ref in payload.imports] == [
        (ImportResolution.UNSUPPORTED, "source_too_large")
    ]


@pytest.mark.parametrize(
    "excluded",
    [pytest.param("pkg/a.go", id="first-member"), pytest.param("pkg/b.go", id="later-member")],
)
def test_excluded_go_member_is_never_read_while_its_package_is_named(excluded: str) -> None:
    # The clause `db` differs from the directory `pkg/`, so the resolver reads members to
    # name the refused package; the masked member is skipped by path, never read.
    repository = Repository(
        {
            "go.mod": "module example.com/m\n\ngo 1.22\n",
            "cmd/main.go": (
                'package main\n\nimport "example.com/m/pkg"\n\nfunc main() {\n\tdb.Open()\n}\n'
            ),
            "pkg/a.go": "package db\n\nfunc Open() {}\n",
            "pkg/b.go": "package db\n\nfunc Close() {}\n",
        }
    )
    patch = "@@ -5,2 +5,3 @@\n func main() {\n+\tdb.Open()\n }"
    diff_map = _diff_map(_vcs_file(repository, "cmd/main.go", patch))

    payload = _build(repository, diff_map, rules_text=f"```review-ignore\n{excluded}\n```\n")

    assert excluded not in repository.read
    assert [(ref.resolution, ref.reason) for ref in payload.imports] == [
        (ImportResolution.EXCLUDED, "excluded")
    ]


def test_file_of_an_unresolved_identity_is_not_read_through_an_import() -> None:
    # The old name breaks the path contract, so the file has no reliable identity.
    repository = _service_repository()
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/service.py", _GREET_PATCH),
        _vcs_file(
            repository,
            "pkg/models.py",
            "@@ -1 +1 @@\n-class Old:\n+class User:",
            deletions=1,
            status="renamed",
            previous="generated/../models.py",
        ),
    )

    payload = _build(repository, diff_map, rules_text="```review-ignore\ngenerated/**\n```\n")

    assert all(block.source.path != "pkg/models.py" for block in payload.blocks)
    assert [ref.resolution for ref in payload.imports] == [ImportResolution.EXCLUDED]
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
    # The rejected name is diagnostic only; the canonical path of that omission is empty.
    assert {(item.reason, item.path, item.diagnostic_path) for item in payload.omissions} == {
        ("unsupported_path", None, "src\\bad.py"),
        ("patch_missing", "pkg/secret.py", None),
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


def test_window_around_a_hunk_is_reduced_to_the_fragment_limits() -> None:
    lines = [f"value_{n} = {n}" for n in range(1, 2001)]
    for index in range(999, 1179):
        lines[index] = f"changed_{index} = 1"
    repository = Repository({"data.py": "\n".join(lines) + "\n"})
    rows = "\n".join(f"+changed_{index} = 1" for index in range(999, 1179))
    patch = f"@@ -1000,0 +1000,180 @@\n{rows}"
    diff_map = _diff_map(_vcs_file(repository, "data.py", patch, additions=180))

    payload = _build(repository, diff_map)

    windows = [block for block in payload.blocks if block.level is ContextLevel.L2]
    # 180 changed lines leave room for ten lines on each side, not for eighty.
    assert [block.source.ranges for block in windows] == [(LineRange(990, 1189),)]
    assert len(windows[0].text.split("\n")) == 200


def test_hunk_larger_than_a_fragment_gets_no_window_and_token_limit_also_applies() -> None:
    lines = [f"value_{n} = {n}" for n in range(1, 2001)]
    repository = Repository({"data.py": "\n".join(lines) + "\n"})
    rows = "\n".join(f"+value_{n} = {n}" for n in range(1000, 1500))
    large = f"@@ -1000,0 +1000,500 @@\n{rows}"

    payload = _build(repository, _diff_map(_vcs_file(repository, "data.py", large, additions=500)))
    assert not [block for block in payload.blocks if block.level is ContextLevel.L2]
    assert ContextLevel.L1 in payload.levels_present

    wide = ["w " * 40 for _ in range(2000)]
    wide_repository = Repository({"wide.txt": "\n".join(wide) + "\n"})
    patch = "@@ -1000 +1000 @@\n-old\n+" + wide[999]
    built = _build(
        wide_repository,
        _diff_map(_vcs_file(wide_repository, "wide.txt", patch, deletions=1)),
    )
    (window,) = [block for block in built.blocks if block.level is ContextLevel.L2]
    # Forty words a line: fifty lines are the 2 000 token limit of one fragment.
    assert window.tokens <= 2000
    assert window.source.ranges == (LineRange(976, 1024),)


def test_neighbouring_windows_merge_only_within_one_fragment() -> None:
    lines = [f"value_{n} = {n}" for n in range(1, 2001)]
    repository = Repository({"data.py": "\n".join(lines) + "\n"})
    patch = "@@ -500 +500 @@\n-old\n+value_500 = 500\n@@ -620 +620 @@\n-old\n+value_620 = 620"
    diff_map = _diff_map(_vcs_file(repository, "data.py", patch, additions=2, deletions=2))

    payload = _build(repository, diff_map)

    ranges = [block.source.ranges[0] for block in payload.blocks if block.level is ContextLevel.L2]
    # Each window is 161 lines; joined they would be 281, so they stay two fragments
    # that share no line.
    assert ranges == [LineRange(420, 580), LineRange(581, 700)]


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


def test_removed_lines_of_a_changed_file_point_at_the_old_revision() -> None:
    repository = _service_repository()
    patch = (
        "@@ -9,5 +9,4 @@\n def greet(user_id):\n"
        '     """Greet a user."""\n     user = User(user_id)\n-    gone = 1\n'
        '     return "hi " + user.name'
    )
    repository.files["pkg/service.py"] = _SERVICE.replace("    extra = 1\n", "")
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", patch, additions=0, deletions=1))

    payload = _build(repository, diff_map)

    diff = next(block for block in payload.blocks if block.level is ContextLevel.L1)
    assert (diff.source.revision, diff.source.ranges) == (_HEAD, (LineRange(9, 12),))
    (old_side,) = diff.related_sources
    assert (old_side.revision, old_side.path, old_side.ranges) == (
        "c" * 40,
        "pkg/service.py",
        (LineRange(9, 13),),
    )


def test_deleted_file_of_an_unknown_old_revision_has_no_confirmed_source_to_show() -> None:
    repository = _service_repository()
    removed = _vcs_file(
        repository,
        "pkg/gone.py",
        "@@ -1,2 +0,0 @@\n-a = 1\n-b = 2",
        additions=0,
        deletions=2,
        status="removed",
    )
    diff_map = replace(_diff_map(removed), diff_old_sha=None)

    payload = _build(repository, diff_map)

    assert payload.blocks == ()
    assert [(item.reason, item.path) for item in payload.omissions] == [
        ("old_revision_unknown", "pkg/gone.py")
    ]


def test_unchanged_binary_file_is_not_imported_even_from_the_facts_cache() -> None:
    # Only ``pkg/service.py`` is changed; the imported file is outside the diff.
    repository = _service_repository()
    diff_map = _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH))
    cache = MemoryCache()
    _build(repository, diff_map, cache=cache)

    # The same blob id now holds binary content: text after a NUL byte.
    repository.files["pkg/models.py"] = "\x00\n" + _MODELS
    parser = InProcessParser()
    payload = _build(repository, diff_map, parser=parser, cache=cache)

    assert [(ref.resolution, ref.reason) for ref in payload.imports] == [
        (ImportResolution.EXCLUDED, "binary")
    ]
    assert ContextLevel.L4 not in payload.levels_present
    assert all(block.source.path != "pkg/models.py" for block in payload.blocks)
    assert "pkg/models.py" not in parser.parsed


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


@pytest.mark.parametrize("boundary", ["tree", "blob"])
@pytest.mark.parametrize("failure", ["cancel", "deadline", "policy"])
def test_run_control_and_policy_errors_escape_optional_reads(boundary: str, failure: str) -> None:
    from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
    from app.modules.reviews.application.run_failures import RunCancelled, RunFailure

    error = {
        "cancel": RunCancelled("installation_unavailable"),
        "deadline": RunFailure("deadline_exceeded"),
        "policy": PolicyError(PolicyErrorCode.CONTEXT_VIOLATION, "untrusted context"),
    }[failure]

    class Unavailable(Repository):
        async def tree(self, revision: str) -> RevisionTree:
            if boundary == "tree":
                raise error
            return await super().tree(revision)

        async def read_blob(self, blob_sha: str) -> bytes:
            raise error

    repository = Unavailable({"app.py": "x = 2\n"})
    diff_map = _diff_map(
        _vcs_file(repository, "app.py", "@@ -1 +1 @@\n-x = 1\n+x = 2", deletions=1)
    )
    with pytest.raises(type(error)) as caught:
        _build(repository, diff_map)
    assert caught.value is error


@pytest.mark.parametrize("boundary", ["tree", "blob"])
def test_optional_source_failure_remains_an_omission(boundary: str) -> None:
    from app.modules.reviews.application.run_failures import RunFailure

    class Unavailable(Repository):
        async def tree(self, revision: str) -> RevisionTree:
            if boundary == "tree":
                raise RunFailure("diff_fetch_failed", "optional tree unavailable")
            return await super().tree(revision)

        async def read_blob(self, blob_sha: str) -> bytes:
            raise RunFailure("diff_fetch_failed", "optional source unavailable")

    repository = Unavailable({"app.py": "x = 2\n"})
    diff_map = _diff_map(
        _vcs_file(repository, "app.py", "@@ -1 +1 @@\n-x = 1\n+x = 2", deletions=1)
    )
    payload = _build(repository, diff_map)
    assert payload.status is ContextStatus.REVIEW
    reason = "tree_unavailable" if boundary == "tree" else "source_read_failed"
    assert any(item.reason == reason for item in payload.omissions)


@pytest.mark.parametrize("boundary", ["tree", "blob"])
@pytest.mark.parametrize("error_code", ["diff_fetch_failed", "commit_messages_fetch_failed"])
def test_required_authority_failure_escapes_context_source_reads(
    boundary: str,
    error_code: Literal["diff_fetch_failed", "commit_messages_fetch_failed"],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable

    failure = RequiredSourceUnavailable(error_code, retry_after=timedelta(seconds=180))

    class Unavailable(Repository):
        async def tree(self, revision: str) -> RevisionTree:
            if boundary == "tree":
                raise failure
            return await super().tree(revision)

        async def read_blob(self, blob_sha: str) -> bytes:
            raise failure

    repository = Unavailable({"app.py": "x = 2\n"})
    diff_map = _diff_map(
        _vcs_file(repository, "app.py", "@@ -1 +1 @@\n-x = 1\n+x = 2", deletions=1)
    )
    with pytest.raises(RequiredSourceUnavailable) as caught:
        _build(repository, diff_map)
    assert caught.value is failure
    assert caught.value.error_code == error_code
    assert caught.value.retry_after == timedelta(seconds=180)
    assert "Context tree read failed" not in caplog.text
    assert "Context file read failed" not in caplog.text


def test_without_a_rag_plan_there_are_no_retrieved_blocks_and_no_rag_stats() -> None:
    repository = _service_repository()
    reviewed = _build(repository, _diff_map(_vcs_file(repository, "pkg/service.py", _GREET_PATCH)))
    empty = _build(repository, _diff_map())

    for payload in (reviewed, empty):
        assert (payload.retrieved, payload.rag) == ((), None)
        assert ContextLevel.RAG not in payload.levels_present
    assert reviewed.tokens_used == sum(block.tokens for block in reviewed.blocks)
