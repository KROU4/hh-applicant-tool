"""Капча hh.ru без браузера: картинку решает человек, ответ отправляем мы.

Протокол повторяет страницу https://hh.ru/account/captcha:
1. страница капчи (ссылка из ошибки API) — в её состоянии captchaState,
   backurl и failurl;
2. POST /captcha?lang=RU — ключ новой картинки;
3. GET /captcha/picture?key=… — сама картинка;
4. POST /account/captcha с ответом — при верном ответе редирект, при
   неверном hh выдаёт новую капчу.

Состояние между шагами хранится в каталоге профиля, чтобы ответ можно было
прислать из Telegram-бота отдельной командой.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

HH_URL = "https://hh.ru"
PENDING_FILENAME = "captcha_pending.json"
IMAGE_FILENAME = "captcha.png"


class CaptchaError(Exception):
    pass


def pending_path(config_path: Path) -> Path:
    return config_path / PENDING_FILENAME


def image_path(config_path: Path) -> Path:
    return config_path / IMAGE_FILENAME


def load_pending(config_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(pending_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear(config_path: Path) -> None:
    pending_path(config_path).unlink(missing_ok=True)
    image_path(config_path).unlink(missing_ok=True)


def _headers(tool: HHApplicantTool, referer: str) -> dict[str, str]:
    return {
        "X-Xsrftoken": tool.xsrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
        "Referer": referer,
    }


def _save(config_path: Path, info: dict[str, Any]) -> None:
    path = pending_path(config_path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def new_image(tool: HHApplicantTool, info: dict[str, Any]) -> Path:
    """Новая картинка для уже известной капчи (после ошибки или по просьбе)."""
    referer = info["captcha_url"]
    response = tool.session.post(
        f"{HH_URL}/captcha",
        params={"lang": "RU"},
        headers=_headers(tool, referer),
    )
    response.raise_for_status()
    key = (response.json() or {}).get("key")
    if not key:
        raise CaptchaError("hh не выдал ключ картинки капчи")
    picture = tool.session.get(
        f"{HH_URL}/captcha/picture",
        params={"key": key},
        headers={"Referer": referer},
    )
    picture.raise_for_status()
    path = image_path(tool.config_path)
    path.write_bytes(picture.content)
    info = {**info, "key": key, "updated_at": time.time()}
    _save(tool.config_path, info)
    return path


def prepare(tool: HHApplicantTool, captcha_url: str) -> Path:
    """Разбирает страницу капчи и сохраняет картинку для человека."""
    page = tool.session.get(captcha_url)
    state = tool.parse_redirect_config(page, check_auth=False)
    hhcaptcha = state.get("hhcaptcha") or {}
    account = state.get("captchaAccountState") or {}
    captcha_state = hhcaptcha.get("captchaState")
    if not captcha_state:
        raise CaptchaError("На странице капчи нет captchaState")
    info = {
        "captcha_url": captcha_url,
        "captcha_state": captcha_state,
        "backurl": account.get("backurl") or "/",
        "failurl": account.get("failurl") or captcha_url,
        "created_at": time.time(),
    }
    return new_image(tool, info)


def submit(tool: HHApplicantTool, answer: str) -> bool:
    """Отправляет ответ человека. False — неверно, новая картинка уже готова."""
    info = load_pending(tool.config_path)
    if not info or not info.get("key"):
        raise CaptchaError("Нет капчи, ожидающей ответа")
    response = tool.session.post(
        f"{HH_URL}/account/captcha",
        params={
            "captchaText": answer.strip(),
            "captchaKey": info["key"],
            "captchaState": info["captcha_state"],
            "backurl": info["backurl"],
            "failurl": info["failurl"],
        },
        headers=_headers(tool, info["captcha_url"]),
        allow_redirects=False,
    )
    location = response.headers.get("Location") or ""
    logger.info(
        "Ответ на капчу: HTTP %s, переход на %s", response.status_code, location[:120]
    )
    # Неверный ответ тоже может быть редиректом — обратно на страницу капчи
    accepted = response.status_code in (301, 302, 303, 307, 308) and (
        "captcha" not in location
    )
    if accepted:
        clear(tool.config_path)
        return True
    new_image(tool, info)
    return False
