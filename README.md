# telegram-bot

Telegram bot (python-telegram-bot) for calculating visits + admin panel.

## Deploy (Railway)
1. Add variables in Railway:
   - `BOT_TOKEN` = your Telegram bot token
   - `PUBLIC_URL` = your Railway public URL, like `https://<your-service>.up.railway.app`
   - (optional) `WEBHOOK_SECRET` = any random string (extra protection)

2. Deploy. The bot will start in WEBHOOK mode automatically.

## Local run
Create `.env` with BOT_TOKEN and run:
```bash
pip install -r requirements.txt
python main.py
```
It will use polling locally.
