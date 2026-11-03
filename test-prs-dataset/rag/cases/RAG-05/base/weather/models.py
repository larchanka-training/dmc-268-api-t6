"""Records of the weather desk."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Forecast:
    """Daily temperatures of one city, in degrees Celsius."""

    city: str
    high_c: int
    low_c: int
