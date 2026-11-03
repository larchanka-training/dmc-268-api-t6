"""When a job is tried again."""

from .backoff import retry_delay
from .models import Job


def next_delay(job: Job) -> int:
    """Wait before the next try of a job."""
    return retry_delay(job.attempts)
