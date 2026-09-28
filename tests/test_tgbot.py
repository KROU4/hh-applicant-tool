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
    "screen", ["main", "settings", "schedule", "stats", "logs", "ai", "account"]
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


def test_apply_finish_counts_responses(bot):
    log = bot.runner.logs_dir / "apply.log"
    log.write_text(
        "[I] шум\n✅️ Закончили рассылку для резюме: Python. Отправлено: 7\n",
        encoding="utf-8",
    )
    task = bot.runner.start("apply", "🚀 Отклики", [])
    bot._on_task_finish(task, 0)
    assert bot._applied_today() == 7
    assert "Отправлено откликов: <b>7</b>" in bot.api.sent[-1][1]


def test_in_hours_wraps_midnight():
    assert in_hours(23, 22, 6)
    assert in_hours(3, 22, 6)
    assert not in_hours(12, 22, 6)
    assert in_hours(9, 9, 21) and not in_hours(21, 9, 21)


def test_summarize_log_prefers_markers():
    log = "a\nb\n\x1b[31m[E] Ошибка сети\x1b[0m\nc\n"
    assert summarize_log(log) == "[E] Ошибка сети"
