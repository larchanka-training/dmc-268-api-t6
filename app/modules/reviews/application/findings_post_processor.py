"""Pure deterministic filtering of parsed model review findings."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from importlib.resources import files
from typing import Literal

from app.modules.reviews.application.diff_anchor import Anchor, AnchorRejection, resolve_anchor
from app.modules.reviews.application.diff_map import DiffMap, is_supported_path
from app.modules.reviews.application.review_output import (
    Effort,
    ReviewFinding,
    ReviewFindingV2,
    ReviewOutput,
    ReviewSummary,
    finding_order_key,
    is_valid_service_defaults_id,
    render_review_body,
)
from app.modules.reviews.application.run_coverage import (
    CoverageItem,
    CoverageStatus,
    RunCoverage,
    finalize_run_coverage,
)
from app.modules.reviews.application.verdict import (
    PUBLISHED_SEVERITIES,
    SeverityThresholdValue,
    coverage_verdict,
    severity_count_phrase,
    severity_counts,
)

DropReason = (
    Literal[
        "lint_pattern",
        "duplicate",
        "severity_threshold",
        "unknown_path",
        "line_absent",
        "anchor_not_visible",
        "evidence_not_shown",
        "evidence_source_disallowed",
        "revision_mismatch",
        "insufficient_context",
        "contradicted",
    ]
    | str
)
Bucket = Literal["inline", "body_only", "dropped"]

REVIEW_BODY_CHAR_BUDGET = 4000
_ADDITIONAL_FINDINGS_HEADING = "## Additional findings"

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

    finding: ReviewFinding | ReviewFindingV2
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
    omitted_body_only_count: int = 0
    review_body_budget_degraded: bool = False
    summary: ReviewSummary | None = None
    coverage: RunCoverage = field(kw_only=True)

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
        diff_map: DiffMap,
        checked_paths: frozenset[str],
        rule_names: frozenset[str],
        repository_max_inline: int | None = None,
        severity_threshold: SeverityThresholdValue = "all",
        allowed_v2_rules: frozenset[str] | None = None,
        pre_dropped: Sequence[ProcessedFinding] = (),
        apply_severity_threshold: bool = True,
        coverage: RunCoverage | None = None,
        summary_only: bool = False,
        incomplete_paths: tuple[str, ...] = (),
    ) -> ProcessedReviewOutput:
        """Classify every finding once in the documented stage order.

        The repository severity threshold runs after lint and dedupe, before routing and the
        inline cap (docs/PIPELINE_SPEC.md §9).
        """
        if coverage is None:
            coverage = RunCoverage(
                CoverageStatus.PARTIAL,
                0,
                reasons=(CoverageItem(None, "classification_unavailable"),),
                head_sha=diff_map.head_sha,
            )
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
        dropped: list[ProcessedFinding] = list(pre_dropped)
        if summary_only:
            # An overview has no analyzed code scope from which to support a finding.
            dropped.extend(
                ProcessedFinding(item.finding, item.position, "dropped", "insufficient_context")
                for item in candidates
            )
            candidates = []

        candidates, lint_dropped = self._drop_lint_patterns(candidates)
        dropped.extend(lint_dropped)
        candidates, duplicate_dropped = self._deduplicate(candidates)
        dropped.extend(duplicate_dropped)
        if apply_severity_threshold:
            candidates, below_threshold = self._drop_below_threshold(candidates, severity_threshold)
            dropped.extend(below_threshold)

        prepared: list[ProcessedFinding] = []
        for item in candidates:
            finding, bucket = _classify_anchor(item.finding, diff_map)
            if bucket == "dropped":
                dropped.append(ProcessedFinding(finding, item.position, "dropped", "unknown_path"))
                continue
            if finding.confidence < self._patterns.min_confidence:
                bucket = "body_only"
            prepared.append(ProcessedFinding(finding, item.position, bucket))

        inline_candidates = [item for item in prepared if item.bucket == "inline"]
        inline_candidates.sort(key=_quality)
        inline = inline_candidates[:cap]
        overflow = [
            ProcessedFinding(item.finding, item.position, "body_only")
            for item in inline_candidates[cap:]
        ]
        body_only = [item for item in prepared if item.bucket == "body_only"] + overflow

        inline = [
            self._repair_attribution(item, rule_names, allowed_v2_rules=allowed_v2_rules)
            for item in inline
        ]
        body_only = [
            self._repair_attribution(item, rule_names, allowed_v2_rules=allowed_v2_rules)
            for item in body_only
        ]
        moved_to_body = [item for item in inline if item.bucket == "body_only"]
        inline = [item for item in inline if item.bucket == "inline"]
        body_only.extend(moved_to_body)
        dropped.sort(key=lambda item: item.position)
        total_below_threshold = sum(
            1 for item in dropped if item.drop_reason == "severity_threshold"
        )
        kept = tuple(inline + body_only)
        coverage = finalize_run_coverage(
            coverage,
            incomplete_paths=incomplete_paths,
            unknown_path_count=sum(item.drop_reason == "unknown_path" for item in dropped),
        )
        final_output = output.model_copy(
            update={
                "summary": summary_from_result(
                    (item.finding.severity for item in kept),
                    (item.finding.category for item in kept),
                    coverage,
                )
            }
        )
        review_body, omitted_body_only_count = _pack_review_body(
            final_output,
            kept,
            tuple(body_only),
            checked_paths,
            severity_threshold,
            total_below_threshold,
            coverage=coverage,
        )
        return ProcessedReviewOutput(
            inline=tuple(inline),
            body_only=tuple(body_only),
            dropped=tuple(dropped),
            review_body=review_body,
            omitted_body_only_count=omitted_body_only_count,
            summary=final_output.summary,
            coverage=coverage,
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
    def _repair_attribution(
        item: ProcessedFinding,
        rule_names: frozenset[str],
        allowed_v2_rules: frozenset[str] | None = None,
    ) -> ProcessedFinding:
        finding = item.finding
        if allowed_v2_rules is not None and finding.rule_name is not None:
            if finding.rule_name in allowed_v2_rules:
                return item
            if finding.rule_name.startswith("service-defaults:"):
                rule_id = finding.rule_name.removeprefix("service-defaults:")
                if is_valid_service_defaults_id(rule_id):
                    return item
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


def _classify_anchor(
    finding: ReviewFinding | ReviewFindingV2, diff_map: DiffMap
) -> tuple[ReviewFinding | ReviewFindingV2, Bucket | Literal["dropped"]]:
    """Map one finding to inline, body_only, or dropped via DiffMap anchoring."""
    anchor = resolve_anchor(
        diff_map, path=finding.path, start_line=finding.start_line, line=finding.line
    )
    if not isinstance(anchor, Anchor) and finding.start_line is not None:
        single = resolve_anchor(diff_map, path=finding.path, start_line=None, line=finding.line)
        if isinstance(single, Anchor):
            updates: dict[str, object] = {"start_line": None}
            if finding.suggestion is not None:
                updates["suggestion"] = None
            finding = finding.model_copy(update=updates)
            anchor = single
    if isinstance(anchor, Anchor):
        return finding, "inline"
    if anchor is AnchorRejection.UNKNOWN_PATH:
        return finding, "dropped"
    return finding, "body_only"


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _quality(item: ProcessedFinding) -> tuple[int, float, int]:
    return (*finding_order_key(item.finding.severity, item.finding.confidence), item.position)


def summary_from_result(
    severities: Iterable[str], categories: Iterable[str], coverage: RunCoverage
) -> ReviewSummary:
    """One backend summary from the already filtered findings and actual scope."""
    counts = severity_counts(severities)
    category_counts = Counter(categories)
    count = sum(counts.values())
    problem = f"Подтверждено замечаний: {count}" if count else "Подтверждённых замечаний нет"
    if category_counts:
        problem += (
            " (" + ", ".join(f"{name}: {n}" for name, n in sorted(category_counts.items())) + ")"
        )
    if coverage.status is not CoverageStatus.COMPLETE:
        problem += "; " + coverage.summary_problem().removesuffix(".")
    effort: Effort = (
        "large"
        if counts["critical"]
        else "medium"
        if counts["high"]
        else "small"
        if counts["medium"] or counts["low"]
        else "none"
    )
    return ReviewSummary(
        problem=problem + ".",
        done_well=(
            f"Проанализировано файлов с текстовыми изменениями: {coverage.reviewed_files}; "
            f"исключено файлов: {sum(coverage.exclusion_counts.values())}."
        ),
        effort=effort,
    )


def _format_checked_files(sorted_paths: tuple[str, ...], listed_count: int) -> str:
    if not sorted_paths:
        return "(none)"
    lines = [f"- {path}" for path in sorted_paths[:listed_count]]
    omitted = len(sorted_paths) - listed_count
    if omitted > 0:
        lines.append(f"- … и ещё {_ru_cardinal(omitted, 'файл', 'файла', 'файлов')}")
    return "\n".join(lines)


def _ru_cardinal(count: int, one: str, few: str, many: str) -> str:
    """Russian cardinal agreement: 1 файл, 2 файла, 5 файлов, 11 файлов, 21 файл."""
    remainder = abs(count) % 100
    if 11 <= remainder <= 14:
        word = many
    else:
        last = remainder % 10
        if last == 1:
            word = one
        elif 2 <= last <= 4:
            word = few
        else:
            word = many
    return f"{count} {word}"


def _overview_block(
    kept: tuple[ProcessedFinding, ...],
    checked_files_text: str,
    severity_threshold: SeverityThresholdValue,
    below_threshold: int,
    *,
    coverage: RunCoverage,
) -> str:
    severities = [item.finding.severity for item in kept]
    counts = severity_counts(severities)
    count_text = severity_count_phrase(counts)
    run_verdict = coverage_verdict(severities, coverage.status)
    coverage_lines = (
        ()
        if coverage.status is CoverageStatus.COMPLETE
        else (
            f"**Coverage:** {coverage.status.value} — {coverage.summary_problem()}",
            coverage.body_explanation(),
        )
    )
    checked_lines = (
        (f"**Проверено файлов с текстовыми изменениями:** {coverage.reviewed_files}",)
        if coverage.status is not CoverageStatus.COMPLETE
        else ()
    ) + ("**Checked files:**", checked_files_text)
    below = (
        (f"**Below severity threshold ({severity_threshold}):** {below_threshold}",)
        if below_threshold
        else ()
    )
    unavailable_label = (
        "нет доступных для анализа изменений"
        if coverage.status is CoverageStatus.NONE
        else "проверка неполная"
    )
    return "\n".join(
        (
            f"**Verdict:** {run_verdict or unavailable_label}",
            f"**Risk summary:** {count_text}",
            *below,
            *coverage_lines,
            *checked_lines,
        )
    )


def format_body_only_overflow_footer(count: int, run_url: str) -> str:
    """Russian overflow line appended at store time when the packer omitted body-only bullets."""
    phrase = _ru_cardinal(count, "находка", "находки", "находок")
    return f"\n\n**Ещё {phrase}:** {run_url}"


def format_body_only_overflow_footer_without_url(count: int) -> str:
    """Overflow count without a run link when the portal URL is not known yet."""
    phrase = _ru_cardinal(count, "находка", "находки", "находок")
    return f"\n\n**Ещё {phrase}**"


def finalize_stored_review_body(
    output: ReviewOutput,
    processed: ProcessedReviewOutput,
    *,
    checked_paths: frozenset[str],
    severity_threshold: SeverityThresholdValue,
    below_threshold: int,
    run_url: str | None,
) -> ProcessedReviewOutput:
    """Append the overflow footer at store time, repacking first when footer space is needed."""
    if processed.summary is not None:
        output = output.model_copy(update={"summary": processed.summary})
    omitted = processed.omitted_body_only_count
    if omitted <= 0:
        return processed

    kept = (*processed.inline, *processed.body_only)

    def footer_for(count: int) -> str:
        if run_url:
            return format_body_only_overflow_footer(count, run_url)
        return format_body_only_overflow_footer_without_url(count)

    def repack(char_budget: int) -> tuple[str, int]:
        return _pack_review_body(
            output,
            kept,
            processed.body_only,
            checked_paths,
            severity_threshold,
            below_threshold,
            char_budget=char_budget,
            coverage=processed.coverage,
        )

    body = processed.review_body
    for _ in range(4):
        footer = footer_for(omitted)
        if len(body) + len(footer) > REVIEW_BODY_CHAR_BUDGET:
            body, omitted = repack(REVIEW_BODY_CHAR_BUDGET - len(footer))
        footer = footer_for(omitted)
        if len(body) + len(footer) <= REVIEW_BODY_CHAR_BUDGET:
            return replace(
                processed,
                review_body=f"{body}{footer}",
                omitted_body_only_count=omitted,
            )
    footer = footer_for(omitted)
    max_body_len = max(0, REVIEW_BODY_CHAR_BUDGET - len(footer))
    body, omitted = repack(max_body_len)
    footer = footer_for(omitted)
    if len(body) + len(footer) > REVIEW_BODY_CHAR_BUDGET:
        body = body[: max(0, REVIEW_BODY_CHAR_BUDGET - len(footer))].rstrip()
    return replace(
        processed,
        review_body=f"{body}{footer}",
        omitted_body_only_count=omitted,
        review_body_budget_degraded=True,
    )


def _pack_review_body(
    output: ReviewOutput,
    kept: tuple[ProcessedFinding, ...],
    body_only: tuple[ProcessedFinding, ...],
    checked_paths: frozenset[str],
    severity_threshold: SeverityThresholdValue,
    below_threshold: int,
    *,
    char_budget: int = REVIEW_BODY_CHAR_BUDGET,
    coverage: RunCoverage,
) -> tuple[str, int]:
    prose = render_review_body(output.summary)
    heading, separator, rest = prose.partition("\n\n")
    # Checked paths describe durable text inputs (#170), not fully reviewed
    # allocations (#213). An unknown policy or any ignored allocation withholds
    # names conservatively because bounded coverage diagnostics hide ignored paths.
    allowed = frozenset(path for path in checked_paths if is_supported_path(path))
    if (
        coverage.exclusion_counts.get("ignored", 0)
        or "classification_unavailable" in coverage.reason_counts
    ):
        allowed = frozenset()
    sorted_paths = tuple(sorted(allowed))
    finding_lines = [_render_body_only_finding(item) for item in body_only]
    path_count = len(sorted_paths)

    def assemble(listed_paths: int, finding_count: int) -> str:
        checked = _format_checked_files(sorted_paths, listed_paths)
        overview = _overview_block(
            kept, checked, severity_threshold, below_threshold, coverage=coverage
        )
        prefix = f"{heading}{separator}{overview}\n\n{rest}"
        if finding_count == 0:
            return prefix
        lines = [prefix, "", _ADDITIONAL_FINDINGS_HEADING, *finding_lines[:finding_count]]
        return "\n".join(lines)

    best_findings = 0
    best_listed = 0
    best_body = assemble(0, 0)

    for listed_paths in range(path_count, -1, -1):
        for finding_count in range(len(finding_lines), -1, -1):
            candidate = assemble(listed_paths, finding_count)
            if len(candidate) > char_budget:
                continue
            better = (
                finding_count > best_findings
                or (finding_count == best_findings and listed_paths > best_listed)
                or (
                    finding_count == best_findings
                    and listed_paths == best_listed
                    and len(candidate) > len(best_body)
                )
            )
            if better:
                best_body = candidate
                best_findings = finding_count
                best_listed = listed_paths

    omitted = len(finding_lines) - best_findings
    return best_body, omitted


def _render_body_only_finding(item: ProcessedFinding) -> str:
    return (
        f"- **{item.finding.title}** "
        f"(`{item.finding.path}:{item.finding.line}`): {item.finding.body}"
    )
