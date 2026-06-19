#!/usr/bin/env python3
"""
Quick smoke test for the FusionSolar Northbound API client.

Usage:
    1. Copy ..env to .env and fill in your Northbound API account
       details (these are NOT your personal FusionSolar login -- see
       "Obtaining an Account" in the NBI Reference, section 3.2.1).
    2. pip install -r requirements.txt
    3. python test_nbi.py

What it does:
    - Logs in (or reuses a cached token if you ran this recently)
    - Fetches the plant list your API account has permission for
    - Fetches device list + real-time KPIs for the first plant found
    - Prints everything in a readable form

It deliberately does NOT call logout() at the end, so the token stays
valid for reuse on your next run -- remember the account allows only one
active session at a time.
"""

import json
import logging
import sys

from client import FusionSolarNBIClient, FusionSolarAPIError, build_client_from_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_nbi")


def load_dotenv_if_present():
    """Tiny .env loader so we don't add a hard dependency on python-dotenv.
    If you'd rather use python-dotenv, just call load_dotenv() instead."""
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


def main() -> int:
    load_dotenv_if_present()

    try:
        client = build_client_from_env()
    except KeyError as exc:
        logger.error(
            "Missing environment variable %s. Copy ..env to .env and fill it in.",
            exc,
        )
        return 1

    try:
        client.login()
    except FusionSolarAPIError as exc:
        logger.error("Login failed: %s", exc)
        logger.error(
            "If failCode is 20400, double-check userName/systemCode, and "
            "confirm this is the Northbound API account, not your personal login."
        )
        return 1

    # --- Plant list ---------------------------------------------------
    logger.info("Fetching plant list...")
    try:
        plants = client.get_all_plants()
    except FusionSolarAPIError as exc:
        logger.error("Plant list call failed: %s", exc)
        return 1

    if not plants:
        logger.warning(
            "Plant list came back empty. This usually means the API account "
            "exists but hasn't been bound to any plants yet -- check "
            "System > Company Management > Northbound Management on the portal."
        )
        return 0

    logger.info("Found %d plant(s):", len(plants))
    for p in plants:
        print(f"  - {p['plantName']!r:30} plantCode={p['plantCode']}  capacity={p.get('capacity')} kWp")

    station_codes = [p["plantCode"] for p in plants[:1]]  # just the first plant for this smoke test

    # --- Device list -----------------------------------------------------
    logger.info("Fetching device list for %s...", station_codes)
    try:
        device_resp = client.get_device_list(station_codes)
        print(json.dumps(device_resp.get("data"), indent=2))
    except FusionSolarAPIError as exc:
        logger.error("Device list call failed: %s", exc)

    # --- Real-time plant data --------------------------------------------
    logger.info("Fetching real-time plant data for %s...", station_codes)
    try:
        rt_resp = client.get_real_time_plant_data(station_codes)
        print(json.dumps(rt_resp.get("data"), indent=2))
    except FusionSolarAPIError as exc:
        logger.error("Real-time plant data call failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
