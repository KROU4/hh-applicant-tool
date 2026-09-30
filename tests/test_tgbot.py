from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hh_applicant_tool.tgbot.bot import (
    HHBot,
    build_apply_args,
    in_hours,
    summarize_log,
)


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []
        self.edited: list[tuple[int, int, str, Any]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.documents: list[Path] = []
        self.files: dict[str, bytes] = {}
        self.photos: list = []

    def send_message(self, chat_id, text, reply_markup=None, *, silent=False):
        self.sent.append((chat_id, text, reply_markup))
        return {}

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))

    def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def send_document(self, chat_id, path, caption=None):
        self.documents.append(path)

    def download_file(self, file_id):
        return self.files[file_id]

    def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.photos.append((chat_id, photo, caption))
        return {"photo": [{"file_id": "small"}, {"file_id": "big-id"}]}


class FakeProc:
    def __init__(self, alive: bool = True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


class FakeRunner:
    def __init__(self, config_path: Path):
        self.logs_dir = config_path / "bot_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.started: list[tuple[str, list[str], bool]] = []
        self.tasks: dict[str, Any] = {}
        self.stopped: list[str] = []
        self.sync_result = (0, "", "")

    def start(self, name, title, args, *, scheduled=False):
        if name in self.tasks and self.tasks[name].proc.poll() is None:
            return None
        task = SimpleNamespace(
            name=name,
            title=title,
            args=args,
            proc=FakeProc(),
            started_at=time.time(),
            elapsed=0,
            scheduled=scheduled,
            stopped_by_user=False,
        )
        self.tasks[name] = task
        self.started.append((name, args, scheduled))
        return task

    def running(self, name):
        task = self.tasks.get(name)
        return bool(task and task.proc.poll() is None)

    def get(self, name):
        return self.tasks.get(name)

    def running_tasks(self):
        return [t for t in self.tasks.values() if t.proc.poll() is None]

    def stop(self, name, *, wait=False):
        task = self.tasks.get(name)
        if not task or task.proc.poll() is not None:
            return False
        task.proc.alive = False
        self.stopped.append(name)
        return True

    def stop_all(self):
        for task in self.running_tasks():
            self.stop(task.name)

    def tail(self, name, lines=40):
        path = self.logs_dir / f"{name}.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def run_sync(self, args, timeout=90):
        return self.sync_result


OWNER = 111


def today_ts() -> float:
    """Полдень сегодня: тест не зависит от того, запущен ли он около полуночи."""
    from datetime import datetime as _dt

    return _dt.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


@pytest.fixture
def bot(tmp_path: Path) -> HHBot:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "token": {"access_token": "a", "refresh_token": "r"},
                "openrouter": {"api_key": "sk-or-x"},
            }
        ),
        encoding="utf-8",
    )
    b = HHBot(FakeAPI(), tmp_path, owner_id=OWNER, runner=FakeRunner(tmp_path))
    b.state.set("schedule", "timezone", None)
    return b


def message(text: str, user: int = OWNER) -> dict:
    return {
        "update_id": 1,
        "message": {"from": {"id": user}, "chat": {"id": user}, "text": text},
    }


def callback(data: str, user: int = OWNER) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb",
            "from": {"id": user},
            "data": data,
            "message": {"chat": {"id": user}, "message_id": 7},
        },
    }


def test_build_apply_args_full(tmp_path):
    letter = tmp_path / "letter.txt"
    args = build_apply_args(
        {
            "use_ai": True,
            "force_message": True,
            "ai_filter": "light",
            "dry_run": True,
            "search": "python -senior",
            "excluded_filter": "junior|php",
            "max_responses": 50,
            "resume_id": "abc",
            "experience": "between3And6",
            "system_prompt": "Пиши кратко",
            "work_format": "REMOTE,HYBRID",
            "send_email": False,
        },
        letter,
    )
    assert args[:3] == ["apply-vacancies", "--ai-rate-limit", "20"]
    assert "--use-ai" in args and "--force-message" in args
    assert "--search=python -senior" in args
    assert "--excluded-filter=junior|php" in args
    assert args[-3:] == ["--work-format", "REMOTE", "HYBRID"]
    assert "--letter-file" not in args


