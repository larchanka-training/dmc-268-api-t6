"""Entry point of the support form."""

from .requests import log_request


def submit(account_id: str) -> str:
    log_request(account_id)
    return "queued"
