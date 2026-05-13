#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/root/monitoring/telegram_bot"
SERVICE_NAME="monitoring-telegram-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "$(id -u)" != "0" ]]; then
  echo "Run as root."
  exit 1
fi

if [[ ! -f "${APP_DIR}/bot.py" ]]; then
  echo "Expected bot.py at ${APP_DIR}/bot.py"
  exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/config.example.env" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  echo "Created ${APP_DIR}/.env. Edit TELEGRAM_BOT_TOKEN before starting."
  exit 0
fi

if ! grep -q "^TELEGRAM_BOT_TOKEN=.*:.*" "${APP_DIR}/.env"; then
  echo "Set TELEGRAM_BOT_TOKEN in ${APP_DIR}/.env before starting."
  exit 1
fi

cp "${APP_DIR}/systemd/${SERVICE_NAME}.service" "${SERVICE_FILE}"
chmod 644 "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager
