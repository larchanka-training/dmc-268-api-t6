"""Public rendering contract for the cache-stable review prompt envelope."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from app.modules.reviews.application.policy_snapshot import (
    DocumentPresence,
    ExecutionProfile,
    PolicyDocument,
    PolicySnapshot,
    PromptBindings,
)
from app.modules.reviews.application.prompt_builder import (
    DEFAULT_BACKEND_V1_CHECKSUM,
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
    parse_unified_diff,
)


def test_prompt_builder_renders_the_review_envelope_golden() -> None:
    context = ReviewContext(
        system="SYSTEM",
        rules=(
            ReviewRule(
                name="Errors & <IO>",
                include=("app/**/*.py", "scripts/*.py"),
                exclude=("tests/**",),
                checks=("Handle <all> errors & preserve context.", "Never swallow errors."),
            ),
        ),
        agents_md="Use <clean> code & tests.",
        conventions=RepoConventions(
            key_patterns=("Use services & ports.",),
            recommendations=("Keep transactions short.",),
        ),
        pr_meta=PullRequestMeta(
            title="Fix <parser>",
            description="Keep & return values.",
            author="octo",
            source_branch="fix/parser",
            target_branch="main",
            labels=("bug", "needs & review"),
            files_changed=2,
            lines_added=3,
            lines_removed=1,
            is_draft=False,
            is_fork=False,
        ),
        changed_files=(
            ChangedFile(
                path="app/a.py",
                status="modified",
                lines=(
                    DiffLine(number=10, type="context", content="before <tag>"),
                    DiffLine(number=11, type="removed", content="old & value"),
                    DiffLine(number=11, type="added", content="new & value"),
                ),
            ),
        ),
        omitted_files=("assets/logo<1>.png",),
    )

    assert (
        PromptBuilder().build(context)
        == """SYSTEM
<custom_instructions>
<rule name="Errors &amp; &lt;IO&gt;" include="app/**/*.py scripts/*.py" exclude="tests/**">
1. Handle &lt;all&gt; errors &amp; preserve context.
2. Never swallow errors.
</rule>
</custom_instructions>
<agents_md>
Use &lt;clean&gt; code &amp; tests.
</agents_md>
<repo_conventions>
<key_patterns>
<pattern>Use services &amp; ports.</pattern>
</key_patterns>
<recommendations>
<recommendation>Keep transactions short.</recommendation>
</recommendations>
</repo_conventions>
<pr_meta>
<title>Fix &lt;parser&gt;</title>
<description>Keep &amp; return values.</description>
<author>octo</author>
<source_branch>fix/parser</source_branch>
<target_branch>main</target_branch>
<labels>bug needs &amp; review</labels>
<files_changed>2</files_changed>
<lines_added>3</lines_added>
<lines_removed>1</lines_removed>
<draft>false</draft>
<fork>false</fork>
</pr_meta>
<changed_files>
<file path="app/a.py" status="modified" language="Python">
<line n="10" type="context">before &lt;tag&gt;</line>
<line n="11" type="removed">old &amp; value</line>
<line n="11" type="added">new &amp; value</line>
</file>
</changed_files>
<omitted_files>
assets/logo&lt;1&gt;.png
</omitted_files>"""
    )


def test_parse_unified_diff_uses_old_numbering_for_removed_lines() -> None:
    files = parse_unified_diff(
        """diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
@@ -7,2 +7,3 @@
 same
-old
+new
+extra
diff --git a/deleted.py b/deleted.py
deleted file mode 100644
@@ -2,1 +0,0 @@
-gone
"""
    )

    assert files == (
        ChangedFile(
            path="new.py",
            status="renamed",
            lines=(
                DiffLine(number=7, type="context", content="same"),
                DiffLine(number=8, type="removed", content="old"),
                DiffLine(number=8, type="added", content="new"),
                DiffLine(number=9, type="added", content="extra"),
            ),
        ),
        ChangedFile(
            path="deleted.py",
            status="removed",
            lines=(DiffLine(number=2, type="removed", content="gone"),),
        ),
    )


def test_parse_unified_diff_keeps_payloads_that_look_like_file_headers() -> None:
    files = parse_unified_diff(
        """diff --git a/app/example.py b/app/example.py
--- a/app/example.py
+++ b/app/example.py
@@ -4 +4 @@
---old_flag
+++new_flag
"""
    )

    assert files == (
        ChangedFile(
            path="app/example.py",
            status="modified",
            lines=(
                DiffLine(number=4, type="removed", content="--old_flag"),
                DiffLine(number=4, type="added", content="++new_flag"),
            ),
        ),
    )

    rendered = PromptBuilder.render_changed_files(files)
    assert '<line n="4" type="removed">--old_flag</line>' in rendered
    assert '<line n="4" type="added">++new_flag</line>' in rendered


def test_parse_unified_diff_keeps_a_path_that_holds_the_b_prefix() -> None:
    files = parse_unified_diff(
        "diff --git a/src/q b/r.py b/src/q b/r.py\n"
        "--- a/src/q b/r.py\n"
        "+++ b/src/q b/r.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    assert files == (
        ChangedFile(
            path="src/q b/r.py",
            status="modified",
            lines=(
                DiffLine(number=1, type="removed", content="old"),
                DiffLine(number=1, type="added", content="new"),
            ),
        ),
    )


def test_parse_unified_diff_takes_a_rename_target_that_holds_the_b_prefix() -> None:
    files = parse_unified_diff(
        "diff --git a/x b/old.py b/y b/new.py\n"
        "similarity index 90%\n"
        "rename from x b/old.py\n"
        "rename to y b/new.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    assert files == (
        ChangedFile(
            path="y b/new.py",
            status="renamed",
            lines=(
                DiffLine(number=1, type="removed", content="old"),
                DiffLine(number=1, type="added", content="new"),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("header", "path"),
    [
        # Two different names: without a ``rename to`` line the first-`` b/`` cut stays.
        ("diff --git a/x b/old.py b/y b/new.py", "old.py b/y b/new.py"),
        # One name on both sides, but not behind ``a/``: the middle split does not apply.
        ("diff --git c/src/q b/r.py b/src/q b/r.py", "r.py b/src/q b/r.py"),
    ],
    ids=["different-names", "no-a-prefix"],
)
def test_parse_unified_diff_keeps_the_provisional_path_outside_the_a_b_form(
    header: str, path: str
) -> None:
    files = parse_unified_diff(f"{header}\n@@ -1 +1 @@\n-old\n+new\n")

    assert files == (
        ChangedFile(
            path=path,
            status="modified",
            lines=(
                DiffLine(number=1, type="removed", content="old"),
                DiffLine(number=1, type="added", content="new"),
            ),
        ),
    )


def test_parse_unified_diff_rejects_a_header_with_two_empty_paths() -> None:
    with pytest.raises(ValueError, match="invalid unified diff header"):
        parse_unified_diff("diff --git a/ b/\n@@ -1 +1 @@\n-a\n+b\n")


def test_parse_unified_diff_ignores_a_rename_line_inside_a_hunk() -> None:
    files = parse_unified_diff(
        "diff --git a/src/app.py b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " first\n"
        "rename to evil.py\n"
        "+added\n"
        " last\n"
    )

    assert files == (
        ChangedFile(
            path="src/app.py",
            status="modified",
            lines=(
                DiffLine(number=1, type="context", content="first"),
                DiffLine(number=2, type="added", content="added"),
                DiffLine(number=3, type="context", content="last"),
            ),
        ),
    )


def test_parse_unified_diff_splits_lines_only_on_line_feed() -> None:
    files = parse_unified_diff(
        "diff --git a/src/app.py b/src/app.py\n"
        "@@ -1,2 +1,4 @@\n"
        " first\n"
        "+x\u2028rename to evil.py\n"
        "+a\x0c b\n"
        " last\n"
    )

    assert files == (
        ChangedFile(
            path="src/app.py",
            status="modified",
            lines=(
                DiffLine(number=1, type="context", content="first"),
                DiffLine(number=2, type="added", content="x\u2028rename to evil.py"),
                DiffLine(number=3, type="added", content="a\x0c b"),
                DiffLine(number=4, type="context", content="last"),
            ),
        ),
    )


@pytest.mark.parametrize(
    "mode_line",
    ["deleted file mode 100644", "new file mode 100644"],
    ids=["deleted-file-mode", "new-file-mode"],
)
def test_parse_unified_diff_ignores_a_file_mode_line_inside_a_hunk(mode_line: str) -> None:
    files = parse_unified_diff(
        f"diff --git a/src/app.py b/src/app.py\n@@ -1 +1,2 @@\n first\n{mode_line}\n+added\n"
    )

    assert files == (
        ChangedFile(
            path="src/app.py",
            status="modified",
            lines=(
                DiffLine(number=1, type="context", content="first"),
                DiffLine(number=2, type="added", content="added"),
            ),
        ),
    )


def test_parse_unified_diff_drops_the_carriage_return_of_crlf_lines() -> None:
    files = parse_unified_diff(
        "diff --git a/src/app.py b/src/app.py\r\n@@ -1,2 +1,2 @@\r\n-old\r\n+new\r\n same\r\n"
    )

    assert files == (
        ChangedFile(
            path="src/app.py",
            status="modified",
            lines=(
                DiffLine(number=1, type="removed", content="old"),
                DiffLine(number=1, type="added", content="new"),
                DiffLine(number=2, type="context", content="same"),
            ),
        ),
    )


def _metadata_context(
    head_sha: str | None = None, commit_messages: tuple[str, ...] = ()
) -> ReviewContext:
    pr_meta = PullRequestMeta(
        title="Add charge",
        description=None,
        author="octo",
        source_branch="feat/charge",
        target_branch="main",
        labels=("ai-review",),
        files_changed=2,
        lines_added=2,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha=head_sha,
        commit_messages=commit_messages,
    )
    return ReviewContext(
        system="SYSTEM",
        rules=(),
        agents_md=None,
        conventions=RepoConventions(key_patterns=(), recommendations=()),
        pr_meta=pr_meta,
        changed_files=(
            ChangedFile("web/app.tsx", "added", (DiffLine(1, "added", "export {}"),)),
            ChangedFile("Makefile", "modified", (DiffLine(2, "added", "all:"),)),
        ),
        omitted_files=(),
    )


_METADATA_HEAD = """SYSTEM
<custom_instructions>

