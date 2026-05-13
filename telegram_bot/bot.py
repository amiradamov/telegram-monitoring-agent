#!/usr/bin/env python3
"""Telegram dashboard bot for monitoring-agent server logs.

The bot reads JSON-line logs uploaded by the Windows monitoring agents:

    /root/monitoring/ip_logs/<computer>/<computer>_ip_monitor.log

It intentionally uses only Python's standard library so it can run on a small
Linux server without a package install step.
"""

from __future__ import annotations

import html
import json
import logging
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("monitoring_telegram_bot")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    log_root: Path = Path("/root/monitoring/ip_logs")
    offline_after_seconds: int = 20 * 60
    alert_check_seconds: int = 60
    stale_ip_after_seconds: int = 24 * 60 * 60
    max_log_lines_per_file: int = 4000
    request_timeout_seconds: int = 20
    allowed_chat_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()


@dataclass
class MachineState:
    name: str
    log_file: Path
    last_event: dict[str, Any] | None = None
    last_public_ip: dict[str, Any] | None = None
    last_resource: dict[str, Any] | None = None
    last_agent_started: dict[str, Any] | None = None
    last_browser_close: dict[str, Any] | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    malformed_lines: int = 0

    @property
    def last_seen(self) -> datetime | None:
        if self.last_event:
            return parse_agent_timestamp(self.last_event.get("timestamp"))
        return None


class TelegramApi:
    def __init__(self, token: str, timeout_seconds: int) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout_seconds = timeout_seconds

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {}
        if payload is not None:
            data = urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(f"{self.base_url}/{method}", data=data, headers=headers)
        with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        result = json.loads(body)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error for {method}: {result}")
        return result

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": 45,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout_seconds=55).get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )


