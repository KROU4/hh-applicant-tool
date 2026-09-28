"""Настройки и состояние бота в `telegram_bot.json` рядом с config.json.

Отдельный файл нужен, чтобы бот не перезатирал config.json, который
параллельно обновляют запущенные операции (например, при обновлении токена).
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "owner_id": None,
    "apply": {
        "use_ai": True,
        "ai_filter": None,  # None | "light" | "heavy"
        "force_message": True,
        "dry_run": False,
        "search": "",
        "excluded_filter": "",
        "max_responses": None,
        "resume_id": None,
        "resume_title": None,
        "work_format": None,  # None | "REMOTE" | "HYBRID" | "REMOTE,HYBRID"
        "experience": None,
        "system_prompt": "",
        "send_email": False,
    },
    "reply": {
        "use_ai": True,
        "only_invitations": False,
    },
    "schedule": {
        "apply_enabled": False,
        "apply_every_hours": 24,
        # Не больше стольких откликов за скользящие 24 часа (0 — без лимита)
        "daily_limit": 200,
        "hours_from": 9,
        "hours_to": 21,
        "update_resumes_enabled": True,
        "autoresponder_enabled": False,
        "autoresponder_delete_discards": False,
        "timezone": "Europe/Moscow",
    },
    "runs": {},
}


def _merge(defaults: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class BotState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        self.data = _merge(DEFAULTS, data)

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def get(self, section: str, key: str | None = None) -> Any:
        with self._lock:
            value = self.data.get(section)
            return value.get(key) if key is not None else value

    def set(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            self.data.setdefault(section, {})[key] = value
            self.save()

    @property
    def owner_id(self) -> int | None:
        return self.data.get("owner_id")

    @owner_id.setter
    def owner_id(self, value: int) -> None:
        with self._lock:
            self.data["owner_id"] = value
            self.save()

    def last_run(self, name: str) -> float:
        return float(self.data["runs"].get(name, 0))

    def mark_run(self, name: str, when: float) -> None:
        self.set("runs", name, when)