def test_build_apply_args_uses_letter_without_ai(tmp_path):
    letter = tmp_path / "letter.txt"
    letter.write_text("Здравствуйте", encoding="utf-8")
    args = build_apply_args({"use_ai": False}, letter)
    assert args[-2:] == ["--letter-file", str(letter)]


def test_apply_args_parse_with_real_parser(tmp_path):
    from hh_applicant_tool.main import HHApplicantTool

    parser = HHApplicantTool()._parser
    args = build_apply_args(
        {
            "use_ai": True,
            "search": "-python",
            "excluded_filter": "junior",
            "work_format": "REMOTE",
            "system_prompt": "--кратко",
        },
        tmp_path / "letter.txt",
    )
    ns = parser.parse_args(args)
    assert ns.search == "-python"
    assert ns.system_prompt == "--кратко"
    assert ns.work_format == ["REMOTE"]


def test_first_start_claims_owner(tmp_path):
    b = HHBot(FakeAPI(), tmp_path, runner=FakeRunner(tmp_path))
    b.handle_update(message("/start", user=42))
    assert b.state.owner_id == 42

    b.handle_update(message("/start", user=99))
    assert b.api.sent[-1][1] == "⛔ Нет доступа"
    assert b.state.owner_id == 42


@pytest.mark.parametrize(
    "screen", ["main", "settings", "schedule", "stats", "logs", "ai", "account", "answers"]
)
def test_all_screens_render(bot, screen):
    text, markup = bot.render(screen)
    assert text and markup["inline_keyboard"]


def test_run_and_stop_apply(bot):
    bot.handle_update(callback("run:apply"))
    assert bot.runner.started[0][0] == "apply"
    assert bot.api.answers[-1] == ("cb", "Запущено")

    bot.handle_update(callback("run:apply"))
    assert bot.api.answers[-1] == ("cb", "Уже выполняется")

    bot.handle_update(callback("stop:apply"))
    assert bot.runner.stopped == ["apply"]


def test_settings_flip_and_cycle(bot):
    bot.handle_update(callback("flip:use_ai"))
    assert bot.state.get("apply", "use_ai") is False
    bot.handle_update(callback("cycle:ai_filter"))
    assert bot.state.get("apply", "ai_filter") == "light"
    bot.handle_update(callback("cycle:work_format"))
    assert bot.state.get("apply", "work_format") == "REMOTE"


def test_text_input_flow(bot):
    bot.handle_update(callback("input:excluded_filter"))
    bot.handle_update(message("junior|php"))
    assert bot.state.get("apply", "excluded_filter") == "junior|php"

    bot.handle_update(callback("input:excluded_filter"))
    bot.handle_update(message("(broken"))
    assert "Некорректное" in bot.api.sent[-2][1]

    bot.handle_update(callback("input:hours"))
    bot.handle_update(message("10-20"))
    assert bot.state.get("schedule", "hours_from") == 10
    assert bot.state.get("schedule", "hours_to") == 20


def test_upload_config_keeps_openrouter_key(bot):
    bot.api.files["f1"] = json.dumps(
        {"token": {"access_token": "new"}}
    ).encode()
    bot.handle_update(
        {
            "update_id": 3,
            "message": {
                "from": {"id": OWNER},
                "chat": {"id": OWNER},
                "document": {"file_id": "f1", "file_name": "config.json"},
            },
        }
    )
    cfg = bot.tool_config()
    assert cfg["token"]["access_token"] == "new"
    assert cfg["openrouter"]["api_key"] == "sk-or-x"


