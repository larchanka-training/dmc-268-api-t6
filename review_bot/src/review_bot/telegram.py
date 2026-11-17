"""Telegram Bot API client. The token is part of every URL, so no URL is ever logged or raised."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from review_bot.ports import TelegramError, TelegramRejected, Update

Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (fixed https host)


class TelegramClient:
    def __init__(self, token: str, opener: Opener = _default_opener) -> None:
        self._base = f"https://api.telegram.org/bot{token}/"
        self._opener = opener

    def _call(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        request = urllib.request.Request(
            self._base + method,
            method="POST",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            # Telegram answered: the call was refused, nothing was delivered.
            raise TelegramRejected(f"Telegram {method}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise TelegramError(f"Telegram {method}: {type(error).__name__}") from None
        if not data.get("ok"):
            raise TelegramRejected(f"Telegram {method}: not ok")
        return data.get("result")

    def send_message(self, chat_id: str, text: str, reply_to: int | None = None) -> None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to is not None:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        self._call("sendMessage", params, timeout=20)

    def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout=timeout + 10)
        updates: list[Update] = []
        for item in result or []:
            message = item.get("message") or {}
            sender = message.get("from") or {}
            updates.append(
                Update(
                    update_id=int(item["update_id"]),
                    chat_id=str((message.get("chat") or {}).get("id", "")),
                    message_id=int(message.get("message_id", 0)),
                    username=sender.get("username"),
                    text=str(message.get("text") or ""),
                )
            )
        return updates
