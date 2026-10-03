"""Fit the L1 review context into a token budget (SD §9 "Сборка и бюджет", §13).

In sprint 2 the context is the diff only (L1): a file either fits whole, is cut with
the truncation trailer of review/README.md "Input envelope", or is moved to
``<omitted_files>``. Levels L2-L4 and the full ``BudgetAllocator`` are backlog.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Protocol

from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    ReviewContext,
    ReviewRule,
)

_TEST_DIRECTORIES = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_NAME_MARKERS = ("test_", "_test.", ".test.", ".spec.")
_CONFIG_SUFFIXES = frozenset(
    {".cfg", ".conf", ".env", ".ini", ".json", ".lock", ".toml", ".xml", ".yaml", ".yml"}
)


class TokenCounter(Protocol):
    """Counts tokens of a text for one model's tokenizer."""

    def count(self, text: str) -> int: ...


def prompt_tokens(context: ReviewContext, counter: TokenCounter) -> int:
    """Tokens of both provider messages of the rendered prompt."""
    prompt = PromptBuilder().build_prompt(context)
    return counter.count(prompt.system) + counter.count(prompt.user)


def fit_review_context(
    context: ReviewContext, *, max_prompt_tokens: int, counter: TokenCounter
) -> ReviewContext:
    """Return the context whose rendered prompt fits ``max_prompt_tokens``.

    Deterministic: files are taken by descending SD §9 priority (ties by path); each one
    is kept whole, else cut to the longest prefix of its lines that fits, else omitted.
    Every check renders the real prompt, so the result is exact for ``counter``. When
    even the prompt without any diff lines does not fit, the all-omitted context is
    returned and the gateway's pre-send count reports ``llm_context_overflow``.
    """
    if prompt_tokens(context, counter) <= max_prompt_tokens:
        return context

    files = context.changed_files
    order = sorted(
        range(len(files)),
        key=lambda index: (-_priority(files[index], context.rules), files[index].path),
    )
    kept: dict[int, ChangedFile] = {}

    def candidate(extra: dict[int, ChangedFile]) -> ReviewContext:
        chosen = {**kept, **extra}
        return replace(
            context,
            changed_files=tuple(chosen[index] for index in sorted(chosen)),
            omitted_files=context.omitted_files
            + tuple(files[index].path for index in range(len(files)) if index not in chosen),
        )

    def fits(extra: dict[int, ChangedFile]) -> bool:
        return prompt_tokens(candidate(extra), counter) <= max_prompt_tokens

    for index in order:
        file = files[index]
        if fits({index: file}):
            kept[index] = file
            continue
        shown = _longest_fitting_prefix(index, file, fits)
        if shown is not None:
            kept[index] = shown
    return candidate({})


def _longest_fitting_prefix(index: int, file: ChangedFile, fits: _Fits) -> ChangedFile | None:
    """Binary search for the largest line prefix (at least one line) that still fits."""
    low, high = 1, len(file.lines) - 1
    best: ChangedFile | None = None
    while low <= high:
        middle = (low + high) // 2
        cut = replace(file, lines=file.lines[:middle], total_lines=len(file.lines))
        if fits({index: cut}):
            best = cut
            low = middle + 1
        else:
            high = middle - 1
    return best


class _Fits(Protocol):
    def __call__(self, extra: dict[int, ChangedFile], /) -> bool: ...


def _priority(file: ChangedFile, rules: tuple[ReviewRule, ...]) -> float:
    """SD §9 step 1: kind weight × log(changed lines + 1) × 1.5 under a custom rule."""
    changed = sum(1 for line in file.lines if line.type != "context")
    weight = _kind_weight(file.path) * math.log(changed + 1)
    if any(_matches_rule(file.path, rule) for rule in rules):
        weight *= 1.5
    return weight


def _kind_weight(path: str) -> float:
    lowered = PurePosixPath(path.lower())
    name = lowered.name
    if _TEST_DIRECTORIES & set(lowered.parts[:-1]) or any(
        marker in name for marker in _TEST_NAME_MARKERS
    ):
        return 0.6
    if lowered.suffix in _CONFIG_SUFFIXES or name == "dockerfile":
        return 0.4
    return 1.0


def _matches_rule(path: str, rule: ReviewRule) -> bool:
    candidate = PurePosixPath(path)

    def matches(patterns: tuple[str, ...]) -> bool:
        return any(
            candidate.full_match(expanded)
            for pattern in patterns
            for expanded in _expand_braces(pattern)
        )

    return matches(rule.include) and not matches(rule.exclude)


def _expand_braces(pattern: str) -> list[str]:
    """``src/**/*.{ts,tsx}`` (review/rules/schema.json) as plain globs for ``full_match``."""
    match = _BRACES.search(pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return [
        expanded
        for option in match.group(1).split(",")
        for expanded in _expand_braces(head + option + tail)
    ]


_BRACES = re.compile(r"\{([^{}]*)\}")
