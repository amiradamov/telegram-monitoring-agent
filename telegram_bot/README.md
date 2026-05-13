# Monitoring Telegram Bot

Telegram bot for the uploaded monitoring-agent JSONL logs.

Expected server log layout, based on the agent docs:

```text
/root/monitoring/ip_logs/
  <computer_name>/
    <computer_name>_ip_monitor.log
```

The bot reads those files directly. It does not need the Windows repository installed on the server and does not write to the log tree.

## What it shows

- Fleet online/offline status from the latest uploaded event timestamp
- Public IP per computer, whether it changed, and how long it has been seen
- CPU/RAM visual bars from `resource_check` events
- Threshold-time top processes
- Browser auto-close events
- Recent parsed events per computer
- Proactive Telegram alerts for offline and high-resource machines

## Telegram credentials needed

Create a bot with BotFather and provide:

- `TELEGRAM_BOT_TOKEN`

Recommended for privacy:

- `TELEGRAM_ALLOWED_CHAT_IDS`, comma-separated numeric chat IDs
- or `TELEGRAM_ADMIN_USER_IDS`, comma-separated numeric Telegram user IDs

If both access-control values are empty, any chat that can message the bot can read monitoring summaries.

## Install on the server

Copy this `telegram_bot` directory to:

```bash
/root/monitoring/telegram_bot
```

Create the environment file:

```bash
cd /root/monitoring/telegram_bot
cp config.example.env .env
nano .env
```

Optional preflight check:

```bash
chmod +x check_server.sh
./check_server.sh
```

Install as a service:

```bash
chmod +x install.sh
./install.sh
```

View logs:

```bash
journalctl -u monitoring-telegram-bot -f
```

## Commands

- `/status` - fleet overview
- `/pc NAME` - detailed computer card
- `/logs NAME [N]` - latest parsed events
- `/ips` - public IP map
- `/top` - CPU/RAM leaderboard
- `/alerts` - current alert list
- `/help` - command list

## Notes

Default offline threshold is 20 minutes because the Windows agent checks public IP every 10 minutes by default and uploads on those checks. Tune `OFFLINE_AFTER_SECONDS` if you change the agent interval.
