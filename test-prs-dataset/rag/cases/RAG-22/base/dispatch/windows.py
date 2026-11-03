"""Booking of delivery windows."""

from datetime import datetime, timedelta

from .schedule import BOOKINGS


def book_slot(start: datetime, minutes: int) -> datetime:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    end = start + timedelta(minutes=minutes)
    BOOKINGS.append((start, end))
    return end
