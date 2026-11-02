"""Records of the clinic scheduler."""

from dataclasses import dataclass
from datetime import date, time


@dataclass
class Slot:
    """One appointment slot of a doctor."""

    day: date
    start: time
    doctor: str
    taken: bool = False
