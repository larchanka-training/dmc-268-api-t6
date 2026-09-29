"""Token budgeting of the L1 review context (SD §9, §13; review/README.md "Input envelope")."""

from __future__ import annotations

import re

from app.modules.reviews.application.prompt_budget import fit_review_context, prompt_tokens
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewRule,
)


class CharCounter:
    """One token per character: exact and easy to reason about in assertions."""

    def count(self, text: str) -> int:
        return len(text)


COUNTER = CharCounter()


def changed_file(path: str, count: int, *, width: int = 40) -> ChangedFile:
    return ChangedFile(
        path=path,
        status="modified",
        lines=tuple(DiffLine(number, "added", "x" * width) for number in range(1, count + 1)),
    )


def context(*files: ChangedFile, rules: tuple[ReviewRule, ...] = ()) -> ReviewContext:
    return ReviewContext(
        system="SYSTEM",
        rules=rules,
        agents_md=None,
        conventions=RepoConventions((), ()),
        pr_meta=PullRequestMeta("T", None, "a", "f", "main", (), len(files), 1, 0, False, False),
        changed_files=files,
        omitted_files=("assets/logo.png",),
    )


def test_context_within_budget_is_returned_unchanged() -> None:
    original = context(changed_file("app/a.py", 5))

    assert fit_review_context(original, max_prompt_tokens=10_000, counter=COUNTER) is original


def test_diff_over_budget_is_cut_with_the_trailer_and_omitted_files_listed() -> None:
    original = context(
        changed_file("app/core.py", 60),
        changed_file("tests/test_core.py", 60),
        changed_file("config/settings.yaml", 60),
    )
    budget = prompt_tokens(original, COUNTER) // 2

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)
    rendered = PromptBuilder().build(fitted)

    assert prompt_tokens(fitted, COUNTER) <= budget
    # the source file has the highest SD §9 priority and is kept whole
    assert fitted.changed_files[0] == original.changed_files[0]
    # the test file is cut and ends with the README trailer
    cut = fitted.changed_files[1]
    assert cut.path == "tests/test_core.py"
    assert cut.total_lines == 60
    assert 0 < len(cut.lines) < 60
    trailer = (
        f"[Showing lines 1-{len(cut.lines)} of 60 total. "
        f"Use offset={len(cut.lines) + 1} to continue reading.]"
    )
    assert f"{trailer}\n</file>" in rendered
    # the config file did not fit at all and is listed for the model
    assert fitted.omitted_files == ("assets/logo.png", "config/settings.yaml")
    assert re.search(
        r"<omitted_files>\nassets/logo.png\nconfig/settings.yaml\n</omitted_files>", rendered
    )


def test_fitting_is_deterministic_and_keeps_the_original_file_order() -> None:
    original = context(
        changed_file("docs/readme.json", 5),
        changed_file("app/b.py", 30),
        changed_file("app/a.py", 30),
    )
    budget = prompt_tokens(original, COUNTER) - 200

    first = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)
    second = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    assert first == second
    whole = [file for file in first.changed_files if file.total_lines is None]
    assert whole == [original.changed_files[1], original.changed_files[2]]
    # the low-priority config file is cut or omitted, never the source files
    assert "docs/readme.json" in first.omitted_files or first.changed_files[0].total_lines == 5
    assert [file.path for file in first.changed_files][-2:] == ["app/b.py", "app/a.py"]


def test_a_path_under_a_custom_rule_is_preferred() -> None:
    rule = ReviewRule("Billing", ("app/billing/**",), ("app/billing/generated/**",), ("Check.",))
    original = context(
        changed_file("app/aaa.py", 20),
        changed_file("app/billing/charge.py", 20),
        rules=(rule,),
    )
    budget = prompt_tokens(original, COUNTER) - 20 * 60

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    kept = {file.path: file for file in fitted.changed_files}
    assert kept["app/billing/charge.py"] == original.changed_files[1]
    assert "app/aaa.py" in fitted.omitted_files or kept["app/aaa.py"].total_lines == 20


def test_nothing_fits_returns_the_all_omitted_context() -> None:
    original = context(changed_file("app/a.py", 3))

    fitted = fit_review_context(original, max_prompt_tokens=10, counter=COUNTER)

    assert fitted.changed_files == ()
    assert fitted.omitted_files == ("assets/logo.png", "app/a.py")
