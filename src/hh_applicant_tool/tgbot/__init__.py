from .bot import HHBot
from .telegram import TelegramAPI


def run_bot(token: str, config_path, owner_id: int | None = None) -> None:
    bot = HHBot(TelegramAPI(token), config_path, owner_id=owner_id)
    try:
        bot.run()
    finally:
        bot.stop()
