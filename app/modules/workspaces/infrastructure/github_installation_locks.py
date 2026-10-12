"""Shared PostgreSQL account serialization for OAuth and positive projection."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_github_account(session: AsyncSession, account_id: int) -> None:
    key = int.from_bytes(
        sha256(f"github-reinstall-account:{account_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