def test_scheduler_starts_due_tasks(bot):
    bot.state.set("schedule", "apply_enabled", True)
    bot.state.set("schedule", "hours_from", 0)
    bot.state.set("schedule", "hours_to", 0)
    bot.state.set("schedule", "autoresponder_enabled", True)

    bot.tick()
    started = {name for name, _, scheduled in bot.runner.started if scheduled}
    assert started == {"apply", "update_resumes", "autoresponder"}

    # Повторный тик сразу после — ничего нового
    bot.runner.tasks["apply"].proc.alive = False
    bot.tick()
    assert len(bot.runner.started) == 3


def test_scheduler_idle_without_token(bot):
    bot.tool_config().save(token={})
    bot.state.set("schedule", "apply_enabled", True)
    bot.tick()
    assert bot.runner.started == []


def test_in_hours_wraps_midnight():
    assert in_hours(23, 22, 6)
    assert in_hours(3, 22, 6)
    assert not in_hours(12, 22, 6)
    assert in_hours(9, 9, 21) and not in_hours(21, 9, 21)


def test_summarize_log_prefers_markers():
    log = "a\nb\n\x1b[31m[E] Ошибка сети\x1b[0m\nc\n"
    assert summarize_log(log) == "[E] Ошибка сети"


def test_normalize_letter_converts_placeholders_and_percent():
    from hh_applicant_tool.tgbot.bot import normalize_letter
    from hh_applicant_tool.utils.string import render_template

    template = normalize_letter(
        "{Здравствуйте|Добрый день}! Откликаюсь на {vacancy_name} в {employer_name}, готов на 100%."
    )
    text = render_template(
        template.replace("{Здравствуйте|Добрый день}", "Здравствуйте"),
        {"vacancy_name": "Python", "employer_name": "ООО Ромашка"},
    )
    assert text == "Здравствуйте! Откликаюсь на Python в ООО Ромашка, готов на 100%."


def test_letter_with_unknown_placeholder_is_rejected(bot):
    reply = bot.apply_input("letter", "Привет, %(salary)s")
    assert reply.startswith("❌")
    assert not bot.letter_path.exists()


def test_refresh_token_code_2_is_success(bot):
    task = bot.runner.start("refresh_token", "🔑", [], scheduled=True)
    bot._on_task_finish(task, 2)
    # Плановая успешная задача молчит
    assert bot.api.sent == []


def test_upload_config_does_not_override_server_keys(bot):
    bot.api.files["f2"] = json.dumps(
        {
            "token": {"access_token": "pc"},
            "openrouter": {"api_key": "sk-or-from-pc"},
            "telegram_bot": {"token": "other"},
        }
    ).encode()
    bot.handle_document(OWNER, {"file_id": "f2", "file_name": "config.json"})
    cfg = bot.tool_config()
    assert cfg["token"]["access_token"] == "pc"
    assert cfg["openrouter"]["api_key"] == "sk-or-x"


def test_telegram_falls_back_to_plain_text():
    from hh_applicant_tool.tgbot.telegram import TelegramAPI

    class Resp:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200
            self.text = ""

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, **kw):
            self.calls.append(json)
            if json.get("parse_mode"):
                return Resp(
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: can't parse entities",
                    }
                )
            return Resp({"ok": True, "result": {}})

    session = Session()
    api = TelegramAPI("1:x", session=session)
    api.send_message(1, "<b>Итог</b>\n<pre>a &lt; b</pre>")
    assert session.calls[-1]["text"] == "Итог\na < b"
    assert "parse_mode" not in session.calls[-1]

    long_text = "<pre>" + "x" * 5000 + "</pre>"
    api.send_message(1, long_text)
    assert len(session.calls[-1]["text"]) <= 4096


def test_redact_hides_bot_token():
    from hh_applicant_tool.tgbot.telegram import redact

    msg = "HTTPSConnectionPool: /bot123456:AA-bb_cc/getUpdates timed out"
    assert "AA-bb_cc" not in redact(msg)


