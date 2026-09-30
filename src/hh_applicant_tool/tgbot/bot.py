"""Telegram-бот: панель управления hh-applicant-tool на кнопках."""

from __future__ import annotations

import html
import json
import logging
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from ..constants import CONFIG_FILENAME, DATABASE_FILENAME, LOG_FILENAME
from ..utils.config import Config
from ..utils import hhcaptcha
from ..utils.string import rand_text, render_template
from .runner import Task, TaskRunner
from .state import BotState
from .telegram import TelegramAPI, TelegramError, button, keyboard, redact

logger = logging.getLogger(__package__)

TASK_TITLES = {
    "apply": "🚀 Отклики",
    "update_resumes": "⬆️ Подъём резюме",
    "reply": "💬 Ответы работодателям",
    "autoresponder": "🤖 Автоответчик",
    "clear": "🧹 Чистка отказов",
    "refresh_token": "🔑 Обновление токена",
}

AI_FILTERS = [None, "light", "heavy"]
AI_FILTER_TITLES = {None: "выкл", "light": "быстрый", "heavy": "полный"}
WORK_FORMATS = [None, "REMOTE", "HYBRID", "REMOTE,HYBRID"]
WORK_FORMAT_TITLES = {
    None: "любой",
    "REMOTE": "удалёнка",
    "HYBRID": "гибрид",
    "REMOTE,HYBRID": "удалёнка+гибрид",
}
EXPERIENCES = [None, "noExperience", "between1And3", "between3And6", "moreThan6"]
EXPERIENCE_TITLES = {
    None: "любой",
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "6+ лет",
}
APPLY_INTERVALS = [24, 12, 8, 6, 4]
# Скользящее окно суточной квоты откликов
# Сколько хранить журнал отправленных откликов
# hh считает лимит откликов за скользящие 24 часа
QUOTA_WINDOW = 24 * 3600
SENT_TIMES_FILENAME = "sent_times.txt"
# После исчерпания лимита ждём, пока освободится хотя бы столько мест
MIN_FREE_SLOTS = 20
# Пауза перед добором лимита после прогона, выбравшего свою порцию
CAP_RETRY_DELAY = 10 * 60
# hh сказал «лимит исчерпан», а наш подсчёт расходится — не чаще раза в час
HH_LIMIT_RETRY = 3600
NEGOTIATION_STATES = {
    "response": "📨 отправлено",
    "invitation": "🎉 приглашения",
    "interview": "🗣 собеседования",
    "discard": "⛔ отказы",
    "hired": "🏆 выход на работу",
    "offer": "💼 офферы",
}

UPDATE_RESUMES_EVERY = 4 * 3600
# Через сколько повторить оборвавшийся прогон откликов
APPLY_RETRY_DELAY = 30 * 60
# После капчи hh ждём дольше
CAPTCHA_RETRY_DELAY = 60 * 60
# apply-vacancies завершается с этим кодом, если hh попросил капчу
CAPTCHA_EXIT_CODE = 3
# hh-applicant-tool captcha --answer: неверный ответ, новая картинка готова
CAPTCHA_WRONG_EXIT_CODE = 4
AUTORESPONDER_RESTART_EVERY = 6 * 3600
AUTORESPONDER_BACKOFF = 10 * 60
REFRESH_TOKEN_EVERY = 20 * 3600
# Ежедневная чистка базы: сохранённые вакансии и отметки о пропуске
CLEANUP_EVERY = 24 * 3600
VACANCY_KEEP_DAYS = 7
SKIPPED_KEEP_DAYS = 30
SCHEDULER_TICK = 20

SUMMARY_MARKERS = (
    "✅",
    "⛔",
    "📝",
    "🚀",
    "📧",
    "Отправлено",
    "[E]",
    "[W]",
    "ERROR",
    "Ошибка",
    "ошибка",
    "Требуется",
    "Traceback",
    "лимит",
)
AUTH_MARKERS = (
    "Требуется авторизация",
    "Авторизация истекла",
    "invalid_grant",
    "bad_authorization",
    "token_revoked",
    "token_expired",
)
SERVER_CONFIG_KEYS = ("openrouter", "telegram_bot", "proxy_url")
# refresh-token возвращает 2, если токен ещё действителен
SUCCESS_CODES = {"refresh_token": (0, 2)}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
APPLIED_RE = re.compile(r"Отправлено:\s*(\d+)")
SENT_RE = re.compile(r"^📨 Отправили отклик", re.M)
HH_LIMIT_MARKER = "Лимит откликов hh.ru исчерпан"
# Блоки тестовых откликов из вывода apply-vacancies --dry-run
DRY_RUN_RE = re.compile(r"(🧪 Тест: .*?)\n=== конец ===", re.S)
DRY_RUN_LIMIT = 5
# События автоответчика (ответил / нужен человек) из его вывода
CHAT_EVENT_END = "=== конец ===".encode()
CHAT_EVENT_RE = re.compile(r"((?:🙋|💬)[^\n]*\n.*?)\n=== конец ===", re.S)

WELCOME_IMAGE = "welcome.jpg"
WELCOME_TEXT = (
    "<b>👋 Привет! Я — автопилот откликов на hh.ru</b>\n\n"
    "🎯 Нахожу вакансии по вашим ключевым словам\n"
    "✍️ Пишу AI-письмо под каждую вакансию\n"
    "🚀 Откликаюсь по расписанию — до 200 раз в сутки\n"
    "💬 Отвечаю рекрутерам в чатах и зову вас, когда нужно ваше решение\n\n"
    "Всё управление — кнопками ниже 👇"
)

ANSWERS_FILENAME = "candidate_answers.txt"

INPUT_PROMPTS = {
    "answers_add": "➕ Пришлите правило одной строкой, например:\n"
    "<code>Переработки: готов при необходимости</code>\n"
    "<code>Судимость: нет</code>",
    "answers_set": "✏️ Пришлите весь список правил целиком (каждое с новой строки). «-» — очистить.",
    "search": "🔍 Пришлите поисковый запрос (например: <code>python разработчик</code>).\nПустой поиск = рекомендованные вакансии. «-» — очистить.",
    "included_filter": "🎯 Пришлите ключевые слова через | (регулярное выражение), например:\n"
    "<code>llm|rag|genai|ai[- ]?engineer|ai-инженер|агент</code>\n"
    "Бот откликается только на вакансии, где есть совпадение в названии или описании. "
    "Если «🔍 Поиск» пуст, запрос для hh соберётся из этих слов автоматически. «-» — очистить.",
    "excluded_filter": "🚫 Пришлите стоп-слова через | (регулярное выражение), например:\n<code>junior|стажир|bitrix|1с|php</code>\n«-» — очистить.",
    "max_responses": "🔢 Сколько откликов максимум за один запуск? Число, «-» — без лимита.",
    "system_prompt": "📝 Пришлите инструкцию для AI-писем (системный промпт).\nНапример: <i>Пиши кратко, по делу, упоминай опыт с Django</i>.\n«-» — вернуть стандартную.",
    "letter": "✉️ Пришлите шаблон письма (используется, когда AI-письма выключены).\n"
    "Подстановки: {vacancy_name}, {employer_name}, {first_name}, {last_name}, {phone}, {email}, {resume_url}.\n"
    "Случайный вариант: {Здравствуйте|Добрый день}.",
    "letter_contact": "📨 Пришлите контакт, который бот добавит в конец каждого AI-письма (например, <code>https://t.me/username</code>). «-» — не добавлять.",
    "hours": "🕘 Пришлите часы работы автооткликов в формате <code>9-21</code>.",
    "daily_limit": "🎯 Сколько откликов максимум за 24 часа (включая ручные запуски)? У hh.ru потолок 200 за скользящие сутки. «-» — без лимита.",
    "openrouter_key": "🔑 Пришлите ключ OpenRouter (sk-or-...).",
}


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60}ч {minutes % 60:02d}м"


def on_off(value: Any) -> str:
    return "✅" if value else "❌"


def cycle(options: list[Any], current: Any) -> Any:
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]


def in_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _split_alternatives(pattern: str) -> list[str]:
    """Делит регулярку по «|» верхнего уровня (не внутри скобок)."""
    parts, depth, current, escaped = [], 0, "", False
    for ch in pattern:
        if escaped:
            current += ch
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            current += ch
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def _option_value(args: list[str], option: str) -> int | None:
    """Числовое значение опции из списка аргументов (--opt N)."""
    try:
        return int(args[args.index(option) + 1])
    except (ValueError, IndexError):
        return None


def normalize_keywords(pattern: str) -> str:
    """Слова через «|» понимаются буквально: C++, C#, .NET, Node.js.

    В регулярном выражении «C++» значит «сколько угодно букв C» — такой
    стоп-фильтр отсекал почти все вакансии. Варианты со скобками, «?», «\\»
    и т.п. остаются регулярными выражениями как есть.
    """
    parts = []
    for alt in _split_alternatives(pattern.strip()):
        if re.fullmatch(r"[\w\s+#.-]+", alt) and re.search(r"[+#.]", alt):
            alt = re.escape(alt.strip()).replace("\\ ", " ").replace("\\-", "-")
        parts.append(alt)
    return "|".join(parts)


def regex_to_query(pattern: str) -> str:
    """Поисковый запрос hh из регулярки: «llm|rag|ai engineer» →
    «llm OR rag OR "ai engineer"». Части с символами регулярок в запрос не
    попадают — их всё равно проверит фильтр --included-filter."""
    words: list[str] = []
    for alt in _split_alternatives(pattern.strip()):
        # «ai[- ]?engineer», «ai\s*engineer» → «ai engineer»; \b не нужен
        alt = re.sub(r"\[[- ]+\][?*]?|\\s[?*+]?|(?<=\w)-\?", " ", alt)
        alt = alt.replace("\\b", "").strip()
        if alt.startswith("(") and alt.endswith(")"):
            # Группа целиком — разворачиваем её варианты
            nested = regex_to_query(alt[1:-1].removeprefix("?:"))
            words += nested.split(" OR ") if nested else []
        elif alt and re.fullmatch(r"[\w\s.+#-]+", alt):
            alt = " ".join(alt.split())
            words.append(f'"{alt}"' if " " in alt else alt)
    return " OR ".join(dict.fromkeys(words))


def build_apply_args(settings: dict[str, Any], letter_path: Path) -> list[str]:
    args = ["apply-vacancies", "--ai-rate-limit", "20"]
    if settings.get("use_ai"):
        args.append("--use-ai")
    elif letter_path.exists():
        args += ["--letter-file", str(letter_path)]
    if settings.get("force_message"):
        args.append("--force-message")
    if settings.get("ai_filter"):
        args += ["--ai-filter", settings["ai_filter"]]
    if settings.get("dry_run"):
        args.append("--dry-run")
    if settings.get("send_email"):
        args.append("--send-email")
    search = settings.get("search") or regex_to_query(
        settings.get("included_filter") or ""
    )
    if search:
        args.append(f"--search={search}")
    if settings.get("included_filter"):
        args.append(
            f"--included-filter={normalize_keywords(settings['included_filter'])}"
        )
    if settings.get("excluded_filter"):
        args.append(
            f"--excluded-filter={normalize_keywords(settings['excluded_filter'])}"
        )
    if settings.get("max_responses"):
        args += ["--max-responses", str(settings["max_responses"])]
    if settings.get("resume_id"):
        args += ["--resume-id", str(settings["resume_id"])]
    if settings.get("experience"):
        args += ["--experience", settings["experience"]]
    if settings.get("system_prompt"):
        args.append(f"--system-prompt={settings['system_prompt']}")
    if settings.get("letter_contact"):
        args.append(f"--letter-contact={settings['letter_contact']}")
    if settings.get("work_format"):
        args += ["--work-format", *settings["work_format"].split(",")]
    if settings.get("search_in_name"):
        args += ["--search-field", "name"]
    return args


LETTER_PLACEHOLDERS = (
    "vacancy_name",
    "vacancy_url",
    "employer_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "resume_title",
    "resume_url",
    "resume_hash",
)


def normalize_letter(text: str) -> str:
    """Переводит {vacancy_name} в %(vacancy_name)s и проверяет шаблон.

    В утилите фигурные скобки означают случайный выбор, а подстановки идут
    через %-форматирование, поэтому одиночный «%» тоже нужно экранировать.
    """
    for name in LETTER_PLACEHOLDERS:
        text = text.replace("{" + name + "}", f"%({name})s")
    text = re.sub(r"%(?!\(\w+\)s)", "%%", text)
    render_template(
        rand_text(text),
        {name: "x" for name in LETTER_PLACEHOLDERS},
        "шаблоне письма",
    )
    return text


def _without_option(args: list[str], option: str) -> list[str]:
    """Убирает опцию вместе со значением из списка аргументов."""
    result: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == option:
            skip = True
            continue
        result.append(arg)
    return result


def summarize_log(text: str, limit: int = 12) -> str:
    lines = [ANSI_RE.sub("", line).rstrip() for line in text.splitlines()]
    lines = [line for line in lines if line.strip()]
    picked = [
        line for line in lines if any(m in line for m in SUMMARY_MARKERS)
    ]
    result = (picked or lines)[-limit:]
    return "\n".join(line[:300] for line in result)


