"""Token cache fake shared by retirement and snapshot tests."""

from __future__ import annotations

from datetime import datetime


class DiscardTokenCache:
    """Each phase mints a token so the inner HTTP boundary is exercised."""

    async def get(self, installation_external_id: int) -> None:
        return None

    async def set(
        self, installation_external_id: int, access_token: str, expires_at: datetime
    ) -> None:
        pass