</custom_instructions>
<agents_md>

</agents_md>
<repo_conventions>
<key_patterns>

</key_patterns>
<recommendations>

</recommendations>
</repo_conventions>
<pr_meta>
<title>Add charge</title>
<description></description>
<author>octo</author>
<source_branch>feat/charge</source_branch>
<target_branch>main</target_branch>
<labels>ai-review</labels>
<files_changed>2</files_changed>
<lines_added>2</lines_added>
<lines_removed>0</lines_removed>
<draft>false</draft>
<fork>false</fork>
"""
_METADATA_TAIL = """</pr_meta>
<changed_files>
<file path="web/app.tsx" status="added" language="TSX">
<line n="1" type="added">export {}</line>
</file>
<file path="Makefile" status="modified">
<line n="2" type="added">all:</line>
</file>
</changed_files>
<omitted_files>

</omitted_files>"""


def test_prompt_metadata_golden_with_commit_messages() -> None:
    context = _metadata_context(
        head_sha="a" * 40, commit_messages=("feat: add charge", "fix: <escape> & keep")
    )

    assert PromptBuilder().build(context) == (
        _METADATA_HEAD
        + "<head_sha>"
        + "a" * 40
        + "</head_sha>\n"
        + "<commit_messages>\n"
        + "<commit>feat: add charge</commit>\n"
        + "<commit>fix: &lt;escape&gt; &amp; keep</commit>\n"
        + "</commit_messages>\n"
        + _METADATA_TAIL
    )


def test_prompt_metadata_golden_without_commit_messages() -> None:
    assert PromptBuilder().build(_metadata_context()) == _METADATA_HEAD + _METADATA_TAIL
    assert PromptBuilder().build(_metadata_context(head_sha="b" * 40)) == (
        _METADATA_HEAD + "<head_sha>" + "b" * 40 + "</head_sha>\n" + _METADATA_TAIL
    )


def test_stable_prefix_does_not_depend_on_the_diff() -> None:
    def context(
        pr_meta: PullRequestMeta, files: tuple[ChangedFile, ...], omitted: tuple[str, ...]
    ) -> ReviewContext:
        return ReviewContext(
            system="SYSTEM WITH FEW-SHOT",
            rules=(ReviewRule("No print", ("app/**",), (), ("Do not print.",)),),
            agents_md="Use ports.",
            conventions=RepoConventions(("Use services.",), ("Test paths.",)),
            pr_meta=pr_meta,
            changed_files=files,
            omitted_files=omitted,
        )

    first = context(
        PullRequestMeta("One", None, "a", "x", "main", (), 1, 1, 0, False, False),
        (ChangedFile("app/a.py", "added", (DiffLine(1, "added", "a = 1"),)),),
        (),
    )
    second = context(
        PullRequestMeta("Two", "d", "b", "y", "dev", ("l",), 3, 9, 4, True, True),
        (ChangedFile("lib/b.ts", "removed", (DiffLine(7, "removed", "b"),)),),
        ("big.bin",),
    )

    first_prompt = PromptBuilder().build_prompt(first)
    second_prompt = PromptBuilder().build_prompt(second)

    assert first_prompt.system == second_prompt.system == "SYSTEM WITH FEW-SHOT"
    first_prefix = first_prompt.user.split("<pr_meta>")[0]
    assert first_prefix == second_prompt.user.split("<pr_meta>")[0]
    assert first_prefix.startswith("<custom_instructions>")
    assert "</repo_conventions>" in first_prefix
    assert PromptBuilder().build(first) == "\n".join((first_prompt.system, first_prompt.user))


def test_commit_messages_are_bounded_in_number_and_length() -> None:
    messages = ("x" * 600,) + tuple(f"fix: change {index}" for index in range(24))

    rendered = PromptBuilder().build(_metadata_context(commit_messages=messages))

    commits = rendered.split("<commit_messages>\n")[1].split("\n</commit_messages>")[0]
    lines = commits.splitlines()
    assert len(lines) == 21
    assert lines[0] == "<commit>" + "x" * 499 + "…</commit>"
    assert lines[19] == "<commit>fix: change 18</commit>"
    assert lines[20] == "<commit>[5 more commits omitted]</commit>"


def _sample_review_context() -> ReviewContext:
    root = Path(__file__).resolve().parents[1]
    return ReviewContext(
        system=(root / "review/prompts/review.system.v2.md").read_text(encoding="utf-8"),
        rules=(
            ReviewRule(
                name="Billing & error handling",
                include=("app/modules/billing/**/*.py",),
                exclude=("tests/**",),
                checks=("Preserve gateway failures & transaction boundaries.",),
            ),
        ),
        agents_md="Use application ports; keep network calls outside DB transactions.",
        conventions=RepoConventions(
            key_patterns=(
                "Use cases live in application; SQL repositories live in infrastructure.",
            ),
            recommendations=("Use cases own transactions (from: standard/correctness)",),
        ),
        pr_meta=PullRequestMeta(
            title="Add <billing> charge flow",
            description="Introduce a charge use case & repository.",
            author="example-author",
            source_branch="feat/billing-charge",
            target_branch="main",
            labels=("backend", "billing & payments"),
            files_changed=3,
            lines_added=127,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
            head_sha="0123456789abcdef0123456789abcdef01234567",
            commit_messages=(
                "feat: add charge & repository\n\nKeep <gateway> failures outside transactions.",
                "test: cover <failure> path",
                "docs: explain billing " + "charge flow. " * 40,
                "",
            ),
        ),
        changed_files=parse_unified_diff((root / "review/examples/sample.diff").read_text()),
        omitted_files=(),
    )


def test_sample_review_provider_messages_match_golden_bytes() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures/prompt_builder"
    messages = PromptBuilder().build_prompt(_sample_review_context())

    assert messages.system.encode("utf-8") == (fixture / "sample.system.txt").read_bytes()
    assert messages.user.encode("utf-8") == (fixture / "sample.user.txt").read_bytes()


def test_incomplete_commit_prefix_renders_literal_uncertainty_without_a_count() -> None:
    context = _metadata_context(commit_messages=("fix: <escape> & keep\nreason", ""))
    context = replace(context, pr_meta=replace(context.pr_meta, commit_messages_incomplete=True))
    prompt = PromptBuilder().build_prompt(context)
    assert (
        "<commit_messages>\n"
        "<commit>fix: &lt;escape&gt; &amp; keep\nreason</commit>\n"
        "<commit></commit>\n"
        "<commit>[Additional commits may be omitted]</commit>\n"
        "</commit_messages>"
    ) in prompt.user
    assert "more commits omitted" not in prompt.user


def _policy_rendering_snapshot(text: str | None) -> PolicySnapshot:
    """Literal documents with complete public execution contracts; no app test factories."""
    digest = sha256((text or "").encode()).hexdigest()
    document = PolicyDocument(
        path=".review/rules.md",
        presence=DocumentPresence.MISSING if text is None else DocumentPresence.PRESENT,
        blob_oid=None if text is None else "a" * 40,
        raw_sha256=None if text is None else digest,
        text_sha256=None if text is None else digest,
        text=text,
        byte_count=None if text is None else len(text.encode()),
        line_count=None if text is None else len(text.splitlines()),
    )
    return PolicySnapshot(
        schema_version=1,
        snapshot_id=UUID(int=1),
        run_id=UUID(int=2),
        repository_id=UUID(int=3),
        policy_base_sha="b" * 40,
        rules=document,
        agents=replace(document, path="AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="ignore-digest",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=UUID(int=4),
        defaults_checksum=DEFAULT_BACKEND_V1_CHECKSUM,
        prompt_bindings=PromptBindings(
            review_system_checksum=sha256(b"SYSTEM").hexdigest(),
            conventions_checksum=sha256(b"SYSTEM").hexdigest(),
            verifier_checksum=sha256(b"SYSTEM").hexdigest(),
            review_system_id=UUID(int=5),
            conventions_id=UUID(int=6),
            verifier_id=UUID(int=7),
        ),
        execution_profile=ExecutionProfile(
            profile_name="fast",
            review_model="review",
            verifier_model="verifier",
            primary_provider_route="primary",
            tokenizer_version="tokenizer/v1",
            counter_version="counter/v1",
            temperature="0.0",
            response_schema_version="review-output/v2",
            recovery_profile_version="recovery/v1",
            budget_profile_version="budget/v1",
        ),
        policy_digest="policy-digest",
    )


@pytest.mark.parametrize(
    ("text", "expected_rules", "expected_agents"),
    [
        (
            None,
            '<repository_rules block_id="policy-rules" path=".review/rules.md" status="absent" />',
            '<project_conventions_document block_id="policy-agents" '
            'path="AGENTS.md" status="absent" />',
        ),
        (
            "",
            '<repository_rules block_id="policy-rules" path=".review/rules.md" status="empty" />',
            '<project_conventions_document block_id="policy-agents" '
            'path="AGENTS.md" status="empty" />',
        ),
        (
            "First & <tag>\n\nLast 'quoted'.\n",
            '<repository_rules block_id="policy-rules" path=".review/rules.md">\n'
            '<line n="1">First &amp; &lt;tag&gt;</line>\n'
            '<line n="2"></line>\n'
            "<line n=\"3\">Last 'quoted'.</line>\n"
            "</repository_rules>",
            '<project_conventions_document block_id="policy-agents" path="AGENTS.md">\n'
            '<line n="1">First &amp; &lt;tag&gt;</line>\n'
            '<line n="2"></line>\n'
            "<line n=\"3\">Last 'quoted'.</line>\n"
            "</project_conventions_document>",
        ),
    ],
    ids=["missing", "empty", "present-with-blank-line-and-final-newline"],
)
def test_public_policy_renderers_preserve_exact_document_bytes(
    text: str | None, expected_rules: str, expected_agents: str
) -> None:
    snapshot = _policy_rendering_snapshot(text)

    assert PromptBuilder.render_policy_rules(snapshot) == expected_rules
    assert PromptBuilder.render_policy_agents(snapshot) == expected_agents


def test_public_service_defaults_renderer_preserves_ids_and_escaping() -> None:
    rule = ReviewRule(
        name="Error Handling Standards",
        include=('app/"odd"&*.py',),
        exclude=("tests/'odd'/**",),
        checks=("First & <safe>", "Keep 'quoted'", "Third", "Fourth"),
    )

    assert PromptBuilder.render_service_defaults(
        (rule,), defaults_checksum=DEFAULT_BACKEND_V1_CHECKSUM
    ) == (
        '<service_defaults block_id="service-defaults">\n'
        '<rule name="Error Handling Standards" include="app/&quot;odd&quot;&amp;*.py" '
        'exclude="tests/&apos;odd&apos;/**">\n'
        '<check id="E1"><line n="1">First &amp; &lt;safe&gt;</line></check>\n'
        '<check id="E2"><line n="2">Keep \'quoted\'</line></check>\n'
        '<check id="E3"><line n="3">Third</line></check>\n'
        '<check id="E4"><line n="4">Fourth</line></check>\n'
        "</rule>\n"
        "</service_defaults>"
    )


def test_public_conventions_renderer_preserves_exact_envelope() -> None:
    conventions = RepoConventions(
        key_patterns=("Pattern & <tag>",), recommendations=('Keep "quotes"',)
    )

    assert PromptBuilder.render_conventions(conventions) == (
        "<repo_conventions>\n"
        "<key_patterns>\n"
        "<pattern>Pattern &amp; &lt;tag&gt;</pattern>\n"
        "</key_patterns>\n"
        "<recommendations>\n"
        '<recommendation>Keep "quotes"</recommendation>\n'
        "</recommendations>\n"
        "</repo_conventions>"
    )
