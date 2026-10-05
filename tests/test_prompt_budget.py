"""Token budgeting of the L1 review context (SD §9, §13; review/README.md "Input envelope")."""

from __future__ import annotations

import json
import re
from pathlib import Path
from time import perf_counter

import pytest

from app.modules.reviews.application.prompt_budget import fit_review_context, prompt_tokens
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    ReviewPrompt,
    ReviewRule,
)
from app.modules.reviews.infrastructure.llm.gateway import HeuristicTokenCounter


class CharCounter:
    """One token per character: exact and easy to reason about in assertions."""

    def count(self, text: str) -> int:
        return len(text)

    def count_length(self, characters: int) -> int:
        return characters


COUNTER = CharCounter()
DEFAULT_FRONTEND = (
    Path(__file__).resolve().parents[1] / "review" / "rules" / "default-frontend.v1.json"
)


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


def test_length_aware_counter_protocol_distinguishes_count_only_fallback() -> None:
    from app.modules.reviews.application.prompt_budget import LengthAwareTokenCounter

    class CountOnlyCounter:
        count_length = None

        def count(self, text: str) -> int:
            return len(text)

    original = context(changed_file("app/a.py", 5), changed_file("tests/test_a.py", 5))
    budget = prompt_tokens(original, COUNTER) - 20

    assert isinstance(COUNTER, LengthAwareTokenCounter)
    assert not isinstance(CountOnlyCounter(), LengthAwareTokenCounter)
    assert fit_review_context(original, max_prompt_tokens=budget, counter=CountOnlyCounter()) == (
        fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)
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
    # The low-priority config file is cut to one line; both source files remain whole.
    assert first.omitted_files == ("assets/logo.png",)
    assert (len(first.changed_files[0].lines), first.changed_files[0].total_lines) == (1, 5)
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
    assert fitted.omitted_files == ("assets/logo.png",)
    assert (len(kept["app/aaa.py"].lines), kept["app/aaa.py"].total_lines) == (2, 20)


def test_nothing_fits_returns_the_all_omitted_context() -> None:
    original = context(changed_file("app/a.py", 3))

    fitted = fit_review_context(original, max_prompt_tokens=10, counter=COUNTER)

    assert fitted.changed_files == ()
    assert fitted.omitted_files == ("assets/logo.png", "app/a.py")


def test_one_token_over_the_budget_changes_the_context() -> None:
    original = context(changed_file("app/a.py", 5), changed_file("app/b.py", 5))
    exact = prompt_tokens(original, COUNTER)

    assert fit_review_context(original, max_prompt_tokens=exact, counter=COUNTER) is original
    fitted = fit_review_context(original, max_prompt_tokens=exact - 1, counter=COUNTER)
    assert fitted != original
    assert prompt_tokens(fitted, COUNTER) <= exact - 1


def test_additive_budget_matches_full_render_with_multiple_omitted_files() -> None:
    class RenderCounter:
        def count(self, text: str) -> int:
            return len(text)

    original = context(
        changed_file("app/alpha.py", 2, width=4),
        changed_file("tests/test_beta.py", 2, width=4),
        changed_file("config/gamma.json", 2, width=4),
    )
    render_counter = RenderCounter()
    full_length = prompt_tokens(original, render_counter)
    all_omitted = fit_review_context(original, max_prompt_tokens=0, counter=render_counter)
    minimum_length = prompt_tokens(all_omitted, render_counter)
    assert len(all_omitted.omitted_files) - len(original.omitted_files) == 3
    saw_multiple_omitted = False

    for budget in range(full_length + 1):
        fast = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)
        rendered = fit_review_context(original, max_prompt_tokens=budget, counter=render_counter)
        assert fast == rendered, f"budget={budget}"
        if len(fast.omitted_files) - len(original.omitted_files) >= 2:
            saw_multiple_omitted = True
        if budget >= minimum_length:
            assert prompt_tokens(fast, render_counter) <= budget, f"budget={budget}"

    assert saw_multiple_omitted


