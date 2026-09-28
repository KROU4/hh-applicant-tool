"""Запуск операций hh-applicant-tool в отдельных процессах.

Каждая операция живёт в своём процессе: бот остаётся отзывчивым, задачу
можно остановить, а падение операции не роняет бота. Вывод пишется в
`bot_logs/<задача>.log` в каталоге профиля.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__package__)

STOP_GRACE_SECONDS = 15


@dataclass
class Task:
    name: str
    title: str
    args: list[str]
    proc: subprocess.Popen
    log_path: Path
    started_at: float = field(default_factory=time.time)
    scheduled: bool = False
    stopped_by_user: bool = False

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at


FinishCallback = Callable[[Task, int], None]


class TaskRunner:
    def __init__(
        self,
        config_path: Path,
        on_finish: FinishCallback,
        python: str = sys.executable,
    ):
        self.config_path = config_path
        self.logs_dir = config_path / "bot_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.on_finish = on_finish
        self.python = python
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def command(self, args: list[str]) -> list[str]:
        return [
            self.python,
            "-m",
            "hh_applicant_tool",
            "--config-dir",
            str(self.config_path),
            *args,
        ]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", TERM="dumb")
        env.pop("HH_PROFILE_ID", None)
        return env

    def start(
        self,
        name: str,
        title: str,
        args: list[str],
        *,
        scheduled: bool = False,
    ) -> Task | None:
        """Запускает задачу. None — если такая задача уже работает."""
        with self._lock:
            if self._is_alive(name):
                return None
            log_path = self.logs_dir / f"{name}.log"
            log_file = log_path.open("w", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    self.command(args),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=self._env(),
                    start_new_session=os.name == "posix",
                )
            finally:
                log_file.close()
            task = Task(
                name=name,
                title=title,
                args=args,
                proc=proc,
                log_path=log_path,
                scheduled=scheduled,
            )
            self._tasks[name] = task
        logger.info("Запущена задача %s: %s", name, args)
        threading.Thread(
            target=self._watch, args=(task,), name=f"watch-{name}", daemon=True
        ).start()
        return task

    def _watch(self, task: Task) -> None:
        code = task.proc.wait()
        logger.info("Задача %s завершилась с кодом %s", task.name, code)
        try:
            self.on_finish(task, code)
        except Exception:
            logger.exception("Ошибка обработчика завершения задачи")

    def _is_alive(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and task.proc.poll() is None

    def running(self, name: str) -> bool:
        with self._lock:
            return self._is_alive(name)

    def get(self, name: str) -> Task | None:
        with self._lock:
            return self._tasks.get(name)

    def running_tasks(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.proc.poll() is None]

    def stop(self, name: str, *, wait: bool = False) -> bool:
        """Мягко останавливает задачу (SIGINT), потом добивает."""
        with self._lock:
            task = self._tasks.get(name)
            if task is None or task.proc.poll() is not None:
                return False
            task.stopped_by_user = True

        def _stop() -> None:
            try:
                if os.name == "posix":
                    task.proc.send_signal(signal.SIGINT)
                    try:
                        task.proc.wait(STOP_GRACE_SECONDS)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                task.proc.terminate()
                try:
                    task.proc.wait(STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    task.proc.kill()
            except ProcessLookupError:
                pass

        if wait:
            _stop()
        else:
            threading.Thread(target=_stop, daemon=True).start()
        return True

    def stop_all(self) -> None:
        for task in self.running_tasks():
            self.stop(task.name, wait=True)

    def tail(self, name: str, lines: int = 40) -> str:
        path = self.logs_dir / f"{name}.log"
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            return "".join(deque(fp, maxlen=lines))

    def run_sync(
        self, args: list[str], timeout: float = 90
    ) -> tuple[int, str, str]:
        """Короткая команда с ожиданием результата: код, stdout, stderr."""
        try:
            result = subprocess.run(
                self.command(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._env(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return 124, "", "Команда не уложилась в таймаут"
        return result.returncode, result.stdout.strip(), result.stderr.strip()