def _max_responses(args: list[str]) -> int:
    return int(args[args.index("--max-responses") + 1])


def test_own_run_limit_is_kept_when_smaller(bot):
    bot.state.set("apply", "max_responses", 30)
    bot.start_task("apply")
    assert _max_responses(bot.runner.started[-1][1]) == 30


def test_daily_schedule_runs_next_morning(bot):
    from datetime import datetime as _dt, timedelta as _td

    bot.state.set("schedule", "apply_enabled", True)
    bot.state.set("schedule", "hours_from", 0)
    bot.state.set("schedule", "hours_to", 0)
    now = today_ts() + 8 * 3600  # вечер
    bot.tick(now)
    run_at = _dt.fromtimestamp(bot.state.last_run("apply_next"))
    tomorrow = (_dt.fromtimestamp(now) + _td(days=1)).date()
    assert run_at.date() == tomorrow and run_at.hour == 0


def test_daily_schedule_uses_window_start(bot):
    from datetime import datetime as _dt

    bot.state.set("schedule", "hours_from", 9)
    run_at = _dt.fromtimestamp(bot.next_apply_time(today_ts(), 24 * 3600))
    assert run_at.hour == 9 and 1 <= run_at.minute * 60 + run_at.second <= 3600


def test_daily_limit_input(bot):
    bot.handle_update(callback("input:daily_limit"))
    bot.handle_update(message("150"))
    assert bot.state.get("schedule", "daily_limit") == 150


def test_letter_contact_is_passed_to_apply(bot, tmp_path):
    from hh_applicant_tool.main import HHApplicantTool

    bot.handle_update(callback("input:letter_contact"))
    bot.handle_update(message("https://t.me/krou4"))
    args = build_apply_args(bot.state.get("apply"), tmp_path / "letter.txt")
    assert "--letter-contact=https://t.me/krou4" in args
    ns = HHApplicantTool()._parser.parse_args(args)
    assert ns.letter_contact == "https://t.me/krou4"


@pytest.mark.parametrize(
    "pattern, query",
    [
        ("llm|rag|ai engineer", 'llm OR rag OR "ai engineer"'),
        ("llm|ai[- ]?engineer|ai-инженер", 'llm OR "ai engineer" OR ai-инженер'),
        ("LLM|(agent|агент)", "LLM OR agent OR агент"),
        (r"rag\b|c++", "rag OR c++"),
    ],
)
def test_regex_to_query(pattern, query):
    from hh_applicant_tool.tgbot.bot import regex_to_query

    assert regex_to_query(pattern) == query


def test_keywords_drive_search_and_filter(bot, tmp_path):
    from hh_applicant_tool.main import HHApplicantTool

    bot.handle_update(callback("input:included_filter"))
    bot.handle_update(message("llm|rag|ai[- ]?engineer"))
    bot.handle_update(callback("flip:search_in_name"))

    args = build_apply_args(bot.state.get("apply"), tmp_path / "letter.txt")
    ns = HHApplicantTool()._parser.parse_args(args)
    assert ns.search == 'llm OR rag OR "ai engineer"'
    assert ns.included_filter == "llm|rag|ai[- ]?engineer"
    assert ns.search_field == ["name"]

    # Явный поиск важнее автоматического
    bot.state.set("apply", "search", "python")
    args = build_apply_args(bot.state.get("apply"), tmp_path / "letter.txt")
    assert HHApplicantTool()._parser.parse_args(args).search == "python"


