from __future__ import annotations


def findings_recall_percent(matched: int, expected: int) -> float:
    if expected <= 0:
        return 0.0
    return matched / expected * 100.0


def findings_precision_percent(matched: int, predicted: int) -> float:
    if predicted <= 0:
        return 0.0
    return matched / predicted * 100.0
