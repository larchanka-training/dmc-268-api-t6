"""Verdict, severity counts and findings hash of one Run (docs/PIPELINE_SPEC.md §11, §12)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid5

type Verdict = Literal["blocking", "attention", "clean"]

SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass(frozen=True)
class HashedFinding:
    """The published content of one finding with ``drop_reason IS NULL``."""

    path: str
    line_start: int
    line_end: int | None
    severity: str
    category: str
    title: str
    body: str
    suggestion: str | None
    inline: bool


def severity_counts(severities: Iterable[str]) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for severity in severities:
        counts[severity] += 1
    return counts


def verdict(severities: Iterable[str]) -> Verdict:
    counts = severity_counts(severities)
    if counts["critical"] or counts["high"]:
        return "blocking"
    if counts["medium"] or counts["low"]:
        return "attention"
    return "clean"


def review_event(repository_review_event: str, run_verdict: Verdict) -> str:
    """``REQUEST_CHANGES`` only for a blocking verdict of an opted-in repository."""
    if repository_review_event.upper() == "REQUEST_CHANGES" and run_verdict == "blocking":
        return "REQUEST_CHANGES"
    return "COMMENT"


def findings_hash(findings: Iterable[HashedFinding]) -> str:
    """SHA-256 of the sorted findings, 64 lowercase hex characters (``comments.findings_hash``)."""
    items = sorted(
        (asdict(item) for item in findings),
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    return sha256(
        json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def publish_message_id(run_id: UUID, head_sha: str, hash_: str) -> UUID:
    """Deterministic ``review.publish/v1`` ``message_id``: UUIDv5 of run, head and hash."""
    return uuid5(run_id, f"{head_sha}:{hash_}")
