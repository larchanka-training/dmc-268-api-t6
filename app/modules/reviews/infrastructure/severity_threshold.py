"""Translate stored publication thresholds into the application contract."""

from __future__ import annotations

from app.common.infrastructure.db.enums import SeverityThreshold
from app.modules.reviews.application.verdict import SeverityThresholdValue


def severity_threshold_value(threshold: SeverityThreshold) -> SeverityThresholdValue:
    """Map every native enum member; mypy rejects an incomplete mapping."""
    match threshold:
        case SeverityThreshold.ALL:
            return "all"
        case SeverityThreshold.WARNING:
            return "warning"
        case SeverityThreshold.CRITICAL:
            return "critical"
