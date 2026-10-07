"""Fit the L1 review context into a token budget (SD §9 "Сборка и бюджет", §13).

In sprint 2 the context is the diff only (L1): a file either fits whole, is cut with
the truncation trailer of review/README.md "Input envelope", or is moved to
``<omitted_files>``. Levels L2-L4 and the full ``BudgetAllocator`` are backlog.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    PromptBuilder,
    ReviewContext,
    ReviewRule,
    render_changed_file,
    render_changed_line,
    truncation_trailer,
    xml_text,
)

_LOGGER = logging.getLogger(__name__)

_TEST_DIRECTORIES = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_NAME_MARKERS = ("test_", "_test.", ".test.", ".spec.")
_CONFIG_SUFFIXES = frozenset(
    {".cfg", ".conf", ".env", ".ini", ".json", ".lock", ".toml", ".xml", ".yaml", ".yml"}
)
_MAX_BRACE_EXPANSION_WORK = 256


class TokenCounter(Protocol):
    """Counts one message's tokens."""

    def count(self, text: str) -> int: ...


@runtime_checkable
class LengthAwareTokenCounter(TokenCounter, Protocol):
    """``count_length(n)`` equals ``count(text)`` for every text of length ``n``."""

    def count_length(self, characters: int) -> int: ...


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
    A length-aware counter uses additive rendered character lengths with its exact
    per-message token rounding. Other counters retain full-message counts, which are
    required when tokenization depends on content. When even the prompt without any
    diff lines does not fit, the all-omitted context is returned and the gateway's
    pre-send count reports ``llm_context_overflow``.
    """
    if prompt_tokens(context, counter) <= max_prompt_tokens:
        return context

    _warn_ignored_rules(context.rules)
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

    if isinstance(counter, LengthAwareTokenCounter):
        lengths = _PromptLengths(context, files, counter)

        def fits(extra: dict[int, ChangedFile]) -> bool:
            return lengths.tokens(kept, extra, counter.count_length) <= max_prompt_tokens

    else:

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


class _FileLengths:
    """Exact rendered lengths of a file and each possible line prefix."""

    def __init__(self, file: ChangedFile) -> None:
        self.file = file
        self.whole = len(render_changed_file(file))
        self.empty = len(render_changed_file(replace(file, lines=(), total_lines=None)))
        prefix = [0]
        for line in file.lines:
            prefix.append(prefix[-1] + len(render_changed_line(line)))
        self.prefix = tuple(prefix)

    def length(self, candidate: ChangedFile) -> int:
        if candidate is self.file:
            return self.whole
        shown = len(candidate.lines)
        # A cut block has ``shown`` separators between its lines and trailer.
        return (
            self.empty
            + self.prefix[shown]
            + shown
            + len(truncation_trailer(shown, len(self.file.lines)))
        )


class _PromptLengths:
    """Account for variable tags without rebuilding the stable prompt prefix."""

    def __init__(
        self, context: ReviewContext, files: tuple[ChangedFile, ...], counter: TokenCounter
    ) -> None:
        empty = replace(context, changed_files=(), omitted_files=())
        self.base_user = len(PromptBuilder().render_input(empty))
        self.system_tokens = counter.count(context.system)
        self.files = tuple(_FileLengths(file) for file in files)
        self.omitted_paths = tuple(len(xml_text(file.path)) for file in files)
        self.original_omitted = tuple(len(xml_text(path)) for path in context.omitted_files)

    def tokens(
        self,
        kept: dict[int, ChangedFile],
        extra: dict[int, ChangedFile],
        count_length: Callable[[int], int],
    ) -> int:
        chosen = {**kept, **extra}
        shown = tuple(sorted(chosen))
        changed_chars = sum(self.files[index].length(chosen[index]) for index in shown)
        changed_chars += max(0, len(shown) - 1)
        omitted = tuple(index for index in range(len(self.files)) if index not in chosen)
        omitted_count = len(self.original_omitted) + len(omitted)
        omitted_chars = sum(self.original_omitted) + sum(
            self.omitted_paths[index] for index in omitted
        )
        omitted_chars += max(0, omitted_count - 1)
        return self.system_tokens + count_length(self.base_user + changed_chars + omitted_chars)


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


def _rule_is_ignored(rule: ReviewRule) -> bool:
    """Whether an include or exclude glob of ``rule`` exceeds the brace expansion limit.

    Such a rule raises no file's priority. An oversized include disables it for every
    path; an oversized exclude matters only for paths its include matches, and skipping
    that exclude must not promote the file.
    """
    return any(_expand_braces(pattern) is None for pattern in (*rule.include, *rule.exclude))


def _matches_rule(path: str, rule: ReviewRule) -> bool:
    if _rule_is_ignored(rule):
        return False
    candidate = PurePosixPath(path)

    def matches(patterns: tuple[str, ...]) -> bool:
        # Every pattern expands within the limit once the rule is not ignored.
        return any(
            candidate.full_match(glob)
            for pattern in patterns
            for glob in _expand_braces(pattern) or ()
        )

    return matches(rule.include) and not matches(rule.exclude)


def _warn_ignored_rules(rules: tuple[ReviewRule, ...]) -> None:
    """Log each rule that ``_rule_is_ignored`` drops from file priority, once per fit."""
    for rule in rules:
        if _rule_is_ignored(rule):
            _LOGGER.warning(
                "Custom rule %r is ignored for file priority: "
                "a brace glob exceeds %d expansion units",
                rule.name,
                _MAX_BRACE_EXPANSION_WORK,
            )


def _expand_braces(pattern: str) -> list[str] | None:
    """Expand brace globs for ``full_match``, within 256 units of work.

    One unit is a candidate examined or an alternative produced. Return ``None``
    when the cap is exceeded; the caller ignores the entire rule for that path.
    This bounds work before a pattern such as ``{a,b}`` repeated 18 times can
    materialize all 262,144 alternatives.
    """
    expanded = [pattern]
    work = 0
    while True:
        next_patterns: list[str] = []
        changed = False
        for candidate in expanded:
            work += 1
            if work > _MAX_BRACE_EXPANSION_WORK:
                return None
            match = _BRACES.search(candidate)
            if match is None:
                next_patterns.append(candidate)
                continue
            changed = True
            options = match.group(1)
            work += options.count(",") + 1
            if work > _MAX_BRACE_EXPANSION_WORK:
                return None
            head, tail = candidate[: match.start()], candidate[match.end() :]
            next_patterns.extend(head + option + tail for option in options.split(","))
        if not changed:
            return next_patterns
        expanded = next_patterns


_BRACES = re.compile(r"\{([^{}]*)\}")
