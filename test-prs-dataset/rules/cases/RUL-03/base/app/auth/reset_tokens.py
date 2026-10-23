"""Password reset tokens."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

TOKEN_TTL = timedelta(minutes=30)


def issue_token(db: sqlite3.Connection, user_id: int, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, (now + TOKEN_TTL).isoformat()),
    )
    return token
