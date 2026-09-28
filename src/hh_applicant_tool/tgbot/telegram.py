"""Минимальный клиент Telegram Bot API на requests (без лишних зависимостей)."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__package__)

MAX_MESSAGE_LENGTH = 4096


class TelegramError(Exception):
    def __init__(self, method: str, description: str, code: int | None = None):
        super().__init__(f"{method}: {description}")
        self.description = description
        self.code = code


def button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def keyboard(*rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [row for row in rows if row]}


def clip(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :]


def plain(text: str) -> str:
    """HTML → обычный текст: запасной вариант, если разметку не приняли."""
    return clip(html.unescape(_TAG_RE.sub("", text)))


_TAG_RE = re.compile(r"</?(?:b|i|code|pre|a)(?:\s[^>]*)?>")
_TOKEN_RE = re.compile(r"bot\d+:[\w-]+")


def redact(text: object) -> str:
    """Убирает токен бота из текста ошибок requests (он есть в URL)."""
    return _TOKEN_RE.sub("bot<token>", str(text))


def _markup_rejected(ex: "TelegramError") -> bool:
    return any(
        s in ex.description.lower()
        for s in ("can't parse entities", "message is too long", "text is too long")
    )


class TelegramAPI:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.file_url = f"https://api.telegram.org/file/bot{token}/"
        self.session = session or requests.Session()

    def call(
        self,
        method: str,
        *,
        http_timeout: float = 30,
        files: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        if files:
            data = {
                k: json.dumps(v) if isinstance(v, (dict, list)) else v
                for k, v in params.items()
            }
            response = self.session.post(
                self.base_url + method,
                data=data,
                files=files,
                timeout=http_timeout,
            )
        else:
            response = self.session.post(
                self.base_url + method, json=params, timeout=http_timeout
            )
        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(
                method, response.text[:200], response.status_code
            ) from None
        if not payload.get("ok"):
            raise TelegramError(
                method,
                payload.get("description", "unknown error"),
                payload.get("error_code"),
            )
        return payload["result"]

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        *,
        silent: bool = False,
    ) -> dict:
        params = dict(
            chat_id=chat_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            disable_notification=silent or None,
        )
        if len(text) <= MAX_MESSAGE_LENGTH:
            try:
                return self.call(
                    "sendMessage", text=text, parse_mode="HTML", **params
                )
            except TelegramError as ex:
                if not _markup_rejected(ex):
                    raise
        return self.call("sendMessage", text=plain(text), **params)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        params = dict(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        try:
            try:
                if len(text) > MAX_MESSAGE_LENGTH:
                    raise TelegramError("editMessageText", "message is too long")
                self.call(
                    "editMessageText", text=text, parse_mode="HTML", **params
                )
            except TelegramError as ex:
                if not _markup_rejected(ex):
                    raise
                self.call("editMessageText", text=plain(text), **params)
        except TelegramError as ex:
            # Нажали ту же кнопку ещё раз — текст не изменился, это не ошибка
            if "message is not modified" not in ex.description:
                raise

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except (TelegramError, requests.RequestException) as ex:
            logger.debug("answerCallbackQuery: %s", redact(ex))

    def send_document(
        self, chat_id: int, path: Path, caption: str | None = None
    ) -> None:
        with path.open("rb") as fp:
            self.call(
                "sendDocument",
                http_timeout=120,
                files={"document": (path.name, fp)},
                chat_id=chat_id,
                caption=caption,
            )

    def download_file(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        response = self.session.get(self.file_url + info["file_path"], timeout=60)
        response.raise_for_status()
        return response.content

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )
