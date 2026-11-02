"""Runs one job until it succeeds or runs out of tries."""

import time
from collections.abc import Callable

from .models import Job
from .scheduling import next_delay

MAX_ATTEMPTS = 5


def run_job(job: Job, handler: Callable[[Job], bool]) -> bool:
    while job.attempts < MAX_ATTEMPTS:
        if handler(job):
            return True
        job.attempts += 1
        time.sleep(next_delay(job))
    return False
