"""Ratios shown in the reports."""


def percent(part: int, whole: int) -> float:
    if whole == 0:
        return 0.0
    return round(100 * part / whole, 1)