def test_the_shipped_brace_glob_of_a_custom_rule_raises_the_priority() -> None:
    rule = next(
        ReviewRule(item["name"], tuple(item["include"]), tuple(item["exclude"]), ("Check.",))
        for item in json.loads(DEFAULT_FRONTEND.read_text(encoding="utf-8"))["rules"]
        if item["include"] == ["src/**/*.{ts,tsx}"]
    )
    original = context(
        changed_file("lib/aaa.ts", 20),
        changed_file("src/widgets/card.tsx", 20),
        rules=(rule,),
    )
    budget = prompt_tokens(original, COUNTER) - 20 * 60

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    kept = {file.path: file for file in fitted.changed_files}
    assert kept["src/widgets/card.tsx"] == original.changed_files[1]


def test_test_directories_are_matched_by_path_segment_not_substring() -> None:
    original = context(
        # sorts first, so a tie in priority would keep it instead of the source file
        changed_file("a/tests/test_a.py", 20),
        changed_file("app/latest/core.py", 20),
    )
    budget = prompt_tokens(original, COUNTER) - 20 * 60

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    kept = {file.path: file for file in fitted.changed_files}
    # "latest/" is a source directory: it outranks the test file
    assert kept["app/latest/core.py"] == original.changed_files[1]


def test_normal_brace_include_and_exclude_patterns_prioritize_only_matching_files() -> None:
    rule = ReviewRule(
        "Source",
        ("src/*.{ts,tsx}",),
        ("src/*.generated.{ts,tsx}",),
        ("Check.",),
    )
    original = context(
        changed_file("src/a.generated.ts", 20),
        changed_file("src/z.tsx", 20),
        rules=(rule,),
    )
    budget = prompt_tokens(original, COUNTER) - 20 * 60

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    kept = {file.path: file for file in fitted.changed_files}
    assert kept["src/z.tsx"] == original.changed_files[1]
    assert fitted.omitted_files == ("assets/logo.png",)
    assert (len(kept["src/a.generated.ts"].lines), kept["src/a.generated.ts"].total_lines) == (
        2,
        20,
    )


def test_oversized_brace_glob_is_ignored_without_expanding_every_combination() -> None:
    rule = ReviewRule("Explosive", ("{a,b}" * 18,), (), ("Check.",))
    original = context(
        changed_file("b" * 18, 20),
        changed_file("aa.py", 20),
        rules=(rule,),
    )
    budget = prompt_tokens(original, COUNTER) - 20 * 60

    started = perf_counter()
    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)
    elapsed = perf_counter() - started

    kept = {file.path: file for file in fitted.changed_files}
    assert elapsed < 0.1
    assert kept["aa.py"] == original.changed_files[1]
    assert fitted.omitted_files == ("assets/logo.png",)
    assert (len(kept["b" * 18].lines), kept["b" * 18].total_lines) == (2, 20)


def test_oversized_exclude_glob_does_not_promote_a_file() -> None:
    rule = ReviewRule("Exclusion", ("b" * 18,), ("{a,b}" * 18,), ("Check.",))
    original = context(
        changed_file("b" * 18, 20),
        changed_file("aa.py", 20),
        rules=(rule,),
    )
    budget = prompt_tokens(original, COUNTER) - 20 * 60

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    kept = {file.path: file for file in fitted.changed_files}
    assert kept["aa.py"] == original.changed_files[1]


@pytest.mark.parametrize(
    ("name", "files", "budget"),
    (
        ("whole", (("app/a.py", 2, 3),), 10_000),
        ("cut", (("app/a.py", 8, 12),), 850),
        ("omitted", (("app/a.py", 5, 8), ("tests/test_a.py", 5, 8)), 900),
        ("too_small", (("app/a.py", 3, 8),), 10),
    ),
)
def test_fitting_preserves_preoptimization_provider_message_bytes(
    name: str, files: tuple[tuple[str, int, int], ...], budget: int
) -> None:
    original = context(*(changed_file(path, lines, width=width) for path, lines, width in files))

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)
    messages = PromptBuilder().build_prompt(fitted)

    fixture = Path(__file__).resolve().parent / "fixtures/prompt_budget" / f"{name}.user.txt"
    assert messages.system.encode("utf-8") == b"SYSTEM"
    assert messages.user.encode("utf-8") == fixture.read_bytes()