class HHBot:
    def __init__(
        self,
        api: TelegramAPI,
        config_path: Path,
        *,
        owner_id: int | None = None,
        runner: TaskRunner | None = None,
    ):
        self.api = api
        self.config_path = config_path
        self.state = BotState(config_path / "telegram_bot.json")
        if owner_id:
            self.state.owner_id = owner_id
        self.runner = runner or TaskRunner(config_path, self._on_task_finish)
        self.letter_path = config_path / "letter.txt"
        self.pending_input: dict[int, str] = {}
        # Один поток: апдейты обрабатываются строго по порядку (нажали «Поиск» →
        # прислали текст). Медленное уходит в _background.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg")
        self._stop = threading.Event()
        self._ai_test_result: str | None = None
        self._account_cache: str | None = None
        self._resume_titles: dict[str, str] = {}
        # Сколько байт лога автоответчика уже переслано владельцу
        self._events_offset: int | None = None

    # ------------------------------------------------------------------ utils

    @property
    def tz(self) -> tzinfo | None:
        name = self.state.get("schedule", "timezone")
        try:
            return ZoneInfo(name) if name else None
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def now_local(self) -> datetime:
        return datetime.now(self.tz)

    def tool_config(self) -> Config:
        # Читаем заново: config.json параллельно обновляют операции
        return Config(self.config_path / CONFIG_FILENAME)

    def notify(self, text: str, *, silent: bool = False, markup=None) -> None:
        owner = self.state.owner_id
        if not owner:
            return
        try:
            self.api.send_message(owner, text, markup, silent=silent)
        except (TelegramError, requests.RequestException) as ex:
            logger.warning("Не удалось отправить уведомление: %s", redact(ex))

    # --------------------------------------------------------------- polling

    def run(self) -> None:
        try:
            self.api.set_commands(
                [
                    ("menu", "Панель управления"),
                    ("start", "Приветствие и панель"),
                    ("stop", "Остановить все задачи"),
                    ("log", "Последние строки лога откликов"),
                    ("help", "Справка"),
                ]
            )
        except (TelegramError, requests.RequestException) as ex:
            logger.warning("setMyCommands: %s", redact(ex))

        threading.Thread(
            target=self._scheduler_loop, name="scheduler", daemon=True
        ).start()
        logger.info("Бот запущен, профиль: %s", self.config_path)
        if self.state.owner_id:
            self.notify("🟢 Бот запущен. /menu — панель управления", silent=True)

        offset = None
        while not self._stop.is_set():
            try:
                updates = self.api.get_updates(offset)
            except (TelegramError, requests.RequestException) as ex:
                logger.warning("getUpdates: %s", redact(ex))
                self._stop.wait(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self._pool.submit(self._handle_update_safe, update)

    def stop(self) -> None:
        self._stop.set()
        self.runner.stop_all()

    def _handle_update_safe(self, update: dict[str, Any]) -> None:
        try:
            self.handle_update(update)
        except Exception:
            logger.exception("Ошибка обработки апдейта")

    def _background(self, fn: Callable[[], None]) -> None:
        """Долгие действия (whoami, тест AI, остановка) не держат очередь."""

        def _run() -> None:
            try:
                fn()
            except Exception:
                logger.exception("Ошибка фонового действия")

        threading.Thread(target=_run, daemon=True).start()

    def _authorized(self, user_id: int, chat_id: int, text: str) -> bool:
        owner = self.state.owner_id
        if owner is None and text.startswith("/start"):
            self.state.owner_id = user_id
            logger.info("Владелец бота: %s", user_id)
            return True
        if owner == user_id:
            return True
        logger.warning("Отказ в доступе пользователю %s", user_id)
        try:
            self.api.send_message(chat_id, "⛔ Нет доступа")
        except TelegramError:
            pass
        return False

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            user_id = query["from"]["id"]
            message = query.get("message") or {}
            chat_id = message.get("chat", {}).get("id", user_id)
            if not self._authorized(user_id, chat_id, ""):
                self.api.answer_callback(query["id"], "Нет доступа")
                return
            self.handle_callback(query, chat_id, message.get("message_id"))
            return

        message = update.get("message")
        if not message:
            return
        user_id = message["from"]["id"]
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if not self._authorized(user_id, chat_id, text):
            return

        if "document" in message:
            self.handle_document(chat_id, message["document"])
            return

        if text.startswith("/"):
            self.handle_command(chat_id, text.split()[0].split("@")[0])
            return

        key = self.pending_input.pop(chat_id, None)
        if key == "captcha" or (
            key is None and hhcaptcha.load_pending(self.config_path)
        ):
            # Ответ на капчу hh (в т.ч. если бот перезапускался и забыл, что ждал)
            answer = text.strip()
            self._background(lambda: self.answer_captcha(chat_id, answer))
            return
        if key and key.startswith("reply:"):
            # Свой ответ работодателю в чат hh
            hh_chat_id = key.removeprefix("reply:")
            self.api.send_message(chat_id, "⏳ Отправляю…")
            self.api.send_message(chat_id, self.send_owner_answer(hh_chat_id, text))
            return
        if key:
            reply = self.apply_input(key, text)
            self.api.send_message(chat_id, reply)
            screen = "schedule" if key in ("hours", "daily_limit") else "ai" if key in (
                "openrouter_key",
            ) else "answers" if key.startswith("answers_") else "settings"
            self.send_screen(chat_id, screen)
            return

        self.send_screen(chat_id, "main")

    # -------------------------------------------------------------- commands

    def handle_command(self, chat_id: int, command: str) -> None:
        if command == "/start":
            self.pending_input.pop(chat_id, None)
            self.send_welcome(chat_id)
            self.send_screen(chat_id, "main")
        elif command == "/menu":
            self.pending_input.pop(chat_id, None)
            self.send_screen(chat_id, "main")
        elif command == "/stop":
            stopped = [t.title for t in self.runner.running_tasks()]
            self.state.set("schedule", "autoresponder_enabled", False)
            self.state.set("schedule", "apply_enabled", False)
            self.api.send_message(
                chat_id,
                "⏹ Останавливаю: "
                + (", ".join(stopped) or "ничего не работало")
                + "\nАвтоотклики и автоответчик выключены.",
            )
            self._background(self.runner.stop_all)
        elif command == "/log":
            self.send_log(chat_id, "apply")
        elif command == "/cancel":
            self.pending_input.pop(chat_id, None)
            self.send_screen(chat_id, "main")
        else:
            self.api.send_message(chat_id, self.help_text())

    def send_welcome(self, chat_id: int) -> None:
        """Картинка с приветствием; после первой загрузки — по file_id."""
        path = self.config_path / WELCOME_IMAGE
        file_id = self.state.get("runs").get("welcome_file_id")
        if not file_id and not path.exists():
            self.api.send_message(chat_id, WELCOME_TEXT)
            return
        try:
            message = self.api.send_photo(chat_id, file_id or path, WELCOME_TEXT)
        except TelegramError:
            if not file_id or not path.exists():
                self.api.send_message(chat_id, WELCOME_TEXT)
                return
            # file_id протух — загружаем файл заново
            message = self.api.send_photo(chat_id, path, WELCOME_TEXT)
        photos = message.get("photo") or []
        if photos:
            self.state.set("runs", "welcome_file_id", photos[-1]["file_id"])

    @staticmethod
    def help_text() -> str:
        return (
            "<b>HH-бот</b> управляет hh-applicant-tool на сервере.\n\n"
            "• /menu — панель с кнопками\n"
            "• /stop — остановить все задачи и автоответчик\n"
            "• /log — хвост лога откликов\n\n"
            "Чтобы перенести авторизацию, пришлите файлы <code>config.json</code> "
            "и <code>cookies.txt</code> из папки профиля hh-applicant-tool документом."
        )

    # --------------------------------------------------------------- screens

    def send_screen(self, chat_id: int, screen: str) -> None:
        text, markup = self.render(screen)
        self.api.send_message(chat_id, text, markup)

    def show(self, chat_id: int, message_id: int | None, screen: str) -> None:
        text, markup = self.render(screen)
        if message_id is None:
            self.api.send_message(chat_id, text, markup)
            return
        try:
            self.api.edit_message(chat_id, message_id, text, markup)
        except TelegramError:
            self.api.send_message(chat_id, text, markup)

    def render(self, screen: str) -> tuple[str, dict]:
        renderer: Callable[[], tuple[str, dict]] = getattr(
            self, f"screen_{screen}", self.screen_main
        )
        return renderer()

    def _task_line(self, name: str) -> str:
        task = self.runner.get(name)
        if task and task.proc.poll() is None:
            return f"{TASK_TITLES[name]}: ▶️ работает {fmt_duration(task.elapsed)}"
        return ""

    def screen_main(self) -> tuple[str, dict]:
        sch = self.state.get("schedule")
        cfg = self.tool_config()
        token = (cfg.get("token") or {}).get("access_token")
        running = [self._task_line(t.name) for t in self.runner.running_tasks()]
        applied = self.applied_24h()

        lines = [
            "<b>🤖 HH панель управления</b>",
            "",
            f"Авторизация hh.ru: {'✅ есть' if token else '❌ нет — см. 👤 Аккаунт'}",
            f"AI (OpenRouter): {'✅' if (cfg.get('openrouter') or {}).get('api_key') else '❌ нет ключа'}",
            "",
            "<b>Сейчас:</b> " + ("\n" + "\n".join(running) if running else "ничего не запущено"),
            "",
            "<b>Расписание:</b>",
            f"{on_off(sch['apply_enabled'])} автоотклики каждые {sch['apply_every_hours']}ч, {sch['hours_from']}:00–{sch['hours_to']}:00",
            f"{on_off(sch['update_resumes_enabled'])} подъём резюме каждые 4ч",
            f"{on_off(sch['autoresponder_enabled'])} автоответчик в чатах",
            "",
            f"Откликов за 24 часа: <b>{applied}</b>"
            + (f" из {sch['daily_limit']}" if sch.get("daily_limit") else ""),
        ]
        if self.state.get("apply", "dry_run"):
            lines.append("⚠️ Включён тестовый режим: отклики не отправляются")

        apply_running = self.runner.running("apply")
        ar_on = sch["autoresponder_enabled"]
        markup = keyboard(
            [
                button("⛔ Остановить отклики", "stop:apply")
                if apply_running
                else button("🚀 Откликнуться сейчас", "run:apply")
            ],
            [
                button("⬆️ Поднять резюме", "run:update_resumes"),
                button("💬 Ответить работодателям", "run:reply"),
            ],
            [
                button(
                    f"🤖 Автоответчик: {'ВКЛ' if ar_on else 'ВЫКЛ'}",
                    "toggle:autoresponder",
                )
            ],
            [
                button("⚙️ Настройки откликов", "screen:settings"),
                button("⏰ Расписание", "screen:schedule"),
            ],
            [
                button("📊 Статистика", "screen:stats"),
                button("📜 Логи", "screen:logs"),
            ],
            [
                button("🧠 AI", "screen:ai"),
                button("👤 Аккаунт", "screen:account"),
            ],
            [button("🔄 Обновить", "screen:main")],
        )
        return "\n".join(lines), markup

    def screen_settings(self) -> tuple[str, dict]:
        s = self.state.get("apply")
        letter = (
            self.letter_path.read_text(encoding="utf-8")[:200]
            if self.letter_path.exists()
            else ""
        )
        lines = [
            "<b>⚙️ Настройки откликов</b>",
            "",
            f"🔍 Поиск: {esc(s['search']) or '<i>из ключевых слов</i>' if not s['search'] and s.get('included_filter') else esc(s['search']) or '<i>рекомендованные вакансии</i>'}",
            f"🎯 Ключевые слова (regex): {esc(s.get('included_filter') or '—')}",
            f"🔤 Искать: {'только в названии' if s.get('search_in_name') else 'везде (название и описание)'}",
            f"   запрос в hh: <code>{esc(s['search'] or regex_to_query(s.get('included_filter') or '') or '—')}</code>",
            f"🚫 Стоп-слова: {esc(s['excluded_filter']) or '—'}",
            f"📄 Резюме: {esc(s['resume_title'] or 'все опубликованные')}",
            f"🌍 Формат работы: {WORK_FORMAT_TITLES.get(s['work_format'], s['work_format'])}",
            f"💼 Опыт: {EXPERIENCE_TITLES.get(s['experience'], s['experience'])}",
            f"🔢 Лимит за запуск: {s['max_responses'] or 'без лимита'}",
            "",
            f"🧠 AI-письма: {on_off(s['use_ai'])}",
            f"✍️ Инструкция AI: {esc(s['system_prompt'][:150]) or 'стандартная'}",
            f"📨 Контакт в конце письма: {esc(s.get('letter_contact') or '—')}",
            f"✉️ Письмо к каждому отклику: {on_off(s['force_message'])}",
            f"🔎 AI-фильтр вакансий: {AI_FILTER_TITLES.get(s['ai_filter'])}",
            f"📧 Письмо на email работодателя: {on_off(s['send_email'])}",
            f"🧪 Тестовый режим: {on_off(s['dry_run'])}",
            "",
            f"Шаблон письма (без AI): {esc(letter) or '—'}",
        ]
        markup = keyboard(
            [
                button("🎯 Ключевые слова", "input:included_filter"),
                button("🚫 Стоп-слова", "input:excluded_filter"),
            ],
            [
                button("🔍 Поиск", "input:search"),
                button(
                    "🔤 В названии ✅" if s.get("search_in_name") else "🔤 В названии ❌",
                    "flip:search_in_name",
                ),
            ],
            [
                button("📄 Резюме", "screen:resumes"),
                button("🔢 Лимит", "input:max_responses"),
            ],
            [
                button("🌍 Формат", "cycle:work_format"),
                button("💼 Опыт", "cycle:experience"),
            ],
            [
                button(f"🧠 AI-письма {on_off(s['use_ai'])}", "flip:use_ai"),
                button("✍️ Инструкция AI", "input:system_prompt"),
            ],
            [
                button(
                    f"✉️ Письмо всегда {on_off(s['force_message'])}",
                    "flip:force_message",
                ),
                button(
                    f"🔎 AI-фильтр: {AI_FILTER_TITLES.get(s['ai_filter'])}",
                    "cycle:ai_filter",
                ),
            ],
            [
                button(f"📧 Email {on_off(s['send_email'])}", "flip:send_email"),
                button(f"🧪 Тест {on_off(s['dry_run'])}", "flip:dry_run"),
            ],
            [
                button("📨 Контакт в письме", "input:letter_contact"),
                button("✉️ Шаблон письма", "input:letter"),
            ],
            [button("📋 Ответы рекрутерам", "screen:answers")],
            [button("◀️ Назад", "screen:main")],
        )
        return "\n".join(lines), markup

    @property
    def answers_path(self) -> Path:
        return self.config_path / ANSWERS_FILENAME

    def screen_answers(self) -> tuple[str, dict]:
        current = (
            self.answers_path.read_text(encoding="utf-8").strip()
            if self.answers_path.exists()
            else ""
        )
        text = (
            "<b>📋 Ответы рекрутерам</b>\n"
            "Ваши решения по типовым вопросам. Автоответчик отвечает по ним сам — "
            "текстом или кнопкой «да/нет». Если вопроса здесь нет, он зовёт вас.\n\n"
            + (f"<pre>{esc(current[:3000])}</pre>" if current else "<i>Пока пусто</i>")
        )
        markup = keyboard(
            [button("➕ Дописать правило", "input:answers_add")],
            [button("✏️ Заменить всё", "input:answers_set")],
            [button("◀️ Назад", "screen:settings")],
        )
        return text, markup

    def screen_schedule(self) -> tuple[str, dict]:
        sch = self.state.get("schedule")
        now = time.time()
        next_apply = self.state.last_run("apply_next")
        next_update = self.state.last_run("update_resumes_next")

        def when(ts: float) -> str:
            if not ts or ts <= now:
                return "при ближайшей проверке"
            return datetime.fromtimestamp(ts, self.tz).strftime("%d.%m %H:%M")

        local = self.now_local()
        lines = [
            "<b>⏰ Расписание</b>",
            f"Время сервера ({sch['timezone']}): {local:%d.%m %H:%M}",
            "",
            f"{on_off(sch['apply_enabled'])} Автоотклики "
            + (
                "раз в сутки (24–25ч)"
                if sch["apply_every_hours"] >= 24
                else f"каждые {sch['apply_every_hours']}ч"
            )
            + f", запуск с {sch['hours_from']}:00 до {sch['hours_to']}:00",
            f"   следующий запуск: {when(next_apply) if sch['apply_enabled'] else '—'}",
            f"🎯 Не больше {sch['daily_limit'] or '∞'} откликов в сутки "
            f"(за 24 часа: {self.applied_24h()}, осталось: "
            f"{self.apply_quota_left() if sch['daily_limit'] else '∞'})",
            f"{on_off(sch['update_resumes_enabled'])} Подъём резюме каждые 4ч",
            f"   следующий запуск: {when(next_update) if sch['update_resumes_enabled'] else '—'}",
            f"{on_off(sch['autoresponder_enabled'])} Автоответчик (проверка чатов раз в 2 мин)",
            f"{on_off(sch['autoresponder_delete_discards'])} Удалять чаты с отказами",
        ]
        markup = keyboard(
            [
                button(
                    f"🚀 Автоотклики {on_off(sch['apply_enabled'])}",
                    "sflip:apply_enabled",
                )
            ],
            [
                button(
                    f"⏱ Каждые {sch['apply_every_hours']}ч", "scycle:apply_every_hours"
                ),
                button("🕘 Часы работы", "input:hours"),
            ],
            [button(f"🎯 Лимит в сутки: {sch['daily_limit'] or '∞'}", "input:daily_limit")],
            [
                button(
                    f"⬆️ Подъём резюме {on_off(sch['update_resumes_enabled'])}",
                    "sflip:update_resumes_enabled",
                )
            ],
            [
                button(
                    f"🤖 Автоответчик {on_off(sch['autoresponder_enabled'])}",
                    "toggle:autoresponder",
                ),
                button(
                    f"🗑 Удалять отказы {on_off(sch['autoresponder_delete_discards'])}",
                    "sflip:autoresponder_delete_discards",
                ),
            ],
            [button("◀️ Назад", "screen:main")],
        )
        return "\n".join(lines), markup

    def screen_stats(self) -> tuple[str, dict]:
        lines = ["<b>📊 Статистика</b>", ""]
        lines.append(f"Откликов за 24 часа: <b>{self.applied_24h()}</b>")
        db_path = self.config_path / DATABASE_FILENAME
        if db_path.exists():
            try:
                lines += self._db_stats(db_path)
            except sqlite3.Error as ex:
                lines.append(f"Не удалось прочитать базу: {esc(ex)}")
        else:
            lines.append("База ещё не создана — появится после первого запуска.")
        markup = keyboard(
            [button("🧹 Удалить отказы", "run:clear")],
            [button("🔄 Обновить", "screen:stats"), button("◀️ Назад", "screen:main")],
        )
        return "\n".join(lines), markup

    def _db_stats(self, db_path: Path) -> list[str]:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.cursor()

            def scalar(sql: str) -> int:
                return int(cur.execute(sql).fetchone()[0] or 0)

            since = "SELECT COUNT(*) FROM negotiations WHERE created_at >= datetime('now', ?)"
            day = int(cur.execute(since, ("-1 day",)).fetchone()[0] or 0)
            week = int(cur.execute(since, ("-7 day",)).fetchone()[0] or 0)
            lines = [
                "",
                "<b>Отклики в базе</b>",
                f"за 24 часа: {day}",
                f"за 7 дней: {week}",
                f"всего: {scalar('SELECT COUNT(*) FROM negotiations')}",
            ]
            states = cur.execute(
                "SELECT state, COUNT(*) FROM negotiations GROUP BY state ORDER BY 2 DESC"
            ).fetchall()
            if states:
                lines.append("")
                lines.append("<b>По статусам</b>")
                lines += [
                    f"{NEGOTIATION_STATES.get(state, esc(state))}: {count}"
                    for state, count in states
                ]
            skipped = scalar(
                "SELECT COUNT(*) FROM skipped_vacancies WHERE created_at >= datetime('now','-1 day')"
            )
            reasons = cur.execute(
                "SELECT reason, COUNT(*) FROM skipped_vacancies "
                "WHERE created_at >= datetime('now','-1 day') "
                "GROUP BY reason ORDER BY 2 DESC LIMIT 5"
            ).fetchall()
            lines += ["", f"<b>Пропущено фильтрами за 24ч:</b> {skipped}"]
            lines += [f"• {esc(r[:60])}: {c}" for r, c in reasons]
            return lines
        finally:
            con.close()

    def screen_logs(self) -> tuple[str, dict]:
        rows = [
            [button(title, f"log:{name}")]
            for name, title in TASK_TITLES.items()
            if (self.runner.logs_dir / f"{name}.log").exists()
        ]
        rows.append([button("📎 Общий лог-файл", "logfile:main")])
        rows.append([button("◀️ Назад", "screen:main")])
        return "<b>📜 Логи</b>\nВыберите задачу:", keyboard(*rows)

    def screen_ai(self) -> tuple[str, dict]:
        from ..ai.openrouter import DEFAULT_TEXT_MODELS

        cfg = self.tool_config()
        orc = cfg.get("openrouter") or {}
        key = orc.get("api_key") or ""
        models = orc.get("models") or list(DEFAULT_TEXT_MODELS)
        lines = [
            "<b>🧠 AI: OpenRouter (только бесплатные модели)</b>",
            "",
            f"Ключ: {'✅ ' + esc(key[:10]) + '…' if key else '❌ не задан'}",
            "",
            "<b>Порядок моделей</b> (при лимите переключаемся на следующую):",
        ]
        lines += [f"{i}. <code>{esc(m)}</code>" for i, m in enumerate(models, 1)]
        if self._ai_test_result:
            lines += ["", "<b>Последний тест:</b>", self._ai_test_result]
        markup = keyboard(
            [button("🧪 Тест генерации письма", "ai:test")],
            [button("🔑 Сменить ключ", "input:openrouter_key")],
            [button("◀️ Назад", "screen:main")],
        )
        return "\n".join(lines), markup

    def screen_account(self) -> tuple[str, dict]:
        cfg = self.tool_config()
        token = cfg.get("token") or {}
        cookies = (self.config_path / "cookies.txt").exists()
        expires = token.get("access_expires_at")
        exp_text = (
            datetime.fromtimestamp(int(expires), self.tz).strftime("%d.%m.%Y %H:%M")
            if expires
            else "—"
        )
        lines = [
            "<b>👤 Аккаунт hh.ru</b>",
            "",
            f"Токен: {'✅' if token.get('access_token') else '❌ нет'} (истекает: {exp_text})",
            f"Cookies (нужны для чатов): {on_off(cookies)}",
        ]
        if self._account_cache:
            lines += ["", self._account_cache]
        lines += [
            "",
            "Перенос авторизации: выполните <code>hh-applicant-tool authorize</code> "
            "на компьютере и пришлите сюда документами <code>config.json</code> "
            "и <code>cookies.txt</code> из папки профиля.",
        ]
        markup = keyboard(
            [button("🔍 Проверить вход (whoami)", "account:whoami")],
            [button("🔑 Обновить токен", "run:refresh_token")],
            [button("📄 Мои резюме", "screen:resumes")],
            [button("◀️ Назад", "screen:main")],
        )
        return "\n".join(lines), markup

    def screen_resumes(self) -> tuple[str, dict]:
        code, out, err = self.runner.run_sync(["call-api", "/resumes/mine"])
        rows = [[button("📚 Все опубликованные", "resume:all")]]
        lines = ["<b>📄 Резюме для откликов</b>", ""]
        try:
            items = json.loads(out)["items"] if code == 0 else []
        except (ValueError, KeyError):
            items = []
        if not items:
            lines.append(
                "Не удалось получить список резюме:\n<pre>"
                + esc((err or out)[-800:])
                + "</pre>"
            )
        current = self.state.get("apply", "resume_id")
        for item in items[:10]:
            status = (item.get("status") or {}).get("name", "")
            mark = "✅ " if item["id"] == current else ""
            lines.append(f"{mark}{esc(item['title'])} — {esc(status)}")
            self._resume_titles[item["id"]] = item["title"]
            rows.append(
                [button(f"{mark}{item['title'][:40]}", f"resume:{item['id']}")]
            )
        rows.append([button("◀️ Назад", "screen:settings")])
        return "\n".join(lines), keyboard(*rows)

    # ------------------------------------------------------------- callbacks

    def handle_callback(
        self, query: dict[str, Any], chat_id: int, message_id: int | None
    ) -> None:
        data = query.get("data") or ""
        action, _, arg = data.partition(":")
        answer: str | None = None

        if action == "screen":
            if arg == "resumes":
                # Список резюме — отдельный процесс и запрос к API, это секунды
                self.api.answer_callback(query["id"], "Загружаю…")
                self._background(lambda: self.show(chat_id, message_id, arg))
                return
            self.api.answer_callback(query["id"])
            self.show(chat_id, message_id, arg)
            return

        if action == "run":
            answer = self.start_task(arg)
            self.show(chat_id, message_id, "stats" if arg == "clear" else "main")
            if arg == "apply" and answer == "Запущено":
                self.api.send_message(
                    chat_id,
                    f"🧪 Тестовый прогон запущен: до {DRY_RUN_LIMIT} вакансий, "
                    "ничего не отправляется. Минуты через 2–4 пришлю вакансии "
                    "и письма, которые ушли бы работодателям."
                    if self.state.get("apply", "dry_run")
                    else "🚀 Отклики запущены. Пришлю итог, когда закончу.",
                )
        elif action == "stop":
            answer = (
                "Останавливаю…" if self.runner.stop(arg) else "Задача не запущена"
            )
            self.show(chat_id, message_id, "main")
        elif action == "toggle" and arg == "autoresponder":
            enabled = not self.state.get("schedule", "autoresponder_enabled")
            self.state.set("schedule", "autoresponder_enabled", enabled)
            if enabled:
                self._ensure_autoresponder()
                answer = "Автоответчик включён"
            else:
                self.runner.stop("autoresponder")
                answer = "Автоответчик выключен"
            self.show(chat_id, message_id, "main")
        elif action == "flip":
            value = not self.state.get("apply", arg)
            self.state.set("apply", arg, value)
            self.show(chat_id, message_id, "settings")
        elif action == "cycle":
            options = {
                "ai_filter": AI_FILTERS,
                "work_format": WORK_FORMATS,
                "experience": EXPERIENCES,
            }[arg]
            self.state.set("apply", arg, cycle(options, self.state.get("apply", arg)))
            self.show(chat_id, message_id, "settings")
        elif action == "sflip":
            value = not self.state.get("schedule", arg)
            self.state.set("schedule", arg, value)
            if arg == "apply_enabled" and value:
                self.state.mark_run("apply_next", 0)
            self.show(chat_id, message_id, "schedule")
        elif action == "scycle":
            self.state.set(
                "schedule",
                arg,
                cycle(APPLY_INTERVALS, self.state.get("schedule", arg)),
            )
            self.show(chat_id, message_id, "schedule")
        elif action == "input":
            self.pending_input[chat_id] = arg
            self.api.send_message(chat_id, INPUT_PROMPTS[arg] + "\n\n/cancel — отмена")
        elif action == "resume":
            if arg == "all":
                self.state.set("apply", "resume_id", None)
                self.state.set("apply", "resume_title", None)
            else:
                self.state.set("apply", "resume_id", arg)
                self.state.set(
                    "apply", "resume_title", self._resume_titles.get(arg, arg)
                )
            answer = "Сохранено"
            self.show(chat_id, message_id, "settings")
        elif action == "log":
            self.send_log(chat_id, arg)
        elif action == "captcha":
            if arg == "refresh":
                self.api.answer_callback(query["id"], "Запрашиваю новую…")
                self._background(lambda: self.refresh_captcha(chat_id))
                return
            self.pending_input.pop(chat_id, None)
            answer = "Хорошо, попробую позже — капча придёт снова"
        elif action == "ans":
            hh_chat_id, _, index = arg.partition(":")
            options = self.state.get("answers").get(hh_chat_id) or []
            if not index.isdigit() or int(index) >= len(options):
                answer = "Этот вопрос уже закрыт"
            else:
                text = options[int(index)]
                self.api.answer_callback(query["id"], "Отправляю…")

                def _send() -> None:
                    self.api.send_message(chat_id, self.send_owner_answer(hh_chat_id, text))

                self._background(_send)
                return
        elif action == "ansin":
            self.pending_input[chat_id] = f"reply:{arg}"
            self.api.send_message(
                chat_id,
                "✍️ Напишите ответ работодателю — отправлю его в чат hh как есть.\n/cancel — отмена",
            )
        elif action == "logfile":
            path = (
                self.config_path / LOG_FILENAME
                if arg == "main"
                else self.runner.logs_dir / f"{arg}.log"
            )
            if path.exists():
                self.api.send_document(chat_id, path)
            else:
                answer = "Лог пока пуст"
        elif action == "ai" and arg == "test":
            self.api.answer_callback(query["id"], "Генерирую, это 5–30 секунд…")

            def _test() -> None:
                self._ai_test_result = self.run_ai_test()
                self.show(chat_id, message_id, "ai")

            self._background(_test)
            return
        elif action == "account" and arg == "whoami":
            self.api.answer_callback(query["id"], "Проверяю…")

            def _whoami() -> None:
                code, out, err = self.runner.run_sync(["whoami"])
                text = out if code == 0 and out else (err or out)
                self._account_cache = (
                    ("✅ " if code == 0 and out else "❌ ")
                    + "<pre>"
                    + esc(ANSI_RE.sub("", text)[-800:])
                    + "</pre>"
                )
                self.show(chat_id, message_id, "account")

            self._background(_whoami)
            return

        self.api.answer_callback(query["id"], answer)

    def send_log(self, chat_id: int, name: str) -> None:
        tail = ANSI_RE.sub("", self.runner.tail(name, 40)) or "лог пуст"
        title = TASK_TITLES.get(name, name)
        self.api.send_message(
            chat_id,
            f"<b>{esc(title)}</b>\n<pre>{esc(tail[-3500:])}</pre>",
            keyboard(
                [
                    button("🔄 Ещё раз", f"log:{name}"),
                    button("📎 Файлом", f"logfile:{name}"),
                ]
            ),
        )

    # ----------------------------------------------------------------- input

    def apply_input(self, key: str, text: str) -> str:
        clear = text in ("-", "—")
        if key in ("search", "excluded_filter", "included_filter", "system_prompt", "letter_contact"):
            if key in ("excluded_filter", "included_filter") and not clear:
                text = normalize_keywords(text)
                try:
                    re.compile(text)
                except re.error as ex:
                    return f"❌ Некорректное выражение: {esc(ex)}"
            self.state.set("apply", key, "" if clear else text)
            return "✅ Очищено" if clear else "✅ Сохранено"
        if key == "max_responses":
            if clear:
                self.state.set("apply", key, None)
                return "✅ Лимит снят"
            if not text.isdigit() or int(text) <= 0:
                return "❌ Нужно положительное число"
            self.state.set("apply", key, int(text))
            return f"✅ Не больше {text} откликов за запуск"
        if key == "letter":
            if clear:
                self.letter_path.unlink(missing_ok=True)
                return "✅ Шаблон удалён"
            try:
                template = normalize_letter(text)
            except (ValueError, TypeError) as ex:
                return f"❌ Ошибка в шаблоне: {esc(ex)}"
            self.letter_path.write_text(template, encoding="utf-8")
            return "✅ Шаблон сохранён"
        if key in ("answers_add", "answers_set"):
            current = (
                self.answers_path.read_text(encoding="utf-8").strip()
                if self.answers_path.exists()
                else ""
            )
            if clear:
                self.answers_path.unlink(missing_ok=True)
                return "✅ Ответы очищены"
            rule = text.strip()
            if key == "answers_add" and current:
                rule = f"{current}\n{rule}"
            self.answers_path.write_text(rule + "\n", encoding="utf-8")
            return "✅ Сохранено — автоответчик учтёт при следующей проверке чатов"
        if key == "daily_limit":
            if clear:
                self.state.set("schedule", key, 0)
                return "✅ Суточный лимит снят"
            if not text.isdigit() or int(text) <= 0:
                return "❌ Нужно положительное число"
            self.state.set("schedule", key, int(text))
            return f"✅ Не больше {text} откликов в сутки"
        if key == "hours":
            match = re.fullmatch(r"\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s*", text)
            if not match or int(match[1]) > 23 or int(match[2]) > 24:
                return "❌ Формат: 9-21"
            self.state.set("schedule", "hours_from", int(match[1]))
            self.state.set("schedule", "hours_to", int(match[2]))
            return "✅ Часы работы сохранены"
        if key == "openrouter_key":
            if not text.startswith("sk-or-"):
                return "❌ Ключ OpenRouter начинается с sk-or-"
            cfg = self.tool_config()
            cfg.save(openrouter={**(cfg.get("openrouter") or {}), "api_key": text})
            return "✅ Ключ сохранён"
        return "Неизвестный параметр"

    def handle_document(self, chat_id: int, document: dict[str, Any]) -> None:
        name = document.get("file_name") or ""
        if document.get("file_size", 0) > 5 * 1024 * 1024:
            self.api.send_message(chat_id, "❌ Файл слишком большой")
            return
        content = self.api.download_file(document["file_id"])

        if name == "config.json":
            try:
                uploaded = json.loads(content.decode("utf-8"))
            except ValueError as ex:
                self.api.send_message(chat_id, f"❌ Не JSON: {esc(ex)}")
                return
            cfg = self.tool_config()
            # Настройки, относящиеся к серверу, конфигом с ПК не затираем
            for key in SERVER_CONFIG_KEYS:
                if key in cfg:
                    uploaded.pop(key, None)
            cfg.save(uploaded)
            # Автоответчик держит старый токен в памяти и при выходе записал бы
            # его обратно — перезапускаем, планировщик поднимет его снова
            self.runner.stop("autoresponder")
            has_token = bool((uploaded.get("token") or {}).get("access_token"))
            self.api.send_message(
                chat_id,
                "✅ config.json загружен"
                + (", токен найден" if has_token else ", но токена в нём нет")
                + ". Проверьте вход: 👤 Аккаунт → whoami",
            )
        elif name.startswith("cookies") and name.endswith(".txt"):
            (self.config_path / "cookies.txt").write_bytes(content)
            self.api.send_message(chat_id, "✅ cookies.txt загружен")
        elif name.endswith(".txt"):
            reply = self.apply_input("letter", content.decode("utf-8", "replace"))
            self.api.send_message(chat_id, reply)
        else:
            self.api.send_message(
                chat_id,
                "Принимаю config.json, cookies.txt или .txt с шаблоном письма",
            )

    # ------------------------------------------------------------------- AI

    def run_ai_test(self) -> str:
        from ..ai.openrouter import STATE, ChatOpenRouter, OpenRouterError

        orc = self.tool_config().get("openrouter") or {}
        if not orc.get("api_key"):
            return "❌ Не задан ключ OpenRouter"
        options = {
            k: orc[k] for k in ("models", "vision_models") if orc.get(k)
        }
        client = ChatOpenRouter(
            orc["api_key"],
            system_prompt=self.state.get("apply", "system_prompt")
            or "Напиши сопроводительное письмо для отклика на эту вакансию. Не используй placeholder'ы.",
            **options,
        )
        started = time.monotonic()
        try:
            text = client.complete(
                "Сгенерируй сопроводительное письмо не более 5 предложений. "
                "[ВАКАНСИЯ] Python-разработчик, компания ТехноСофт. "
                "[РЕЗЮМЕ] Python-разработчик, 4 года опыта, Django, FastAPI, PostgreSQL."
            )
        except OpenRouterError as ex:
            return f"❌ {esc(ex)}"
        return (
            f"✅ <code>{esc(STATE.last_model)}</code>, {time.monotonic() - started:.1f}с\n"
            f"<i>{esc(text[:1200])}</i>"
        )

    # ----------------------------------------------------------------- tasks

    def task_args(self, name: str) -> list[str]:
        if name == "apply":
            return build_apply_args(self.state.get("apply"), self.letter_path)
        if name == "update_resumes":
            return ["update-resumes"]
        if name == "reply":
            args = ["reply-employers"]
            if self.state.get("reply", "use_ai"):
                args.append("--use-ai")
            if self.state.get("reply", "only_invitations"):
                args.append("--only-invitations")
            return args
        if name == "autoresponder":
            args = ["autoresponder", "--interval", "120"]
            if self.state.get("schedule", "autoresponder_delete_discards"):
                args.append("--delete")
            contact = self.state.get("apply", "letter_contact")
            if contact:
                args.append(f"--contact={contact}")
            return args
        if name == "clear":
            return ["clear-negotiations"]
        if name == "refresh_token":
            return ["refresh-token"]
        raise ValueError(name)

    def apply_quota_left(self, now: float | None = None) -> int:
        limit = int(self.state.get("schedule", "daily_limit") or 0)
        if limit <= 0:
            return 10**9
        return max(limit - self.applied_24h(now), 0)

    def start_task(self, name: str, *, scheduled: bool = False) -> str:
        args = self.task_args(name)
        if name == "apply":
            own = self.state.get("apply", "max_responses")
            if self.state.get("apply", "dry_run"):
                # Тест ничего не отправляет: квота не нужна, а несколько
                # вакансий достаточно, чтобы оценить поиск и письма
                limit = min(own, DRY_RUN_LIMIT) if own else DRY_RUN_LIMIT
            else:
                left = self.apply_quota_left()
                if left <= 0:
                    return "Суточный лимит откликов уже набран"
                # Квота на 24 часа важнее лимита «за запуск» из настроек
                limit = min(left, own) if own else left
            if limit < 10**9:
                args = _without_option(args, "--max-responses")
                args += ["--max-responses", str(limit)]
        task = self.runner.start(
            name, TASK_TITLES[name], args, scheduled=scheduled
        )
        if task is None:
            return "Уже выполняется"
        if name == "autoresponder":
            # Лог пересоздан — события читаем с начала, ничего не теряя
            self._events_offset = 0
        self.state.mark_run(name, time.time())
        return "Запущено"

    def _on_task_finish(self, task: Task, code: int) -> None:
        # Весь лог: при нескольких резюме итоги первых уходят далеко вверх
        log = self.runner.tail(task.name, 100_000)
        if code in SUCCESS_CODES.get(task.name, (0,)):
            code = 0
        applied = 0
        if task.name == "apply":
            # По каждой отправке: итоговой строки нет, если прогон оборвался
            applied = max(
                len(SENT_RE.findall(log)),
                sum(int(n) for n in APPLIED_RE.findall(log)),
            )
        if task.name == "autoresponder":
            if task.elapsed < 120 and not task.stopped_by_user:
                self.state.mark_run(
                    "autoresponder_backoff", time.time() + AUTORESPONDER_BACKOFF
                )
                self.notify(
                    "⚠️ Автоответчик упал сразу после запуска, повторю через 10 мин.\n<pre>"
                    + esc(summarize_log(log, 8))
                    + "</pre>"
                )
            return

        summary = summarize_log(log)
        auth_problem = code != 0 and any(m in log for m in AUTH_MARKERS)
        captcha = task.name == "apply" and code == CAPTCHA_EXIT_CODE
        if task.stopped_by_user:
            head = f"⏹ {task.title}: остановлено"
        elif captcha:
            head = f"⏸ {task.title}: пауза — hh попросил капчу"
        elif code == 0:
            head = f"✅ {task.title}: готово за {fmt_duration(task.elapsed)}"
        else:
            head = f"❌ {task.title}: ошибка (код {code})"

        # Плановые задачи без ошибок не должны спамить уведомлениями
        if task.scheduled and code == 0 and task.name != "apply":
            return
        dry_run = task.name == "apply" and "--dry-run" in task.args
        if dry_run and not task.stopped_by_user:
            self._send_dry_run_report(log)
        limit_note = ""
        # Прогон упёрся в выданную ему порцию лимита, а вакансии ещё есть:
        # за время прогона освобождаются новые места — добираем их
        cap = _option_value(task.args, "--max-responses")
        cap_reached = cap is not None and applied >= cap
        if (
            task.name == "apply"
            and not dry_run
            and not captcha
            and not task.stopped_by_user
            and (
                HH_LIMIT_MARKER in log
                or self.apply_quota_left() == 0
                or cap_reached
            )
        ):
            # Лимит 200 за 24 часа занят: продолжаем, когда освободятся места
            resume_at = max(
                self.quota_free_at(MIN_FREE_SLOTS), time.time() + CAP_RETRY_DELAY
            )
            if HH_LIMIT_MARKER in log:
                resume_at = max(resume_at, time.time() + HH_LIMIT_RETRY)
            self.state.mark_run("apply_next", resume_at)
            limit_note = (
                "\n\n⏳ Лимит hh — 200 откликов за 24 часа. Продолжу "
                + datetime.fromtimestamp(resume_at, self.tz).strftime("%d.%m в %H:%M")
                + ", когда освободятся места."
            )
        retry_note = ""
        if (
            task.name == "apply"
            and code != 0
            and not dry_run
            and not task.stopped_by_user
            and self.state.get("schedule", "apply_enabled")
            and self.apply_quota_left() > 0
        ):
            # Прогон оборвался — добираем суточную квоту, а не ждём сутки.
            # После капчи ждём дольше: hh снимает её со временем
            delay = CAPTCHA_RETRY_DELAY if captcha else APPLY_RETRY_DELAY
            retry_at = time.time() + delay
            self.state.mark_run("apply_next", retry_at)
            sch = self.state.get("schedule")
            if in_hours(
                datetime.fromtimestamp(retry_at, self.tz).hour,
                sch["hours_from"],
                sch["hours_to"],
            ):
                when = (
                    "введите капчу — продолжу сразу, иначе повторю через "
                    f"{delay // 60} мин."
                    if captcha
                    else f"повторю через {delay // 60} мин."
                )
            else:
                when = f"продолжу завтра с {sch['hours_from']}:00."
            retry_note = (
                f"\n\n🔁 Осталось {self.apply_quota_left()} откликов из лимита — {when}"
            )
        text = f"<b>{esc(head)}</b>"
        if task.name == "apply" and (code == 0 or captcha) and not dry_run:
            text += f"\nОтправлено откликов: <b>{applied}</b>"
        if summary:
            text += f"\n<pre>{esc(summary[-3000:])}</pre>"
        if auth_problem:
            text += "\n\n🔑 Похоже, слетела авторизация — загляните в 👤 Аккаунт."
        text += retry_note + limit_note
        self.notify(
            text,
            silent=task.scheduled and code == 0,
            markup=keyboard([button("📜 Полный лог", f"log:{task.name}"), button("🏠 Меню", "screen:main")]),
        )
        if captcha:
            self.ask_captcha()

    # ---------------------------------------------------------------- captcha

    def ask_captcha(self, chat_id: int | None = None, caption: str = "") -> bool:
        """Картинка капчи hh владельцу; ответ он присылает текстом."""
        owner = chat_id or self.state.owner_id
        image = hhcaptcha.image_path(self.config_path)
        if not owner or not image.exists():
            return False
        self.pending_input[owner] = "captcha"
        self.api.send_photo(
            owner,
            image,
            caption
            or "🔐 <b>hh.ru просит капчу</b>\nПришлите текст с картинки одним сообщением — "
            "отправлю его в hh и продолжу отклики.",
            reply_markup=keyboard(
                [
                    button("🔄 Другая картинка", "captcha:refresh"),
                    button("⏭ Позже", "captcha:later"),
                ]
            ),
        )
        return True

    def answer_captcha(self, chat_id: int, answer: str) -> None:
        if not answer:
            self.ask_captcha(chat_id, "Пустой ответ. Пришлите текст с картинки:")
            return
        self.api.send_message(chat_id, "⏳ Проверяю…")
        code, out, err = self.runner.run_sync(["captcha", f"--answer={answer}"])
        logger.info("Ответ на капчу из Telegram: код %s", code)
        if code == 0:
            started = self.start_task("apply")
            self.api.send_message(
                chat_id,
                "✅ Капча принята. "
                + (
                    "Продолжаю отклики."
                    if started == "Запущено"
                    else f"Отклики: {started.lower()}."
                ),
            )
        elif code == CAPTCHA_WRONG_EXIT_CODE:
            self.ask_captcha(chat_id, "❌ Неверно. Вот новая картинка — пришлите текст с неё:")
        else:
            self.api.send_message(
                chat_id,
                "⚠️ Не удалось отправить капчу:\n<pre>"
                + esc(ANSI_RE.sub("", err or out)[-600:])
                + "</pre>\nПопробую снова при следующем прогоне.",
            )

    def refresh_captcha(self, chat_id: int) -> None:
        code, out, err = self.runner.run_sync(["captcha", "--refresh"])
        if code != 0 or not self.ask_captcha(chat_id, "🔄 Новая картинка — пришлите текст с неё:"):
            self.api.send_message(chat_id, "Капча уже не ждёт ответа.")

    def _send_dry_run_report(self, log: str) -> None:
        """Каждый тестовый отклик — отдельным сообщением: вакансия и письмо."""
        blocks = DRY_RUN_RE.findall(ANSI_RE.sub("", log))
        self.notify(
            f"🧪 <b>Тест: подходящих вакансий {len(blocks)}</b>"
            + ("" if blocks else "\nНичего не нашлось — проверьте ключевые слова и поиск.")
        )
        for block in blocks[:DRY_RUN_LIMIT]:
            head, _, letter = block.partition("--- письмо ---")
            title, _, url = head.strip().partition("\n")
            title = title.removeprefix("🧪 Тест: откликнулся бы на ").strip()
            self.notify(
                f"<b>{esc(title)}</b>\n{esc(url.strip())}\n\n{esc(letter.strip()[:3500])}"
            )

    @property
    def sent_times_path(self) -> Path:
        return self.config_path / SENT_TIMES_FILENAME

    def sent_times(self, now: float | None = None) -> list[float]:
        """Время откликов за последние 24 часа, по возрастанию.

        hh считает лимит 200 за скользящие 24 часа по каждому отклику,
        поэтому apply-vacancies пишет время каждого отправленного отклика.
        """
        now = now or time.time()
        try:
            lines = self.sent_times_path.read_text(encoding="utf-8").split()
        except OSError:
            return []
        times = []
        for line in lines:
            try:
                ts = float(line)
            except ValueError:
                continue
            if ts > now - QUOTA_WINDOW:
                times.append(ts)
        return sorted(times)

    def applied_24h(self, now: float | None = None) -> int:
        return len(self.sent_times(now))

    def quota_free_at(self, slots: int, now: float | None = None) -> float:
        """Когда освободится `slots` мест в лимите (по старым откликам)."""
        now = now or time.time()
        limit = int(self.state.get("schedule", "daily_limit") or 0)
        times = self.sent_times(now)
        need = len(times) - (limit - slots) if limit else 0
        if need <= 0:
            return now
        return times[min(need, len(times)) - 1] + QUOTA_WINDOW + 60

    def _prune_sent_times(self, now: float) -> None:
        keep = [ts for ts in self.sent_times(now)]
        tmp = self.sent_times_path.with_suffix(".tmp")
        tmp.write_text("".join(f"{ts:.0f}\n" for ts in keep), encoding="utf-8")
        tmp.replace(self.sent_times_path)

    # ------------------------------------------------------------- scheduler

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(SCHEDULER_TICK):
            try:
                self.tick()
            except Exception:
                logger.exception("Ошибка планировщика")

    def _ensure_autoresponder(self) -> None:
        if self.runner.running("autoresponder"):
            return
        if time.time() < self.state.last_run("autoresponder_backoff"):
            return
        self.start_task("autoresponder", scheduled=True)

    def forward_chat_events(self) -> None:
        """Пересылает владельцу ответы автоответчика и вопросы, где нужен он сам."""
        path = self.runner.logs_dir / "autoresponder.log"
        if not path.exists():
            return
        size = path.stat().st_size
        if self._events_offset is None or size < self._events_offset:
            # Первый запуск бота — старое не шлём; файл пересоздан — читаем с нуля
            self._events_offset = size if self._events_offset is None else 0
            if size == self._events_offset:
                return
        with path.open("rb") as fp:
            fp.seek(self._events_offset)
            chunk = fp.read()
        end = chunk.rfind(CHAT_EVENT_END)
        if end < 0:
            return
        complete = chunk[: end + len(CHAT_EVENT_END)]
        self._events_offset += len(complete)
        text = ANSI_RE.sub("", complete.decode("utf-8", "replace"))
        for block in CHAT_EVENT_RE.findall(text):
            head, _, body = block.partition("\n")
            # Обычные ответы видны в 📜 Логи; уведомляем только когда нужен владелец
            if not head.startswith("🙋"):
                continue
            fields = dict(
                line.split(": ", 1)
                for line in body.splitlines()
                if line.startswith(("Чат: ", "Кнопки: "))
            )
            chat_id = fields.get("Чат", "").strip()
            options = [
                o.strip() for o in fields.get("Кнопки", "").split(" | ") if o.strip()
            ]
            visible = "\n".join(
                line
                for line in body.splitlines()
                if not line.startswith(("Чат: ", "Кнопки: "))
            ).strip()
            rows = []
            if chat_id.isdigit():
                self.state.set("answers", chat_id, options)
                rows = [
                    [button(o[:40], f"ans:{chat_id}:{i}") for i, o in enumerate(options[:4])],
                    [button("✍️ Ответить самому", f"ansin:{chat_id}")],
                ]
            hint = (
                "\n\n👉 Робот ждёт ответа кнопкой — выберите вариант, бот отправит его в чат."
                if options
                else "\n\n👉 Бот перевёл разговор в Telegram. Можно ответить в чат hh прямо отсюда."
            )
            self.notify(
                f"<b>{esc(head)}</b>\n{esc(visible[:3500])}{hint}",
                markup=keyboard(*rows) if rows else None,
            )

    def send_owner_answer(self, hh_chat_id: str, text: str) -> str:
        """Отправляет ответ владельца в чат hh через автоответчик."""
        code, out, err = self.runner.run_sync(
            ["autoresponder", "--send-chat", hh_chat_id, f"--text={text}"]
        )
        if code == 0:
            self.state.set("answers", hh_chat_id, None)
            return f"✅ Отправлено в чат hh: «{esc(text[:300])}»"
        return "❌ Не удалось отправить:\n<pre>" + esc((err or out)[-800:]) + "</pre>"

    def next_apply_time(self, now: float, every: int) -> float:
        """Раз в сутки — завтра в начале окна со сдвигом до часа.

        Интервал 24 ч от старта «сползал» к вечеру, и прогон начинался перед
        самым концом окна. Чаще раза в сутки — через интервал.
        """
        if every < 86400:
            return now + every + random.randint(60, 600)
        start_hour = self.state.get("schedule", "hours_from")
        tomorrow = datetime.fromtimestamp(now, self.tz) + timedelta(days=1)
        run_at = tomorrow.replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        return run_at.timestamp() + random.randint(60, 3600)

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        try:
            self.forward_chat_events()
        except OSError as ex:
            logger.warning("Не удалось прочитать события автоответчика: %s", ex)
        sch = self.state.get("schedule")
        local = datetime.fromtimestamp(now, self.tz)

        # Без авторизации плановые задачи только сыпали бы ошибками
        token = self.tool_config().get("token") or {}
        if not token.get("access_token"):
            return

        if (
            sch["apply_enabled"]
            and in_hours(local.hour, sch["hours_from"], sch["hours_to"])
            and now >= self.state.last_run("apply_next")
            and not self.runner.running("apply")
        ):
            every = sch["apply_every_hours"] * 3600
            if self.apply_quota_left(now) <= 0:
                # Лимит занят — ждём, пока освободятся места по старым откликам
                self.state.mark_run(
                    "apply_next", self.quota_free_at(MIN_FREE_SLOTS, now)
                )
            else:
                self.start_task("apply", scheduled=True)
                self.state.mark_run("apply_next", self.next_apply_time(now, every))


        if (
            sch["update_resumes_enabled"]
            and now >= self.state.last_run("update_resumes_next")
            and not self.runner.running("update_resumes")
        ):
            self.start_task("update_resumes", scheduled=True)
            self.state.mark_run(
                "update_resumes_next",
                now + UPDATE_RESUMES_EVERY + random.randint(60, 600),
            )

        if sch["autoresponder_enabled"]:
            task = self.runner.get("autoresponder")
            if (
                task
                and task.proc.poll() is None
                and task.elapsed > AUTORESPONDER_RESTART_EVERY
            ):
                # Периодический перезапуск подхватывает обновлённый токен
                self.runner.stop("autoresponder", wait=True)
            self._ensure_autoresponder()
        elif self.runner.running("autoresponder"):
            self.runner.stop("autoresponder")

        if (
            now - self.state.last_run("refresh_token") > REFRESH_TOKEN_EVERY
            and not self.runner.running_tasks()
            and token.get("refresh_token")
        ):
            self.start_task("refresh_token", scheduled=True)

        if (
            now - self.state.last_run("cleanup") > CLEANUP_EVERY
            and not self.runner.running("apply")
        ):
            self.state.mark_run("cleanup", now)
            try:
                self.cleanup_storage()
            except sqlite3.Error as ex:
                logger.warning("Не удалось почистить базу: %s", ex)

    def cleanup_storage(self) -> None:
        """Раз в сутки: старые сохранённые вакансии и отметки о пропуске.

        Контакты работодателей не трогаем — они могут пригодиться.
        """
        if self.sent_times_path.exists():
            self._prune_sent_times(time.time())
        db_path = self.config_path / DATABASE_FILENAME
        if not db_path.exists():
            return
        con = sqlite3.connect(db_path, timeout=30)
        try:
            vacancies = con.execute(
                "DELETE FROM vacancies WHERE updated_at < datetime('now', ?)",
                (f"-{VACANCY_KEEP_DAYS} days",),
            ).rowcount
            skipped = con.execute(
                "DELETE FROM skipped_vacancies WHERE created_at < datetime('now', ?)",
                (f"-{SKIPPED_KEEP_DAYS} days",),
            ).rowcount
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()
        logger.info(
            "Чистка базы: удалено вакансий %d, отметок о пропуске %d",
            vacancies,
            skipped,
        )
