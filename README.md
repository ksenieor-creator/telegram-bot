# telegram-bot (Render)

## Render settings
- Service type: **Background Worker** (or Worker)
- Build command: `pip install -r requirements.txt`
- Start command: `bash start.sh`
- Environment variable:
  - `BOT_TOKEN` = your Telegram bot token (set in Render Dashboard, not in code)

## Why this pack
Pins `python-telegram-bot` to `13.15` to match legacy `Updater`-based code.
