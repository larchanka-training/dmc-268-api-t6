from __future__ import annotations

from collections.abc import Iterable


def collect_required_checks(check_runs: Iterable[str]) -> list[str]:
    return sorted(set(check_runs))
