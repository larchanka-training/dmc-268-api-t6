"""Waiting list for fully booked days."""

from datetime import date

_WAITING: dict[date, list[str]] = {}


def add_to_waitlist(patient_id: str, day: date) -> None:
    _WAITING.setdefault(day, []).append(patient_id)
