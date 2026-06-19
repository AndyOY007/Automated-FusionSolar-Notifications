#!/usr/bin/env python3
"""
On-demand real-time status report: PV production power, grid power,
an estimated load figure, per-device online/offline state, and active
alarms -- for one plant or all plants your API account can see.

This is deliberately a manual, run-it-yourself script for now (no
scheduling/automation). That comes later.

Under the hood this makes one batched pass across all requested plants
(get_fleet_snapshot) rather than looping per plant, because FusionSolar's
flow control is enforced per ACCOUNT -- e.g. real-time plant data allows
only Roundup(plant_count/100) calls every 5 minutes, which for most
accounts is exactly 1 call every 5 minutes for the WHOLE fleet, not one
per plant. Looping per plant burns that budget on the first plant and
407s on the rest.

Usage:
    python realtime_status.py                  # all plants
    python realtime_status.py --plant NE=49730270
    python realtime_status.py --plant NE=49730270 --alarm-hours 48
    python realtime_status.py --json            # machine-readable output

Even batched, avoid running this repeatedly within the same 5-minute
window -- the underlying limits are tight enough that a second run too
soon will still 407.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from client import FusionSolarNBIClient, FusionSolarAPIError, build_client_from_env
from telegram_notify import TelegramNotifier, build_notifier_from_env, format_fleet_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("realtime_status")


def load_dotenv_if_present():
    from pathlib import Path
    import os

    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def fmt_kw(value):
    return "n/a" if value is None else f"{value:.2f} kW"


def print_snapshot(plant_name: str, snapshot: dict) -> None:
    print(f"\n=== {plant_name}  ({snapshot['stationCode']}) ===")
    print(f"  Plant health      : {snapshot['plant_health']}")
    print(f"  PV production     : {fmt_kw(snapshot['pv_power_kw'])}")
    print(f"  Grid power        : {fmt_kw(snapshot['grid_power_kw'])}  (- importing from grid / + exporting to grid)")
    if snapshot["battery_power_kw"] is not None:
        print(f"  Battery power     : {fmt_kw(snapshot['battery_power_kw'])}  (+charging/-discharging, unverified sign)")
    print(f"  Load (estimated)  : {fmt_kw(snapshot['load_estimate_kw'])}  <- derived, see caveat in README")

    print("  Devices:")
    for dev in snapshot["devices"]:
        if dev.get("status") == "n/a":
            print(f"    - {dev['devName']:25} [{dev['label']}] not monitored via real-time API")
            continue
        online = dev.get("online")
        online_str = "ONLINE" if online else ("OFFLINE" if online is False else "unknown")
        extra = f", {dev['inverter_state']}" if dev.get("inverter_state") else ""
        power_str = fmt_kw(dev.get("power_kw"))
        print(f"    - {dev['devName']:25} [{dev['label']:20}] {online_str:8} power={power_str}{extra}")

    alarms = snapshot["active_alarms"]
    if not alarms:
        print("  Active alarms     : none")
    else:
        print(f"  Active alarms     : {len(alarms)}")
        for a in alarms:
            sev = a.get("lev")
            sev_label = {1: "CRITICAL", 2: "MAJOR", 3: "MINOR", 4: "WARNING"}.get(sev, str(sev))
            raised = datetime.fromtimestamp(a["raiseTime"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"    - [{sev_label}] {a['alarmName']} on {a.get('devName')} (since {raised})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plant",
        action="append",
        dest="plants",
        help="Plant code (plantCode, e.g. NE=49730270). Repeat flag for multiple. Default: all plants.",
    )
    parser.add_argument(
        "--alarm-hours",
        type=int,
        default=24,
        help="Lookback window in hours for active alarms (default: 24).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of the human-readable summary.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send a Telegram message with the snapshot summary. Requires "
             "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Verify bot token and chat ID are working (sends a test message), then exit.",
    )
    args = parser.parse_args()

    load_dotenv_if_present()

    # --test-telegram: verify credentials and exit without touching FusionSolar
    if args.test_telegram:
        try:
            notifier = build_notifier_from_env()
        except KeyError as exc:
            logger.error(
                "Missing %s in .env -- add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", exc
            )
            return 1
        ok = notifier.test_connection()
        return 0 if ok else 1

    try:
        client = build_client_from_env()
    except KeyError as exc:
        logger.error("Missing environment variable %s. Copy .env.example to .env and fill it in.", exc)
        return 1

    try:
        client.login()
    except FusionSolarAPIError as exc:
        logger.error("Login failed: %s", exc)
        return 1

    try:
        all_plants = client.get_all_plants()
    except FusionSolarAPIError as exc:
        logger.error("Plant list call failed: %s", exc)
        return 1

    plant_by_code = {p["plantCode"]: p for p in all_plants}

    if args.plants:
        targets = []
        for code in args.plants:
            if code not in plant_by_code:
                logger.error("Plant code %s not found in your account's plant list.", code)
                return 1
            targets.append(plant_by_code[code])
    else:
        targets = all_plants

    target_codes = [p["plantCode"] for p in targets]

    logger.info("Building fleet snapshot for %d plant(s) in one batched pass...", len(target_codes))
    try:
        snapshots = client.get_fleet_snapshot(target_codes, alarm_lookback_hours=args.alarm_hours)
    except FusionSolarAPIError as exc:
        logger.error("Fleet snapshot failed: %s", exc)
        return 1

    results = {
        code: {"plantName": plant_by_code[code]["plantName"], "snapshot": snapshot}
        for code, snapshot in snapshots.items()
    }

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for code, entry in results.items():
            print_snapshot(entry["plantName"], entry["snapshot"])

    if args.notify:
        try:
            notifier = build_notifier_from_env()
        except KeyError as exc:
            logger.error(
                "Cannot send Telegram notification: missing %s in .env. "
                "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", exc
            )
            return 1

        plant_names = {code: entry["plantName"] for code, entry in results.items()}
        flat_snapshots = {code: entry["snapshot"] for code, entry in results.items()}
        message = format_fleet_snapshot(flat_snapshots, plant_names)

        logger.info("Sending Telegram notification...")
        ok = notifier.send_message(message)
        if not ok:
            logger.error("Telegram notification failed.")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
