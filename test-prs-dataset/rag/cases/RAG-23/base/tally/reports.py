"""Progress report per area."""

from .areas import AREAS
from .counts import done, planned
from .ratios import percent


def progress_line(area: str) -> str:
    return f"{area}: {percent(done(area), planned(area))}%"


def report() -> str:
    return "\n".join(progress_line(area) for area in AREAS)
