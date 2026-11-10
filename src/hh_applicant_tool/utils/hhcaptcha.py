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
import os
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import capsolver

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

HH_URL = "https://hh.ru"
PENDING_FILENAME = "captcha_pending.json"
IMAGE_FILENAME = "captcha.png"

# Порядок решателей по умолчанию: сначала бесплатная vision-модель, затем
# платный CapSolver. Недоступные (без ключа) пропускаются.
DEFAULT_PROVIDERS = ("openrouter", "capsolver")
DEFAULT_ATTEMPTS = 3
# Пауза перед отправкой ответа — как будто человек вводил текст руками
DEFAULT_SOLVE_DELAY = (4.0, 9.0)


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


def solve_with_ai(
    tool: HHApplicantTool,
    captcha_url: str,
    attempts: int | None = None,
    pause: tuple[float, float] | None = None,
) -> bool:
    """Решает капчу автоматически и отправляет ответ.

    Решатели перебираются по порядку: vision-модель (OpenRouter), затем
    CapSolver. Между распознаванием и отправкой пауза, как при ручном вводе.
    False — ни один решатель не справился: капчу решит владелец в Telegram,
    картинка к этому моменту уже лежит в каталоге профиля.
    """
    providers = _providers(tool)
    if not providers:
        logger.debug("Автоматические решатели капчи не настроены")
        return False

    conf = tool.config.get("captcha") or {}
    attempts = attempts or int(conf.get("attempts") or DEFAULT_ATTEMPTS)
    if pause is None:
        low, high = conf.get("solve_delay") or DEFAULT_SOLVE_DELAY
        pause = (float(low), float(high))

    prepare(tool, captcha_url)
    # Сначала все попытки бесплатной модели, затем платный CapSolver: неверный
    # ответ одной модели не должен отменять другого решателя. После каждой
    # ошибки hh уже выдал новую картинку — её и показывает следующий решатель.
    for provider in providers:
        for attempt in range(1, attempts + 1):
            info = load_pending(tool.config_path)
            if not info:
                return False
            try:
                raw = _solve_provider(
                    tool,
                    provider,
                    image_path(tool.config_path).read_bytes(),
                    captcha_url,
                )
            except Exception as ex:
                logger.warning("Решатель %s: %s", provider, ex)
                break
            answer = " ".join(raw.split())
            if not answer:
                new_image(tool, info)
                continue
            time.sleep(random.uniform(*pause))
            if submit(tool, answer):
                logger.info(
                    "Капча решена (%s) с попытки %d", provider, attempt
                )
                return True
            logger.info(
                "Капча (%s): ответ %r неверный (попытка %d)",
                provider,
                answer,
                attempt,
            )
    return False


def configured_providers(tool: HHApplicantTool) -> list[str]:
    """Настроенные решатели в заданном порядке; без ключей — не берём."""
    return _providers(tool)


def _providers(tool: HHApplicantTool) -> list[str]:
    """Настроенные решатели в заданном порядке; без ключей — не берём."""
    conf = tool.config.get("captcha") or {}
    configured = conf.get("providers") or DEFAULT_PROVIDERS
    result = []
    for provider in configured:
        provider = str(provider).lower()
        if provider == "openrouter" and _openrouter_key(tool):
            result.append(provider)
        elif provider == "capsolver" and (tool.config.get("capsolver") or {}).get(
            "api_key"
        ):
            result.append(provider)
    return result


def _openrouter_key(tool: HHApplicantTool) -> str:
    # Секция цели перекрывает общий ключ роутера
    captcha_ai = tool.config.get("openai_captcha") or {}
    if captcha_ai.get("api_key"):
        return captcha_ai["api_key"]
    return (tool.config.get("openrouter") or {}).get("api_key") or ""


def solve_image_text(
    tool: HHApplicantTool, image: bytes, captcha_url: str
) -> str:
    """Распознаёт картинку перебором настроенных решателей, без отправки."""
    last_error = "решатели не настроены"
    for provider in _providers(tool):
        try:
            raw = _solve_provider(tool, provider, image, captcha_url)
        except Exception as ex:
            logger.warning("Решатель %s: %s", provider, ex)
            last_error = str(ex)
            continue
        if text := " ".join(raw.split()):
            return text
    raise CaptchaError(last_error)


def _solve_provider(
    tool: HHApplicantTool, provider: str, image: bytes, captcha_url: str
) -> str:
    if provider == "openrouter":
        ai = tool.get_captcha_ai()
        if hasattr(type(ai), "solve_captcha_consensus"):
            return ai.solve_captcha_consensus(image)
        return ai.solve_captcha(image)

    conf = tool.config.get("capsolver") or {}
    return capsolver.solve_image(
        conf.get("api_key") or "",
        image,
        website_url=captcha_url,
        module=conf.get("module") or "common",
        timeout=float(conf.get("timeout") or 60.0),
        max_retries=int(conf.get("max_retries") or 3),
        proxy_url=_proxy_url(tool),
    )


def _proxy_url(tool: HHApplicantTool) -> str | None:
    """Прокси для CapSolver: свой из конфига или общий из окружения."""
    if url := tool.config.get("capsolver", {}).get("proxy_url"):
        return url
    if url := tool.config.get("proxy_url"):
        return url
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
    )

