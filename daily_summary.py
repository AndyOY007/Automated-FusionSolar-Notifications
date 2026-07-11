"""
Daily energy summary builder.

Fetches today's accumulated energy figures from the NBI API and computes
all analytics in plain Python BEFORE the LLM sees anything. The LLM's
job is purely to narrate pre-computed results -- never to calculate.

Fields returned per plant:
    generation_kwh      : Total PV yield for the day (day_power from API)
    consumption_kwh     : Total site consumption (day_use_energy from API)
    export_kwh          : Energy exported to the grid (day_on_grid_energy)
    import_kwh          : Energy imported from the grid (derived)
    self_consumed_kwh   : PV energy used on-site (derived)
    expected_kwh        : Expected yield (capacity × PSH × efficiency)
    deviation_pct       : (generation - expected) / expected × 100
    performance_ratio   : generation / expected  (0.0 - 1.0)
    flags               : Anomaly flags -- set in Python, narrated by LLM

Flags (set deterministically, never by the LLM):
    zero_generation           : day_power is 0 or None at 8pm
    severe_underperformance   : deviation_pct < -20%
    moderate_underperformance : deviation_pct between -20% and -10%
    high_grid_import          : grid import > total PV generation
    no_consumption_data       : day_use_energy not returned by API
    plant_offline             : real_health_state != 3 (Healthy)

PSH and efficiency:
    Ghana default PSH = 4.5 h/day.
    System efficiency 0.80 accounts for cable/inverter losses, soiling,
    temperature derating. Adjust per site if you have measured data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("daily_summary")

# ── Site / location defaults ──────────────────────────────────────────
PEAK_SUN_HOURS: float = 4.5
SYSTEM_EFFICIENCY_FACTOR: float = 0.80

# Anomaly thresholds
SEVERE_UNDERPERFORMANCE_PCT: float = -20.0
MODERATE_UNDERPERFORMANCE_PCT: float = -10.0
HIGH_IMPORT_RATIO: float = 1.0   # import > generation triggers flag


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _expected_kwh(capacity_kwp: Optional[float]) -> Optional[float]:
    if capacity_kwp is None or capacity_kwp <= 0:
        return None
    return round(capacity_kwp * PEAK_SUN_HOURS * SYSTEM_EFFICIENCY_FACTOR, 2)


def _deviation_pct(generation: float, expected: Optional[float]) -> Optional[float]:
    if expected is None or expected <= 0:
        return None
    return round((generation - expected) / expected * 100, 1)


def build_plant_summary(plant: dict, rt_data: dict) -> dict:
    """
    Build one plant's daily summary.

    plant   : row from get_all_plants() -- has plantName, plantCode, capacity
    rt_data : plant's dataItemMap from get_real_time_plant_data()
    """
    name     = plant.get("plantName", plant.get("plantCode", "Unknown"))
    code     = plant.get("plantCode")
    capacity = _safe_float(plant.get("capacity"))

    generation  = _safe_float(rt_data.get("day_power"))
    consumption = _safe_float(rt_data.get("day_use_energy"))
    export      = _safe_float(rt_data.get("day_on_grid_energy"))
    health_code = rt_data.get("real_health_state")

    # Derived figures
    self_consumed = None
    grid_import   = None
    if generation is not None and export is not None:
        self_consumed = round(max(generation - export, 0), 2)
    if consumption is not None and self_consumed is not None:
        grid_import = round(max(consumption - self_consumed, 0), 2)

    expected   = _expected_kwh(capacity)
    deviation  = _deviation_pct(generation or 0, expected)
    perf_ratio = round(generation / expected, 3) if (generation and expected) else None

    # ── Anomaly flags ─────────────────────────────────────────────────
    flags = []

    if health_code is not None and int(health_code) != 3:
        flags.append("plant_offline")

    if generation is None or generation == 0:
        flags.append("zero_generation")
    elif deviation is not None:
        if deviation < SEVERE_UNDERPERFORMANCE_PCT:
            flags.append("severe_underperformance")
        elif deviation < MODERATE_UNDERPERFORMANCE_PCT:
            flags.append("moderate_underperformance")

    if consumption is None:
        flags.append("no_consumption_data")

    if grid_import is not None and generation is not None and generation > 0:
        if grid_import > generation * HIGH_IMPORT_RATIO:
            flags.append("high_grid_import")

    return {
        "name":              name,
        "code":              code,
        "capacity_kwp":      capacity,
        "generation_kwh":    round(generation, 2) if generation is not None else None,
        "expected_kwh":      expected,
        "deviation_pct":     deviation,
        "performance_ratio": perf_ratio,
        "consumption_kwh":   round(consumption, 2) if consumption is not None else None,
        "export_kwh":        round(export, 2) if export is not None else None,
        "self_consumed_kwh": self_consumed,
        "import_kwh":        grid_import,
        "flags":             flags,
        "health_state":      int(health_code) if health_code is not None else None,
    }


def build_daily_summary(client) -> dict:
    """
    Fetches today's accumulated energy data for the full fleet and returns
    a structured summary dict ready to pass to the LLM or fallback template.

    Uses get_real_time_plant_data() which already returns accumulated daily
    figures (day_power, day_use_energy, day_on_grid_energy) -- no extra
    API endpoint needed beyond what the regular fleet snapshot already uses.
    """
    logger.info("Building daily summary...")

    plants = client.get_all_plants()
    station_codes = [p["plantCode"] for p in plants]
    plant_by_code = {p["plantCode"]: p for p in plants}

    # Fetch daily-accumulated KPIs in one batched call
    from client import _chunked
    rt_rows_by_code: dict[str, dict] = {}
    for chunk in _chunked(station_codes, 100):
        resp = client.get_real_time_plant_data(chunk)
        for row in resp.get("data") or []:
            rt_rows_by_code[row["stationCode"]] = row.get("dataItemMap") or {}

    plant_summaries = []
    for code in station_codes:
        plant   = plant_by_code[code]
        rt_data = rt_rows_by_code.get(code, {})
        summary = build_plant_summary(plant, rt_data)
        plant_summaries.append(summary)

        if summary["flags"]:
            logger.info("  %s — flags: %s", summary["name"], summary["flags"])
        else:
            logger.info(
                "  %s — %.1f kWh / %.1f kWh expected",
                summary["name"],
                summary["generation_kwh"] or 0,
                summary["expected_kwh"] or 0,
            )

    # Fleet totals
    total_generation  = sum(p["generation_kwh"]  or 0 for p in plant_summaries)
    total_consumption = sum(
        p["consumption_kwh"] or 0 for p in plant_summaries
        if p["consumption_kwh"] is not None
    )
    total_expected = sum(
        p["expected_kwh"] or 0 for p in plant_summaries
        if p["expected_kwh"] is not None
    )
    plants_with_flags = [p["name"] for p in plant_summaries if p["flags"]]

    return {
        "date":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "psh":   PEAK_SUN_HOURS,
        "plants": plant_summaries,
        "fleet_total": {
            "generation_kwh":           round(total_generation, 2),
            "consumption_kwh":          round(total_consumption, 2),
            "expected_kwh":             round(total_expected, 2),
            "plants_needing_attention": plants_with_flags,
        },
    }
