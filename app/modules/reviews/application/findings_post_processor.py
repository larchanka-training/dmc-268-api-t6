"""Pure deterministic filtering of parsed model review findings."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from importlib.resources import files
from typing import Literal

from app.modules.reviews.application.check_runs import summary_only_line
from app.modules.reviews.application.coverage_schema import REASON_TEXT
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
    Verdict,
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
        "ignored_file",
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
RiskScore = Literal["High", "Medium", "Low"]

REVIEW_BODY_CHAR_BUDGET = 4000
_ADDITIONAL_FINDINGS_HEADING = "## Additional findings"
_CHECKED_FILES_WITHHELD = "(список не показывается: имена файлов в этом ревью скрыты)"

_EXECUTIVE_BLOCK_CHAR_LIMIT = 1500
# Names on one checklist line, the first that keeps the block within its limit.
_CHECKLIST_NAME_LIMITS = (5, 3, 1, 0)
# CVS §8.2: the change summary agent's text is a signed description, never a finding.
_CHANGE_SUMMARY_SIGNATURE = "**What changed** (автоописание, не замечание):"
_DISPLAYED_NAME_CHARS = 120
# CVS §8.2: such a name is unusable or unproven, so only its reason and count are public.
_COUNT_ONLY_REASONS = frozenset({"unsupported_path", "identity_unresolved"})
_UNNAMED_FILE_LABEL = "имя файла скрыто"
_SENSITIVE_FILES = (
    (
        "Изменённые миграции",
        re.compile(r"(?:^|/)(?:migrations|alembic/versions|db/migrate)/"),
    ),
    (
        "Изменённые CI-workflows",
        re.compile(
            r"^(?:\.github/workflows/|\.circleci/|\.gitlab-ci\.yml$|bitbucket-pipelines\.yml$)"
        ),
    ),
    (
        "Изменённые манифесты зависимостей",
        re.compile(
            r"(?:^|/)(?:pyproject\.toml|requirements[^/]*\.txt|setup\.py|setup\.cfg|Pipfile"
            r"|package\.json|go\.mod|Cargo\.toml|pom\.xml|build\.gradle(?:\.kts)?|Gemfile"
            r"|composer\.json)$"
        ),
    ),
)

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
    # The GitHub link to the rules lines of an inline finding (#465); set by the store.
    rule_link: str | None = None


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
    # Rendered once by ``process``: a store-time repack has no diff map to build it from.
    executive_block: str = field(default="", kw_only=True)
    # How many leading sentences of the change summary the block shows (#316 Part 3).
    change_summary_shown: int = field(default=0, kw_only=True)
    # Canonical names of files with an omission reason, never listed in the body (CVS §8.2).
    unnamed_paths: frozenset[str] = field(default=frozenset(), kw_only=True)

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
        summary_reason: str | None = None,
        change_summary: tuple[str, ...] = (),
    ) -> ProcessedReviewOutput:
        """Classify every finding once in the documented stage order.

        The repository severity threshold runs after lint and dedupe, before routing and the
        inline cap (docs/PIPELINE_SPEC.md §9). ``change_summary`` is the change summary
        agent's answer, shown signed in the Executive summary and never hashed.
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
                    summary_reason=summary_reason if summary_only else None,
                    name_summary_only=summary_only,
                )
            }
        )
        # A finding reaches such a file by its provider name as well (``resolve_anchor``).
        unnamed_paths = frozenset(
            name
            for file in diff_map.files
            if file.omission_reason is not None
            for name in (
                file.new_path,
                file.old_path,
                file.provider_new_path,
                file.provider_old_path,
            )
            if name
        )
        executive_block, change_summary_shown = _executive_block(
            kept,
            coverage,
            diff_map,
            unnamed_paths=unnamed_paths,
            summary_only=summary_only,
            change_summary=change_summary,
        )
        review_body, omitted_body_only_count = _pack_review_body(
            final_output,
            kept,
            tuple(body_only),
            checked_paths,
            severity_threshold,
            total_below_threshold,
            coverage=coverage,
            executive_block=executive_block,
            unnamed_paths=unnamed_paths,
        )
        return ProcessedReviewOutput(
            inline=tuple(inline),
            body_only=tuple(body_only),
            dropped=tuple(dropped),
            review_body=review_body,
            omitted_body_only_count=omitted_body_only_count,
            summary=final_output.summary,
            coverage=coverage,
            executive_block=executive_block,
            change_summary_shown=change_summary_shown,
            unnamed_paths=unnamed_paths,
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
    severities: Iterable[str],
    categories: Iterable[str],
    coverage: RunCoverage,
    summary_reason: str | None = None,
    *,
    name_summary_only: bool = False,
) -> ReviewSummary:
    """One backend summary from the already filtered findings and actual scope."""
    counts = severity_counts(severities)
    category_counts = Counter(categories)
    count = sum(counts.values())
    if name_summary_only:
        # Same line as the check-run, including the one fallback for an unknown reason.
        problem = f"{summary_only_line(summary_reason)}."
    else:
        problem = f"Подтверждено замечаний: {count}" if count else "Подтверждённых замечаний нет"
        if category_counts:
            problem += (
                " ("
                + ", ".join(f"{name}: {n}" for name, n in sorted(category_counts.items()))
                + ")"
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
        problem=problem if name_summary_only else problem + ".",
        done_well=(
            f"Проанализировано файлов с текстовыми изменениями: {coverage.reviewed_files}; "
            f"исключено файлов: {sum(coverage.exclusion_counts.values())}."
        ),
        effort=effort,
    )


