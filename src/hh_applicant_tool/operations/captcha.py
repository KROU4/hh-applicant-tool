from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation
from ..utils import hhcaptcha

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

# Коды выхода для Telegram-бота
WRONG_ANSWER_EXIT_CODE = 4


class Namespace(BaseNamespace):
    prepare: str | None
    answer: str | None
    refresh: bool


class Operation(BaseOperation):
    """Капча hh.ru: картинка для человека и отправка его ответа."""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--prepare", metavar="URL", help="Ссылка на капчу из ошибки API")
        group.add_argument("--answer", help="Текст с картинки")
        group.add_argument(
            "--refresh", action="store_true", help="Другая картинка для той же капчи"
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> int | None:
        try:
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
