"""
Huawei FusionSolar SmartPVMS Northbound API client.

Built from SmartPVMS 25.1.0 NBI Reference (Issue 02, 2025-07-17).

Key behaviors this client respects, straight from the spec:
- Login returns an XSRF-TOKEN (in a response header / cookie) that is valid
  for 30 minutes, and the validity window slides forward as long as you keep
  using it within that window.
- An API account can have only ONE online session at a time. Logging in
  again invalidates the previously issued token. -> We cache the token to
  disk and reuse it across runs instead of logging in every time.
- Login is rate-limited to 5 calls / 10 minutes per account, and 5 wrong
  passwords in 10 minutes locks the account for 30 minutes.
- failCode 305 means "you're not online, log in again" -> we catch this and
  do a single re-login + retry.
- failCode 407 / HTTP-level throttling means you're calling too fast -> we
  back off.
- Plant List / Device List / Real-time Plant Data all accept a maximum of
  100 plants per call.

Domain: FusionSolar is split across regional clusters (the subdomain you
log into, e.g. "sg5", "intl", "uni000xxx" etc.). Use the same subdomain you
use to log into the FusionSolar web portal.
"""

from __future__ import annotations

import json
import os
import time
import logging
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger("fusionsolar_nbi")

TOKEN_VALIDITY_SECONDS = 30 * 60  # 30 minutes, per spec
MAX_PLANTS_PER_CALL = 100
MAX_DEVICES_PER_CALL = 100  # same-type devices per getDevRealKpi / getAlarmList call

# Device type IDs we know how to interpret for the real-time snapshot, per
# section 5.1.2.2 of the NBI Reference. category groups them for the
# PV / grid / battery roll-up math in get_plant_snapshot().
DEVICE_TYPE_REGISTRY: dict[int, dict] = {
    1: {"label": "String Inverter", "category": "pv", "power_key": "active_power", "power_unit": "kW"},
    38: {"label": "Residential Inverter", "category": "pv", "power_key": "active_power", "power_unit": "kW"},
    17: {"label": "Grid Meter", "category": "grid", "power_key": "active_power", "power_unit": "W"},
    47: {"label": "Power Sensor", "category": "grid", "power_key": "active_power", "power_unit": "W"},
    39: {"label": "Residential Battery", "category": "battery", "power_key": "ch_discharge_power", "power_unit": "W"},
    41: {"label": "C&I/Utility ESS", "category": "battery", "power_key": "ch_discharge_power", "power_unit": "W"},
}

# Table 5-1 in the NBI Reference. Only the states relevant to a quick
# "is it actually producing" read are annotated with is_online; the rest
# are still shown verbatim if encountered.
INVERTER_STATE_DESCRIPTIONS: dict[int, str] = {
    0: "Standby: initializing",
    1: "Standby: insulation resistance detecting",
    2: "Standby: irradiation detecting",
    3: "Standby: grid detecting",
    256: "Start",
    512: "Grid-connected",
    513: "Grid-connected: power limited",
    514: "Grid-connected: self-derating",
    768: "Shutdown: on fault",
    769: "Shutdown: on command",
    770: "Shutdown: OVGR",
    771: "Shutdown: communication interrupted",
    772: "Shutdown: power limited",
    773: "Shutdown: manual startup required",
    774: "Shutdown: DC switch disconnected",
    1025: "Grid scheduling: cosPsi-P curve",
    1026: "Grid scheduling: Q-U curve",
    1280: "Ready for terminal test",
    1281: "Terminal testing",
    1536: "Inspection in progress",
    1792: "AFCI self-check",
    2048: "I-V scanning",
    2304: "DC input detection",
    40960: "Standby: no irradiation",
    45056: "Communication interrupted (written by SmartLogger)",
    49152: "Loading... (written by SmartLogger)",
}

PLANT_HEALTH_STATE_DESCRIPTIONS: dict[int, str] = {
    1: "Disconnected",
    2: "Faulty",
    3: "Healthy",
}