def _format_checked_files(
    sorted_paths: tuple[str, ...], listed_count: int, *, withheld: bool = False
) -> str:
    if withheld:
        return _CHECKED_FILES_WITHHELD
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


def _risk_score(
    run_verdict: Verdict | None, coverage: RunCoverage, *, summary_only: bool
) -> RiskScore:
    """The verdict read with the completeness of the review (PIPELINE_SPEC §11)."""
    if run_verdict == "blocking":
        return "High"
    if run_verdict == "attention" or summary_only:
        return "Medium"
    # Exclusions are not incompleteness: a change of only binary or ignored files stays Low.
    if coverage.status is CoverageStatus.PARTIAL or coverage.reason_counts:
        return "Medium"
    return "Low"


def _names_withheld(coverage: RunCoverage) -> bool:
    """An unknown policy or any ignored allocation: a name from the diff may be a private one.

    Bounded coverage diagnostics hide ignored paths, so they cannot be told apart.
    """
    return bool(
        coverage.exclusion_counts.get("ignored", 0)
        or "classification_unavailable" in coverage.reason_counts
    )


def _inline_code(text: str) -> str:
    """Bound a name and fence it, so that a backtick in it cannot end the code span."""
    display = text[:_DISPLAYED_NAME_CHARS]
    runs = re.findall(r"`+", display)
    if not runs:
        return f"`{display}`"
    fence = "`" * (1 + max(len(run) for run in runs))
    return f"{fence} {display} {fence}"


def _checklist_line(label: str, count: int, names: Sequence[str], limit: int) -> str:
    listed = names[:limit]
    if not listed:
        return f"- {label} ({count})"
    remaining = count - len(listed)
    return f"- {label} ({count}): {', '.join(listed)}" + (
        f" и ещё {remaining}" if remaining > 0 else ""
    )


def _unreviewed_file_lines(coverage: RunCoverage, limit: int) -> list[str]:
    """One line per reason of the coverage: the classification of the diff under its policy.

    ``reason_counts`` stays exact when the saved ``reasons`` are bounded.
    """
    names: dict[str, list[str]] = {}
    for item in coverage.reasons:
        if item.path is not None and item.reason not in _COUNT_ONLY_REASONS:
            names.setdefault(item.reason, []).append(_inline_code(item.path))
    return [
        _checklist_line(
            f"Не проверено — {REASON_TEXT.get(reason, reason)}",
            count,
            sorted(names.get(reason, ())),
            limit,
        )
        for reason, count in sorted(coverage.reason_counts.items())
    ]


def _hide_names(text: str, unnamed_paths: frozenset[str]) -> str:
    """The text of a finding on another file may repeat the exact name of an unnamed one.

    Only a whole path counts: ``app/utils.py`` is another file, even when ``utils.py`` is hidden.
    """
    if not unnamed_paths:
        return text
    names = "|".join(re.escape(name) for name in sorted(unnamed_paths, key=len, reverse=True))
    return re.sub(
        rf"(?<![\w./\\-])(?:\.?/)?(?:{names})(?![\w/\\-]|\.\w)", _UNNAMED_FILE_LABEL, text
    )


def _unnamed_finding(finding: ReviewFinding | ReviewFindingV2) -> str:
    return f"**Находка** ({_UNNAMED_FILE_LABEL}, {finding.severity})"


def _blocking_finding_lines(
    kept: tuple[ProcessedFinding, ...], limit: int, unnamed_paths: frozenset[str]
) -> list[str]:
    blocking = sorted(
        (item.finding for item in kept if item.finding.severity in {"critical", "high"}),
        key=lambda finding: (finding.severity, finding.path, finding.line, finding.title),
    )
    if not blocking:
        return []
    names = [
        _unnamed_finding(finding)
        if finding.path in unnamed_paths
        else (
            f"**{_hide_names(finding.title, unnamed_paths)}** "
            f"({_inline_code(f'{finding.path}:{finding.line}')})"
        )
        for finding in blocking
    ]
    return [_checklist_line("Подтвердите находки critical/high", len(names), names, limit)]


