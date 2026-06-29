#!/usr/bin/env python3
"""
FusionSolar + Telegram stack health check.

Runs a live end-to-end test of every feature and prints a clear
pass/fail result for each one. Safe to run at any time -- it makes
real API calls but stays well within rate limits (one fleet snapshot
total, same as a single scheduled run).

Usage:
    python healthcheck.py
    python healthcheck.py --no-telegram   # skip Telegram send (API-only check)
    python healthcheck.py --verbose       # print raw API data alongside results

Rate limit note (from SmartPVMS NBI Reference v25.1.0, section 4.2):
    For 9 plants (your fleet):
      Login          : 5 calls / 10 min
      Plant List     : 34 calls / day
      Device List    : 25 calls / day
      Real-Time Plant: 1 call  / 5 min
      Real-Time Device: 2 calls / 5 min  (inverters + sensors = 2 types)
      Active Alarms  : 2 calls / 30 min
    This script makes ONE of each -- well within all limits.
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── colour helpers (no third-party deps) ─────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

PASS = f"{GREEN}✔  PASS{RESET}"
FAIL = f"{RED}✘  FAIL{RESET}"
WARN = f"{YELLOW}⚠  WARN{RESET}"
SKIP = f"{YELLOW}–  SKIP{RESET}"

WIDTH = 52   # label column width


def banner(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print("─" * (WIDTH + 12))


def result(label: str, status: str, detail: str = "") -> None:
    detail_str = f"  ({detail})" if detail else ""
    print(f"  {label:<{WIDTH}} {status}{detail_str}")


# ─────────────────────────────────────────────────────────────────────

def load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def check_env() -> dict:
    """Returns dict of {key: value|None}."""
    required = [
        "FUSIONSOLAR_BASE_URL",
        "FUSIONSOLAR_USERNAME",
        "FUSIONSOLAR_SYSTEM_CODE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    return {k: os.environ.get(k) for k in required}


def rate_limit_summary(n_plants: int, device_types: list) -> str:
    """Compute the per-run budget consumed and daily/5-min budget available."""
    lines = []
    # Real-time plant data: Roundup(n_plants/100) per 5 min
    rt_plant_budget = math.ceil(n_plants / 100)
    lines.append(f"Real-time plant data : 1 call used / {rt_plant_budget} allowed per 5 min")
    # Real-time device data: sum of Roundup(n_devices_of_type/100) per 5 min
    rt_dev_budget = sum(math.ceil(n / 100) for n in device_types.values())
    lines.append(f"Real-time device data: {len(device_types)} call(s) used / {rt_dev_budget} allowed per 5 min")
    # Alarms: MAX(Roundup(n_plants/100), sum of Roundup per type) per 30 min
    alarm_budget = max(math.ceil(n_plants / 100), rt_dev_budget)
    lines.append(f"Active alarms        : 1 call used / {alarm_budget} allowed per 30 min")
    # Plant list: Roundup(n_plants/100)*10 + 24 per day
    pl_budget = math.ceil(n_plants / 100) * 10 + 24
    lines.append(f"Plant list           : 1 call used / {pl_budget} allowed per day")
    # Device list: Roundup(n_plants/100) + 24 per day
    dl_budget = math.ceil(n_plants / 100) + 24
    lines.append(f"Device list          : 1 call used / {dl_budget} allowed per day")
    return "\n    ".join(lines)


# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram tests.")
    parser.add_argument("--verbose", action="store_true", help="Print raw API response snippets.")
    args = parser.parse_args()

    load_dotenv()

    passed = failed = warned = skipped = 0

    def ok(label, detail=""):
        nonlocal passed
        result(label, PASS, detail)
        passed += 1

    def fail(label, detail=""):
        nonlocal failed
        result(label, FAIL, detail)
        failed += 1

    def warn(label, detail=""):
        nonlocal warned
        result(label, WARN, detail)
        warned += 1

    def skip(label, detail=""):
        nonlocal skipped
        result(label, SKIP, detail)
        skipped += 1

    # ── 1. Environment ────────────────────────────────────────────────
    banner("1 · Environment variables")
    env = check_env()
    env_ok = True
    for key, value in env.items():
        if value:
            ok(key, f"{value[:6]}…" if len(value) > 6 else "set")
        else:
            fail(key, "MISSING — check your .env file")
            env_ok = False

    if not env_ok:
        print(f"\n{RED}Cannot continue — fix missing env vars first.{RESET}")
        return 1

    # ── 2. FusionSolar auth ───────────────────────────────────────────
    banner("2 · FusionSolar — authentication")
    from client import FusionSolarNBIClient, FusionSolarAPIError

    client = FusionSolarNBIClient(
        base_url=env["FUSIONSOLAR_BASE_URL"],
        username=env["FUSIONSOLAR_USERNAME"],
        system_code=env["FUSIONSOLAR_SYSTEM_CODE"],
    )

    try:
        client.login()
        token_age = time.time() - client.token_obtained_at
        if token_age < 5:
            ok("Login (fresh)", "new session created")
        else:
            ok("Login (cached token reused)", f"token age {token_age:.0f}s")
    except FusionSolarAPIError as e:
        fail("Login", f"failCode {e.fail_code}: {e.message}")
        print(f"\n{RED}Cannot continue — login failed.{RESET}")
        return 1

    # ── 3. Plant list ─────────────────────────────────────────────────
    banner("3 · FusionSolar — plant list")
    try:
        plants = client.get_all_plants()
        n_plants = len(plants)
        if n_plants > 0:
            ok(f"Plant list returned", f"{n_plants} plant(s)")
            for p in plants:
                cap = p.get("capacity") or 0
                ok(f"  {p['plantName'][:45]}", f"{cap} kWp  ({p['plantCode']})")
        else:
            warn("Plant list", "API responded OK but no plants returned — check account plant bindings")
    except FusionSolarAPIError as e:
        fail("Plant list", f"failCode {e.fail_code}: {e.message}")
        plants = []

    if not plants:
        print(f"\n{RED}Cannot continue — no plants available.{RESET}")
        return 1

    station_codes = [p["plantCode"] for p in plants]
    plant_names   = {p["plantCode"]: p["plantName"] for p in plants}

    # ── 4. Fleet snapshot ─────────────────────────────────────────────
    banner("4 · FusionSolar — fleet snapshot (real-time data)")
    device_type_counts: dict = {}
    snapshots = {}

    try:
        snapshots = client.get_fleet_snapshot(station_codes)
        ok("get_fleet_snapshot() completed", f"{len(snapshots)} plant(s) returned")

        for code, snap in snapshots.items():
            name = plant_names.get(code, code)[:35]
            health = snap.get("plant_health", "Unknown")

            # PV power
            pv = snap.get("pv_power_kw")
            if pv is not None:
                ok(f"  {name} — PV power", f"{pv:.2f} kW")
            else:
                warn(f"  {name} — PV power", "n/a (night / offline?)")

            # Grid power
            grid = snap.get("grid_power_kw")
            if grid is not None:
                flow = "importing" if grid < 0 else "exporting"
                ok(f"  {name} — Grid power", f"{grid:.2f} kW ({flow})")
            else:
                warn(f"  {name} — Grid power", "n/a — no grid meter / power sensor?")

            # Load estimate
            load = snap.get("load_estimate_kw")
            if load is not None:
                ok(f"  {name} — Load (estimated)", f"{load:.2f} kW")
            else:
                warn(f"  {name} — Load (estimated)", "n/a — needs both PV and grid data")

            # Plant health
            if health == "Healthy":
                ok(f"  {name} — Plant health", health)
            elif health in ("Faulty", "Disconnected"):
                warn(f"  {name} — Plant health", health)
            else:
                warn(f"  {name} — Plant health", health)

            # Device online status
            devices = snap.get("devices") or []
            online  = [d for d in devices if d.get("online") is True]
            offline = [d for d in devices if d.get("online") is False]
            monitored = [d for d in devices if d.get("status") != "n/a"]
            if offline:
                warn(
                    f"  {name} — Device status",
                    f"{len(online)}/{len(monitored)} online, "
                    f"{len(offline)} OFFLINE: {', '.join(d['devName'] for d in offline)}",
                )
            elif monitored:
                ok(f"  {name} — Device status", f"{len(online)}/{len(monitored)} online")
            else:
                skip(f"  {name} — Device status", "no monitored devices returned")

            # Active alarms
            alarms = snap.get("active_alarms") or []
            if alarms:
                warn(
                    f"  {name} — Active alarms",
                    f"{len(alarms)} alarm(s): "
                    + ", ".join(a.get("alarmName", "?") for a in alarms[:3]),
                )
            else:
                ok(f"  {name} — Active alarms", "none")

            # collect device type counts for rate limit calc
            for dev in devices:
                if dev.get("status") != "n/a" and dev.get("devTypeId"):
                    tid = dev["devTypeId"]
                    device_type_counts[tid] = device_type_counts.get(tid, 0) + 1

        if args.verbose and snapshots:
            import json
            first_code = next(iter(snapshots))
            print(f"\n  Raw snapshot for {plant_names.get(first_code, first_code)}:")
            snap_clean = {k: v for k, v in snapshots[first_code].items() if k != "devices"}
            print("  " + json.dumps(snap_clean, indent=4).replace("\n", "\n  "))

    except FusionSolarAPIError as e:
        fail("get_fleet_snapshot()", f"failCode {e.fail_code}: {e.message}")

    # ── 5. Token cache ────────────────────────────────────────────────
    banner("5 · Token cache")
    cache_path = client.token_cache_path
    if cache_path.exists() and cache_path.stat().st_size > 0:
        ok("Token cache file exists", str(cache_path))
        # Check file permissions (should ideally be 600)
        mode = oct(cache_path.stat().st_mode)[-3:]
        if mode == "600":
            ok("Token cache permissions", "600 (owner-only read/write)")
        else:
            warn("Token cache permissions", f"{mode} — recommended: chmod 600 {cache_path}")
    else:
        warn("Token cache file", "missing or empty — will re-login on next run")

    # ── 6. Scheduler config ───────────────────────────────────────────
    banner("6 · Scheduler configuration")
    try:
        import scheduler as sched_module
        rules = sched_module.SCHEDULE_CONFIG
        if rules:
            ok("SCHEDULE_CONFIG loaded", f"{len(rules)} rule(s)")
            for r in rules:
                ok(f"  Rule '{r['name']}'",
                   f"day={r['day_of_week']}  hour={r['hour']}  minute={r['minute']}")
        else:
            warn("SCHEDULE_CONFIG", "empty — scheduler will do nothing")
    except Exception as e:
        fail("scheduler.py import", str(e))

    # ── 7. Rate limit budget ──────────────────────────────────────────
    banner("7 · API rate limit budget (your 9-plant fleet)")
    if n_plants and device_type_counts:
        summary = rate_limit_summary(n_plants, device_type_counts)
        print(f"    {summary}")
        # Check if 30-min schedule fits within real-time plant data limit
        # 1 call per 5 min = 12 per hour; 30-min schedule = 2 per hour per plant budget
        rt_budget_per_hour = math.ceil(n_plants / 100) * 12  # calls allowed per hour
        runs_per_hour = 2  # 30-min weekday schedule
        if runs_per_hour <= rt_budget_per_hour:
            ok("30-min schedule fits real-time plant limit",
               f"{runs_per_hour} runs/hr ≤ {rt_budget_per_hour} allowed/hr")
        else:
            warn("Schedule may exceed rate limits",
                 f"{runs_per_hour} runs/hr but only {rt_budget_per_hour} allowed/hr")
    else:
        skip("Rate limit calculation", "no plant/device data available")

    # ── 8. Telegram ───────────────────────────────────────────────────
    banner("8 · Telegram")
    if args.no_telegram:
        skip("Telegram connection test", "--no-telegram flag set")
        skip("Telegram message send", "--no-telegram flag set")
    else:
        from telegram_notify import TelegramNotifier, format_fleet_snapshot
        notifier = TelegramNotifier(
            bot_token=env["TELEGRAM_BOT_TOKEN"],
            chat_id=env["TELEGRAM_CHAT_ID"],
        )

        # connection test
        import requests as _req
        try:
            resp = _req.get(
                f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/getMe",
                timeout=10,
            )
            body = resp.json()
            if body.get("ok"):
                bot_name = body["result"].get("username", "?")
                ok("Telegram bot reachable", f"@{bot_name}")
            else:
                fail("Telegram bot reachable", body.get("description", "unknown error"))
        except Exception as e:
            fail("Telegram bot reachable", str(e))

        # send a real (but clearly labelled) health-check message
        if snapshots:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                msg = (
                    f"🔧 <b>Health Check — {ts}</b>\n"
                    + format_fleet_snapshot(snapshots, plant_names)
                )
                sent = notifier.send_message(msg)
                if sent:
                    ok("Telegram message sent", "check your chat for the health-check report")
                else:
                    fail("Telegram message send", "send_message() returned False — check logs")
            except Exception as e:
                fail("Telegram message send", str(e))
        else:
            skip("Telegram message send", "no snapshot data to send")

    # ── Summary ───────────────────────────────────────────────────────
    total = passed + failed + warned + skipped
    banner("Summary")
    print(f"  {GREEN}{passed} passed{RESET}  |  "
          f"{RED}{failed} failed{RESET}  |  "
          f"{YELLOW}{warned} warnings{RESET}  |  "
          f"{YELLOW}{skipped} skipped{RESET}  |  "
          f"{total} total\n")

    if failed:
        print(f"{RED}  ✘ Health check FAILED — fix the items marked FAIL above.{RESET}\n")
        return 1
    elif warned:
        print(f"{YELLOW}  ⚠ Health check passed with warnings — review items marked WARN above.{RESET}\n")
        return 0
    else:
        print(f"{GREEN}  ✔ All checks passed — your stack is healthy.{RESET}\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
