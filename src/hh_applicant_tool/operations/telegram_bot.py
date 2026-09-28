from __future__ import annotations

import argparse
import logging
from os import getenv
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    token: str | None
    owner_id: int | None


class Operation(BaseOperation):
    """Telegram-бот с панелью управления на кнопках: отклики, расписание, автоответчик, логи."""

    __aliases__ = ["bot"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--token",
            help="Токен бота. Также берётся из telegram_bot.token в конфиге или TELEGRAM_BOT_TOKEN",
        )
        parser.add_argument(
            "--owner-id",
            type=int,
            help="Telegram ID владельца. Если не задан, владельцем станет первый, кто напишет /start",
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> int | None:
        from ..tgbot import run_bot

        bot_config = tool.config.get("telegram_bot") or {}
        token = (
            args.token or bot_config.get("token") or getenv("TELEGRAM_BOT_TOKEN")
        )
        if not token:
            logger.error(
                "Не задан токен бота: --token, telegram_bot.token в конфиге "
                "или переменная TELEGRAM_BOT_TOKEN"
            )
            return 1
        owner_id = args.owner_id or bot_config.get("owner_id")
        run_bot(token, tool.config_path, owner_id)
        return None
