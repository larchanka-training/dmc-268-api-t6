"""Intake of erasure requests."""

from .audit import record


def log_request(account_id: str) -> None:
    record("erasure_requested", account_id, 0)
