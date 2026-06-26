"""
Telegram notification module for FusionSolar fleet snapshots.

Uses the Telegram Bot API directly -- no third-party libraries needed,
just the bot token from BotFather and your chat ID.

Getting your chat ID (one-time setup):
    1. Start a conversation with your bot on Telegram (search its username
       and press Start, or send it any message).
    2. Open this URL in your browser (replace with your actual token):
       https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
    3. Look for "chat": {"id": <number>} in the response -- that number
       is your TELEGRAM_CHAT_ID. It can be negative for group chats.
    4. Add both values to your .env file.

You can also send to a group or channel by using the group's chat ID
(negative number) or a channel's username prefixed with @.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger("telegram_notify")

TELEGRAM_API_BASE = "https://api.telegram.org"

PLANT_HEALTH_EMOJI = {
    "Healthy": "🟢",
    "Faulty": "🔴",
    "Disconnected": "⚫",
    "Unknown": "⚪",
}

ALARM_SEVERITY_EMOJI = {
    1: "🚨",  # Critical
    2: "🔴",  # Major
    3: "🟡",  # Minor
    4: "🔵",  # Warning
}


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self._base = f"{TELEGRAM_API_BASE}/bot{bot_token}"

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Sends a text message to the configured chat. Returns True on
        success, False on failure (logs the error but does not raise)."""
        url = f"{self._base}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            body = resp.json()
            if body.get("ok"):
                logger.info("Telegram message sent (message_id=%s).", body["result"]["message_id"])
                return True
            else:
                logger.error("Telegram API error: %s", body.get("description"))
                return False
        except requests.RequestException as exc:
            logger.error("Telegram request failed: %s", exc)
            return False

    def test_connection(self) -> bool:
        """Calls getMe to verify the token is valid, then sends a test
        message to confirm the chat_id is reachable."""
        url = f"{self._base}/getMe"
        try:
            resp = requests.get(url, timeout=self.timeout)
            body = resp.json()
            if not body.get("ok"):
                logger.error("Bot token invalid: %s", body.get("description"))
                return False
            bot_name = body["result"].get("username")
            logger.info("Bot verified: @%s", bot_name)
        except requests.RequestException as exc:
            logger.error("Could not reach Telegram API: %s", exc)
            return False

        return self.send_message("✅ FusionSolar bot connected successfully.")


def build_notifier_from_env() -> TelegramNotifier:
    """Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    return TelegramNotifier(bot_token=token, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------

def format_daily_energy(
    snapshots: dict,
    plant_names: dict,
    timestamp: Optional[float] = None,
) -> str:
    """Formats a get_daily_energy() result into a Telegram HTML message.

    Intended to be sent once daily at 18:00 as a day-so-far energy summary.

    snapshots:   {stationCode: {pv_kwh, grid_export_kwh, grid_import_kwh, load_kwh}}
                 as returned by client.get_daily_energy()
    plant_names: {stationCode: plantName} -- from the plant list
    """
    ts = timestamp or time.time()
    date_str = time.strftime("%d %b %Y", time.localtime(ts))

    total_pv     = sum(s.get("pv_kwh",          0.0) for s in snapshots.values())
    total_export = sum(s.get("grid_export_kwh", 0.0) for s in snapshots.values())
    total_import = sum(s.get("grid_import_kwh", 0.0) for s in snapshots.values())
    total_load   = sum(s.get("load_kwh",        0.0) for s in snapshots.values())

    lines = [
        f"<b>⚡ Daily Energy Summary — {date_str}</b>",
        "",
        f"🌞 PV generated    :  <b>{total_pv:.1f} kWh</b>",
        f"↗ Grid exported   :  <b>{total_export:.1f} kWh</b>",
        f"↙ Grid imported   :  <b>{total_import:.1f} kWh</b>",
        f"🏠 Load consumed   :  <b>{total_load:.1f} kWh</b>",
        "",
        "─────────────────────",
    ]

    for code, data in snapshots.items():
        name   = plant_names.get(code, code)
        pv     = data.get("pv_kwh",          0.0)
        export = data.get("grid_export_kwh", 0.0)
        imp    = data.get("grid_import_kwh", 0.0)
        load   = data.get("load_kwh",        0.0)

        lines.append(
            f"<b>{name}</b>\n"
            f"  🌞 {pv:.1f}  |  ↗ {export:.1f}  |  ↙ {imp:.1f}  |  🏠 {load:.1f} kWh"
        )

    return "\n".join(lines)


def format_fleet_snapshot(
    snapshots: dict,
    plant_names: dict,
    timestamp: Optional[float] = None,
) -> str:
    """Formats a get_fleet_snapshot() result into a readable Telegram HTML
    message. Long enough to be useful, short enough to fit in one message.

    snapshots: {stationCode: snapshot_dict} from client.get_fleet_snapshot()
    plant_names: {stationCode: plantName} -- from the plant list
    """
    ts = timestamp or time.time()
    time_str = time.strftime("%d %b %Y  %H:%M", time.localtime(ts))

    lines = [f"<b>☀️ FusionSolar Status Report</b>", f"<i>{time_str}</i>", ""]

    total_pv = 0.0
    total_load = 0.0
    alarm_lines = []

    for code, snap in snapshots.items():
        name = plant_names.get(code, code)
        health = snap.get("plant_health", "Unknown")
        emoji = PLANT_HEALTH_EMOJI.get(health, "⚪")

        pv = snap.get("pv_power_kw")
        grid = snap.get("grid_power_kw")
        load = snap.get("load_estimate_kw")

        pv_str = f"{pv:.2f} kW" if pv is not None else "n/a"
        load_str = f"{load:.2f} kW" if load is not None else "n/a"

        # Grid direction label
        if grid is None:
            grid_str = "n/a"
        elif grid < 0:
            grid_str = f"{abs(grid):.2f} kW ↙ importing"
        else:
            grid_str = f"{grid:.2f} kW ↗ exporting"

        lines.append(f"{emoji} <b>{name}</b>")
        lines.append(f"  PV: {pv_str}  |  Grid: {grid_str}  |  Load: {load_str}")

        # Active alarms summary inline
        alarms = snap.get("active_alarms") or []
        if alarms:
            alarm_summary = ", ".join(
                f"{ALARM_SEVERITY_EMOJI.get(a.get('lev'), '⚠️')} {a.get('alarmName', 'Unknown alarm')}"
                for a in alarms[:3]
            )
            if len(alarms) > 3:
                alarm_summary += f" (+{len(alarms) - 3} more)"
            lines.append(f"  ⚠️ Alarms: {alarm_summary}")
            # Collect for the summary section
            for a in alarms:
                alarm_lines.append(
                    f"  {ALARM_SEVERITY_EMOJI.get(a.get('lev'), '⚠️')} "
                    f"{name} — {a.get('alarmName', '?')} "
                    f"[{a.get('devName', 'unknown device')}]"
                )

        if pv is not None:
            total_pv += pv
        if load is not None:
            total_load += load

        lines.append("")

    # Fleet totals
    lines.append("─────────────────────")
    lines.append(f"<b>Fleet total</b>")
    lines.append(f"  PV output : <b>{total_pv:.2f} kW</b>")
    lines.append(f"  Est. load : <b>{total_load:.2f} kW</b>")

    if alarm_lines:
        lines.append("")
        lines.append(f"<b>⚠️ Active alarms ({len(alarm_lines)})</b>")
        lines.extend(alarm_lines)

    return "\n".join(lines)
