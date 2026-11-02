"""Booking of appointments for patients."""

from datetime import date

from .schedule import open_slots
from .waitlist import add_to_waitlist


def book_first_free(patient_id: str, day: date) -> str:
    slots = open_slots(day)
    if not slots:
        add_to_waitlist(patient_id, day)
        return "waitlisted"
    slot = slots[0]
    return f"{patient_id} booked with {slot.doctor} at {slot.start:%H:%M}"
