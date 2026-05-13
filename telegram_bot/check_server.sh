#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/monitoring/telegram_bot}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"

echo "Monitoring Telegram Bot preflight"
echo "App dir: ${APP_DIR}"
echo

if [[ "$(id -u)" != "0" ]]; then
  echo "WARN: not running as root. Service installation needs root."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 is not installed."
  echo "Install it with: apt update && apt install -y python3"
  exit 1
fi

python3 - <<'PY'
import json
import pathlib
import urllib.request
print("OK: python3 standard library imports work")
PY

if ! command -v systemctl >/dev/null 2>&1; then
  echo "FAIL: systemctl is not available. This install script expects systemd."
  exit 1
fi
echo "OK: systemd/systemctl found"

if [[ ! -f "${APP_DIR}/bot.py" ]]; then
  echo "FAIL: ${APP_DIR}/bot.py not found."
  echo "Pull/copy the repo so telegram_bot lives at ${APP_DIR}."
  exit 1
fi
echo "OK: bot.py found"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "WARN: ${ENV_FILE} not found."
  echo "Create it with: cp ${APP_DIR}/config.example.env ${ENV_FILE}"
else
  if grep -q '^TELEGRAM_BOT_TOKEN=.*:.*' "${ENV_FILE}"; then
    echo "OK: TELEGRAM_BOT_TOKEN appears to be set"
  else
    echo "FAIL: TELEGRAM_BOT_TOKEN is missing or still placeholder in ${ENV_FILE}"
    exit 1
  fi
fi

LOG_ROOT="$(grep -E '^MONITOR_LOG_ROOT=' "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
LOG_ROOT="${LOG_ROOT:-/root/monitoring/ip_logs}"

if [[ -d "${LOG_ROOT}" ]]; then
  echo "OK: log root exists: ${LOG_ROOT}"
  FOUND_LOGS="$(find "${LOG_ROOT}" -mindepth 2 -maxdepth 2 -name '*_ip_monitor.log' | head -n 1 || true)"
  if [[ -n "${FOUND_LOGS}" ]]; then
    echo "OK: monitoring log files found"
  else
    echo "WARN: no *_ip_monitor.log files found yet under ${LOG_ROOT}"
  fi
else
  echo "WARN: log root does not exist yet: ${LOG_ROOT}"
fi

if python3 - <<'PY'
import urllib.request
urllib.request.urlopen("https://api.telegram.org", timeout=10).read(64)
PY
then
  echo "OK: Telegram API is reachable"
else
  echo "FAIL: Telegram API is not reachable from this server."
  exit 1
fi

echo
echo "Preflight complete."
