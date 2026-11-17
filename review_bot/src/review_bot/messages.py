"""Text of the Telegram messages (HTML parse mode), in the format of the example in #601."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from html import escape

from review_bot.config import Member
from review_bot.models import Item

BATCH_HEADER = "🔎 Ревью ждут {count} PR"
DIGEST_HEADER = "🌙 Висит без ревью к {time}"
FOOTER = "/mine: твои PR сейчас"


def _line(
    item: Item, aliases: Mapping[str, str], now: datetime, age_after: timedelta | None
) -> str:
    pr = item.pr
    ref = f"{aliases.get(pr.repo, pr.repo)}#{pr.number} {pr.title}"
    parts = [f'<a href="{escape(pr.url)}">{escape(ref)}</a>', escape(pr.author)]
    if item.urgent:
        parts.append("urgent")
    if item.request.round > 1:
        parts.append(f"раунд {item.request.round}")
    if age_after is not None:
        waited = now - item.request.queued_at
        if waited > age_after:
            parts.append(f"ждёт {int(waited.total_seconds() // 3600)} ч")
    prefix = "🔥 " if item.urgent else ""
    return f"• {prefix}" + " · ".join(parts)


def _oldest_first(items: Sequence[Item]) -> list[Item]:
    return sorted(items, key=lambda i: (i.request.queued_at, i.pr.repo, i.pr.number))


def _blocks(
    items: Sequence[Item],
    team: Sequence[Member],
    aliases: Mapping[str, str],
    now: datetime,
    age_after: timedelta | None,
) -> list[str]:
    nicks = {member.key: member.telegram for member in team}
    by_reviewer: dict[str, list[Item]] = {}
    for item in _oldest_first(items):
        by_reviewer.setdefault(item.request.reviewer, []).append(item)
    blocks: list[str] = []
    # Blocks in the order of their oldest pull request.
    for reviewer, own in by_reviewer.items():
        nick = nicks.get(reviewer)
        name = f"@{escape(nick)}" if nick else escape(own[0].request.login)
        lines = [_line(item, aliases, now, age_after) for item in own]
        blocks.append("\n".join([name, *lines]))
    return blocks


def batch_message(
    items: Sequence[Item], team: Sequence[Member], aliases: Mapping[str, str], now: datetime
) -> str:
    count = len({(i.pr.repo, i.pr.number) for i in items})
    blocks = _blocks(items, team, aliases, now, age_after=None)
    return "\n\n".join([BATCH_HEADER.format(count=count), *blocks, FOOTER])


def digest_message(
    items: Sequence[Item],
    team: Sequence[Member],
    aliases: Mapping[str, str],
    now: datetime,
    digest_time: str,
    age_after: timedelta,
) -> str:
    blocks = _blocks(items, team, aliases, now, age_after=age_after)
    return "\n\n".join([DIGEST_HEADER.format(time=digest_time), *blocks, FOOTER])


def mine_message(
    items: Sequence[Item], aliases: Mapping[str, str], now: datetime, age_after: timedelta
) -> str:
    if not items:
        return "На тебе сейчас нет PR, которые ждут ревью."
    lines = [_line(item, aliases, now, age_after) for item in _oldest_first(items)]
    return "\n".join([f"Твои PR сейчас: {len(lines)}", "", *lines])


def unknown_sender_message() -> str:
    return "Не нашёл тебя в составе команды бота: попроси добавить твой Telegram-ник."
