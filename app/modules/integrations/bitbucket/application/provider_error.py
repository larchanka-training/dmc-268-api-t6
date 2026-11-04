"""Safe Bitbucket provider outcomes shared by transport and application layers."""

from __future__ import annotations


class BitbucketProviderError(ValueError):
    """Stable error code and retry delay; never carries a response body or credential."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


class BitbucketPullRequestResponseError(BitbucketProviderError):
    """Malformed response scoped to one PR, safe for a push page to skip."""

    def __init__(self) -> None:
        super().__init__("invalid_provider_response")
