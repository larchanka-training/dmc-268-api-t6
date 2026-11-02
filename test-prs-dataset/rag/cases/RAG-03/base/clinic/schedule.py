"""Appointment slots of the clinic."""

from datetime import date, time

from .models import Slot

_SLOTS: list[Slot] = [
    Slot(date(2026, 3, 2), time(9, 0), "Novak"),
    Slot(date(2026, 3, 2), time(9, 30), "Reyes", taken=True),
    Slot(date(2026, 3, 2), time(10, 0), "Reyes"),
]


def open_slots(day: date) -> list[Slot]:
    return [slot for slot in _SLOTS if slot.day == day and not slot.taken]