def load_settings() -> Settings:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    return Settings(
        bot_token=token,
        log_root=Path(os.environ.get("MONITOR_LOG_ROOT", "/root/monitoring/ip_logs")),
        offline_after_seconds=get_int_env("OFFLINE_AFTER_SECONDS", 20 * 60),
        alert_check_seconds=get_int_env("ALERT_CHECK_SECONDS", 60),
        stale_ip_after_seconds=get_int_env("STALE_IP_AFTER_SECONDS", 24 * 60 * 60),
        max_log_lines_per_file=get_int_env("MAX_LOG_LINES_PER_FILE", 4000),
        request_timeout_seconds=get_int_env("TELEGRAM_REQUEST_TIMEOUT_SECONDS", 20),
        allowed_chat_ids=parse_id_set(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        admin_user_ids=parse_id_set(os.environ.get("TELEGRAM_ADMIN_USER_IDS", "")),
    )


def get_int_env(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def parse_id_set(value: str) -> frozenset[int]:
    ids: set[int] = set()
    for item in value.replace(" ", "").split(","):
        if item:
            ids.add(int(item))
    return frozenset(ids)


def parse_agent_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def age_seconds(then: datetime | None) -> int | None:
    if then is None:
        return None
    return max(0, int((now_utc() - then.astimezone(timezone.utc)).total_seconds()))


def human_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def pct_bar(value: Any, width: int = 10) -> str:
    percent_value = to_float(value)
    if percent_value is None:
        return "[??????????]"
    percent = max(0.0, min(100.0, percent_value))
    full = round((percent / 100.0) * width)
    return "[" + ("#" * full) + ("-" * (width - full)) + f"] {percent:.1f}%"


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def read_tail_lines(path: Path, max_lines: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        LOG.warning("failed reading %s: %s", path, exc)
        return []
    return lines[-max_lines:]


def load_machine(log_file: Path, max_lines: int) -> MachineState:
    name = log_file.stem.removesuffix("_ip_monitor")
    state = MachineState(name=name, log_file=log_file)

    for line in read_tail_lines(log_file, max_lines):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            state.malformed_lines += 1
            continue

        if not isinstance(event, dict):
            state.malformed_lines += 1
            continue

        state.last_event = event
        state.recent_events.append(event)
        state.recent_events = state.recent_events[-25:]

        event_type = event.get("event_type")
        if event_type == "public_ip_check":
            state.last_public_ip = event
        elif event_type == "resource_check":
            state.last_resource = event
            if event.get("browser_closed"):
                state.last_browser_close = event
        elif event_type == "agent_started":
            state.last_agent_started = event

    return state


def load_all_machines(settings: Settings) -> list[MachineState]:
    root = settings.log_root
    if not root.exists():
        return []

    log_files = sorted(root.glob("*/*_ip_monitor.log"))
    machines = [load_machine(path, settings.max_log_lines_per_file) for path in log_files]
    return sorted(machines, key=lambda item: item.name.lower())


def find_machine(settings: Settings, name: str) -> MachineState | None:
    needle = name.lower()
    for machine in load_all_machines(settings):
        if machine.name.lower() == needle:
            return machine
    return None


def status_word(machine: MachineState, settings: Settings) -> str:
    age = age_seconds(machine.last_seen)
    if age is None:
        return "NO DATA"
    if age <= settings.offline_after_seconds:
        return "ONLINE"
    return "OFFLINE"


def summarize_fleet(settings: Settings) -> str:
    machines = load_all_machines(settings)
    if not machines:
        return (
            "<b>Monitoring Bot</b>\n"
            f"No log files found under <code>{escape(settings.log_root)}</code>."
        )

    online = [item for item in machines if status_word(item, settings) == "ONLINE"]
    offline = [item for item in machines if status_word(item, settings) == "OFFLINE"]
    high = [
        item
        for item in machines
        if item.last_resource
        and (
            (to_float(item.last_resource.get("cpu_usage_percent")) or 0) >= 90
            or (to_float(item.last_resource.get("ram_usage_percent")) or 0) >= 90
        )
    ]

    lines = [
        "<b>Fleet Status</b>",
        f"Machines: <b>{len(machines)}</b> | Online: <b>{len(online)}</b> | Offline: <b>{len(offline)}</b>",
        f"High resource now: <b>{len(high)}</b>",
        "",
    ]

    for machine in machines:
        lines.extend(machine_card(machine, settings, compact=True))
        lines.append("")

    lines.append("Commands: /pc NAME, /logs NAME, /ips, /top, /alerts")
    return "\n".join(lines).strip()


def machine_card(machine: MachineState, settings: Settings, compact: bool) -> list[str]:
    last_resource = machine.last_resource or {}
    last_ip = machine.last_public_ip or {}
    age = age_seconds(machine.last_seen)
    state = status_word(machine, settings)
    cpu = last_resource.get("cpu_usage_percent")
    ram = last_resource.get("ram_usage_percent")
    ip_value = last_ip.get("public_ip", "unknown")
    changed = last_ip.get("changed")
    uptime = (machine.last_agent_started or {}).get("system_uptime_human", "unknown")

    header = f"<b>{escape(machine.name)}</b>  <code>{state}</code>  seen {human_duration(age)} ago"
    lines = [
        header,
        f"IP: <code>{escape(ip_value)}</code> | changed: <b>{escape(changed)}</b>",
        f"CPU {pct_bar(cpu)}",
        f"RAM {pct_bar(ram)}",
    ]

    if not compact:
        lines.append(f"Uptime at last start: {escape(uptime)}")
        lines.append(f"Log: <code>{escape(machine.log_file)}</code>")
        if machine.malformed_lines:
            lines.append(f"Malformed JSON lines in parsed tail: <b>{machine.malformed_lines}</b>")
        if machine.last_browser_close:
            close_age = human_duration(age_seconds(parse_agent_timestamp(machine.last_browser_close.get("timestamp"))))
            closed = ", ".join(machine.last_browser_close.get("closed_process_names") or [])
            lines.append(f"Last browser close: {escape(close_age)} ago ({escape(closed or 'process unknown')})")

        top_processes = last_resource.get("top_processes") or []
        if top_processes:
            lines.append("")
            lines.append("<b>Threshold-time top processes</b>")
            for proc in top_processes[:5]:
                lines.append(
                    f"- {escape(proc.get('name'))} pid {escape(proc.get('pid'))}: "
                    f"{escape(proc.get('memory_mb'))} MB, CPU {escape(proc.get('cpu_percent'))}%"
                )

    return lines


def format_machine(settings: Settings, name: str) -> str:
    machine = find_machine(settings, name)
    if not machine:
        return f"Machine not found: <code>{escape(name)}</code>"
    return "\n".join(machine_card(machine, settings, compact=False))


def format_logs(settings: Settings, name: str, count: int) -> str:
    machine = find_machine(settings, name)
    if not machine:
        return f"Machine not found: <code>{escape(name)}</code>"

    events = machine.recent_events[-count:]
    if not events:
        return f"No parsed events for <b>{escape(machine.name)}</b>."

    lines = [f"<b>Recent events for {escape(machine.name)}</b>"]
    for event in events:
        event_type = event.get("event_type", "unknown")
        timestamp = event.get("timestamp", "unknown")
        summary = event_summary(event)
        lines.append(f"<code>{escape(timestamp)}</code> <b>{escape(event_type)}</b> {summary}")
    return "\n".join(lines)


def event_summary(event: dict[str, Any]) -> str:
    event_type = event.get("event_type")
    if event_type == "public_ip_check":
        changed = "changed" if event.get("changed") else "stable"
        return f"IP <code>{escape(event.get('public_ip'))}</code> ({changed})"
    if event_type == "resource_check":
        return (
            f"CPU {escape(event.get('cpu_usage_percent'))}% "
            f"RAM {escape(event.get('ram_usage_percent'))}% "
            f"threshold={escape(event.get('threshold_reached'))}"
        )
    if event_type == "agent_started":
        return f"uptime {escape(event.get('system_uptime_human'))}"
    if event_type == "server_sync_failed":
        return f"error {escape(event.get('error'))}"
    if event_type == "server_sync_success":
        return f"uploaded {escape(event.get('file_size_bytes'))} bytes"
    return ""


def format_ips(settings: Settings) -> str:
    machines = load_all_machines(settings)
    if not machines:
        return "No machines found."
    lines = ["<b>Public IP Map</b>"]
    for machine in machines:
        event = machine.last_public_ip or {}
        ip_value = event.get("public_ip", "unknown")
        seen_at = event.get("public_ip_seen_at", "unknown")
        age = event.get("public_ip_age_human", "unknown")
        changed = event.get("changed")
        lines.append(
            f"<b>{escape(machine.name)}</b>: <code>{escape(ip_value)}</code> "
            f"| age {escape(age)} | changed {escape(changed)} | since <code>{escape(seen_at)}</code>"
        )
    return "\n".join(lines)


def format_top(settings: Settings) -> str:
    machines = load_all_machines(settings)
    rows: list[tuple[float, str]] = []
    for machine in machines:
        event = machine.last_resource or {}
        cpu = to_float(event.get("cpu_usage_percent")) or 0
        ram = to_float(event.get("ram_usage_percent")) or 0
        score = max(cpu, ram)
        rows.append((score, f"<b>{escape(machine.name)}</b> CPU {pct_bar(cpu)} RAM {pct_bar(ram)}"))

    if not rows:
        return "No resource data found."
    rows.sort(reverse=True, key=lambda item: item[0])
    return "<b>Resource Leaderboard</b>\n" + "\n".join(row for _, row in rows[:15])


def format_alerts(settings: Settings) -> str:
    machines = load_all_machines(settings)
    lines = ["<b>Current Alerts</b>"]
    count = 0
    for machine in machines:
        state = status_word(machine, settings)
        resource = machine.last_resource or {}
        public_ip = machine.last_public_ip or {}
        cpu = to_float(resource.get("cpu_usage_percent")) or 0
        ram = to_float(resource.get("ram_usage_percent")) or 0
        ip_age = to_int(public_ip.get("public_ip_age_seconds"))

        if state == "OFFLINE":
            count += 1
            lines.append(f"- <b>{escape(machine.name)}</b> offline for {human_duration(age_seconds(machine.last_seen))}")
        if cpu >= 90 or ram >= 90:
            count += 1
            lines.append(f"- <b>{escape(machine.name)}</b> high resources: CPU {cpu:.1f}%, RAM {ram:.1f}%")
        if ip_age is not None and ip_age > settings.stale_ip_after_seconds:
            count += 1
            lines.append(f"- <b>{escape(machine.name)}</b> public IP age {human_duration(ip_age)}")

    if count == 0:
        lines.append("No active alerts.")
    return "\n".join(lines)


def help_text(settings: Settings) -> str:
    return "\n".join(
        [
            "<b>Monitoring Bot</b>",
            f"Reading logs from <code>{escape(settings.log_root)}</code>",
            "",
            "/status - fleet overview",
            "/pc NAME - detailed computer card",
            "/logs NAME [N] - latest parsed events",
            "/ips - public IP map",
            "/top - CPU/RAM leaderboard",
            "/alerts - current alert list",
            "/help - commands",
        ]
    )


def is_allowed(settings: Settings, message: dict[str, Any]) -> bool:
    chat_id = int(message["chat"]["id"])
    user_id = int(message.get("from", {}).get("id", 0))
    if not settings.allowed_chat_ids and not settings.admin_user_ids:
        return True
    return chat_id in settings.allowed_chat_ids or user_id in settings.admin_user_ids


def handle_command(settings: Settings, message: dict[str, Any]) -> str | None:
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return None

    command, _, rest = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    args = rest.split()

    if command in {"/start", "/help"}:
        return help_text(settings)
    if command in {"/status", "/refresh"}:
        return summarize_fleet(settings)
    if command == "/pc":
        if not args:
            return "Usage: /pc COMPUTER_NAME"
        return format_machine(settings, args[0])
    if command == "/logs":
        if not args:
            return "Usage: /logs COMPUTER_NAME [COUNT]"
        count = 10
        if len(args) > 1:
            requested_count = to_int(args[1])
            if requested_count is None:
                return "Usage: /logs COMPUTER_NAME [COUNT]"
            count = max(1, min(25, requested_count))
        return format_logs(settings, args[0], count)
    if command == "/ips":
        return format_ips(settings)
    if command == "/top":
        return format_top(settings)
    if command == "/alerts":
        return format_alerts(settings)
    return "Unknown command. Try /help."


def alert_key(machine: MachineState, settings: Settings) -> tuple[str, str] | None:
    if status_word(machine, settings) == "OFFLINE":
        return (machine.name, "offline")
    resource = machine.last_resource or {}
    cpu = to_float(resource.get("cpu_usage_percent")) or 0
    ram = to_float(resource.get("ram_usage_percent")) or 0
    if cpu >= 90 or ram >= 90:
        return (machine.name, "high_resource")
    return None


def alert_text(machine: MachineState, settings: Settings) -> str:
    resource = machine.last_resource or {}
    return "\n".join(
        [
            "<b>Monitoring Alert</b>",
            *machine_card(machine, settings, compact=True),
            f"Last event: <code>{escape((machine.last_event or {}).get('event_type'))}</code>",
            f"CPU: {escape(resource.get('cpu_usage_percent'))}% | RAM: {escape(resource.get('ram_usage_percent'))}%",
        ]
    )


def send_alerts(
    api: TelegramApi,
    settings: Settings,
    known_alerts: set[tuple[str, str]],
    chat_ids: set[int],
) -> set[tuple[str, str]]:
    current_alerts: set[tuple[str, str]] = set()
    for machine in load_all_machines(settings):
        key = alert_key(machine, settings)
        if key is None:
            continue
        current_alerts.add(key)
        if key in known_alerts:
            continue
        for chat_id in chat_ids:
            api.send_message(chat_id, alert_text(machine, settings))
    return current_alerts


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = load_settings()
    api = TelegramApi(settings.bot_token, settings.request_timeout_seconds)

    stop = False

    def handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    offset: int | None = None
    known_chats: set[int] = set(settings.allowed_chat_ids)
    known_alerts: set[tuple[str, str]] = set()
    next_alert_at = 0.0

    LOG.info("bot started; reading logs from %s", settings.log_root)
    while not stop:
        try:
            for update in api.get_updates(offset):
                offset = int(update["update_id"]) + 1
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                chat_id = int(message["chat"]["id"])
                if not is_allowed(settings, message):
                    api.send_message(chat_id, "This chat is not allowed to use the monitoring bot.")
                    continue

                known_chats.add(chat_id)
                response = handle_command(settings, message)
                if response:
                    api.send_message(chat_id, response)

            if time.monotonic() >= next_alert_at:
                if known_chats:
                    known_alerts = send_alerts(api, settings, known_alerts, known_chats)
                next_alert_at = time.monotonic() + settings.alert_check_seconds

        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
            LOG.warning("polling loop error: %s", exc)
            time.sleep(5)
        except Exception:
            LOG.error("unexpected error:\n%s", traceback.format_exc())
            time.sleep(5)

    LOG.info("bot stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
