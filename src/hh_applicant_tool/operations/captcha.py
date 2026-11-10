from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation
from ..utils import capsolver, hhcaptcha

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

# Коды выхода для Telegram-бота
WRONG_ANSWER_EXIT_CODE = 4


class Namespace(BaseNamespace):
    prepare: str | None
    answer: str | None
    refresh: bool
    auto: str | None
    check: bool


class Operation(BaseOperation):
    """Капча hh.ru: картинка для человека, отправка ответа, автопроверка."""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--prepare", metavar="URL", help="Ссылка на капчу из ошибки API")
        group.add_argument("--answer", help="Текст с картинки")
        group.add_argument(
            "--refresh", action="store_true", help="Другая картинка для той же капчи"
        )
        group.add_argument(
            "--auto",
            metavar="URL",
            help="Решить капчу автоматически (OpenRouter, затем CapSolver)",
        )
        group.add_argument(
            "--check",
            action="store_true",
            help="Проверить настройку решателей и баланс CapSolver",
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> int | None:
        try:
            if args.check:
                return self._check(tool)
            if args.auto:
                ok = hhcaptcha.solve_with_ai(tool, args.auto)
                print("✅ Капча решена автоматически" if ok else "❌ Автоматически решить не удалось")
                return None if ok else 1
            if args.prepare:
                path = hhcaptcha.prepare(tool, args.prepare)
                print(f"Картинка капчи: {path}")
                return None
            if args.refresh:
                info = hhcaptcha.load_pending(tool.config_path)
                if not info:
                    logger.error("Нет капчи, ожидающей ответа")
                    return 1
                print(f"Картинка капчи: {hhcaptcha.new_image(tool, info)}")
                return None
            if hhcaptcha.submit(tool, args.answer or ""):
                print("✅ Капча принята")
                return None
            print("❌ Неверно — новая картинка готова")
            return WRONG_ANSWER_EXIT_CODE
        finally:
            tool.save_cookies()

    def _check(self, tool: HHApplicantTool) -> int:
        providers = hhcaptcha.configured_providers(tool)
        print("Решатели:", ", ".join(providers) or "не настроены")
        capsolver_conf = tool.config.get("capsolver") or {}
        if capsolver_conf.get("api_key"):
            try:
                balance = capsolver.get_balance(capsolver_conf["api_key"])
                print(f"Баланс CapSolver: {balance}")
            except Exception as ex:
                logger.error("CapSolver недоступен: %s", ex)
                return 1
        return 0 if providers else 1

