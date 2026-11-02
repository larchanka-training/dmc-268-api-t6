"""Handlers the runner can call."""

from .models import Job


def send_welcome_mail(job: Job) -> bool:
    address = job.payload.get("address")
    return isinstance(address, str) and "@" in address
