RETRYABLE_STATES = frozenset({"failed", "cancelled"})


def can_retry_review(status: str, retries_used: int, retry_limit: int) -> bool:
    return status in RETRYABLE_STATES and retries_used < retry_limit
