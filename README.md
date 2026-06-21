# FusionSolar Northbound API → Telegram Fleet Monitor

Automated real-time monitoring for a fleet of Huawei FusionSolar (SmartPVMS) solar plants, using Huawei's official **Northbound API** rather than scraping the web portal. Pushes scheduled fleet status reports — PV production, grid import/export, estimated load, device online/offline state, and active alarms — to a Telegram chat.

Built against the **SmartPVMS Northbound API Reference (v25.1.0)**.

---

## Why this exists

The FusionSolar web portal (SmartPVMS / SG5) has no official way to pull data programmatically without reverse-engineering its internal login flow. This project instead uses Huawei's documented **Northbound (NBI) API**, which is the supported integration path for third-party monitoring tools.

## What it does

- Logs into the Northbound API and caches the session token (the API only allows **one active session per account**, so re-logging in unnecessarily invalidates your other tools).
- Pulls the full plant list, device list, and real-time KPIs for an entire fleet in a small, fixed number of batched API calls — regardless of fleet size — to stay within Huawei's account-wide rate limits.
- Computes, per plant:
  - **PV production power** (sum of inverter output)
  - **Grid power flow** (negative = importing, positive = exporting — confirmed against live data)
  - **Estimated load** (`PV − grid − battery`, derived since the API has no direct "load" field)
  - **Device online/offline status** and inverter operating state
  - **Active alarms** (severity, device, alarm name)
- Sends a formatted report to a **Telegram bot** on a configurable schedule.

## What it does *not* do (yet)

- No historical/trend data pulls (only real-time snapshots).
- No automatic alerting thresholds beyond surfacing whatever FusionSolar already flags as an active alarm.
- No web dashboard — this is a backend/notification tool only.

---

## Project structure

| File | Purpose |
|---|---|
| `client.py` | Core Northbound API client: auth, token caching, plant/device list, real-time data, alarms, fleet snapshot logic. |
| `telegram_notify.py` | Telegram Bot API wrapper + message formatting for fleet reports. |
| `realtime_status.py` | On-demand CLI: run manually to print (and optionally send) a fleet status report right now. |
| `scheduler.py` | Long-running process that fires the report automatically on a configurable schedule. |
| `mock_logic_test.py` | Offline test suite (mocked HTTP) covering auth, retries, rate-limit handling, and snapshot math — no live API needed. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Template for required credentials/config — copy to `.env` and fill in. |

---

## Setup

### 1. Get Northbound API credentials

These are **not** your personal FusionSolar login. A company administrator must create a dedicated API account:

> FusionSolar portal → **System** → **Company Management** → **Company Management** → **Northbound Management** → **Add**

Bind it to the plants you want to monitor and enable the Basic APIs you need (plant list, device list, real-time data, alarms).

### 2. Get a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow the prompts → save the bot token.
2. Send your new bot any message (e.g. "hi").
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id": ...}` — that's your chat ID. (For a group: add the bot to the group, message there, then check the same URL — group IDs are negative numbers.)

### 3. Configure environment

```bash
cp .env.example .env
```

Fill in:

```env
FUSIONSOLAR_BASE_URL=https://<your-region>.fusionsolar.huawei.com
FUSIONSOLAR_USERNAME=your_northbound_api_username
FUSIONSOLAR_SYSTEM_CODE=your_northbound_api_password

TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=987654321
```

`FUSIONSOLAR_BASE_URL` should match the regional subdomain you use to log into the FusionSolar web portal (e.g. `intl`, `sg5`, etc.) — no trailing slash, no `/thirdData` suffix.

### 4. Install and test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python realtime_status.py --test-telegram   # confirms Telegram is wired up correctly
python realtime_status.py                   # prints a one-off fleet report to the terminal
python realtime_status.py --notify          # same, plus sends it to Telegram
python scheduler.py --run-once              # fires one scheduled-style report immediately
```

### 5. Offline tests (no live API calls)

```bash
python mock_logic_test.py
```

Validates token caching/reuse, the re-login-on-expiry path, rate-limit retry behavior, fleet-batching call counts, and the PV/grid/load math — all against mocked HTTP responses, safe to run anytime.

---

## Running the scheduler

`scheduler.py` runs continuously and fires a report on a schedule defined in the `SCHEDULE_CONFIG` block at the top of the file:

```python
SCHEDULE_CONFIG = [
    {"name": "weekday_every_30min", "day_of_week": "mon-fri", "hour": "*", "minute": "*/30"},
    {"name": "weekend_hourly",      "day_of_week": "sat,sun", "hour": "*", "minute": "0"},
]
```

Edit this block to change cadence — no other code needs to change. `day_of_week`, `hour`, and `minute` follow standard cron syntax (via [APScheduler](https://apscheduler.readthedocs.io/)).

```bash
python scheduler.py
```

### Running it permanently (e.g. on a Raspberry Pi)

For always-on deployment, run it as a `systemd` service so it survives reboots and restarts automatically on failure:

```ini
# /etc/systemd/system/fusionsolar.service
[Unit]
Description=FusionSolar fleet scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/Northbound-FusionSolar
ExecStart=/home/YOUR_USER/Northbound-FusionSolar/.venv/bin/python scheduler.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable fusionsolar
sudo systemctl start fusionsolar

# check on it
sudo systemctl status fusionsolar
journalctl -u fusionsolar -f
```

---

## Important API constraints (from the NBI Reference)

- **One session per account.** Logging in invalidates any other active session for that account — the client caches and reuses the token (valid ~30 min, sliding) instead of logging in on every run.
- **Rate limits are per account, not per plant.** E.g. real-time plant data allows roughly `Roundup(plant_count / 100)` calls per 5 minutes — for most accounts that's a single call covering the *entire* fleet, not one call per plant. All fleet operations in this project are batched accordingly.
- **Max 100 plants/devices per call** on the relevant endpoints; the client chunks automatically if you exceed that.
- Real-time device data requires a separate call **per device type** (inverters, power sensors/grid meters, batteries can't be mixed in one call).

## Sign conventions (confirmed against live data)

- **Grid power**: negative = importing from grid, positive = exporting to grid.
- **Load estimate**: `PV power − grid power − battery charge power` (derived; the API has no direct instantaneous load field).
- **Battery power** (where present): assumed positive = charging, negative = discharging — not yet verified against a live battery-equipped site.

---

## Security notes

- `.env` holds live credentials and **must not be committed**. It's covered by `.gitignore` — double check before pushing.
- If a bot token or API credential is ever accidentally exposed (commit history, screenshot, etc.), rotate it immediately — Telegram via BotFather (`/revoke` or `/token`), FusionSolar via your company admin resetting the Northbound account password.

