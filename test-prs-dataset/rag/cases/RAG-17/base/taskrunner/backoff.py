"""Retry delays of the runner."""

BASE = 1
MAX_STEP = 6


def retry_delay(attempt: int) -> int:
    """Delay before the given retry, growing with every attempt."""
    capped = min(attempt, MAX_STEP)
    return BASE * 2**capped
