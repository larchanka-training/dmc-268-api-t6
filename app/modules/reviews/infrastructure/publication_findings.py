"""Build ``PublishedFinding`` payloads for provider publication (#171)."""

from __future__ import annotations

import logging

from app.modules.reviews.application.diff_map import DiffMap
from app.modules.reviews.application.publication_coordinates import (
    enrich_bitbucket_publication_finding,
    enrich_gitlab_publication_finding,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.infrastructure.models import Finding

_LOGGER = logging.getLogger(__name__)


def published_finding_from_row(
    item: Finding,
    diff_map: DiffMap | None,
    *,
    provider: str,
) -> PublishedFinding:
    """Retry-safe provider payload; GitLab coordinates come from DiffMap when available."""
    finding = PublishedFinding(
        path=item.file_path,
        line=item.line_end or item.line_start,
        start_line=item.line_start if item.line_end is not None else None,
        severity=item.severity.value,
        category=item.category.value,
        title=item.title,
        body=item.body,
        suggestion=item.suggestion,
        confidence=float(item.confidence),
        rule_name=item.rule_name,
        finding_id=item.id,
    )
    if provider == "bitbucket":
        return enrich_bitbucket_publication_finding(diff_map, finding)
    if provider != "gitlab":
        return finding
    if diff_map is None:
        _LOGGER.warning(
            "Publication DiffMap missing for finding %s; GitLab coordinates omitted",
            item.id,
        )
        return finding
    return enrich_gitlab_publication_finding(diff_map, finding)
