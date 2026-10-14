"""Pure deterministic filtering of parsed model review findings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

from app.modules.reviews.application.review_output import (
    ReviewFinding,
    ReviewOutput,
    finding_order_key,
    render_review_body,
)
from app.modules.reviews.application.verdict import (
    PUBLISHED_SEVERITIES,
    SeverityThresholdValue,
    severity_count_phrase,
    severity_counts,
    verdict,
)

DropReason = Literal["lint_pattern", "duplicate", "severity_threshold", "unknown_path"]
Bucket = Literal["inline", "body_only", "dropped"]

_ATTRIBUTION_PREFIX = "According to custom instructions in '"
_ATTRIBUTION_RE = re.compile(r"^According to custom instructions in '(?P<name>[^']+)' \([^)]*\): ")


@dataclass(frozen=True)
class LintFilterPatterns:
    """Compiled, versioned filter configuration."""

    drop_if_any: tuple[re.Pattern[str], ...]
    keep_if_any: tuple[re.Pattern[str], ...]
    min_confidence: float
    max_inline: int

    @classmethod
    def from_json(cls, source: str) -> LintFilterPatterns:
        """Build compiled patterns from the versioned JSON artifact."""
        data = json.loads(source)
        return cls(
            drop_if_any=tuple(re.compile(item, re.IGNORECASE) for item in data["drop_if_any"]),
            keep_if_any=tuple(re.compile(item, re.IGNORECASE) for item in data["keep_if_any"]),
            min_confidence=float(data["min_confidence"]),
            max_inline=int(data["max_inline"]),
        )


@dataclass(frozen=True)
class ProcessedFinding:
    """One model finding and its exactly-one publication bucket."""

    finding: ReviewFinding
    position: int
    bucket: Bucket
    drop_reason: DropReason | None = None


@dataclass(frozen=True)
class ProcessedReviewOutput:
    """Post-processed provider payload and durable finding classifications."""

    inline: tuple[ProcessedFinding, ...]
    body_only: tuple[ProcessedFinding, ...]
    dropped: tuple[ProcessedFinding, ...]
    review_body: str

    @property
    def buckets(self) -> tuple[tuple[ProcessedFinding, ...], ...]:
        """All three disjoint buckets in stable output order."""
        return (self.inline, self.body_only, self.dropped)


class FindingsPostProcessor:
    """Apply the documented finding-filter stages, severity threshold included, without I/O."""

    def __init__(self, patterns: LintFilterPatterns) -> None:
        self._patterns = patterns

    @property
    def patterns(self) -> LintFilterPatterns:
        return self._patterns

    @classmethod
    def from_default_patterns(cls) -> FindingsPostProcessor:
        """Load the immutable repository-owned lint filter artifact."""
        path = files("review.postprocess").joinpath("lint-filter-patterns.json")
        return cls(LintFilterPatterns.from_json(path.read_text(encoding="utf-8")))

    def process(
        self,
        output: ReviewOutput,
        *,
        hunk_lines: dict[str, frozenset[int]],
        rule_names: frozenset[str],
        repository_max_inline: int | None = None,
        severity_threshold: SeverityThresholdValue = "all",
    ) -> ProcessedReviewOutput:
        """Classify every finding once in the documented stage order.

        The repository severity threshold runs after lint and dedupe, before routing and the
        inline cap (docs/PIPELINE_SPEC.md §9).
        """
        cap = min(
            self._patterns.max_inline,
            repository_max_inline
            if repository_max_inline is not None
            else self._patterns.max_inline,
        )
        candidates = [
            ProcessedFinding(item, position, "inline")
            for position, item in enumerate(output.findings)
        ]
        dropped: list[ProcessedFinding] = []

        candidates, lint_dropped = self._drop_lint_patterns(candidates)
        dropped.extend(lint_dropped)
        candidates, duplicate_dropped = self._deduplicate(candidates)
        dropped.extend(duplicate_dropped)
        candidates, below_threshold = self._drop_below_threshold(candidates, severity_threshold)
        dropped.extend(below_threshold)

        prepared: list[ProcessedFinding] = []
        for item in candidates:
            finding = item.finding
            bucket: Bucket = (
                "body_only" if finding.confidence < self._patterns.min_confidence else "inline"
            )
            if finding.path not in hunk_lines:
                dropped.append(ProcessedFinding(finding, item.position, "dropped", "unknown_path"))
                continue
            valid_lines = hunk_lines[finding.path]
            if finding.line not in valid_lines:
                bucket = "body_only"
            if finding.start_line is not None and (
                finding.start_line not in valid_lines or finding.start_line >= finding.line
            ):
                finding = finding.model_copy(update={"start_line": None})
            prepared.append(ProcessedFinding(finding, item.position, bucket))

        inline_candidates = [item for item in prepared if item.bucket == "inline"]
        inline_candidates.sort(key=_quality)
        inline = inline_candidates[:cap]
        overflow = [
            ProcessedFinding(item.finding, item.position, "body_only")
            for item in inline_candidates[cap:]
        ]
        body_only = [item for item in prepared if item.bucket == "body_only"] + overflow

        inline = [self._repair_attribution(item, rule_names) for item in inline]
        body_only = [self._repair_attribution(item, rule_names) for item in body_only]
        moved_to_body = [item for item in inline if item.bucket == "body_only"]
        inline = [item for item in inline if item.bucket == "inline"]
        body_only.extend(moved_to_body)
        dropped.sort(key=lambda item: item.position)
        return ProcessedReviewOutput(
            inline=tuple(inline),
            body_only=tuple(body_only),
            dropped=tuple(dropped),
            review_body=_render_body(
                output,
                tuple(inline + body_only),
                tuple(body_only),
                hunk_lines,
                severity_threshold,
                len(below_threshold),
            ),
        )

    def _drop_lint_patterns(
        self, candidates: list[ProcessedFinding]
    ) -> tuple[list[ProcessedFinding], list[ProcessedFinding]]:
        kept: list[ProcessedFinding] = []
        dropped: list[ProcessedFinding] = []
        for item in candidates:
            text = f"{item.finding.title}\n{item.finding.body}"
            lint_match = any(
                pattern.search(text) is not None for pattern in self._patterns.drop_if_any
            )
            keep_match = any(
                pattern.search(text) is not None for pattern in self._patterns.keep_if_any
            )
            if lint_match and not keep_match and item.finding.severity not in {"critical", "high"}:
                dropped.append(
                    ProcessedFinding(item.finding, item.position, "dropped", "lint_pattern")
                )
            else:
                kept.append(item)
        return kept, dropped

    @staticmethod
    def _deduplicate(
        candidates: list[ProcessedFinding],
    ) -> tuple[list[ProcessedFinding], list[ProcessedFinding]]:
        winners: dict[tuple[str, int, str], ProcessedFinding] = {}
        dropped: list[ProcessedFinding] = []
        for item in candidates:
            key = (item.finding.path, item.finding.line, _normalize_title(item.finding.title))
            incumbent = winners.get(key)
            if incumbent is None or _quality(item) < _quality(incumbent):
                if incumbent is not None:
                    dropped.append(
                        ProcessedFinding(
                            incumbent.finding, incumbent.position, "dropped", "duplicate"
                        )
                    )
                winners[key] = item
            else:
                dropped.append(
                    ProcessedFinding(item.finding, item.position, "dropped", "duplicate")
                )
        return sorted(winners.values(), key=lambda item: item.position), dropped

    @staticmethod
    def _drop_below_threshold(
        candidates: list[ProcessedFinding], threshold: SeverityThresholdValue
    ) -> tuple[list[ProcessedFinding], list[ProcessedFinding]]:
        published = PUBLISHED_SEVERITIES[threshold]
        kept: list[ProcessedFinding] = []
        dropped: list[ProcessedFinding] = []
        for item in candidates:
            if item.finding.severity in published:
                kept.append(item)
            else:
                dropped.append(
                    ProcessedFinding(item.finding, item.position, "dropped", "severity_threshold")
                )
        return kept, dropped

    @staticmethod
    def _repair_attribution(item: ProcessedFinding, rule_names: frozenset[str]) -> ProcessedFinding:
        finding = item.finding
        match = _ATTRIBUTION_RE.match(finding.body)
        quoted_name = match.group("name") if match is not None else None
        valid = (
            match is not None
            and finding.rule_name is not None
            and quoted_name == finding.rule_name
            and finding.rule_name in rule_names
        )
        if valid:
            return item
        repaired = finding.model_copy(update={"rule_name": None})
        if match is not None:
            repaired = repaired.model_copy(update={"body": finding.body[match.end() :]})
            return ProcessedFinding(repaired, item.position, "body_only")
        return ProcessedFinding(repaired, item.position, item.bucket)


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _quality(item: ProcessedFinding) -> tuple[int, float, int]:
    return (*finding_order_key(item.finding.severity, item.finding.confidence), item.position)


def _render_body(
    output: ReviewOutput,
    kept: tuple[ProcessedFinding, ...],
    body_only: tuple[ProcessedFinding, ...],
    hunk_lines: dict[str, frozenset[int]],
    severity_threshold: SeverityThresholdValue,
    below_threshold: int,
) -> str:
    prose = render_review_body(output.summary)
    heading, separator, rest = prose.partition("\n\n")
    overview = _overview(kept, hunk_lines, severity_threshold, below_threshold)
    prefix = f"{heading}{separator}{overview}\n\n{rest}"
    if not body_only:
        return prefix
    lines = [prefix, "", "## Additional findings"]
    lines.extend(_render_body_only_finding(item) for item in body_only)
    body = "\n".join(lines)
    if len(prefix) >= 4000:
        return prefix
    return body[:4000]


def _overview(
    kept: tuple[ProcessedFinding, ...],
    hunk_lines: dict[str, frozenset[int]],
    severity_threshold: SeverityThresholdValue,
    below_threshold: int,
) -> str:
    severities = [item.finding.severity for item in kept]
    counts = severity_counts(severities)
    count_text = severity_count_phrase(counts)
    paths = sorted(hunk_lines)
    checked = "\n".join(f"- {path}" for path in paths) if paths else "(none)"
    below = (
        (f"**Below severity threshold ({severity_threshold}):** {below_threshold}",)
        if below_threshold
        else ()
    )
    return "\n".join(
        (
            f"**Verdict:** {verdict(severities)}",
            f"**Risk summary:** {count_text}",
            *below,
            "**Checked files:**",
            checked,
        )
    )


def _render_body_only_finding(item: ProcessedFinding) -> str:
    return (
        f"- **{item.finding.title}** "
        f"(`{item.finding.path}:{item.finding.line}`): {item.finding.body}"
    )
