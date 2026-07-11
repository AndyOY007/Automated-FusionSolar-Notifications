#!/usr/bin/env python3
"""
Scheduled FusionSolar fleet reporting -> Telegram.

Two report types run on independent schedules:

  1. REAL-TIME SNAPSHOT (run_report_job)
     Weekdays: every 30 minutes
     Weekends: every hour
     Sends current PV production, grid flow, load, device status, alarms.

  2. DAILY LLM ANALYSIS (run_llm_report_job)
     Every day at 20:00 UTC
     Fetches today's accumulated energy data, computes analytics in Python,
     feeds a pre-computed structured summary to a local LLM (Qwen2.5-1.5B),
     and sends a plain-English daily performance report to Telegram.
     Falls back to a template report if LLM inference fails.

Edit SCHEDULE_CONFIG (real-time) or LLM_SCHEDULE_CONFIG (daily analysis)
at the top of this file to change any timing. Nothing else needs to change.

Usage:
    python scheduler.py                  # runs forever (Ctrl+C to stop)
    python scheduler.py --run-once       # fires real-time job once, exits
    python scheduler.py --run-llm-once   # fires LLM daily report once, exits
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from client import FusionSolarAPIError, build_client_from_env
from telegram_notify import build_notifier_from_env, format_fleet_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scheduler")
logging.getLogger("apscheduler").setLevel(logging.WARNING)


# =====================================================================
# REAL-TIME SNAPSHOT SCHEDULE
# Fires run_report_job() on each rule's cadence.
# day_of_week: mon-fri, sat,sun, * etc.
# hour: "*" = all hours, "6-18" = 6am to 6pm only
# minute: "*/30" = every 30 min, "0" = on the hour
# =====================================================================
SCHEDULE_CONFIG = [
    {
        "name": "weekday_every_30min",
        "day_of_week": "mon-fri",
        "hour": "7-18",
        "minute": "*/30",
    },
    {
        "name": "weekend_hourly",
        "day_of_week": "sat,sun",
        "hour": "8-16",
        "minute": "0",
    },
]

# =====================================================================
# DAILY LLM ANALYSIS SCHEDULE
# Fires run_llm_report_job() once per day.
# Default: every day at 20:00 UTC (8pm -- sun has set in Ghana,
# so the day's generation figures are complete).
# =====================================================================
LLM_SCHEDULE_CONFIG = {
    "name": "daily_llm_analysis",
    "day_of_week": "*",
    "hour": "20",
    "minute": "0",
}

# Path to the venv Python -- used to launch llm_report.py as a subprocess.
# The subprocess must use the same venv that has llama-cpp-python installed.
_THIS_DIR = Path(__file__).parent
VENV_PYTHON = str(_THIS_DIR / ".venv" / "bin" / "python")
LLM_SCRIPT  = str(_THIS_DIR / "llm_report.py")

# How long to wait for LLM inference before giving up (seconds).
# 180s = 3 minutes. Qwen2.5-1.5B at ~3-6 tok/s for 250 tokens = ~45-85s.
LLM_TIMEOUT_SECONDS = 180

# Alarm lookback for the real-time snapshot reports
ALARM_LOOKBACK_HOURS = 24
# =====================================================================
# END SCHEDULE CONFIG
# =====================================================================


def load_dotenv_if_present():
    from pathlib import Path
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ─────────────────────────────────────────────────────────────────────
# Job 1: real-time snapshot (existing)
# ─────────────────────────────────────────────────────────────────────

def run_report_job() -> None:
    """Fetch fleet snapshot, send to Telegram. Any failure is caught and
    logged -- the scheduler keeps running and retries on the next tick."""
    import requests.exceptions

    started_at = datetime.now(timezone.utc)
    logger.info("Running scheduled fleet report...")

    try:
        client  = build_client_from_env()
        notifier = build_notifier_from_env()
    except KeyError as exc:
        logger.error("Missing environment variable %s -- check your .env file.", exc)
        return

    try:
        client.login()
        all_plants    = client.get_all_plants()
        station_codes = [p["plantCode"] for p in all_plants]
        plant_names   = {p["plantCode"]: p["plantName"] for p in all_plants}
        snapshots     = client.get_fleet_snapshot(
            station_codes, alarm_lookback_hours=ALARM_LOOKBACK_HOURS
        )
    except FusionSolarAPIError as exc:
        logger.error("Fleet snapshot failed (API error %s): %s", exc.fail_code, exc.message)
        return
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Fleet snapshot failed -- network unreachable (transient). "
                       "Will retry at next scheduled run. Detail: %s", exc)
        return
    except requests.exceptions.Timeout:
        logger.warning("Fleet snapshot failed -- request timed out. "
                       "Will retry at next scheduled run.")
        return
    except requests.exceptions.RequestException as exc:
        logger.error("Fleet snapshot failed -- unexpected network error: %s", exc)
        return

    message = format_fleet_snapshot(snapshots, plant_names)
    ok = notifier.send_message(message)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if ok:
        logger.info("Report sent successfully (%.1fs).", elapsed)
    else:
        logger.error("Report generated but Telegram send failed (%.1fs).", elapsed)


# ─────────────────────────────────────────────────────────────────────
# Job 2: daily LLM analysis (new)
# ─────────────────────────────────────────────────────────────────────

def _fallback_template_report(summary: dict) -> str:
    """
    Plain-text template report used when LLM inference fails or times out.
    Always sends *something* at 8pm even if the model crashes or OOMs.
    Uses the same pre-computed summary dict as the LLM prompt.
    """
    date   = summary.get("date", "today")
    fleet  = summary.get("fleet_total", {})
    plants = summary.get("plants", [])

    lines = [
        f"📊 Daily Energy Report — {date}",
        f"Fleet total: {fleet.get('generation_kwh', 0):.1f} kWh generated "
        f"/ {fleet.get('expected_kwh', 0):.1f} kWh expected",
        "",
    ]

    attention = fleet.get("plants_needing_attention") or []
    if attention:
        lines.append(f"⚠️ Needs attention: {', '.join(attention)}")
        lines.append("")

    for p in plants:
        gen  = p.get("generation_kwh")
        exp  = p.get("expected_kwh")
        dev  = p.get("deviation_pct")
        flags = p.get("flags") or []

        gen_str = f"{gen:.1f} kWh" if gen is not None else "no data"
        dev_str = f" ({dev:+.1f}%)" if dev is not None else ""
        flag_str = f" ⚠️ {', '.join(flags)}" if flags else " ✔"
        lines.append(f"  {p['name']}: {gen_str}{dev_str}{flag_str}")

    lines.append("")
    lines.append("(Report generated from template — LLM inference unavailable)")
    return "\n".join(lines)


def run_llm_report_job() -> None:
    """
    Daily 8pm job:
      1. Fetch today's energy data + compute analytics (Python, deterministic)
      2. Launch llm_report.py as a subprocess, pipe the summary JSON to it
      3. Read the generated report from stdout
      4. Fall back to template if inference fails or times out
      5. Send whichever report to Telegram
    """
    import requests.exceptions
    from daily_summary import build_daily_summary

    started_at = datetime.now(timezone.utc)
    logger.info("Running daily LLM analysis report...")

    try:
        client   = build_client_from_env()
        notifier = build_notifier_from_env()
    except KeyError as exc:
        logger.error("Missing environment variable %s -- check your .env file.", exc)
        return

    # ── Step 1: build deterministic summary (no LLM involved yet) ────
    try:
        client.login()
        summary = build_daily_summary(client)
    except FusionSolarAPIError as exc:
        logger.error("Daily summary fetch failed (API error %s): %s", exc.fail_code, exc.message)
        return
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Daily summary fetch failed -- network unreachable: %s", exc)
        return
    except requests.exceptions.RequestException as exc:
        logger.error("Daily summary fetch failed -- network error: %s", exc)
        return

    summary_json = json.dumps(summary)

    # ── Step 2: run LLM inference in an isolated subprocess ───────────
    report_text = None
    used_fallback = False

    if not os.path.exists(LLM_SCRIPT):
        logger.warning("llm_report.py not found at %s -- using fallback template.", LLM_SCRIPT)
        used_fallback = True
    else:
        try:
            logger.info("Launching LLM subprocess (timeout=%ds)...", LLM_TIMEOUT_SECONDS)
            result = subprocess.run(
                [VENV_PYTHON, LLM_SCRIPT],
                input=summary_json,
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                report_text = result.stdout.strip()
                logger.info("LLM inference succeeded.")
                if result.stderr.strip():
                    # llm_report.py logs to stderr -- show it at DEBUG level
                    for line in result.stderr.strip().splitlines():
                        logger.debug("  [llm] %s", line)
            else:
                logger.error(
                    "LLM subprocess exited with code %d. stderr:\n%s",
                    result.returncode,
                    result.stderr[:500],
                )
                used_fallback = True

        except subprocess.TimeoutExpired:
            logger.error(
                "LLM inference timed out after %ds -- using fallback template.",
                LLM_TIMEOUT_SECONDS,
            )
            used_fallback = True
        except FileNotFoundError:
            logger.error(
                "Python binary not found at %s. "
                "Make sure the venv is set up: python3 -m venv .venv && "
                ".venv/bin/pip install -r requirements.txt",
                VENV_PYTHON,
            )
            used_fallback = True

    if used_fallback or not report_text:
        report_text = _fallback_template_report(summary)

    # ── Step 3: send to Telegram ──────────────────────────────────────
    ok = notifier.send_message(report_text, parse_mode="HTML")  # plain text, no HTML

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if ok:
        logger.info("Daily LLM report sent successfully (%.1fs, fallback=%s).",
                    elapsed, used_fallback)
    else:
        logger.error("Daily LLM report generated but Telegram send failed (%.1fs).", elapsed)


# ─────────────────────────────────────────────────────────────────────
# Scheduler wiring
# ─────────────────────────────────────────────────────────────────────

def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC")

    # Real-time snapshot jobs
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

    # Daily LLM analysis job
    llm_rule = LLM_SCHEDULE_CONFIG
    llm_trigger = CronTrigger(
        day_of_week=llm_rule["day_of_week"],
        hour=llm_rule["hour"],
        minute=llm_rule["minute"],
        timezone="UTC",
    )
    scheduler.add_job(
        run_llm_report_job, llm_trigger,
        id=llm_rule["name"], name=llm_rule["name"],
    )
    logger.info(
        "Scheduled rule '%s': day_of_week=%s hour=%s minute=%s",
        llm_rule["name"], llm_rule["day_of_week"], llm_rule["hour"], llm_rule["minute"],
    )

    return scheduler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Fire the real-time snapshot job immediately once, then exit.",
    )
    parser.add_argument(
        "--run-llm-once",
        action="store_true",
        help="Fire the daily LLM analysis job immediately once, then exit. "
             "Useful for testing the LLM pipeline end-to-end.",
    )
    args = parser.parse_args()

    load_dotenv_if_present()

    if args.run_once:
        run_report_job()
        return 0

    if args.run_llm_once:
        run_llm_report_job()
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