def test_dry_run_report_sends_each_letter(bot):
    log = (
        "🚀 Начинаю рассылку откликов для резюме: Senior AI Engineer\n"
        "🧪 Тест: откликнулся бы на «LLM Engineer» — Лайфтех\n"
        "https://hh.ru/vacancy/1\n--- письмо ---\nДобрый день!\nПишу по вакансии.\n"
        "=== конец ===\n"
        "🧪 Тест: откликнулся бы на «AI Developer» — ТГТ\n"
        "https://hh.ru/vacancy/2\n--- письмо ---\nДобрый день! <b>RAG</b>\n=== конец ===\n"
        "✅️ Закончили рассылку для резюме: Senior AI Engineer. Отправлено: 2\n"
    )
    (bot.runner.logs_dir / "apply.log").write_text(log, encoding="utf-8")
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies", "--dry-run"])
    bot._on_task_finish(task, 0)

    texts = [t for _, t, _ in bot.api.sent]
    assert "подходящих вакансий 2" in texts[0]
    assert texts[1].startswith("<b>«LLM Engineer» — Лайфтех</b>\nhttps://hh.ru/vacancy/1")
    assert "Пишу по вакансии." in texts[1]
    assert "&lt;b&gt;RAG&lt;/b&gt;" in texts[2]


def test_chat_events_are_forwarded_once(bot):
    log = bot.runner.logs_dir / "autoresponder.log"
    log.write_text("старое событие\n", encoding="utf-8")
    bot.forward_chat_events()  # первый проход: старое не пересылаем
    assert bot.api.sent == []

    with log.open("a", encoding="utf-8") as fp:
        fp.write(
            "🙋 Нужно ваше решение: «AI Engineer» — Лайфтех\nhttps://hh.ru/vacancy/1\n"
            "Работодатель (Анна): Какая зарплата?\nОтвет: Обсудим в Telegram\n=== конец ===\n"
            "💬 Ответил в чате: «ML» — ТГТ\nhttps://hh.ru/vacancy/2\n"
            "Работодатель (Павел): Спасибо\nОтвет: Хорошо\n=== конец ===\n"
            "💬 Ответил в чате: «незаконченный блок"
        )
    bot.forward_chat_events()
    bot.forward_chat_events()  # повторно ничего не шлём

    texts = [t for _, t, _ in bot.api.sent]
    # Обычные ответы не пересылаются — только где нужно решение владельца
    assert len(texts) == 1
    assert texts[0].startswith("<b>🙋 Нужно ваше решение") and "ответить в чат hh" in texts[0]
    assert "Какая зарплата?" in texts[0]


def test_chat_events_after_log_recreated(bot):
    log = bot.runner.logs_dir / "autoresponder.log"
    log.write_text("x" * 500, encoding="utf-8")
    bot.forward_chat_events()
    log.write_text(
        "🙋 Нужно ваше решение: «ML» — ТГТ\nhttps://hh.ru/vacancy/2\nОтвет: Ок\n=== конец ===\n",
        encoding="utf-8",
    )
    bot.forward_chat_events()
    assert len(bot.api.sent) == 1


def test_start_sends_welcome_photo_once_then_by_file_id(bot):
    (bot.config_path / "welcome.jpg").write_bytes(b"jpg")
    bot.handle_update(message("/start"))
    bot.handle_update(message("/start"))
    first, second = bot.api.photos
    assert isinstance(first[1], Path) and "автопилот откликов" in first[2]
    assert second[1] == "big-id"
    # После приветствия — панель управления
    assert "HH панель управления" in bot.api.sent[-1][1]


