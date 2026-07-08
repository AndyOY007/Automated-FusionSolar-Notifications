# Architecture

A code-level walkthrough of how this project works internally. For setup/usage instructions, see [README.md](README.md).

---

## High-level flow

```
scheduler.py (or realtime_status.py)
        │
        ▼
client.py  ──── FusionSolarNBIClient ────►  Huawei Northbound API
        │                                    (login, plant/device list,
        │                                     real-time data, alarms)
        ▼
telegram_notify.py ──── format + send ────►  Telegram Bot API
```

`client.py` knows nothing about Telegram. `telegram_notify.py` knows nothing about FusionSolar. `realtime_status.py` and `scheduler.py` are the two entry points that wire the two together — one for on-demand runs, one for scheduled runs. This separation means either side can be swapped (e.g. a different notification channel, or a different data source) without touching the other.

---

## `client.py`

The core API client. Everything here maps to a documented endpoint in the SmartPVMS Northbound API Reference (v25.1.0).

### Module-level constants and lookup tables

- **`TOKEN_VALIDITY_SECONDS`** — 30 minutes, matching the spec's documented session token lifetime.
- **`MAX_PLANTS_PER_CALL`, `MAX_DEVICES_PER_CALL`** — both 100, the documented per-call ceiling for plant/device-batched endpoints. Any method that takes a list of codes chunks it against this limit using `_chunked()`.
- **`DEVICE_TYPE_REGISTRY`** — maps Huawei's numeric `devTypeId` (1 = string inverter, 17 = grid meter, 47 = power sensor, 39/41 = battery types, etc.) to how this codebase should treat that device: which category it rolls up into (`pv` / `grid` / `battery`), which field in the API response holds its power reading (`active_power` vs `ch_discharge_power`), and what unit that field is actually returned in (some are kW, some are W — the registry exists specifically so unit conversion happens in exactly one place).
- **`INVERTER_STATE_DESCRIPTIONS`** — Table 5-1 from the spec, mapping numeric `inverter_state` codes (e.g. `512`) to human-readable text ("Grid-connected"). Unrecognized codes fall back to `"Unknown state (N)"` rather than failing.
- **`PLANT_HEALTH_STATE_DESCRIPTIONS`** — `1/2/3 → Disconnected/Faulty/Healthy`.
- **`ALARM_SEVERITY_DESCRIPTIONS`** — `1–4 → Critical/Major/Minor/Warning`.

### Module-level helper functions

**`_chunked(items, size)`** — simple generator that yields successive slices of a list. Used everywhere a station/device list might exceed the 100-per-call ceiling, so a fleet of, say, 250 plants would still work correctly (in 3 chunked calls) without anyone needing to think about it at the call site.

**`_extract_status(data_item_map)`** — pulls `(online: bool|None, inverter_state_description: str|None)` out of a raw API response's `dataItemMap`. This exists as a single shared function because of a real bug encountered during development: offline devices sometimes return `run_state`/`inverter_state` keys *present but set to `null`*, not omitted. Checking `"key" in dict` doesn't catch that — `None` is still "in" the dict — so this function explicitly checks `value is not None` before calling `int()` on it. Both `get_plant_snapshot()` and `get_fleet_snapshot()` call this rather than duplicating the logic, so the null-safety fix only had to happen once.

### `FusionSolarAPIError`

