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
TELEGRAM_MAX_MESSAGE_LEN = 4096


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


def _grouped_blocks(
    items: Sequence[Item],
    team: Sequence[Member],
    aliases: Mapping[str, str],
    now: datetime,
    age_after: timedelta | None,
) -> list[tuple[str, list[tuple[Item, str]]]]:
    nicks = {member.key: member.telegram for member in team}
    by_reviewer: dict[str, list[Item]] = {}
    for item in _oldest_first(items):
        by_reviewer.setdefault(item.request.reviewer, []).append(item)
    groups: list[tuple[str, list[tuple[Item, str]]]] = []
    # Blocks in the order of their oldest pull request.
    for reviewer, own in by_reviewer.items():
        nick = nicks.get(reviewer)
        name = f"@{escape(nick)}" if nick else escape(own[0].request.login)
        entries = [(item, _line(item, aliases, now, age_after)) for item in own]
        groups.append((name, entries))
    return groups


def _render_chunk(header: str, blocks: Sequence[tuple[str, list[str]]]) -> str:
    rendered_blocks = ["\n".join([name, *lines]) for name, lines in blocks]
    return "\n\n".join([header, *rendered_blocks, FOOTER])


def _chunk_messages(
    header: str,
    items: Sequence[Item],
    team: Sequence[Member],
    aliases: Mapping[str, str],
    now: datetime,
    age_after: timedelta | None,
    *,
    max_len: int = TELEGRAM_MAX_MESSAGE_LEN,
) -> list[tuple[str, list[Item]]]:
    groups = _grouped_blocks(items, team, aliases, now, age_after)
    chunks: list[tuple[str, list[Item]]] = []
    current_blocks: list[tuple[str, list[str]]] = []
    current_items: list[Item] = []

    for name, entries in groups:
        for item, line in entries:
            if current_blocks and current_blocks[-1][0] == name:
                candidate = [*current_blocks[:-1], (name, [*current_blocks[-1][1], line])]
            else:
                candidate = [*current_blocks, (name, [line])]
            if current_items and len(_render_chunk(header, candidate)) > max_len:
                chunks.append((_render_chunk(header, current_blocks), current_items))
                current_blocks = [(name, [line])]
                current_items = [item]
            else:
                current_blocks = candidate
                current_items.append(item)
    if current_items:
        chunks.append((_render_chunk(header, current_blocks), current_items))
    return chunks


def batch_messages(
    items: Sequence[Item], team: Sequence[Member], aliases: Mapping[str, str], now: datetime
) -> list[tuple[str, list[Item]]]:
    count = len({(i.pr.repo, i.pr.number) for i in items})
    return _chunk_messages(
        BATCH_HEADER.format(count=count), items, team, aliases, now, age_after=None
    )


def digest_messages(
    items: Sequence[Item],
    team: Sequence[Member],
    aliases: Mapping[str, str],
    now: datetime,
    digest_time: str,
    age_after: timedelta,
) -> list[tuple[str, list[Item]]]:
    return _chunk_messages(
        DIGEST_HEADER.format(time=digest_time), items, team, aliases, now, age_after=age_after
    )


def batch_message(
    items: Sequence[Item], team: Sequence[Member], aliases: Mapping[str, str], now: datetime
) -> str:
    return "\n\n".join(text for text, _ in batch_messages(items, team, aliases, now))


def digest_message(
    items: Sequence[Item],
    team: Sequence[Member],
    aliases: Mapping[str, str],
    now: datetime,
    digest_time: str,
    age_after: timedelta,
) -> str:
    return "\n\n".join(
        text for text, _ in digest_messages(items, team, aliases, now, digest_time, age_after)
    )


def mine_message(
    items: Sequence[Item], aliases: Mapping[str, str], now: datetime, age_after: timedelta
) -> str:
    if not items:
        return "На тебе сейчас нет PR, которые ждут ревью."
    lines = [_line(item, aliases, now, age_after) for item in _oldest_first(items)]
    return "\n".join([f"Твои PR сейчас: {len(lines)}", "", *lines])


def unknown_sender_message() -> str:
    return "Не нашёл тебя в составе команды бота: попроси добавить твой Telegram-ник."
