#!/usr/bin/env python3
"""
Scheduled FusionSolar fleet reporting -> Telegram.

Runs continuously in the foreground and fires off a fleet snapshot +
Telegram notification on a schedule:
    - Weekdays (Mon-Fri): every 30 minutes
    - Weekends (Sat-Sun): every 60 minutes

All of that is just the DEFAULT -- edit the SCHEDULE_CONFIG block below
to change it. No need to touch any other code in this file.

Usage:
    python scheduler.py                 # uses .env, runs forever (Ctrl+C to stop)
    python scheduler.py --run-once      # fires the job immediately once, then exits
                                         # (useful for testing the schedule's job logic
                                         # without waiting for the next scheduled time)

This is a long-running foreground process. To keep it running after you
close the terminal, options include:
    - tmux/screen: start a session, run this inside it, detach.
    - nohup python scheduler.py > scheduler.log 2>&1 &
    - A proper process manager (launchd on macOS, systemd on Linux, or
      pm2/supervisor) if you want it to survive reboots -- ask me if you
      want help setting one of those up later.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from client import FusionSolarNBIClient, FusionSolarAPIError, build_client_from_env
from telegram_notify import build_notifier_from_env, format_fleet_snapshot, format_daily_energy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scheduler")
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # keep its internal chatter quiet


# =====================================================================
# SCHEDULE CONFIG -- edit this block whenever you want to change timing.
# Nothing below this block needs to change for a timing adjustment.
#
# Each entry is a separate schedule "rule". A rule fires the same report
# job, just on its own cadence. day_of_week uses APScheduler's cron
# syntax: mon,tue,wed,thu,fri,sat,sun (or ranges like "mon-fri").
#
# minute: "*/N" means "every N minutes". hour: "*" means every hour;
# restrict it (e.g. "5-19") if you only want reports during daylight/
# working hours.
# =====================================================================
SCHEDULE_CONFIG = [
    {
        "name": "weekday_every_30min",
        "day_of_week": "mon-fri",
        "hour": "7-17",        # all 24 hours -- narrow this if you only want e.g. "5-19"
        "minute": "*/30",
    },
    {
        "name": "weekend_hourly",
        "day_of_week": "sat,sun",
        "hour": "8-16",
        "minute": "0",       # once per hour, on the hour
    },
]

# Daily energy summary fires at 18:00 UTC every day (Ghana is UTC+0, so
# this is 6:00 PM local time). Kept separate from SCHEDULE_CONFIG above
# because it calls a different job function and a different API endpoint.
DAILY_ENERGY_HOUR = 18
DAILY_ENERGY_MINUTE = 0

# Lookback window for active alarms included in each report
ALARM_LOOKBACK_HOURS = 24
# =====================================================================
# END SCHEDULE CONFIG
# =====================================================================


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


def run_report_job() -> None:
    """The actual job: fetch the fleet snapshot, send it to Telegram.
    Any failure here is logged but does not crash the scheduler -- the
    next scheduled run will simply try again."""
    started_at = datetime.now(timezone.utc)
    logger.info("Running scheduled fleet report...")

    try:
        client = build_client_from_env()
        notifier = build_notifier_from_env()
    except KeyError as exc:
        logger.error("Missing environment variable %s -- check your .env file.", exc)
        return

    try:
        client.login()
        all_plants = client.get_all_plants()
        station_codes = [p["plantCode"] for p in all_plants]
        plant_names = {p["plantCode"]: p["plantName"] for p in all_plants}

        snapshots = client.get_fleet_snapshot(station_codes, alarm_lookback_hours=ALARM_LOOKBACK_HOURS)
    except FusionSolarAPIError as exc:
        logger.error("Fleet snapshot failed: %s", exc)
        return

    message = format_fleet_snapshot(snapshots, plant_names)
    ok = notifier.send_message(message)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if ok:
        logger.info("Report sent successfully (%.1fs).", elapsed)
    else:
        logger.error("Report generated but Telegram send failed (%.1fs).", elapsed)


def run_daily_energy_job() -> None:
    """Fires once daily at 18:00 -- sends today's PV / grid / load kWh
    totals to Telegram. Uses a separate API endpoint (getStationKpi) from
    the real-time snapshot job, so it never interferes with the regular
    reporting schedule."""
    started_at = datetime.now(timezone.utc)
    logger.info("Running daily energy summary...")

    try:
        client = build_client_from_env()
        notifier = build_notifier_from_env()
    except KeyError as exc:
        logger.error("Missing environment variable %s -- check your .env file.", exc)
        return

    try:
        client.login()
        all_plants = client.get_all_plants()
        station_codes = [p["plantCode"] for p in all_plants]
        plant_names = {p["plantCode"]: p["plantName"] for p in all_plants}

        snapshots = client.get_daily_energy(station_codes)
    except FusionSolarAPIError as exc:
        logger.error("Daily energy fetch failed: %s", exc)
        return

    message = format_daily_energy(snapshots, plant_names)
    ok = notifier.send_message(message)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if ok:
        logger.info("Daily energy summary sent successfully (%.1fs).", elapsed)
    else:
        logger.error("Daily energy summary generated but Telegram send failed (%.1fs).", elapsed)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC")
    for rule in SCHEDULE_CONFIG:
        trigger = CronTrigger(
            day_of_week=rule["day_of_week"],
            hour=rule["hour"],
            minute=rule["minute"],
            timezone="UTC",
        )
        scheduler.add_job(run_report_job, trigger, id=rule["name"], name=rule["name"])
        logger.info(
            "Scheduled rule '%s': day_of_week=%s hour=%s minute=%s",
            rule["name"], rule["day_of_week"], rule["hour"], rule["minute"],
        )
    # Daily energy summary at 18:00 UTC every day
    scheduler.add_job(
        run_daily_energy_job,
        CronTrigger(hour=DAILY_ENERGY_HOUR, minute=DAILY_ENERGY_MINUTE, timezone="UTC"),
        id="daily_energy_summary",
        name="daily_energy_summary",
    )
    logger.info(
        "Scheduled rule 'daily_energy_summary': every day at %02d:%02d UTC",
        DAILY_ENERGY_HOUR, DAILY_ENERGY_MINUTE,
    )
    return scheduler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the fleet snapshot report job immediately, once, then exit.",
    )
    parser.add_argument(
        "--run-daily-energy-once",
        action="store_true",
        help="Run the daily energy summary job immediately, once, then exit "
             "(useful for testing the 18:00 job without waiting for the trigger).",
    )
    args = parser.parse_args()

    load_dotenv_if_present()

    if args.run_once:
        run_report_job()
        return 0

    if args.run_daily_energy_once:
        run_daily_energy_job()
        return 0

    scheduler = build_scheduler()
    logger.info("Scheduler starting. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
