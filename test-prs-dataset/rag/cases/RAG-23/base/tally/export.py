"""CSV export of the progress figures."""

from .areas import AREAS
from .counts import done, planned
from .ratios import percent


def rows() -> list[str]:
    return [f"{area},{percent(done(area), planned(area))}" for area in AREAS]