def test_nonadditive_counter_preserves_preoptimization_message_bytes() -> None:
    class FloorCounter:
        def count(self, text: str) -> int:
            return len(text) // 7

    original = context(changed_file("app/a.py", 8, width=12))

    fitted = fit_review_context(original, max_prompt_tokens=130, counter=FloorCounter())
    messages = PromptBuilder().build_prompt(fitted)

    fixture = Path(__file__).resolve().parent / "fixtures/prompt_budget/nonadditive.user.txt"
    assert messages.system.encode("utf-8") == b"SYSTEM"
    assert messages.user.encode("utf-8") == fixture.read_bytes()


def test_length_counter_avoids_full_prompt_renders_for_each_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = context(
        changed_file("app/core.py", 180),
        changed_file("tests/test_core.py", 180),
        changed_file("config/settings.yaml", 180),
    )
    budget = prompt_tokens(original, COUNTER) // 2
    calls = 0
    render = PromptBuilder.build_prompt

    def counted(self: PromptBuilder, value: ReviewContext) -> ReviewPrompt:
        nonlocal calls
        calls += 1
        return render(self, value)

    monkeypatch.setattr(PromptBuilder, "build_prompt", counted)

    fitted = fit_review_context(original, max_prompt_tokens=budget, counter=COUNTER)

    assert calls <= 1
    assert fitted.changed_files[0] == original.changed_files[0]
    assert prompt_tokens(fitted, COUNTER) <= budget


@pytest.mark.parametrize(
    ("chars_per_token", "budgets"),
    ((3.2, (575, 574, 287, 10)), (4.0, (461, 460, 230, 10))),
)
def test_length_based_heuristic_matches_fixed_selection_and_exact_counter(
    chars_per_token: float, budgets: tuple[int, int, int, int]
) -> None:
    class CountOnlyCounter:
        def __init__(self, original: HeuristicTokenCounter) -> None:
            self.original = original

        def count(self, text: str) -> int:
            return self.original.count(text)

    escaped = ChangedFile(
        "app/core&review.py",
        "modified",
        tuple(DiffLine(n, "added", "x < y & z") for n in range(1, 12)),
    )
    original = context(
        escaped,
        changed_file("tests/test_core.py", 9, width=7),
        changed_file("config/settings.yaml", 4, width=3),
    )
    heuristic = HeuristicTokenCounter(chars_per_token)
    count_only = CountOnlyCounter(heuristic)
    expected = (
        (
            (
                ("app/core&review.py", 11, None),
                ("tests/test_core.py", 9, None),
                ("config/settings.yaml", 4, None),
            ),
            ("assets/logo.png",),
        ),
        (
            (
                ("app/core&review.py", 11, None),
                ("tests/test_core.py", 9, None),
                ("config/settings.yaml", 2, 4),
            ),
            ("assets/logo.png",),
        ),
        (
            (("app/core&review.py", 3, 11),),
            ("assets/logo.png", "tests/test_core.py", "config/settings.yaml"),
        ),
        (
            (),
            (
                "assets/logo.png",
                "app/core&review.py",
                "tests/test_core.py",
                "config/settings.yaml",
            ),
        ),
    )

    for budget, (expected_files, expected_omitted) in zip(budgets, expected, strict=True):
        fast = fit_review_context(original, max_prompt_tokens=budget, counter=heuristic)
        selected = tuple(
            (file.path, len(file.lines), file.total_lines) for file in fast.changed_files
        )
        assert selected == expected_files
        assert fast.omitted_files == expected_omitted

        # The count-only path remains a supplemental parity check, not the oracle.
        exact = fit_review_context(original, max_prompt_tokens=budget, counter=count_only)
        fast_messages = PromptBuilder().build_prompt(fast)
        exact_messages = PromptBuilder().build_prompt(exact)
        assert fast == exact
        assert fast_messages.system.encode() == exact_messages.system.encode()
        assert fast_messages.user.encode() == exact_messages.user.encode()
