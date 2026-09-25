"""Public rendering contract for the cache-stable review prompt envelope."""

from __future__ import annotations

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
<file path="app/a.py" status="modified">
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