def test_owner_answers_robot_button_from_telegram(bot):
    log = bot.runner.logs_dir / "autoresponder.log"
    log.write_text("", encoding="utf-8")
    bot.forward_chat_events()
    with log.open("a", encoding="utf-8") as fp:
        fp.write(
            "🙋 Нужно ваше решение: «AI-разработчик» — Инфогород\n"
            "https://hh.ru/vacancy/1\n"
            "Работодатель (Робот-рекрутер): Готовы ли к офисному формату?\n"
            "Ответ: не отправлен — выберите вариант\n"
            "Чат: 5666562040\nКнопки: да | нет\n=== конец ===\n"
        )
    bot.forward_chat_events()
    _, text, markup = bot.api.sent[-1]
    assert "Чат:" not in text and "Робот ждёт ответа кнопкой" in text
    buttons = markup["inline_keyboard"]
    assert [b["callback_data"] for b in buttons[0]] == ["ans:5666562040:0", "ans:5666562040:1"]

    bot.runner.sync_result = (0, "Отправлено", "")
    calls = []
    bot.runner.run_sync = lambda args, timeout=90: calls.append(args) or (0, "ok", "")
    bot._background = lambda fn: fn()
    bot.handle_update(callback("ans:5666562040:1"))
    assert calls[-1] == ["autoresponder", "--send-chat", "5666562040", "--text=нет"]
    assert "Отправлено в чат hh" in bot.api.sent[-1][1]

    # Свой ответ текстом
    bot.handle_update(callback("ansin:5666562040"))
    bot.handle_update(message("Готов обсудить гибрид"))
    assert calls[-1][-1] == "--text=Готов обсудить гибрид"


def test_answers_screen_add_and_replace(bot):
    text, markup = bot.render("answers")
    assert "Пока пусто" in text

    bot.handle_update(callback("input:answers_add"))
    bot.handle_update(message("Формат: только удалёнка"))
    bot.handle_update(callback("input:answers_add"))
    bot.handle_update(message("График: любой"))
    assert bot.answers_path.read_text(encoding="utf-8") == "Формат: только удалёнка\nГрафик: любой\n"
    assert "График: любой" in bot.render("answers")[0]

    bot.handle_update(callback("input:answers_set"))
    bot.handle_update(message("-"))
    assert not bot.answers_path.exists()


def test_daily_storage_cleanup(bot):
    import sqlite3

    con = sqlite3.connect(bot.config_path / "data")
    con.executescript(
        "CREATE TABLE vacancies (id INTEGER, updated_at DATETIME);"
        "CREATE TABLE skipped_vacancies (id INTEGER, created_at DATETIME);"
        "INSERT INTO vacancies VALUES (1, datetime('now', '-10 days')), (2, datetime('now'));"
        "INSERT INTO skipped_vacancies VALUES (1, datetime('now', '-40 days')), (2, datetime('now', '-1 day'));"
    )
    con.commit()
    con.close()

    bot.tick()
    bot.tick()  # второй раз в тот же день ничего не делает

    con = sqlite3.connect(bot.config_path / "data")
    assert [r[0] for r in con.execute("SELECT id FROM vacancies")] == [2]
    assert [r[0] for r in con.execute("SELECT id FROM skipped_vacancies")] == [2]
    con.close()
    assert bot.state.last_run("cleanup") > 0


def test_captcha_is_sent_to_owner_and_answer_resumes_apply(bot):
    bot.state.set("schedule", "apply_enabled", True)
    bot.state.set("schedule", "hours_from", 0)
    bot.state.set("schedule", "hours_to", 0)
    (bot.config_path / "captcha.png").write_bytes(b"PNG")
    (bot.config_path / "captcha_pending.json").write_text('{"key": "k"}', encoding="utf-8")
    (bot.runner.logs_dir / "apply.log").write_text("⏸ hh запросил капчу\n", encoding="utf-8")
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies"])
    task.proc.alive = False

    bot._on_task_finish(task, 3)

    chat, photo, caption = bot.api.photos[-1]
    assert chat == OWNER and photo.name == "captcha.png" and "просит капчу" in caption
    assert "введите капчу — продолжу сразу" in bot.api.sent[-1][1]

    calls = []
    bot.runner.run_sync = lambda args, timeout=90: calls.append(args) or (0, "ok", "")
    bot._background = lambda fn: fn()
    bot.handle_update(message("хатку кропил"))
    assert calls[-1] == ["captcha", "--answer=хатку кропил"]
    assert bot.runner.started[-1][0] == "apply"
    assert "Капча принята" in bot.api.sent[-1][1]