ALARM_SEVERITY_DESCRIPTIONS: dict[int, str] = {
    1: "Critical",
    2: "Major",
    3: "Minor",
    4: "Warning",
}


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _extract_status(data_item_map: dict) -> tuple:
    """Pulls (online, inverter_state_description) out of a device's
    dataItemMap. Offline devices often return these keys present but set
    to null rather than omitting them, so we check for None explicitly
    rather than just key membership."""
    online = None
    run_state = data_item_map.get("run_state")
    meter_status = data_item_map.get("meter_status")
    if run_state is not None:
        online = bool(int(run_state))
    elif meter_status is not None:
        online = bool(int(meter_status))

    inverter_state_desc = None
    inverter_state = data_item_map.get("inverter_state")
    if inverter_state is not None:
        code_val = int(inverter_state)
        inverter_state_desc = INVERTER_STATE_DESCRIPTIONS.get(code_val, f"Unknown state ({code_val})")

    return online, inverter_state_desc


class FusionSolarAPIError(Exception):
    """Raised when the API returns success=false with a failCode."""

    def __init__(self, fail_code: Any, message: Optional[str], raw: dict):
        self.fail_code = fail_code
        self.message = message
        self.raw = raw
        super().__init__(f"FusionSolar API error {fail_code}: {message}")


class FusionSolarNBIClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        system_code: str,
        token_cache_path: Optional[str] = None,
        timeout: int = 30,
    ):
        """
        base_url: e.g. "https://sg5.fusionsolar.huawei.com" (no trailing slash,
                  no /thirdData suffix -- that gets appended per-call).
        username: the Northbound API account username (NOT your personal
                  FusionSolar login).
        system_code: the Northbound API account's password, called
                  "systemCode" in the API.
        token_cache_path: where to persist the token + obtained_at timestamp
                  between script runs, so we don't burn the single-session
                  slot by re-logging in every time. Defaults to a file next
                  to this module.
        """
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.system_code = system_code
        self.timeout = timeout

        self.token_cache_path = Path(
            token_cache_path or Path(__file__).with_name(".nbi_token_cache.json")
        )

        self.session = requests.Session()
        self.token: Optional[str] = None
        self.token_obtained_at: float = 0.0

        self._load_cached_token()

    # ------------------------------------------------------------------
    # Token cache handling
    # ------------------------------------------------------------------

    def _load_cached_token(self) -> None:
        if not self.token_cache_path.exists():
            return
        raw = self.token_cache_path.read_text().strip()
        if not raw:
            return  # empty file (e.g. from a prior logout) -- nothing to load, not an error
        try:
            cached = json.loads(raw)
            token = cached.get("token")
            obtained_at = cached.get("obtained_at", 0)
            if token and (time.time() - obtained_at) < TOKEN_VALIDITY_SECONDS:
                self.token = token
                self.token_obtained_at = obtained_at
                logger.info("Loaded cached XSRF-TOKEN (age %.0fs).", time.time() - obtained_at)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read token cache: %s", exc)

    def _save_cached_token(self) -> None:
        try:
            self.token_cache_path.write_text(
                json.dumps({"token": self.token, "obtained_at": self.token_obtained_at})
            )
        except OSError as exc:
            logger.warning("Could not persist token cache: %s", exc)

    def _clear_cached_token(self) -> None:
        self.token = None
        self.token_obtained_at = 0.0
        if self.token_cache_path.exists():
            try:
                self.token_cache_path.unlink()
            except OSError:
                pass

    @property
    def _token_is_fresh(self) -> bool:
        return bool(self.token) and (time.time() - self.token_obtained_at) < TOKEN_VALIDITY_SECONDS

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, force: bool = False) -> str:
        """Logs in and stores the XSRF-TOKEN. Reuses a cached, still-valid
        token unless force=True (since re-login invalidates the existing
        session for this account)."""
        if self._token_is_fresh and not force:
            logger.info("Reusing existing token, skipping login.")
            return self.token  # type: ignore[return-value]

        url = f"{self.base_url}/thirdData/login"
        payload = {"userName": self.username, "systemCode": self.system_code}

        resp = self.session.post(url, json=payload, timeout=self.timeout)
        body = self._parse_body(resp)

        if not body.get("success"):
            raise FusionSolarAPIError(body.get("failCode"), body.get("message"), body)

        token = self._extract_token(resp)
        if not token:
            raise FusionSolarAPIError(
                "no_token",
                "Login reported success but no XSRF-TOKEN was found in the "
                "response headers/cookies. Dump resp.headers and resp.cookies "
                "to inspect.",
                body,
            )

        self.token = token
        self.token_obtained_at = time.time()
        self._save_cached_token()
        logger.info("Login successful, token cached.")
        return token

    @staticmethod
    def _extract_token(resp: requests.Response) -> Optional[str]:
        """The spec documents the token as returned 'in the response header'
        as XSRF-TOKEN. In practice Huawei's implementation has shipped this
        both as a direct response header and as a Set-Cookie. Check both."""
        # Direct header (case-insensitive lookup via requests' header dict)
        for header_name in ("xsrf-token", "XSRF-TOKEN"):
            value = resp.headers.get(header_name)
            if value:
                return value

        # Cookie jar (requests parses Set-Cookie automatically)
        cookie_token = resp.cookies.get("XSRF-TOKEN")
        if cookie_token:
            return cookie_token

        # Fallback: scan raw Set-Cookie headers in case of casing/formatting quirks
        set_cookie_headers = resp.raw.headers.get_all("Set-Cookie") if resp.raw else None
        if set_cookie_headers:
            for raw_cookie in set_cookie_headers:
                if "XSRF-TOKEN" in raw_cookie.upper():
                    # crude parse: XSRF-TOKEN=value; ...
                    for part in raw_cookie.split(";"):
                        if "=" in part and "XSRF-TOKEN" in part.upper():
                            return part.split("=", 1)[1].strip()
        return None

    def logout(self) -> None:
        if not self.token:
            return
        url = f"{self.base_url}/thirdData/logout"
        try:
            resp = self.session.post(
                url, json={"xsrfToken": self.token}, timeout=self.timeout
            )
            body = self._parse_body(resp)
            if body.get("success"):
                logger.info("Logout successful.")
            else:
                logger.warning("Logout returned failure: %s", body)
        finally:
            self._clear_cached_token()

    # ------------------------------------------------------------------
    # Core request handling
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_body(resp: requests.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            return {"success": False, "failCode": resp.status_code, "message": resp.text}

    def _post(self, path: str, payload: dict, _retried_relogin: bool = False, _rate_limit_retries: int = 0) -> dict:
        if not self.token:
            self.login()

        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, */*",
            "XSRF-TOKEN": self.token,
        }

        resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        body = self._parse_body(resp)

        if body.get("success"):
            return body

        fail_code = body.get("failCode")

        # 305: session expired / not logged in -> re-login once, then retry
        if fail_code in (305, "305") and not _retried_relogin:
            logger.info("Got failCode 305 (not online), re-logging in and retrying once.")
            self.login(force=True)
            return self._post(path, payload, _retried_relogin=True, _rate_limit_retries=_rate_limit_retries)

        # 407 / 429: rate limited. Note that most basic-API limits are
        # measured per ACCOUNT over a 5-30 minute window (see module
        # docstring / flow control table), so a short backoff genuinely
        # cannot fix this if you're calling the same endpoint once per
        # plant in a loop -- that's a call-pattern problem, not a timing
        # one. We retry a couple of times with modest backoff to absorb
        # transient system-level throttling (failCode 429), but if you're
        # hitting this repeatedly, batch your calls (see get_fleet_snapshot)
        # instead of looping per plant/device.
        if fail_code in (407, "407", 429, "429") and _rate_limit_retries < 2:
            wait_seconds = 5 * (_rate_limit_retries + 1)
            logger.warning(
                "Rate limited (failCode %s). Waiting %ds before retry %d/2.",
                fail_code, wait_seconds, _rate_limit_retries + 1,
            )
            time.sleep(wait_seconds)
            return self._post(
                path, payload, _retried_relogin=_retried_relogin, _rate_limit_retries=_rate_limit_retries + 1
            )

        raise FusionSolarAPIError(fail_code, body.get("message"), body)

    # ------------------------------------------------------------------
    # Basic APIs
    # ------------------------------------------------------------------

    def get_plant_list(
        self,
        page_no: int = 1,
        grid_connected_start_time: Optional[int] = None,
        grid_connected_end_time: Optional[int] = None,
    ) -> dict:
        """POST /thirdData/stations -- up to 100 plants per page."""
        payload: dict[str, Any] = {"pageNo": page_no}
        if grid_connected_start_time is not None:
            payload["gridConnectedStartTime"] = grid_connected_start_time
        if grid_connected_end_time is not None:
            payload["gridConnectedEndTime"] = grid_connected_end_time
        return self._post("/thirdData/stations", payload)

    def get_all_plants(self) -> list[dict]:
        """Convenience wrapper that pages through the full plant list."""
        all_plants: list[dict] = []
        page_no = 1
        while True:
            body = self.get_plant_list(page_no=page_no)
            data = body.get("data") or {}
            all_plants.extend(data.get("list") or [])
            page_count = data.get("pageCount") or 1
            if page_no >= page_count:
                break
            page_no += 1
        return all_plants

    def get_device_list(self, station_codes: list[str]) -> dict:
        """POST /thirdData/getDevList -- max 100 plants' worth of devices per call."""
        if len(station_codes) > MAX_PLANTS_PER_CALL:
            raise ValueError(f"Max {MAX_PLANTS_PER_CALL} plants per call.")
        payload = {"stationCodes": ",".join(station_codes)}
        return self._post("/thirdData/getDevList", payload)

    def get_real_time_plant_data(self, station_codes: list[str]) -> dict:
        """POST /thirdData/getStationRealKpi -- max 100 plants per call."""
        if len(station_codes) > MAX_PLANTS_PER_CALL:
            raise ValueError(f"Max {MAX_PLANTS_PER_CALL} plants per call.")
        payload = {"stationCodes": ",".join(station_codes)}
        return self._post("/thirdData/getStationRealKpi", payload)

    def get_real_time_device_data(
        self, dev_ids: list, dev_type_id: int, sns: Optional[list] = None
    ) -> dict:
        """POST /thirdData/getDevRealKpi -- real-time KPIs for devices of a
        SINGLE device type (devTypeId is mandatory and applies to the whole
        call -- you cannot mix e.g. inverters and power sensors in one call).
        Max 100 devices of that type per call.
        """
        if len(dev_ids) > MAX_DEVICES_PER_CALL:
            raise ValueError(f"Max {MAX_DEVICES_PER_CALL} devices of the same type per call.")
        payload: dict[str, Any] = {"devTypeId": dev_type_id}
        if dev_ids:
            payload["devIds"] = ",".join(str(d) for d in dev_ids)
        elif sns:
            payload["sns"] = ",".join(sns)
        else:
            raise ValueError("Must supply dev_ids or sns.")
        return self._post("/thirdData/getDevRealKpi", payload)

    def get_active_alarms(
        self,
        station_codes: Optional[list] = None,
        sns: Optional[list] = None,
        begin_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
        language: str = "en_US",
        levels: Optional[list] = None,
        dev_types: Optional[list] = None,
    ) -> dict:
        """POST /thirdData/getAlarmList -- active (unresolved) alarms.
        Either station_codes or sns is required; begin/end time window is
        mandatory. Defaults to the last 24 hours if no window is given."""
        if not station_codes and not sns:
            raise ValueError("Must supply station_codes or sns.")
        now_ms = int(time.time() * 1000)
        if end_time_ms is None:
            end_time_ms = now_ms
        if begin_time_ms is None:
            begin_time_ms = end_time_ms - 24 * 60 * 60 * 1000

        payload: dict[str, Any] = {
            "beginTime": begin_time_ms,
            "endTime": end_time_ms,
            "language": language,
        }
        if station_codes:
            payload["stationCodes"] = ",".join(station_codes)
        if sns:
            payload["sns"] = ",".join(sns)
        if levels:
            payload["levels"] = ",".join(str(l) for l in levels)
        if dev_types:
            payload["devTypes"] = ",".join(str(d) for d in dev_types)
        return self._post("/thirdData/getAlarmList", payload)

    # ------------------------------------------------------------------
    # High-level snapshot: combines device list + real-time device data +
    # real-time plant data + active alarms into one readable structure.
    # ------------------------------------------------------------------

    def get_fleet_snapshot(
        self, station_codes: Optional[list] = None, alarm_lookback_hours: int = 24
    ) -> dict:
        """Same data as get_plant_snapshot, but for MULTIPLE plants at once,
        batched into a small fixed number of API calls.

        This exists because FusionSolar's flow control is enforced per
        ACCOUNT, not per plant -- e.g. Real-Time Plant Data allows only
        Roundup(total_plants/100) calls every 5 minutes. For an account
        with under 100 plants, that's exactly ONE call every 5 minutes,
        not one per plant. Calling get_plant_snapshot() in a loop burns
        that entire budget on the first plant and 407s on the rest.
        This method makes ~4-5 calls total (device list, one real-time
        device call per device type present, one plant-data call, one
        alarms call) no matter how many plants you pass in, as long as
        you're under the 100-plants-per-call ceiling.

        Returns: {stationCode: snapshot_dict, ...} -- same per-plant shape
        as get_plant_snapshot()'s return value.
        """
        if station_codes is None:
            station_codes = [p["plantCode"] for p in self.get_all_plants()]

        # --- device list across all plants ---------------------------
        all_devices: list[dict] = []
        for chunk in _chunked(station_codes, MAX_PLANTS_PER_CALL):
            resp = self.get_device_list(chunk)
            all_devices.extend(resp.get("data") or [])

        devices_by_type: dict[int, list[dict]] = {}
        for dev in all_devices:
            devices_by_type.setdefault(dev["devTypeId"], []).append(dev)

        # working accumulators, one per plant
        snapshots: dict[str, dict] = {
            code: {
                "stationCode": code,
                "plant_health": "Unknown",
                "pv_power_kw": None,
                "grid_power_kw": None,
                "battery_power_kw": None,
                "load_estimate_kw": None,
                "devices": [],
                "active_alarms": [],
                "_pv_sum": 0.0,
                "_grid_sum": 0.0,
                "_batt_sum": 0.0,
                "_any_pv": False,
                "_any_grid": False,
                "_any_batt": False,
            }
            for code in station_codes
        }

        # --- real-time device data, ONE call per device type across
        #     ALL plants (chunked only if a single type exceeds 100
        #     devices fleet-wide) ----------------------------------------
        for dev_type_id, dev_list in devices_by_type.items():
            type_info = DEVICE_TYPE_REGISTRY.get(dev_type_id)
            if not type_info:
                for dev in dev_list:
                    snap = snapshots.get(dev.get("stationCode"))
                    if snap is not None:
                        snap["devices"].append(
                            {
                                "devName": dev["devName"],
                                "devTypeId": dev_type_id,
                                "label": "Unmonitored device type",
                                "status": "n/a",
                                "raw": None,
                            }
                        )
                continue

            id_to_dev = {dev["id"]: dev for dev in dev_list}
            for chunk in _chunked(list(id_to_dev.keys()), MAX_DEVICES_PER_CALL):
                resp = self.get_real_time_device_data(dev_ids=chunk, dev_type_id=dev_type_id)
                for entry in resp.get("data") or []:
                    dev_meta = id_to_dev.get(entry.get("devId"), {})
                    snap = snapshots.get(dev_meta.get("stationCode"))
                    if snap is None:
                        continue
                    data_item_map = entry.get("dataItemMap") or {}

                    power_raw = data_item_map.get(type_info["power_key"])
                    power_kw = None
                    if power_raw is not None:
                        power_kw = float(power_raw)
                        if type_info["power_unit"] == "W":
                            power_kw /= 1000.0

                    online, inverter_state_desc = _extract_status(data_item_map)

                    snap["devices"].append(
                        {
                            "devName": dev_meta.get("devName"),
                            "devTypeId": dev_type_id,
                            "label": type_info["label"],
                            "category": type_info["category"],
                            "power_kw": power_kw,
                            "online": online,
                            "inverter_state": inverter_state_desc,
                            "raw": data_item_map,
                        }
                    )

                    if power_kw is not None:
                        if type_info["category"] == "pv":
                            snap["_pv_sum"] += power_kw
                            snap["_any_pv"] = True
                        elif type_info["category"] == "grid":
                            snap["_grid_sum"] += power_kw
                            snap["_any_grid"] = True
                        elif type_info["category"] == "battery":
                            snap["_batt_sum"] += power_kw
                            snap["_any_batt"] = True

        # --- plant-level health, ONE call across all plants ------------
        for chunk in _chunked(station_codes, MAX_PLANTS_PER_CALL):
            resp = self.get_real_time_plant_data(chunk)
            for row in resp.get("data") or []:
                snap = snapshots.get(row.get("stationCode"))
                if snap is None:
                    continue
                health_code = row.get("dataItemMap", {}).get("real_health_state")
                if health_code is not None:
                    snap["plant_health"] = PLANT_HEALTH_STATE_DESCRIPTIONS.get(int(health_code), "Unknown")

        # --- active alarms, ONE call across all plants ------------------
        now_ms = int(time.time() * 1000)
        begin_ms = now_ms - alarm_lookback_hours * 60 * 60 * 1000
        for chunk in _chunked(station_codes, MAX_PLANTS_PER_CALL):
            try:
                resp = self.get_active_alarms(station_codes=chunk, begin_time_ms=begin_ms, end_time_ms=now_ms)
                for alarm in resp.get("data") or []:
                    if alarm.get("status") == 1:
                        snap = snapshots.get(alarm.get("stationCode"))
                        if snap is not None:
                            snap["active_alarms"].append(alarm)
            except FusionSolarAPIError as exc:
                logger.warning("Could not fetch alarms for plant chunk: %s", exc)

        # --- finalize derived fields, drop internal accumulators -------
        for snap in snapshots.values():
            if snap["_any_pv"]:
                snap["pv_power_kw"] = round(snap["_pv_sum"], 3)
            if snap["_any_grid"]:
                snap["grid_power_kw"] = round(snap["_grid_sum"], 3)
            if snap["_any_batt"]:
                snap["battery_power_kw"] = round(snap["_batt_sum"], 3)
            if snap["_any_pv"] and snap["_any_grid"]:
                # Energy balance with the confirmed sign convention:
                # grid_power_kw is NEGATIVE when importing from the grid,
                # POSITIVE when exporting surplus to the grid. So load is
                # PV minus that signed grid value (subtracting a negative
                # import value correctly ADDS it back into load), minus
                # net battery charge.
                snap["load_estimate_kw"] = round(snap["_pv_sum"] - snap["_grid_sum"] - snap["_batt_sum"], 3)
            for internal_key in ("_pv_sum", "_grid_sum", "_batt_sum", "_any_pv", "_any_grid", "_any_batt"):
                snap.pop(internal_key, None)

        return snapshots

    def get_plant_snapshot(self, station_code: str, alarm_lookback_hours: int = 24) -> dict:
        """Builds a real-time status snapshot for ONE plant. If you're
        checking more than one plant, use get_fleet_snapshot() instead --
        calling this in a loop across plants will burn through FusionSolar's
        per-account (not per-plant) rate limits almost immediately.

        Covers PV production
        power, grid power flow, an estimated load figure, per-device
        online/offline state, and any active alarms.

        NOTE on the load estimate: the Northbound API has no single
        "instantaneous load" field, so we derive it from the basic energy
        balance: load = PV - grid_power - battery_charge, where
        grid_power_kw is NEGATIVE while importing from the grid and
        POSITIVE while exporting surplus (confirmed against live data --
        see the power sensor / grid meter readings). Still treat this as
        a derived estimate, not a separately-metered ground truth.
        """
        devices = self.get_device_list([station_code])
        devices_by_type: dict[int, list[dict]] = {}
        for dev in devices.get("data") or []:
            devices_by_type.setdefault(dev["devTypeId"], []).append(dev)

        device_results: list[dict] = []
        pv_power_kw = 0.0
        grid_power_kw = 0.0
        battery_power_kw = 0.0
        any_pv_data = any_grid_data = any_battery_data = False

        for dev_type_id, dev_list in devices_by_type.items():
            type_info = DEVICE_TYPE_REGISTRY.get(dev_type_id)
            if not type_info:
                # Communication-only devices (Dongle, SmartLogger, etc.) --
                # no real-time power/status KPIs documented for these.
                for dev in dev_list:
                    device_results.append(
                        {
                            "devName": dev["devName"],
                            "devTypeId": dev_type_id,
                            "label": "Unmonitored device type",
                            "status": "n/a",
                            "raw": None,
                        }
                    )
                continue

            id_to_dev = {dev["id"]: dev for dev in dev_list}
            for chunk in _chunked(list(id_to_dev.keys()), MAX_DEVICES_PER_CALL):
                resp = self.get_real_time_device_data(dev_ids=chunk, dev_type_id=dev_type_id)
                for entry in resp.get("data") or []:
                    dev_id = entry.get("devId")
                    dev_meta = id_to_dev.get(dev_id, {})
                    data_item_map = entry.get("dataItemMap") or {}

                    power_raw = data_item_map.get(type_info["power_key"])
                    power_kw = None
                    if power_raw is not None:
                        power_kw = float(power_raw)
                        if type_info["power_unit"] == "W":
                            power_kw /= 1000.0

                    # Online/offline: run_state (0/1) covers inverters,
                    # power sensors, and batteries. meter_status is the
                    # power-sensor-specific equivalent if run_state is absent.
                    online, inverter_state_desc = _extract_status(data_item_map)

                    device_results.append(
                        {
                            "devName": dev_meta.get("devName"),
                            "devTypeId": dev_type_id,
                            "label": type_info["label"],
                            "category": type_info["category"],
                            "power_kw": power_kw,
                            "online": online,
                            "inverter_state": inverter_state_desc,
                            "raw": data_item_map,
                        }
                    )

                    if power_kw is not None:
                        if type_info["category"] == "pv":
                            pv_power_kw += power_kw
                            any_pv_data = True
                        elif type_info["category"] == "grid":
                            grid_power_kw += power_kw
                            any_grid_data = True
                        elif type_info["category"] == "battery":
                            battery_power_kw += power_kw
                            any_battery_data = True

        # Plant-level health (separate, cheaper endpoint -- gives a
        # connected/faulty/healthy read independent of the device breakdown)
        plant_health_code = None
        try:
            rt_plant = self.get_real_time_plant_data([station_code])
            plant_rows = rt_plant.get("data") or []
            if plant_rows:
                plant_health_code = int(plant_rows[0]["dataItemMap"].get("real_health_state"))
        except (FusionSolarAPIError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Could not fetch plant health state: %s", exc)

        # Load estimate: only meaningful if we actually have PV and grid
        # readings. Confirmed sign convention: grid_power_kw is NEGATIVE
        # when importing from the grid, POSITIVE when exporting surplus.
        # Load = PV - grid (subtracting a negative import value adds it
        # back into load) - net battery charge.
        load_estimate_kw = None
        if any_pv_data and any_grid_data:
            load_estimate_kw = pv_power_kw - grid_power_kw - battery_power_kw

        # Active alarms for this plant
        alarms: list[dict] = []
        try:
            now_ms = int(time.time() * 1000)
            begin_ms = now_ms - alarm_lookback_hours * 60 * 60 * 1000
            alarm_resp = self.get_active_alarms(
                station_codes=[station_code], begin_time_ms=begin_ms, end_time_ms=now_ms
            )
            alarms = [a for a in (alarm_resp.get("data") or []) if a.get("status") == 1]
        except FusionSolarAPIError as exc:
            logger.warning("Could not fetch alarms for %s: %s", station_code, exc)

        return {
            "stationCode": station_code,
            "plant_health": PLANT_HEALTH_STATE_DESCRIPTIONS.get(plant_health_code, "Unknown"),
            "pv_power_kw": round(pv_power_kw, 3) if any_pv_data else None,
            "grid_power_kw": round(grid_power_kw, 3) if any_grid_data else None,
            "battery_power_kw": round(battery_power_kw, 3) if any_battery_data else None,
            "load_estimate_kw": round(load_estimate_kw, 3) if load_estimate_kw is not None else None,
            "devices": device_results,
            "active_alarms": alarms,
        }

    # ------------------------------------------------------------------
    # Context manager convenience
    # ------------------------------------------------------------------

    def __enter__(self) -> "FusionSolarNBIClient":
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Deliberately do NOT log out here for normal short-lived scripts --
        # logging out invalidates the token immediately, which is wasteful
        # if you're going to run this script again in a few minutes and
        # could otherwise reuse the cached token. Call client.logout()
        # explicitly if you really want to end the session.
        pass


def build_client_from_env() -> FusionSolarNBIClient:
    """Reads FUSIONSOLAR_BASE_URL, FUSIONSOLAR_USERNAME, FUSIONSOLAR_SYSTEM_CODE
    from the environment (e.g. loaded via a .env file) and builds a client."""
    base_url = os.environ["FUSIONSOLAR_BASE_URL"]
    username = os.environ["FUSIONSOLAR_USERNAME"]
    system_code = os.environ["FUSIONSOLAR_SYSTEM_CODE"]
    return FusionSolarNBIClient(base_url=base_url, username=username, system_code=system_code)
