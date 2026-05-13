# telegram-monitoring-agent

Telegram dashboard bot for the Windows monitoring-agent server logs.

The bot reads JSON-line log files uploaded by monitoring agents under:

```text
/root/monitoring/ip_logs/<computer_name>/<computer_name>_ip_monitor.log
```

See [telegram_bot/README.md](telegram_bot/README.md) for setup and command details.
