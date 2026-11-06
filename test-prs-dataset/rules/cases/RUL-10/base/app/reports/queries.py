"""Read queries of the reports service."""

from __future__ import annotations

from typing import Any, Protocol


class Connection(Protocol):
    async def fetch(self, query: str) -> list[dict[str, Any]]: ...


async def report_by_id(conn: Connection, report_id: int) -> dict[str, Any]:
    rows = await conn.fetch(f"SELECT * FROM reports WHERE id = {report_id}")
    return rows[0]


async def latest_for_team(conn: Connection, team: str) -> dict[str, Any]:
    rows = await conn.fetch(f"SELECT * FROM reports WHERE team = '{team}' ORDER BY created_at DESC")
    return rows[0]


async def owner_of(conn: Connection, report_id: int) -> dict[str, Any]:
    rows = await conn.fetch(f"SELECT owner FROM reports WHERE id = {report_id}")
    return rows[0]
