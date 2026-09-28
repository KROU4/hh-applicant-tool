"""Роутер по бесплатным моделям OpenRouter.

Запрос отправляется в первую доступную модель из списка приоритетов. Если
модель перегружена (429 от провайдера), недоступна или вернула пустой ответ,
она уходит на «остывание», и запрос повторяется на следующей. Состояние
остывания общее для всех клиентов процесса, поэтому генерация писем и
AI-фильтр не долбят одну и ту же упавшую модель.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import requests
from urllib3.util import Timeout

from .openai import ChatOpenAI, OpenAIError

logger = logging.getLogger(__package__)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Отобраны вручную по качеству русского текста и стабильности (сентябрь 2026).
# Порядок = приоритет. Модели, пропавшие из каталога, отфильтруются сами.
DEFAULT_TEXT_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3.5-lightning:free",
)

DEFAULT_VISION_MODELS: tuple[str, ...] = (
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "dots-studio/dots-3-note-preview:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
)

# Бесплатные модели, которые не стоит подбирать автоматически
_AUTO_EXCLUDE = re.compile(r"safety|guard|code|coder|-fin|-sante|lfm|inkling", re.I)
# Незакрытый <think> — ответ обрезан посреди рассуждений
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S | re.I)
# Ошибки, после которых модель бесполезна до перезапуска
_PERMANENT_RE = re.compile(
    r"agentic harness|not a valid model|no endpoints|not enabled|does not exist",
    re.I,
)

# Сколько модель отдыхает после ошибки, секунды
COOLDOWN_RATE_LIMIT = 90.0
COOLDOWN_ERROR = 45.0
COOLDOWN_REJECTED = 1800.0
CATALOG_TTL = 3600.0
# Лимит бесплатных моделей общий на аккаунт: 20 запросов в минуту
FREE_MIN_INTERVAL = 60.0 / 20


class OpenRouterError(OpenAIError):
    pass


class OpenRouterDailyLimit(OpenRouterError):
    """Дневной лимит бесплатных запросов исчерпан — дальше пробовать бессмысленно."""


class _RouterState:
    """Общее для процесса состояние: остывание моделей и кэш каталога."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.request_lock = Lock()
        self.previous_request_at: float = 0.0
        self.cooldown_until: dict[str, float] = {}
        self.disabled: dict[str, str] = {}
        self.daily_limit_until: float = 0.0
        self.catalog: dict[str, dict[str, Any]] | None = None
        self.catalog_fetched_at: float = 0.0
        self.last_model: str | None = None
        self.stats: dict[str, dict[str, int]] = {}

    def reset(self) -> None:
        with self.lock:
            self.previous_request_at = 0.0
            self.cooldown_until.clear()
            self.disabled.clear()
            self.daily_limit_until = 0.0
            self.catalog = None
            self.catalog_fetched_at = 0.0
            self.last_model = None
            self.stats.clear()


STATE = _RouterState()