def test_wrong_captcha_answer_sends_new_image(bot):
    (bot.config_path / "captcha.png").write_bytes(b"PNG")
    (bot.config_path / "captcha_pending.json").write_text('{"key": "k"}', encoding="utf-8")
    bot.runner.run_sync = lambda args, timeout=90: (4, "", "")
    bot._background = lambda fn: fn()
    bot.handle_update(message("неверно"))
    assert "Неверно" in bot.api.photos[-1][2]
    assert bot.pending_input[OWNER] == "captcha"


def test_stop_words_are_literal(bot, tmp_path):
    import re as _re

    from hh_applicant_tool.tgbot.bot import normalize_keywords

    assert normalize_keywords("junior|C++|c#|.net") == r"junior|C\+\+|c\#|\.net"
    # Регулярные выражения и повторная нормализация не меняются
    assert normalize_keywords(r"ai[- ]?engineer|C\+\+") == r"ai[- ]?engineer|C\+\+"
    args = build_apply_args({"excluded_filter": "junior|C++"}, tmp_path / "letter.txt")
    pattern = [a for a in args if a.startswith("--excluded-filter=")][0].split("=", 1)[1]
    assert not _re.search(pattern, "LLM-инженер Lexica", _re.I)
    assert _re.search(pattern, "C++ developer", _re.I)



def write_sent(bot, times):
    bot.sent_times_path.write_text("".join(f"{t:.0f}\n" for t in times), encoding="utf-8")


def test_quota_is_rolling_24h_by_each_response(bot):
    now = time.time()
    # 30 часов назад — уже не считается; 20 часов назад и час назад — считаются
    write_sent(bot, [now - 30 * 3600] * 5 + [now - 20 * 3600] * 150 + [now - 3600] * 30)
    assert bot.applied_24h(now) == 180
    assert bot.apply_quota_left(now) == 20
    assert "Откликов за 24 часа: <b>180</b> из 200" in bot.render("main")[0]


def test_apply_run_is_capped_by_free_quota(bot):
    write_sent(bot, [time.time() - 3600] * 150)
    bot.handle_update(callback("run:apply"))
    args = bot.runner.started[-1][1]
    assert _max_responses(args) == 50
    assert args.count("--max-responses") == 1


def test_own_run_limit_is_kept_when_smaller_than_quota(bot):
    bot.state.set("apply", "max_responses", 30)
    bot.start_task("apply")
    assert _max_responses(bot.runner.started[-1][1]) == 30


def test_quota_full_schedules_run_when_slots_free(bot):
    now = time.time()
    oldest = now - 20 * 3600
    write_sent(bot, [oldest + i for i in range(200)])
    assert bot.start_task("apply") == "Суточный лимит откликов уже набран"

    bot.state.set("schedule", "apply_enabled", True)
    bot.state.set("schedule", "hours_from", 0)
    bot.state.set("schedule", "hours_to", 0)
    bot.state.set("schedule", "update_resumes_enabled", False)
    bot.tick(now)
    assert "apply" not in [name for name, _, _ in bot.runner.started]
    # 20 мест освободятся, когда 20-й по старшинству отклик выйдет из окна
    expected = oldest + 19 + 24 * 3600 + 60
    assert bot.state.last_run("apply_next") == pytest.approx(expected, abs=2)


def test_hh_limit_message_reschedules_after_free_slots(bot):
    now = time.time()
    write_sent(bot, [now - 23 * 3600] * 100 + [now - 60] * 100)
    bot.state.set("schedule", "apply_enabled", True)
    (bot.runner.logs_dir / "apply.log").write_text(
        "📨 Отправили отклик на вакансию https://hh.ru/vacancy/1\n"
        "⛔ Лимит откликов hh.ru исчерпан. Попробуйте позже.\n",
        encoding="utf-8",
    )
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies"])
    task.proc.alive = False
    bot._on_task_finish(task, 0)
    resume_at = bot.state.last_run("apply_next")
    assert now + 3600 - 5 <= resume_at <= now + 3600 + 120
    assert "200 откликов за 24 часа" in bot.api.sent[-1][1]