A plain exception carrying `fail_code`, `message`, and the raw response body. Every method that talks to the API raises this on failure rather than returning `None` or a partial dict — callers can `except FusionSolarAPIError` and inspect `.fail_code` to decide what to do (the snapshot methods do exactly this around the alarms call, since a failed alarm fetch shouldn't kill the whole report).

### `FusionSolarNBIClient`

#### Construction & token cache (`__init__`, `_load_cached_token`, `_save_cached_token`, `_clear_cached_token`, `_token_is_fresh`)

The constructor stores connection info and immediately tries to load a previously-cached token from disk (`.nbi_token_cache.json` by default, next to `client.py`). This matters because of a hard constraint in the spec: **an API account can only have one active session at a time** — logging in again invalidates whatever token was issued before. If this client logged in on every script run, it would constantly kick itself out of its own previous session, and any other tool using the same account would break too.

- `_load_cached_token` reads the cache file, and treats both "file doesn't exist" and "file is empty" as normal, silent no-ops (an empty file can legitimately happen after a `logout()` call). It only logs a warning if the file exists, has content, and fails to parse.
- `_token_is_fresh` is a property checking the token exists *and* is younger than `TOKEN_VALIDITY_SECONDS`.
- `_save_cached_token` / `_clear_cached_token` write or delete the cache file as the token's lifecycle changes.

#### `login(force=False)`

If a fresh cached token already exists and `force` isn't set, this returns immediately without making a network call at all — this is the main mechanism that prevents session-stealing across script runs. Otherwise it POSTs to `/thirdData/login` with `userName`/`systemCode`, then calls `_extract_token()` to pull the session token out of the response.

#### `_extract_token(resp)`

The spec says the token comes back as an `XSRF-TOKEN` response header, but real-world API responses aren't always consistent with their own documentation. This method checks, in order: the `XSRF-TOKEN` header directly, the parsed cookie jar, and finally a raw `Set-Cookie` header scan as a last resort — so a small Huawei-side inconsistency in *where* the token shows up doesn't break the whole client.

#### `logout()`

POSTs to `/thirdData/logout`, then clears the local cache regardless of whether the API call itself succeeded (so a failed logout call can't leave a stale, now-probably-invalid token sitting in the cache). Notably, **nothing in this codebase calls `logout()` automatically** — see the `__exit__` note below.

#### `_post(path, payload, ...)` — the shared request engine

Every API call in this client funnels through this one method, which is where all the cross-cutting error handling lives:

- **`failCode 305`** ("not logged in") → triggers exactly one automatic re-login (`force=True`) and retries the original call once. The `_retried_relogin` flag prevents infinite loops if the re-login itself doesn't fix it.
- **`failCode 407` / `429`** (rate limited) → retries up to twice with increasing backoff (5s, then 10s), via the `_rate_limit_retries` counter. This is a real fix for a bug that originally just slept and then raised anyway without retrying. That said, the code is explicit in its comments that backoff *cannot* fix a rate limit that's structural (i.e. looping a per-account-limited call once per plant) — that's what `get_fleet_snapshot()` exists to avoid in the first place.
- Anything else → raises `FusionSolarAPIError` with the API's own fail code and message attached.

#### Basic API wrappers

Thin, mostly 1:1 wrappers around documented endpoints — each just builds the right payload shape and calls `_post`:

- `get_plant_list(page_no, ...)` → `/thirdData/stations`
- `get_all_plants()` → pages through `get_plant_list` automatically until `pageCount` is exhausted, returning one flat list
- `get_device_list(station_codes)` → `/thirdData/getDevList`
- `get_real_time_plant_data(station_codes)` → `/thirdData/getStationRealKpi`
- `get_real_time_device_data(dev_ids, dev_type_id)` → `/thirdData/getDevRealKpi` (note: **one device type per call** — this is a hard API constraint, not a design choice, which is why the snapshot methods loop over `devices_by_type.items()` rather than passing mixed device IDs in one call)
- `get_active_alarms(station_codes, begin_time_ms, end_time_ms, ...)` → `/thirdData/getAlarmList`, defaulting to a 24-hour lookback window if none is given

#### `get_fleet_snapshot(station_codes, alarm_lookback_hours)` — the main high-level method

This is what `scheduler.py` and `realtime_status.py` actually call. It exists specifically to solve a rate-limiting bug discovered during testing: FusionSolar enforces most Basic API limits **per account**, not per plant (e.g. real-time plant data allows roughly `Roundup(plant_count / 100)` calls per 5 minutes — for most accounts, that's *one* call covering the entire fleet). An earlier version of this code called a per-plant snapshot method in a loop, which burned the entire 5-minute budget on the first plant and 407'd on every plant after it.

The fix is structural: regardless of how many plants you pass in, this method makes a small, fixed number of calls:

1. **One device-list call** (chunked only if you exceed 100 plants) — pulls every device across every requested plant in one shot, then buckets them by `devTypeId` in `devices_by_type`.
2. **One real-time-device-data call per distinct device type present** in the fleet (e.g. one call for all string inverters across all 9 plants, one for all power sensors) — not one per plant. Device types not in `DEVICE_TYPE_REGISTRY` (communication-only hardware like dongles/SmartLoggers) are recorded as `"status": "n/a"` placeholders rather than calling an endpoint that doesn't apply to them.
3. **One real-time-plant-data call** (chunked at 100) for plant-level health state.
4. **One active-alarms call** (chunked at 100) for the whole fleet.

Internally it builds a `snapshots` dict keyed by `stationCode`, with private accumulator fields (`_pv_sum`, `_grid_sum`, `_batt_sum`, `_any_pv`, etc. — underscore-prefixed because they're working state, stripped out before the final return) that get added to as each batched response comes back and gets fanned out to the right plant via each device's `stationCode`. At the end, it computes the final rounded power totals and the load estimate per plant, deletes the internal accumulator keys, and returns the clean per-plant dicts.

**The load estimate formula**, and why: the API has no direct "instantaneous load" field anywhere, so it's derived from energy balance:

```
load_estimate_kw = pv_power_kw − grid_power_kw − battery_power_kw
```

This relies on a sign convention for `grid_power_kw` that was *confirmed empirically against live data* during development (not assumed from documentation, which doesn't specify it): **negative means importing from the grid, positive means exporting surplus to the grid.** That's why the formula subtracts rather than adds the grid term — subtracting a negative import value correctly adds that imported power back into the load total. The battery sign (`positive = charging`) is still flagged in code comments as *not yet verified* against a live battery-equipped site, since none of the monitored plants currently have one reporting data.

#### `get_plant_snapshot(station_code, alarm_lookback_hours)` — single-plant version

Same logic and same return shape as `get_fleet_snapshot`, but for exactly one plant, making its own separate calls rather than sharing a batch. Its docstring explicitly warns not to call this in a loop across plants — it's kept in the codebase mainly for cases where you genuinely only care about one specific plant and don't want to pull the whole fleet's data, not as the default fleet-reporting path.

#### `__enter__` / `__exit__`

Allows `with FusionSolarNBIClient(...) as client:` usage — `__enter__` logs in, but `__exit__` deliberately does **not** call `logout()`. Logging out immediately invalidates the token, which would be wasteful for short-lived scripts that might run again a few minutes later and could otherwise reuse the cached token. Call `client.logout()` explicitly if you actually want to end the session (e.g. you're permanently done and want to free up the account for another tool).

#### `build_client_from_env()`

Reads `FUSIONSOLAR_BASE_URL`, `FUSIONSOLAR_USERNAME`, `FUSIONSOLAR_SYSTEM_CODE` from the environment (populated from `.env` by each entry point's own small dotenv loader) and constructs a client. Raises a plain `KeyError` if anything's missing, which both `realtime_status.py` and `scheduler.py` catch and turn into a clear "check your .env" log message.

---

## `telegram_notify.py`

Self-contained Telegram Bot API wrapper plus message formatting. Has no knowledge of FusionSolar's data shapes beyond what it needs to render a snapshot dict into text.

### `TelegramNotifier`

- **`test_connection()`** — sends a fixed "bot is connected" message, returns `True`/`False`, logs the outcome. This is what `realtime_status.py --test-telegram` calls, specifically so you can verify the Telegram half works in isolation from FusionSolar.
- **`send_message(text, parse_mode="HTML")`** — the main send path. Returns `True`/`False` rather than raising, so a Telegram outage doesn't crash the whole reporting job — `scheduler.py`'s job function just logs "report generated but Telegram send failed" and waits for the next scheduled run.
- **`_send_single(text, parse_mode)`** — does the actual HTTP POST to `https://api.telegram.org/bot<token>/sendMessage`, parses the JSON response, and raises a plain `RuntimeError` internally if Telegram reports `"ok": false` (caught by the two public methods above, which is why they can return booleans instead of propagating exceptions).
- **`_split_message(text)`** — Telegram caps a single message at 4096 characters. This splits longer text into sequential chunks sent as separate messages, so a large fleet report doesn't silently get truncated or rejected.

### `build_notifier_from_env()`

Same pattern as `client.py`'s equivalent — reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the environment.

### Formatting functions

- **`format_plant_snapshot(plant_name, snapshot)`** — renders one plant's data as an HTML-formatted block: a health emoji (🟢/🔴/⚪/❓ depending on state), PV/grid/battery/load lines, a flagged list of any offline devices, and an alarm list with severity emoji if any are active.
- **`format_fleet_snapshot(snapshots, plant_names)`** — wraps the whole fleet into one message: a timestamped header with total fleet PV and total active alarm count, a flagged "needs attention" line listing any non-healthy plants by name, then each plant's block from `format_plant_snapshot` concatenated together.

---

## `realtime_status.py`

The on-demand CLI entry point — meant to be run manually, not scheduled.

Flow: load `.env` → build a `FusionSolarNBIClient` → `login()` → `get_all_plants()` (to resolve plant codes to names for display, and to know which plants exist if `--plant` filters weren't given) → call `client.get_fleet_snapshot()` **once**, batched, for whichever plants were requested → print a human-readable report to the terminal → if `--notify` was passed, also build a `TelegramNotifier` and send the same data via `format_fleet_snapshot`.

`--test-telegram` short-circuits all of the above and just calls `notifier.test_connection()`, so you can verify Telegram credentials without touching FusionSolar at all.

`--json` switches the terminal output to raw JSON instead of the formatted report (useful for piping into another tool).

---

## `scheduler.py`

The long-running, always-on entry point — what actually runs continuously (typically as a systemd service on something like a Raspberry Pi).

### `SCHEDULE_CONFIG`

A plain list of dicts at the top of the file, intentionally separated from all the scheduling machinery below it so it's the one block meant to be hand-edited. Each entry becomes one independent APScheduler cron rule (`day_of_week` / `hour` / `minute`, standard cron syntax). The default ships with two rules — every 30 minutes on weekdays, hourly on weekends — but it's just a list, so adding a third rule (e.g. a different cadence for one specific day) means adding one more dict, not changing any logic.

### `run_report_job()`

The actual unit of work that fires on each scheduled trigger: builds a client and a notifier from `.env`, logs in, fetches the plant list, calls `get_fleet_snapshot()` once for the whole fleet, formats it, and sends it. Every failure mode (missing env vars, a `FusionSolarAPIError`, a Telegram send failure) is caught and logged rather than allowed to propagate — a single bad run should never crash the whole long-running process, since the next scheduled run will simply try again on its own.

### `build_scheduler()`

Converts each `SCHEDULE_CONFIG` entry into an APScheduler `CronTrigger` (explicitly pinned to UTC) and registers it against `run_report_job` on a `BlockingScheduler`.

### `main()`

Parses `--run-once` (bypasses the scheduler entirely and just calls `run_report_job()` immediately, for testing the job logic without waiting for the next trigger time) versus the default behavior of starting the blocking scheduler and running forever until interrupted.

---

## `mock_logic_test.py`

An offline test suite using `unittest.mock.patch` to stub out `requests.Session.post`, so the whole client's branching logic can be verified without making real network calls or needing live FusionSolar credentials. Each test constructs a `fake_response()` (a `MagicMock` standing in for a `requests.Response`) with a specific JSON body, then asserts on the client's resulting behavior. Notable cases covered:

- Token extraction from a response header, and that it gets persisted to the cache file.
- That a fresh cached token is reused without making any network call at all.
- That a `failCode 305` triggers exactly one re-login-and-retry, not an infinite loop.
- That `get_all_plants()` correctly pages through multiple `pageCount` pages.
- That an unrecoverable fail code raises `FusionSolarAPIError` with the right code attached.
- That `get_plant_snapshot()` correctly combines an inverter + a power sensor's readings and skips an unmonitored device type (a dongle).
- That `get_active_alarms()` builds the right payload shape.
- That `get_fleet_snapshot()` resolves 2 plants in exactly 5 HTTP calls — the specific assertion that proves the batching fix actually works, since a regression back to per-plant looping would make this number scale with plant count instead of staying fixed.
- That `_post` actually retries on a 407 rather than sleeping and raising anyway (regression test for that specific historical bug).
- That a device reporting `null` for `run_state`/`inverter_state` (rather than omitting the key) doesn't crash the snapshot — regression test for the exact `TypeError: int() argument must be ... not 'NoneType'` bug hit during real-world testing.

Run it any time with `python mock_logic_test.py` — it should always pass regardless of whether you have live credentials configured, and is a fast way to confirm a code change hasn't broken existing behavior before testing against the real API (which is rate-limited and slower to iterate against).