def _sensitive_file_lines(diff_map: DiffMap, coverage: RunCoverage, limit: int) -> list[str]:
    """Changed migrations, CI workflows and dependency manifests, reviewed or not.

    A file with an omission reason has no usable name and is already on its reason's line.
    """
    lines = []
    for label, pattern in _SENSITIVE_FILES:
        paths = []
        for file in diff_map.files:
            names = [name for name in (file.new_path, file.old_path) if name]
            matched = [name for name in names if pattern.search(name)]
            if file.omission_reason is None and matched:
                # A rename shows the name that made it a migration, a workflow or a manifest.
                paths.append(_inline_code(matched[0]))
        if paths:
            shown = 0 if _names_withheld(coverage) else limit
            lines.append(_checklist_line(label, len(paths), sorted(paths), shown))
    return lines


def _executive_block(
    kept: tuple[ProcessedFinding, ...],
    coverage: RunCoverage,
    diff_map: DiffMap,
    *,
    unnamed_paths: frozenset[str],
    summary_only: bool,
    change_summary: tuple[str, ...],
) -> tuple[str, int]:
    """The block and how many leading sentences of the change summary it shows.

    Over the limit, the checklist names fold first (5, 3, 1, then none), then the last
    sentences go; the verdict and the checklist counts always stay.
    """
    run_verdict = coverage_verdict((item.finding.severity for item in kept), coverage.status)
    unavailable_label = (
        "нет доступных для анализа изменений"
        if coverage.status is CoverageStatus.NONE
        else "проверка неполная"
    )
    risk = _risk_score(run_verdict, coverage, summary_only=summary_only)
    verdict_line = (
        "## Executive summary\n\n"
        f"**Verdict:** {run_verdict or unavailable_label} · **Risk Score:** {risk}"
    )
    sentences = tuple(_hide_names(sentence.strip(), unnamed_paths) for sentence in change_summary)
    block = verdict_line
    for shown in range(len(sentences), -1, -1):
        header = (
            f"{verdict_line}\n\n{_CHANGE_SUMMARY_SIGNATURE} {' '.join(sentences[:shown])}"
            if shown
            else verdict_line
        )
        for limit in _CHECKLIST_NAME_LIMITS:
            lines = (
                *_unreviewed_file_lines(coverage, limit),
                *_blocking_finding_lines(kept, limit, unnamed_paths),
                *_sensitive_file_lines(diff_map, coverage, limit),
            )
            block = "\n".join((f"{header}\n\n**What to check:**", *lines)) if lines else header
            if len(block) <= _EXECUTIVE_BLOCK_CHAR_LIMIT:
                return block, shown
    return block, 0


def _overview_block(
    kept: tuple[ProcessedFinding, ...],
    checked_files_text: str,
    severity_threshold: SeverityThresholdValue,
    below_threshold: int,
    *,
    coverage: RunCoverage,
) -> str:
    counts = severity_counts(item.finding.severity for item in kept)
    count_text = severity_count_phrase(counts)
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
    return "\n".join(
        (
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
            executive_block=processed.executive_block,
            unnamed_paths=processed.unnamed_paths,
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
    executive_block: str,
    unnamed_paths: frozenset[str],
) -> tuple[str, int]:
    prose = render_review_body(output.summary)
    heading, separator, rest = prose.partition("\n\n")
    # Checked paths describe durable text inputs (#170), not fully reviewed
    # allocations (#213). An unknown policy or any ignored allocation withholds
    # names conservatively because bounded coverage diagnostics hide ignored paths;
    # the section then says the list is withheld instead of reading as empty.
    allowed = frozenset(path for path in checked_paths if is_supported_path(path)) - unnamed_paths
    names_withheld = bool(allowed and _names_withheld(coverage))
    sorted_paths = () if names_withheld else tuple(sorted(allowed))
    finding_lines = [_render_body_only_finding(item, unnamed_paths) for item in body_only]
    path_count = len(sorted_paths)

    def assemble(listed_paths: int, finding_count: int) -> str:
        checked = _format_checked_files(sorted_paths, listed_paths, withheld=names_withheld)
        overview = _overview_block(
            kept, checked, severity_threshold, below_threshold, coverage=coverage
        )
        lead = f"{executive_block}\n\n" if executive_block else ""
        prefix = f"{lead}{heading}{separator}{overview}\n\n{rest}"
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


def _render_body_only_finding(item: ProcessedFinding, unnamed_paths: frozenset[str]) -> str:
    finding = item.finding
    if finding.path in unnamed_paths:
        # No model text at all: it would name the file in words an exact match cannot catch.
        return f"- {_unnamed_finding(finding)}"
    return (
        f"- **{_hide_names(finding.title, unnamed_paths)}** "
        f"(`{finding.path}:{finding.line}`): {_hide_names(finding.body, unnamed_paths)}"
    )