def test_apply_finish_reports_sent_count(bot):
    log = "🚀 Начинаю\n" + "📨 Отправили отклик на вакансию https://hh.ru/vacancy/1\n" * 7
    (bot.runner.logs_dir / "apply.log").write_text(log, encoding="utf-8")
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies"])
    task.proc.alive = False
    bot._on_task_finish(task, 0)
    assert "Отправлено откликов: <b>7</b>" in bot.api.sent[-1][1]


def test_failed_apply_run_is_retried_while_quota_left(bot):
    bot.state.set("schedule", "apply_enabled", True)
    bot.state.set("schedule", "hours_from", 0)
    bot.state.set("schedule", "hours_to", 0)
    write_sent(bot, [time.time() - 60] * 44)
    (bot.runner.logs_dir / "apply.log").write_text("[E] 403 Client Error\n", encoding="utf-8")
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies"])
    task.proc.alive = False
    before = time.time()
    bot._on_task_finish(task, 1)
    assert bot.state.last_run("apply_next") == pytest.approx(before + 1800, abs=5)
    assert "повторю через 30 мин" in bot.api.sent[-1][1]
    assert "Осталось 156 откликов из лимита" in bot.api.sent[-1][1]


def test_captcha_pause_retries_in_an_hour(bot):
    bot.state.set("schedule", "apply_enabled", True)
    bot.state.set("schedule", "hours_from", 0)
    bot.state.set("schedule", "hours_to", 0)
    (bot.runner.logs_dir / "apply.log").write_text(
        "📨 Отправили отклик на вакансию https://hh.ru/vacancy/1\n"
        "⏸ hh запросил капчу — ставлю отклики на паузу\n",
        encoding="utf-8",
    )
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies"])
    task.proc.alive = False
    before = time.time()
    bot._on_task_finish(task, 3)
    assert bot.state.last_run("apply_next") == pytest.approx(before + 3600, abs=5)
    text = bot.api.sent[-1][1]
    assert "пауза — hh попросил капчу" in text
    assert "Отправлено откликов: <b>1</b>" in text


def test_dry_run_is_limited_and_ignores_quota(bot):
    bot.state.set("apply", "dry_run", True)
    write_sent(bot, [time.time() - 60] * 200)
    assert bot.start_task("apply") == "Запущено"
    args = bot.runner.started[-1][1]
    assert _max_responses(args) == 5 and "--dry-run" in args


def test_sent_times_are_pruned_by_daily_cleanup(bot):
    now = time.time()
    write_sent(bot, [now - 50 * 3600, now - 60])
    bot.cleanup_storage()
    assert bot.sent_times_path.read_text(encoding="utf-8").split() == [f"{now - 60:.0f}"]


def test_run_that_used_its_share_continues_when_slots_free(bot):
    now = time.time()
    write_sent(bot, [now - 23.9 * 3600] * 50 + [now - 60] * 150)
    bot.state.set("schedule", "apply_enabled", True)
    (bot.runner.logs_dir / "apply.log").write_text(
        "📨 Отправили отклик на вакансию https://hh.ru/vacancy/1\n" * 20, encoding="utf-8"
    )
    task = bot.runner.start("apply", "🚀 Отклики", ["apply-vacancies", "--max-responses", "20"])
    task.proc.alive = False
    bot._on_task_finish(task, 0)
    resume_at = bot.state.last_run("apply_next")
    # старые 50 выйдут из окна через ~6 минут, но не раньше 10-минутной паузы
    assert now + 600 - 5 <= resume_at <= now + 700
    assert "Продолжу" in bot.api.sent[-1][1]
