#!/usr/bin/env bash
# Установка/обновление бота на сервере без Docker и без Playwright.
# Ожидает исходники в /opt/hh-applicant-tool/app (например, распакованный git archive).
#
#   TELEGRAM_BOT_TOKEN=... OPENROUTER_API_KEY=... bash install.sh
#
# Токены нужны только при первой установке: они записываются в config.json.
set -euo pipefail

ROOT=/opt/hh-applicant-tool
APP=$ROOT/app
CONFIG=$ROOT/config

id hhbot >/dev/null 2>&1 || useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin hhbot

if ! python3 -c 'import ensurepip' 2>/dev/null; then
  apt-get update -qq && apt-get install -y -qq python3-venv
fi

[ -x "$ROOT/venv/bin/python" ] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install -q --upgrade pip
"$ROOT/venv/bin/pip" install -q --no-cache-dir "$APP"

mkdir -p "$CONFIG"
"$ROOT/venv/bin/python" - "$CONFIG/config.json" <<'EOF'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text()) if path.exists() else {}
if os.environ.get("TELEGRAM_BOT_TOKEN"):
    config.setdefault("telegram_bot", {})["token"] = os.environ["TELEGRAM_BOT_TOKEN"]
if os.environ.get("OPENROUTER_API_KEY"):
    config.setdefault("openrouter", {})["api_key"] = os.environ["OPENROUTER_API_KEY"]
path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
EOF

# Картинка приветствия по /start (свою можно положить поверх)
[ -f "$CONFIG/welcome.jpg" ] || cp "$APP/deploy/telegram/welcome.jpg" "$CONFIG/welcome.jpg"

chown -R hhbot:hhbot "$CONFIG"
chmod 700 "$CONFIG"

install -m 644 "$APP/deploy/hh-bot.service" /etc/systemd/system/hh-bot.service
systemctl daemon-reload
systemctl enable hh-bot.service >/dev/null
systemctl restart hh-bot.service
sleep 3
systemctl --no-pager --lines=15 status hh-bot.service
