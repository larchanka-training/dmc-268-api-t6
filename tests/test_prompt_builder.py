"""Public rendering contract for the cache-stable review prompt envelope."""

from __future__ import annotations

from pathlib import Path

from app.modules.reviews.application.prompt_builder import (
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

    rendered = PromptBuilder._render_changed_files(files)
    assert '<line n="4" type="removed">--old_flag</line>' in rendered
    assert '<line n="4" type="added">++new_flag</line>' in rendered


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
            commit_messages=("feat: add charge & repository", "test: cover <failure> path"),
        ),
        changed_files=parse_unified_diff((root / "review/examples/sample.diff").read_text()),
        omitted_files=(),
    )


def test_sample_review_provider_messages_match_golden_bytes() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures/prompt_builder"
    messages = PromptBuilder().build_prompt(_sample_review_context())

    assert messages.system.encode("utf-8") == (fixture / "sample.system.txt").read_bytes()
    assert messages.user.encode("utf-8") == (fixture / "sample.user.txt").read_bytes()
