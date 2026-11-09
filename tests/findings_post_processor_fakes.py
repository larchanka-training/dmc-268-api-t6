"""Builders shared by the findings post-processor test files."""

from __future__ import annotations

import re
from typing import cast
from uuid import UUID

from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_output import ReviewOutput
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest


def _added_file_diff_map(path: str, line_count: int) -> DiffMap:
    """One added file whose new-side lines ``1..line_count`` are anchorable."""
    lines = "\n".join(f"+line{n}" for n in range(1, line_count + 1))
    patch = f"@@ -0,0 +1,{line_count} @@\n{lines}"
    return _diff_map_from_vcs(
        VcsFile(
            path,
            "added",
            None,
            None,
            line_count,
            0,
            line_count,
            patch,
        )
    )


def _diff_map_from_vcs(*files: VcsFile) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(
            installation_external_id=0, repository_full_name="acme/app", number=1
        ),
        head_sha="c" * 40,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="",
            description=None,
            author="",
            source_branch="",
            target_branch="",
            labels=(),
            files_changed=len(files),
            lines_added=sum(item.additions for item in files),
            lines_removed=sum(item.deletions for item in files),
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha="a" * 40,
    )


def finding(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "path": "app/example.py",
        "line": 12,
        "start_line": None,
        "severity": "low",
        "category": "correctness",
        "title": "Missing validation",
        "body": "The request is used without checking its required value.",
        "suggestion": "validate(value)",
        "confidence": 0.9,
        "rule_name": None,
    }
    value.update(overrides)
    return value


def output(*findings: dict[str, object]) -> ReviewOutput:
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    ordered = sorted(
        findings,
        key=lambda item: (
            rank[cast(str, item["severity"])],
            -cast(float, item["confidence"]),
        ),
    )
    return ReviewOutput.model_validate(
        {
            "findings": list(ordered),
            "summary": {
                "problem": "A required value is not validated.",
                "done_well": "The endpoint remains small.",
                "effort": "small",
            },
        }
    )


def _count_listed_additional_findings(review_body: str) -> int:
    """Count whole finding bullets under the Additional findings section (test-side D-3 assert)."""
    marker = "\n## Additional findings\n"
    start = review_body.find(marker)
    if start < 0:
        return 0
    tail = review_body[start + len(marker) :]
    footer_marker = "\n\n**Ещё "
    footer_at = tail.find(footer_marker)
    if footer_at >= 0:
        tail = tail[:footer_at]
    return sum(1 for line in tail.splitlines() if line.startswith("- **"))


def _parse_body_only_overflow_footer_count(review_body: str) -> int:
    """Read ``N`` from a store-layer overflow footer (with or without a run URL)."""
    with_url = re.search(r"\n\n\*\*Ещё (\d+) наход(?:ка|ки|ок):\*\*", review_body)
    if with_url is not None:
        return int(with_url.group(1))
    without_url = re.search(r"\n\n\*\*Ещё (\d+) наход(?:ка|ки|ок)\*\*", review_body)
    return int(without_url.group(1)) if without_url is not None else 0


def _body_only_accounted_in_review_body(review_body: str) -> int:
    """Visible Additional findings bullets plus any overflow footer count (D-3)."""
    return _count_listed_additional_findings(review_body) + _parse_body_only_overflow_footer_count(
        review_body
    )
