import signal

from .bot import HHBot
from .telegram import TelegramAPI


def run_bot(token: str, config_path, owner_id: int | None = None) -> None:
    bot = HHBot(TelegramAPI(token), config_path, owner_id=owner_id)

    def _exit(signum, frame):
        raise SystemExit(0)

    # systemctl stop/restart шлёт SIGTERM: без обработчика бот умер бы сразу,
    # не дав операциям сохранить обновлённый токен. Ctrl+C — туда же.
    previous = {
        sig: signal.signal(sig, _exit) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        bot.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        bot.stop()
