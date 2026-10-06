"""The installation access token could not be obtained."""

from __future__ import annotations


class InstallationAccessTokenError(Exception):
    """An adapter could not obtain the installation access token.

    The failure belongs to the installation, not to the repository whose request
    needed the token: the original error is the ``__cause__``, and the text never
    carries a URL, a response body or a token. ``transient`` is true when GitHub could
    not answer the token request (a transport error, a timeout or an error status),
    which a later attempt can heal; a malformed token response or an App key that
    cannot sign is a permanent fault.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient
