---
title: OnePlay Subscription Tracker
emoji: ⚡
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
---

# ⚡ OnePlay Live Subscription Availability Tracker

A lightweight, zero-maintenance background tracker that continuously monitors [OnePlay](https://www.oneplay.in) subscription plan availability via their internal REST API and immediately dispatches real-time push notifications (Telegram / Discord) when a target tier comes back in stock.

---

## 🌟 Key Features & Specifications

- **Minimalist & Zero Dependencies**: Built purely with the Python standard library (`urllib.request`, `json`, `ssl`). No `pip install` required.
- **Ultra-Low Memory Footprint**: Operates with **< 20MB RAM** (well below the 50MB ceiling).
- **Bandwidth-Optimized (HTTP ETags)**: Caches the `ETag` response header and sends `If-None-Match`. On `304 Not Modified`, the evaluation loop terminates immediately with **0 body bytes transferred**.
- **Anti-Bot Jitter**: Polls every 20–30s with randomized jitter ($\pm 3$ seconds) to avoid rigid bot signatures.
- **Resilient Polling**:
  - **429 (Rate Limited)** / **5xx (Server Error)**: Automatic exponential backoff (60s).
  - **Network Timeouts / DNS Failures**: Silent retry after 10s without crashing.
- **Edge-Triggered State Machine**: Alerts only on the transition: `SOLD_OUT` $\rightarrow$ `AVAILABLE`.
- **Alert Debounce / Lock**: Once an alert fires for a plan, locks repeat alerts for 10 minutes (or until the plan flips back to `SOLD_OUT`) to eliminate notification spam.
- **Dual Notification Channels**:
  - **Telegram Bot API**: Formatted HTML message with plan details and direct checkout link.
  - **Discord Webhook**: Rich embed with green stock badges, price, duration, and direct link.

---

## 📁 Project Structure

```text
oneplay-tracker/
├── tracker.py               # Main single-file tracker & CLI
├── test_tracker.py          # Unit & integration test suite
├── .env.example             # Configuration template
├── Dockerfile               # Alpine-based ultra-lightweight container (~45MB)
├── docker-compose.yml       # Docker compose service definition
├── oneplay-tracker.service  # Linux systemd service unit template
├── .gitignore
└── README.md                # Documentation
```

---

## 🚀 Quick Start

### 1. Local CLI Execution

Prerequisites: Python 3.8+ installed (no pip packages needed).

```bash
# Clone or navigate into directory
cd oneplay-tracker

# Copy sample configuration
cp .env.example .env

# Run a quick check to view all live plans in the console:
python tracker.py --once
```

### 2. Configure Notifications (`.env`)

Edit your `.env` file to customize target plans and notification endpoints:

```env
# Plans to watch (comma-separated, case-insensitive, or * for all)
TARGET_PLANS="Prime, Enthusiast"

# Polling interval and jitter
POLL_INTERVAL_SECONDS=25
POLL_JITTER_SECONDS=3
DEBOUNCE_MINUTES=10

# Telegram Bot API (Optional)
TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
TELEGRAM_CHAT_ID="987654321"

# Discord Webhook (Optional)
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

### 3. Verify Setup with a Test Alert

Verify that your Telegram or Discord credentials work before running in the background:

```bash
python tracker.py --test-alert
```

### 4. Run Continuously

```bash
# Standard background execution
python tracker.py

# Or dry-run mode (tracks API without sending real notifications)
python tracker.py --dry-run

# Or verbose debug logging
python tracker.py --debug
```

---

## 🐳 Docker Deployment

A lightweight Dockerfile based on `python:3.12-alpine` is provided. The container runs as an unprivileged user and consumes ~15MB RAM.

```bash
# Build and start in detached mode
docker compose up -d

# View live streaming logs
docker compose logs -f

# Stop the container
docker compose down
```

---

## 🐧 Linux systemd Service Deployment

To run as a system daemon on Ubuntu / Debian / CentOS / Raspberry Pi:

1. Copy the project files to `/opt/oneplay-tracker`:
   ```bash
   sudo mkdir -p /opt/oneplay-tracker
   sudo cp tracker.py .env /opt/oneplay-tracker/
   ```

2. Copy the service unit:
   ```bash
   sudo cp oneplay-tracker.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

3. Enable and start:
   ```bash
   sudo systemctl enable --now oneplay-tracker
   ```

4. Monitor logs:
   ```bash
   journalctl -u oneplay-tracker -f
   ```

---

## 🔧 CLI Commands & Flags

| Flag | Description |
| :--- | :--- |
| `--once` | Performs a single poll cycle, displays a formatted table of all plans and stock status, and exits. |
| `--test-alert` | Sends a synthetic drop alert to verify Telegram/Discord configuration and exits. |
| `--dry-run` | Runs continuous polling but simulates notifications in the log without sending network webhooks. |
| `--debug` | Enables verbose debug logs (e.g. ETag 304 responses, request latency). |
| `--env-file PATH`| Path to custom `.env` file (default: `.env`). |

---

## 🧪 Testing

Run the included unit test suite to verify availability logic, state machine transitions, ETag caching, and payload generation:

```bash
python -m unittest test_tracker.py
```

---

## 📡 API Contract & Availability Rules

- **Endpoint**: `GET https://rendermix.oneplay.in/v2/subscriptions/available_plans`
- **Headers**: `User-Agent`, `Origin: https://www.oneplay.in`, `Referer: https://www.oneplay.in/`, `Accept: application/json`
- **Evaluation Criteria**:
  $$\text{AVAILABLE} \iff (\text{is\_sold\_out} == \text{False}) \land (\text{is\_live\_for_purchase} == \text{True})$$
  *(Note: Handles nested `plan_config.is_sold_out`, root-level `is_sold_out`, and string/boolean variations).*
- **Checkout URL**: `https://www.oneplay.in/subscription.html`
