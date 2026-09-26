"""Deterministic language classification shared by repository workflows."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Protocol


class LanguageSourceFile(Protocol):
    """A repository tree item whose byte size contributes to a language share."""

    @property
    def path(self) -> str: ...

    @property
    def size(self) -> int: ...


LANGUAGE_BY_SUFFIX = {
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JSX",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".svelte": "Svelte",
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".vue": "Vue",
}


def classify_languages(files: Iterable[LanguageSourceFile]) -> dict[str, int]:
    """Return deterministic whole-percent language shares from source byte sizes.

    Unknown extensions never affect the denominator. Largest-remainder allocation
    sums to 100; exact ties use the language name, so provider ordering cannot
    change the persisted result.
    """
    totals: defaultdict[str, int] = defaultdict(int)
    for file in files:
        language = LANGUAGE_BY_SUFFIX.get(PurePosixPath(file.path).suffix.lower())
        if language is not None and file.size > 0:
            totals[language] += file.size
    total = sum(totals.values())
    if total == 0:
        return {}

    percentages = {language: size * 100 // total for language, size in totals.items()}
    remainder = 100 - sum(percentages.values())
    ranked = sorted(
        totals,
        key=lambda language: (-(totals[language] * 100 % total), language),
    )
    for language in ranked[:remainder]:
        percentages[language] += 1
    return {language: percentages[language] for language in sorted(percentages)}