@dataclass
class ChatOpenRouter(ChatOpenAI):
    """Совместим с ChatOpenAI: те же complete() и solve_captcha()."""

    base_url: str = OPENROUTER_CHAT_URL
    models: list[str] = field(default_factory=lambda: list(DEFAULT_TEXT_MODELS))
    vision_models: list[str] = field(
        default_factory=lambda: list(DEFAULT_VISION_MODELS)
    )
    # Разрешить подбирать другие бесплатные модели, если все из списка пропали
    auto_discover: bool = True
    # Сколько максимум ждать остывания моделей, прежде чем сдаться
    max_wait: float = 180.0
    # Запас токенов на «размышления» reasoning-моделей
    reasoning_headroom: int = 2048
    # У бесплатных моделей OpenRouter лимит 20 запросов в минуту
    rate_limit: int = 20
    timeout: float = 90.0

    def _default_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/s3rgeym/hh-applicant-tool",
            "X-Title": "hh-applicant-tool",
        }

    @property
    def _min_request_interval(self) -> float:
        # rate_limit может задать только более редкие запросы, чем разрешает
        # OpenRouter (apply-vacancies выставляет 40 из --ai-rate-limit)
        if self.rate_limit <= 0:
            return 0.0
        return max(60.0 / self.rate_limit, FREE_MIN_INTERVAL)

    def _request(self, payload: dict) -> requests.Response:
        """Как у ChatOpenAI, но интервал общий для всех клиентов процесса."""
        with STATE.request_lock:
            delay = (
                self._min_request_interval
                - time.monotonic()
                + STATE.previous_request_at
            )
            if STATE.previous_request_at and delay > 0:
                time.sleep(delay)
            try:
                return self.session.post(
                    self.base_url,
                    json=payload,
                    headers=self._default_headers(),
                    timeout=Timeout(
                        connect=self.connect_timeout, total=self.timeout
                    ),
                )
            finally:
                STATE.previous_request_at = time.monotonic()

    # --- каталог моделей ---

    def _catalog(self) -> dict[str, dict[str, Any]] | None:
        now = time.monotonic()
        with STATE.lock:
            if (
                STATE.catalog is not None
                and now - STATE.catalog_fetched_at < CATALOG_TTL
            ):
                return STATE.catalog
        try:
            r = self.session.get(
                OPENROUTER_MODELS_URL,
                timeout=Timeout(connect=self.connect_timeout, total=30),
            )
            r.raise_for_status()
            catalog = {m["id"]: m for m in r.json()["data"]}
        except (requests.RequestException, ValueError, KeyError) as ex:
            logger.warning("Не удалось получить каталог OpenRouter: %s", ex)
            return STATE.catalog
        with STATE.lock:
            STATE.catalog = catalog
            STATE.catalog_fetched_at = now
        return catalog

    def _candidates(self, *, vision: bool) -> list[str]:
        preferred = list(self.vision_models if vision else self.models)
        catalog = self._catalog()
        if catalog is None:
            return preferred

        available = [m for m in preferred if m in catalog]
        if available or not self.auto_discover:
            return available

        # Все отобранные модели пропали из каталога — берём любые бесплатные
        found = []
        for mid, m in catalog.items():
            if not mid.endswith(":free") or _AUTO_EXCLUDE.search(mid):
                continue
            modalities = m.get("architecture", {}).get("input_modalities") or []
            if vision and "image" not in modalities:
                continue
            if (m.get("context_length") or 0) < 32000:
                continue
            found.append(m)
        found.sort(key=lambda m: -(m.get("context_length") or 0))
        logger.warning(
            "Модели из списка недоступны, использую найденные бесплатные: %s",
            [m["id"] for m in found],
        )
        return [m["id"] for m in found]

    # --- учёт состояния моделей ---

    @staticmethod
    def _cool_down(model: str, seconds: float, reason: str) -> None:
        with STATE.lock:
            STATE.cooldown_until[model] = time.monotonic() + seconds
            STATE.stats.setdefault(model, {"ok": 0, "fail": 0})["fail"] += 1
        logger.info("Модель %s отдыхает %.0fс: %s", model, seconds, reason)

    @staticmethod
    def _disable(model: str, reason: str) -> None:
        with STATE.lock:
            STATE.disabled[model] = reason
        logger.warning("Модель %s отключена: %s", model, reason)

    @staticmethod
    def _mark_ok(model: str) -> None:
        with STATE.lock:
            STATE.cooldown_until.pop(model, None)
            STATE.last_model = model
            STATE.stats.setdefault(model, {"ok": 0, "fail": 0})["ok"] += 1

    @staticmethod
    def _retry_after(response: requests.Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("Retry-After")
        try:
            return float(value) if value else None
        except ValueError:
            return None

    # --- основной цикл ---

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        vision: bool,
        max_tokens: int,
        temperature: float,
    ) -> str:
        if STATE.daily_limit_until > time.time():
            raise OpenRouterDailyLimit(
                "Дневной лимит бесплатных запросов OpenRouter исчерпан"
            )

        deadline = time.monotonic() + self.max_wait
        last_error = "нет доступных моделей"
        attempts = 0

        while True:
            # Медленные падения (таймауты по 90с) могут идти по кругу вечно,
            # если проверять срок только когда все модели остывают
            if attempts and time.monotonic() >= deadline:
                raise OpenRouterError(
                    f"Все модели OpenRouter заняты или отвечают ошибкой: {last_error}"
                )
            candidates = [
                m
                for m in self._candidates(vision=vision)
                if m not in STATE.disabled
            ]
            if not candidates:
                raise OpenRouterError(
                    f"Нет доступных бесплатных моделей OpenRouter ({last_error})"
                )

            now = time.monotonic()
            ready = [
                m for m in candidates if STATE.cooldown_until.get(m, 0) <= now
            ]
            if not ready:
                wake = min(STATE.cooldown_until.get(m, 0) for m in candidates)
                if wake > deadline:
                    raise OpenRouterError(
                        f"Все модели OpenRouter заняты: {last_error}"
                    )
                delay = max(wake - now, 1.0)
                logger.info("Все модели отдыхают, жду %.0fс", delay)
                time.sleep(delay)
                continue

            for model in ready:
                attempts += 1
                result = self._try_model(
                    model,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if result.text is not None:
                    return result.text
                last_error = f"{model}: {result.error}"

    @dataclass
    class _Attempt:
        text: str | None = None
        error: str = ""

    def _try_model(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> _Attempt:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens + self.reasoning_headroom,
            # Рассуждения нам не нужны в ответе, а минимальное усилие
            # экономит время и токены
            "reasoning": {"effort": "low", "exclude": True},
            "stream": False,
        }
        logger.debug("OpenRouter запрос к %s", model)

        try:
            response = self._request(payload)
        except requests.RequestException as ex:
            self._cool_down(model, COOLDOWN_ERROR, f"сеть: {ex}")
            return self._Attempt(error=f"сеть: {ex}")

        try:
            data = response.json()
        except ValueError:
            data = {}

        error = data.get("error") if isinstance(data, dict) else None
        status = response.status_code
        if error and isinstance(error, dict):
            status = error.get("code") or status
        message = (
            (error or {}).get("message", "")
            if isinstance(error, dict)
            else str(error or "")
        )
        raw = ""
        if isinstance(error, dict):
            raw = str((error.get("metadata") or {}).get("raw") or "")

        if status == 200 and not error:
            try:
                content = data["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                self._cool_down(model, COOLDOWN_ERROR, "кривой ответ")
                return self._Attempt(error="кривой ответ")
            content = _THINK_RE.sub("", content).strip()
            if not content:
                self._cool_down(model, COOLDOWN_ERROR, "пустой ответ")
                return self._Attempt(error="пустой ответ")
            self._mark_ok(model)
            logger.debug("OpenRouter ответ от %s", model)
            return self._Attempt(text=content)

        text = f"{status} {message} {raw}".strip()

        if status == 401:
            raise OpenRouterError(f"Неверный ключ OpenRouter: {message}")
        if status == 402:
            raise OpenRouterError(f"OpenRouter требует оплату: {message}")
        details = f"{message} {raw}".lower()
        if status == 429 and "per-day" in details:
            STATE.daily_limit_until = time.time() + 3600
            raise OpenRouterDailyLimit(
                f"Дневной лимит бесплатных запросов OpenRouter: {message}"
            )
        if status == 429 and "per-min" in details:
            # Лимит аккаунта, а не модели: ждём, а не жжём остальные модели
            wait = self._retry_after(response) or 60.0
            logger.info("Лимит OpenRouter в минуту, жду %.0fс", wait)
            time.sleep(wait)
            return self._Attempt(error=text)
        if status == 429:
            wait = self._retry_after(response) or COOLDOWN_RATE_LIMIT
            self._cool_down(model, wait, "перегружена (429)")
            return self._Attempt(error=text)
        if status == 404 or _PERMANENT_RE.search(details):
            self._disable(model, text[:200])
            return self._Attempt(error=text)
        if status in (400, 403) and not _is_transient(message):
            # Может относиться к конкретному запросу (длина контекста,
            # модерация) — надолго откладываем, но не выключаем совсем
            self._cool_down(model, COOLDOWN_REJECTED, text[:200])
            return self._Attempt(error=text)

        self._cool_down(model, COOLDOWN_ERROR, text[:200])
        return self._Attempt(error=text)

    # --- публичный интерфейс ChatOpenAI ---

    def complete(self, message: str) -> str:
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": message})

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("AI системный промпт: %s", self.system_prompt)
            logger.debug("AI запрос: %s", message)

        return self._chat(
            messages,
            vision=False,
            max_tokens=self.max_completion_tokens,
            temperature=self.temperature,
        )

    def solve_captcha(self, image_data: bytes) -> str:
        image_base64 = base64.b64encode(image_data).decode("utf-8")
        messages = [
            {
                "role": "system",
                "content": "Ты должен распознать текст на изображении. Верни ТОЛЬКО текст, без каких-либо объяснений или дополнительных символов.",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_base64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": "Распознай текст на изображении. Верни только результат распознавания (текст на изображении).",
                    },
                ],
            },
        ]
        text = self._chat(messages, vision=True, max_tokens=20, temperature=0.0)
        # Модели иногда отвечают «Текст: abc12» — берём последнее слово
        words = text.strip().split()
        return words[-1] if words else ""


def _is_transient(message: str) -> bool:
    message = message.lower()
    return any(
        s in message
        for s in ("temporarily", "timeout", "overloaded", "try again", "retry")
    )


def router_status(models: list[str] | None = None) -> list[dict[str, Any]]:
    """Состояние моделей для вывода в Telegram-боте."""
    now = time.monotonic()
    result = []
    with STATE.lock:
        for model in models or DEFAULT_TEXT_MODELS:
            until = STATE.cooldown_until.get(model, 0)
            stats = STATE.stats.get(model, {"ok": 0, "fail": 0})
            result.append(
                {
                    "model": model,
                    "disabled": STATE.disabled.get(model),
                    "cooldown": max(0.0, until - now),
                    "ok": stats["ok"],
                    "fail": stats["fail"],
                }
            )
    return result
