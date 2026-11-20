from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)

# Маркер для Telegram-бота: токен жив, а веб-сессия hh.ru умерла
WEB_SESSION_EXPIRED_MARKER = "🔑 Сессия сайта hh.ru истекла"
WEB_SESSION_URL = "https://hh.ru/applicant/resumes"


class Namespace(BaseNamespace):
    pass


class Operation(BaseOperation):
    """Обновляет access_token и refresh_token в случае необходимости."""

    __aliases__ = ["refresh"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, tool: HHApplicantTool, args: BaseNamespace) -> None:
        if tool.api_client.is_access_expired:
            tool.api_client.refresh_access_token()
            if not tool.save_token():
                print("⚠️ Токен не был обновлен!")
                return 1
            print("✅ Токен успешно обновлен.")
            code = None
        else:
            # logger.debug("Токен валиден, игнорируем обновление.")
            print("ℹ️ Токен не истек, обновление не требуется.")
            code = 2
        self._check_web_session(tool)
        return code

    @staticmethod
    def _check_web_session(tool: HHApplicantTool) -> None:
        """Проверяет веб-сессию (по ней решаются тесты) и заодно её продлевает.

        Бот по маркеру сразу предложит войти по коду, не дожидаясь, пока
        прогон потеряет все вакансии с тестом.
        """
        try:
            tool.get_redirect_config(WEB_SESSION_URL)
        except Exception as ex:
            if "Авторизация истекла" in str(ex):
                print(WEB_SESSION_EXPIRED_MARKER)
