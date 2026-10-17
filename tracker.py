#!/usr/bin/env python3
"""
OnePlay Live Subscription Availability Tracker
==============================================
A lightweight, zero-maintenance background tracker that continuously monitors
OnePlay subscription plan availability via their internal REST API and
immediately dispatches push notifications (Discord / Telegram) when a target
tier comes back in stock.

Design Philosophy & Resource Profile:
- Zero external dependencies (Python standard library only).
- Ultra-low memory footprint (<20MB RAM, safe for set-and-forget hosting).
- Bandwidth-optimized with HTTP ETags (304 Not Modified = 0 byte transfer).
- Resilient polling with randomized jitter (±3s) and exponential backoff.
- Precise Notification Lifecycle:
  1. Immediate In-Stock Alert on transition (SOLD_OUT -> AVAILABLE).
  2. Optional single one-time backup reminder after N minutes (no repetitive spam).
  3. Immediate Out-of-Stock Alert on transition (AVAILABLE -> SOLD_OUT) so you
     know when the purchasing window has closed.
- Atomic state persistence (state.json) so cloud/container restarts never duplicate alerts.
"""

import argparse
import datetime
import http.server
import json
import logging
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------
DEFAULT_API_URL = "https://rendermix.oneplay.in/v2/subscriptions/available_plans"
DEFAULT_CHECKOUT_URL = "https://www.oneplay.in/subscription.html"
DEFAULT_TARGET_PLANS = "Haven HD, Haven 4K, Enthusiast, Prime, 799, 1099, 1299, 1599"
DEFAULT_POLL_INTERVAL_SECONDS = 25.0
DEFAULT_POLL_JITTER_SECONDS = 3.0
DEFAULT_BACKUP_DELAY_SECONDS = 180.0  # 3 minutes for single safety backup ping
DEFAULT_BACKOFF_SECONDS = 60.0
DEFAULT_RETRY_NETWORK_SECONDS = 10.0
DEFAULT_STATE_FILE = "state.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Status Enum
STATUS_SOLD_OUT = "SOLD_OUT"
STATUS_AVAILABLE = "AVAILABLE"

# Alert Type Enum
ALERT_IN_STOCK = "IN_STOCK"
ALERT_BACKUP = "BACKUP"
ALERT_OUT_OF_STOCK = "OUT_OF_STOCK"
ALERT_TEST = "TEST"


# ---------------------------------------------------------------------------
# Lightweight .env Parser (Zero Third-Party Dependency)
# ---------------------------------------------------------------------------
def load_dotenv(dotenv_path: str = ".env") -> None:
    """Parses .env file and injects keys into os.environ if not already set."""
    if not os.path.exists(dotenv_path):
        return

    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                if key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        logging.warning("Failed to load .env from %s: %s", dotenv_path, e)


# ---------------------------------------------------------------------------
# Configuration Loader
# ---------------------------------------------------------------------------
class TrackerConfig:
    def __init__(self, env_path: str = ".env"):
        load_dotenv(env_path)

        self.api_url: str = os.getenv("ONEPLAY_API_URL", DEFAULT_API_URL)
        self.checkout_url: str = os.getenv("ONEPLAY_CHECKOUT_URL", DEFAULT_CHECKOUT_URL)
        self.user_agent: str = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
        self.state_file: str = os.getenv("STATE_FILE", DEFAULT_STATE_FILE)

        # Health check web port (Required for cloud health checkers like Koyeb)
        self.port: int = int(os.getenv("PORT", "8080"))

        # Target plan names or prices
        raw_targets = os.getenv("TARGET_PLANS", DEFAULT_TARGET_PLANS).strip()
        if raw_targets in ("*", "ALL", "all"):
            self.target_plans: Set[str] = set()
            self.track_all: bool = True
        else:
            self.target_plans = {
                t.strip().lower() for t in raw_targets.split(",") if t.strip()
            }
            self.track_all = False

        # Polling & Timers
        self.poll_interval: float = float(
            os.getenv("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))
        )
        self.poll_jitter: float = float(
            os.getenv("POLL_JITTER_SECONDS", str(DEFAULT_POLL_JITTER_SECONDS))
        )

        # Notification policy
        self.send_backup_notification: bool = (
            os.getenv("SEND_BACKUP_NOTIFICATION", "true").lower() in ("true", "1", "yes")
        )
        self.backup_delay_seconds: float = float(
            os.getenv("BACKUP_DELAY_SECONDS", str(DEFAULT_BACKUP_DELAY_SECONDS))
        )
        self.notify_on_out_of_stock: bool = (
            os.getenv("NOTIFY_ON_OUT_OF_STOCK", "true").lower() in ("true", "1", "yes")
        )

        self.backoff_seconds: float = float(
            os.getenv("BACKOFF_SECONDS", str(DEFAULT_BACKOFF_SECONDS))
        )
        self.network_retry_seconds: float = float(
            os.getenv("NETWORK_RETRY_SECONDS", str(DEFAULT_RETRY_NETWORK_SECONDS))
        )

        # Cold startup behavior
        self.alert_on_startup: bool = (
            os.getenv("ALERT_ON_STARTUP", "false").lower() in ("true", "1", "yes")
        )

        # Notification credentials
        self.discord_webhook_url: Optional[str] = os.getenv("DISCORD_WEBHOOK_URL")
        self.telegram_bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")

        # Logging level
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    def is_target(self, plan: "PlanInfo") -> bool:
        """Checks if plan matches target filter by plan name, category, or price."""
        # Ensure we only track Indian INR tiers
        if plan.currency.upper() != "INR":
            return False

        if self.track_all:
            return True
        name_lower = plan.plan_name.strip().lower()
        if name_lower in self.target_plans:
            return True
        # Match price
        if plan.price is not None and str(plan.price).strip() in self.target_plans:
            return True
        base_val = plan.raw_plan.get("value")
        if base_val is not None and str(base_val).strip() in self.target_plans:
            return True
        return False


# ---------------------------------------------------------------------------
# Plan Model
# ---------------------------------------------------------------------------
class PlanInfo:
    def __init__(
        self,
        plan_id: str,
        plan_name: str,
        category: str,
        price: Any,
        currency: str,
        duration_days: int,
        is_sold_out: bool,
        is_live_for_purchase: bool,
        raw_plan: Dict[str, Any],
    ):
        self.plan_id = plan_id
        self.plan_name = plan_name
        self.category = category
        self.price = price
        self.currency = currency.upper()
        self.duration_days = duration_days
        self.is_sold_out = is_sold_out
        self.is_live_for_purchase = is_live_for_purchase
        self.raw_plan = raw_plan

    @property
    def is_available(self) -> bool:
        """
        Availability Rule per spec:
        is_sold_out: must be false
        is_live_for_purchase: must be true
        """
        return (not self.is_sold_out) and self.is_live_for_purchase

    @property
    def status(self) -> str:
        return STATUS_AVAILABLE if self.is_available else STATUS_SOLD_OUT

    @property
    def unique_key(self) -> str:
        """Unique key for state tracking to differentiate multiple variants."""
        return f"{self.category}:{self.plan_name}:{self.duration_days}d"

    def format_price(self) -> str:
        if self.price is None:
            return "N/A"
        sym = "₹" if self.currency == "INR" else "$"
        try:
            num = float(self.price)
            if num.is_integer():
                return f"{sym}{int(num):,} {self.currency}"
            return f"{sym}{num:,.2f} {self.currency}"
        except (ValueError, TypeError):
            return f"{sym}{self.price} {self.currency}"


# ---------------------------------------------------------------------------
# Atomic State Persistence (Memory Database with Disk Backup)
# ---------------------------------------------------------------------------
class StateManager:
    """
    Maintains plan state in fast in-memory structures and atomically persists
    to state.json. Guarantees:
    - Zero duplicate notifications across container/server restarts.
    - Tracks drop duration (how long plan was available before selling out).
    - Atomic writes to avoid file corruption on unexpected shutdowns.
    """

    def __init__(self, file_path: str = DEFAULT_STATE_FILE):
        self.file_path = file_path
        self._states: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Loads saved state from state.json if present."""
        if not os.path.exists(self.file_path) or os.path.getsize(self.file_path) == 0:
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._states = data
                    logging.debug("Loaded %d plan state records from %s", len(self._states), self.file_path)
        except Exception as e:
            logging.warning("Could not read existing state file %s: %s", self.file_path, e)

    def save(self) -> None:
        """Atomically saves state using a temporary file."""
        try:
            temp_path = f"{self.file_path}.tmp.{os.getpid()}"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._states, f, indent=2)
            # Atomic replace
            os.replace(temp_path, self.file_path)
        except Exception as e:
            logging.warning("Failed to persist state to %s: %s", self.file_path, e)

    def get_plan_state(self, plan_key: str) -> Optional[Dict[str, Any]]:
        return self._states.get(plan_key)

    def set_initial_state(self, plan_key: str, status: str, notified: bool = False, now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        self._states[plan_key] = {
            "status": status,
            "in_stock_notified": notified,
            "backup_notified": False,
            "available_since": now if status == STATUS_AVAILABLE else None,
            "last_updated": now,
        }
        self.save()

    def mark_in_stock(self, plan_key: str, now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        self._states[plan_key] = {
            "status": STATUS_AVAILABLE,
            "in_stock_notified": True,
            "backup_notified": False,
            "available_since": now,
            "last_updated": now,
        }
        self.save()

    def mark_backup_sent(self, plan_key: str, now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        record = self._states.get(plan_key)
        if record:
            record["backup_notified"] = True
            record["last_updated"] = now
            self.save()

    def mark_sold_out(self, plan_key: str, now: Optional[float] = None) -> Optional[float]:
        """
        Marks plan as sold out and returns duration in seconds it was available (if tracked).
        """
        if now is None:
            now = time.time()
        record = self._states.get(plan_key)
        duration = None
        if record and record.get("available_since"):
            duration = max(0.0, now - float(record["available_since"]))

        self._states[plan_key] = {
            "status": STATUS_SOLD_OUT,
            "in_stock_notified": False,
            "backup_notified": False,
            "available_since": None,
            "last_updated": now,
        }
        self.save()
        return duration


# ---------------------------------------------------------------------------
# Notification Dispatcher
# ---------------------------------------------------------------------------
class NotificationDispatcher:
    """Dispatches drop, backup, and sold-out alerts to Discord and Telegram."""

    def __init__(self, config: TrackerConfig):
        self.config = config
        self.ssl_context = ssl.create_default_context()

    @staticmethod
    def format_duration(seconds: Optional[float]) -> str:
        """Formats duration seconds into human-readable text (e.g. '14m 20s')."""
        if seconds is None:
            return "Unknown"
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        if mins > 0:
            return f"{mins}m {secs}s"
        return f"{secs}s"

    def dispatch(
        self,
        plan: PlanInfo,
        alert_type: str,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """Sends alert through Discord Webhook and Telegram Bot API."""
        timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        success = True

        has_telegram = bool(self.config.telegram_bot_token and self.config.telegram_chat_id)
        has_discord = bool(self.config.discord_webhook_url)

        if not has_telegram and not has_discord:
            logging.warning(
                "🚨 [%s] %s (%s) triggered but NO notification channels configured in .env!",
                alert_type,
                plan.plan_name,
                plan.format_price(),
            )
            return False

        if has_discord:
            if not self._send_discord(plan, alert_type, timestamp_str, duration_seconds):
                success = False

        if has_telegram:
            if not self._send_telegram(plan, alert_type, timestamp_str, duration_seconds):
                success = False

        return success

    def _send_discord(
        self,
        plan: PlanInfo,
        alert_type: str,
        timestamp: str,
        duration_seconds: Optional[float],
    ) -> bool:
        webhook_url = self.config.discord_webhook_url
        if not webhook_url:
            return False

        duration_text = self.format_duration(duration_seconds)

        if alert_type == ALERT_IN_STOCK:
            title = "🚨 OnePlay Plan In Stock!"
            color = 0x00FF66  # Vibrant Green
            description = (
                f"A tracked plan is **NOW AVAILABLE FOR PURCHASE** on OnePlay!\n"
                f"[**Click Here to Subscribe Immediately**]({self.config.checkout_url})"
            )
            fields = [
                {"name": "Plan Name", "value": f"**{plan.plan_name}**", "inline": True},
                {"name": "Category", "value": plan.category, "inline": True},
                {"name": "Price", "value": f"**{plan.format_price()}**", "inline": True},
                {"name": "Duration", "value": f"{plan.duration_days} Days", "inline": True},
                {"name": "Status", "value": "🟢 **IN STOCK**", "inline": True},
                {"name": "Detected At", "value": timestamp, "inline": True},
            ]
        elif alert_type == ALERT_BACKUP:
            title = "⚠️ OnePlay Plan Still In Stock (Backup Reminder)"
            color = 0xFEE75C  # Gold / Amber
            description = (
                f"**One-time backup alert**: The plan is still live for purchase in case you missed the first notification.\n"
                f"[**Click Here to Subscribe**]({self.config.checkout_url})"
            )
            fields = [
                {"name": "Plan Name", "value": f"**{plan.plan_name}**", "inline": True},
                {"name": "Category", "value": plan.category, "inline": True},
                {"name": "Price", "value": f"**{plan.format_price()}**", "inline": True},
                {"name": "Duration", "value": f"{plan.duration_days} Days", "inline": True},
                {"name": "Notice", "value": "Single backup notification (alerts will now remain silent while active)", "inline": False},
            ]
        elif alert_type == ALERT_OUT_OF_STOCK:
            title = "🔻 OnePlay Plan Sold Out"
            color = 0xED4245  # Red
            description = (
                f"The purchasing window for **{plan.plan_name}** has closed.\n"
                f"The plan is now marked as **SOLD OUT** on OnePlay."
            )
            fields = [
                {"name": "Plan Name", "value": f"**{plan.plan_name}**", "inline": True},
                {"name": "Category", "value": plan.category, "inline": True},
                {"name": "Status", "value": "🔴 **SOLD OUT**", "inline": True},
                {"name": "Was In Stock For", "value": duration_text, "inline": True},
                {"name": "Closed At", "value": timestamp, "inline": True},
            ]
        else:  # TEST
            title = "🧪 [TEST] OnePlay Availability Notification"
            color = 0x5865F2  # Discord Blurple
            description = "This is a test notification confirming your Discord webhook is active and functioning."
            fields = [
                {"name": "Plan Name", "value": plan.plan_name, "inline": True},
                {"name": "Price", "value": plan.format_price(), "inline": True},
                {"name": "Status", "value": "Test OK", "inline": True},
            ]

        embed = {
            "title": title,
            "url": self.config.checkout_url,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {
                "text": "OnePlay Live Subscription Availability Tracker • Zero Maintenance"
            },
        }

        payload = {
            "username": "OnePlay Stock Tracker",
            "avatar_url": "https://www.oneplay.in/favicon.ico",
            "embeds": [embed],
        }

        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "OnePlayTrackerBot/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=10) as resp:
                if resp.status in (200, 204):
                    logging.info("Discord %s alert dispatched for %s", alert_type, plan.plan_name)
                    return True
                logging.error("Discord returned unexpected status %d", resp.status)
                return False
        except Exception as e:
            logging.error("Failed to send Discord alert: %s", e)
            return False

    def _send_telegram(
        self,
        plan: PlanInfo,
        alert_type: str,
        timestamp: str,
        duration_seconds: Optional[float],
    ) -> bool:
        token = self.config.telegram_bot_token
        chat_id = self.config.telegram_chat_id
        if not token or not chat_id:
            return False

        duration_text = self.format_duration(duration_seconds)
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        if alert_type == ALERT_IN_STOCK:
            html_text = (
                f"🚨 <b>ONEPLAY DROP ALERT!</b>\n\n"
                f"<b>Plan:</b> {plan.plan_name} ({plan.category})\n"
                f"<b>Price:</b> {plan.format_price()}\n"
                f"<b>Duration:</b> {plan.duration_days} Day(s)\n"
                f"<b>Status:</b> 🟢 <b>IN STOCK</b>\n"
                f"<b>Detected:</b> <code>{timestamp}</code>\n\n"
                f'👉 <a href="{self.config.checkout_url}"><b>Subscribe Now on OnePlay</b></a>'
            )
        elif alert_type == ALERT_BACKUP:
            html_text = (
                f"⚠️ <b>BACKUP REMINDER: Still In Stock!</b>\n\n"
                f"<b>Plan:</b> {plan.plan_name} ({plan.category})\n"
                f"<b>Price:</b> {plan.format_price()}\n"
                f"<i>One-time backup ping in case the first was missed. Alerts will now remain silent while active.</i>\n\n"
                f'👉 <a href="{self.config.checkout_url}"><b>Subscribe Now</b></a>'
            )
        elif alert_type == ALERT_OUT_OF_STOCK:
            html_text = (
                f"🔻 <b>ONEPLAY PLAN SOLD OUT</b>\n\n"
                f"<b>Plan:</b> {plan.plan_name} ({plan.category})\n"
                f"<b>Status:</b> 🔴 <b>SOLD OUT</b>\n"
                f"<b>Was In Stock For:</b> {duration_text}\n"
                f"<b>Closed At:</b> <code>{timestamp}</code>\n"
                f"<i>The purchasing window has closed. Tracker is armed for the next drop.</i>"
            )
        else:
            html_text = f"🧪 <b>[TEST ALERT]</b> Notifications configured successfully for {plan.plan_name}!"

        payload = {
            "chat_id": chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=10) as resp:
                if resp.status == 200:
                    logging.info("Telegram %s alert dispatched for %s", alert_type, plan.plan_name)
                    return True
                return False
        except Exception as e:
            logging.error("Failed to send Telegram alert: %s", e)
            return False


# ---------------------------------------------------------------------------
# API Client & Parser
# ---------------------------------------------------------------------------
class OnePlayClient:
    """Interacts with the OnePlay internal REST API with ETag support."""

    def __init__(self, config: TrackerConfig):
        self.config = config
        self.cached_etag: Optional[str] = None
        self.ssl_context = ssl.create_default_context()

    def fetch_plans(self) -> Tuple[int, Optional[Dict[str, Any]]]:
        """Fetches subscription plans using ETag cache validation."""
        headers = {
            "User-Agent": self.config.user_agent,
            "Origin": "https://www.oneplay.in",
            "Referer": "https://www.oneplay.in/",
            "Accept": "application/json, text/plain, */*",
            # Force Indian catalog on international / US cloud runners (GitHub Actions, AWS, Azure)
            "X-Forwarded-For": "49.37.128.1",
            "CF-IPCountry": "IN",
            "Client-IP": "49.37.128.1",
        }

        if self.cached_etag:
            headers["If-None-Match"] = self.cached_etag

        req = urllib.request.Request(self.config.api_url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=10) as resp:
                etag = resp.headers.get("ETag")
                if etag:
                    self.cached_etag = etag.strip()
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8"))
                return resp.status, data

        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None
            elif e.code == 429:
                logging.warning("Received HTTP 429 (Rate Limited) from OnePlay API.")
                return 429, None
            elif 500 <= e.code < 600:
                logging.warning("Received HTTP %d (Server Error) from OnePlay API.", e.code)
                return e.code, None
            else:
                logging.warning("HTTP Error %d from OnePlay API: %s", e.code, e.reason)
                return e.code, None

        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
            logging.error("Network error while connecting to OnePlay API: %s", e)
            return -1, None
        except Exception as e:
            logging.error("Unexpected error in fetch_plans: %s", e)
            return -1, None

    @staticmethod
    def _parse_bool(val: Any, default: bool = False) -> bool:
        """Safely normalizes booleans or boolean-like strings."""
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        s = str(val).strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
        return default

    def parse_plans(self, payload: Dict[str, Any]) -> List[PlanInfo]:
        """Parses tabs and plans array from the API response payload."""
        plans_list: List[PlanInfo] = []
        tabs = payload.get("tabs", {})

        tabs_items: List[Tuple[str, Dict[str, Any]]] = []
        if isinstance(tabs, dict):
            for tab_label, tab_val in tabs.items():
                if isinstance(tab_val, dict):
                    tabs_items.append((tab_label, tab_val))
        elif isinstance(tabs, list):
            for tab_val in tabs:
                if isinstance(tab_val, dict):
                    tab_label = tab_val.get("label") or tab_val.get("category_id") or "Unknown"
                    tabs_items.append((str(tab_label), tab_val))

        for tab_label, tab_val in tabs_items:
            plans = tab_val.get("plans") or []
            if not isinstance(plans, list):
                continue

            for p in plans:
                if not isinstance(p, dict):
                    continue

                plan_id = str(p.get("id", ""))
                plan_name = str(p.get("plan_name", "")).strip()
                if not plan_name:
                    continue

                plan_config = p.get("plan_config")
                if not isinstance(plan_config, dict):
                    plan_config = {}

                category_obj = p.get("category")
                if isinstance(category_obj, dict) and category_obj.get("label"):
                    category = str(category_obj.get("label"))
                else:
                    category = tab_label

                price = plan_config.get("actual_price")
                if price is None:
                    price = p.get("value")

                currency = str(p.get("currency", "INR"))
                duration_days = int(p.get("plan_duration_in_days") or 0)

                raw_sold_out = plan_config.get("is_sold_out")
                if raw_sold_out is None:
                    raw_sold_out = p.get("is_sold_out")
                is_sold_out = self._parse_bool(raw_sold_out, default=False)

                raw_live = p.get("is_live_for_purchase")
                if raw_live is None:
                    raw_live = plan_config.get("is_live_for_purchase")
                is_live = self._parse_bool(raw_live, default=True)

                plan_info = PlanInfo(
                    plan_id=plan_id,
                    plan_name=plan_name,
                    category=category,
                    price=price,
                    currency=currency,
                    duration_days=duration_days,
                    is_sold_out=is_sold_out,
                    is_live_for_purchase=is_live,
                    raw_plan=p,
                )
                plans_list.append(plan_info)

        return plans_list


# ---------------------------------------------------------------------------
# Tracker State Machine & Worker
# ---------------------------------------------------------------------------
class OnePlayTracker:
    """Manages lifecycle events, backup reminders, and sold-out notifications."""

    def __init__(self, config: TrackerConfig):
        self.config = config
        self.client = OnePlayClient(config)
        self.dispatcher = NotificationDispatcher(config)
        self.state_mgr = StateManager(config.state_file)

        self.running: bool = True
        self.first_run: bool = True

    def stop(self) -> None:
        """Graceful shutdown trigger."""
        self.running = False

    def evaluate_plans(self, plans: List[PlanInfo], dry_run: bool = False) -> None:
        """
        Evaluates plans with edge transitions:
        1. SOLD_OUT -> AVAILABLE: Fire immediate In-Stock Alert.
        2. Continuous AVAILABLE: Fire single backup reminder after delay, then stay silent.
        3. AVAILABLE -> SOLD_OUT: Fire Out-of-Stock Alert so user knows window closed.
        """
        now = time.time()

        for plan in plans:
            if not self.config.is_target(plan):
                continue

            key = plan.unique_key
            current_status = plan.status
            rec = self.state_mgr.get_plan_state(key)

            # Cold startup evaluation
            if rec is None:
                status_emoji = "🟢 AVAILABLE" if plan.is_available else "🔴 SOLD OUT"
                logging.info(
                    "[Initial State] %s (%s) is currently %s [%s]",
                    plan.plan_name,
                    plan.category,
                    status_emoji,
                    plan.format_price(),
                )

                if self.first_run and self.config.alert_on_startup and plan.is_available:
                    logging.info(
                        "[Startup Alert] ALERT_ON_STARTUP enabled. Triggering drop alert for %s!",
                        plan.plan_name,
                    )
                    if not dry_run:
                        self.dispatcher.dispatch(plan, ALERT_IN_STOCK)
                    self.state_mgr.set_initial_state(key, current_status, notified=True, now=now)
                else:
                    self.state_mgr.set_initial_state(key, current_status, notified=False, now=now)
                continue

            previous_status = rec.get("status", STATUS_SOLD_OUT)

            # Edge transition: SOLD_OUT -> AVAILABLE
            if previous_status == STATUS_SOLD_OUT and current_status == STATUS_AVAILABLE:
                logging.warning(
                    "🎉 [EDGE TRANSITION] %s (%s) is now AVAILABLE! Triggering drop alert!",
                    plan.plan_name,
                    plan.category,
                )
                if not dry_run:
                    self.dispatcher.dispatch(plan, ALERT_IN_STOCK)
                else:
                    logging.info("[Dry Run] Skipped network dispatch for %s.", plan.plan_name)

                self.state_mgr.mark_in_stock(key, now=now)

            # Continuous availability: check for single backup notification
            elif previous_status == STATUS_AVAILABLE and current_status == STATUS_AVAILABLE:
                if self.config.send_backup_notification and not rec.get("backup_notified", False):
                    available_since = rec.get("available_since") or now
                    elapsed = now - float(available_since)
                    if elapsed >= self.config.backup_delay_seconds:
                        logging.info(
                            "⚠️ [Backup Alert] %s has been available for %.1f min. Sending one-time backup notification.",
                            plan.plan_name,
                            elapsed / 60.0,
                        )
                        if not dry_run:
                            self.dispatcher.dispatch(plan, ALERT_BACKUP)
                        self.state_mgr.mark_backup_sent(key, now=now)

            # Edge transition: AVAILABLE -> SOLD_OUT
            elif previous_status == STATUS_AVAILABLE and current_status == STATUS_SOLD_OUT:
                logging.info(
                    "🔻 [State Flip] %s (%s) went OUT OF STOCK (AVAILABLE -> SOLD_OUT).",
                    plan.plan_name,
                    plan.category,
                )
                duration_seconds = self.state_mgr.mark_sold_out(key, now=now)

                if self.config.notify_on_out_of_stock:
                    logging.info(
                        "Dispatched Out-of-Stock alert for %s (was available for %s).",
                        plan.plan_name,
                        NotificationDispatcher.format_duration(duration_seconds),
                    )
                    if not dry_run:
                        self.dispatcher.dispatch(
                            plan,
                            ALERT_OUT_OF_STOCK,
                            duration_seconds=duration_seconds,
                        )

        self.first_run = False

    def poll_once(self, dry_run: bool = False) -> bool:
        """Executes a single poll cycle."""
        status, payload = self.client.fetch_plans()

        if status == 304:
            logging.debug(
                "[ETag 304] Server returned Not Modified (cached ETag: %s). 0 body bytes transferred.",
                self.client.cached_etag,
            )
            return True

        if status == 200 and payload is not None:
            plans = self.client.parse_plans(payload)
            target_plans = [p for p in plans if self.config.is_target(p)]

            logging.debug(
                "Parsed %d total plans (%d matching target filters). ETag: %s",
                len(plans),
                len(target_plans),
                self.client.cached_etag,
            )
            self.evaluate_plans(plans, dry_run=dry_run)
            return True

        if status == 429:
            logging.warning(
                "Rate limit hit (429). Exponential backoff: waiting %ds...",
                int(self.config.backoff_seconds),
            )
            time.sleep(self.config.backoff_seconds)
            return False

        if status >= 500 or status == -1:
            wait_time = (
                self.config.backoff_seconds if status >= 500 else self.config.network_retry_seconds
            )
            logging.warning("API error or network failure (%d). Retrying in %ds...", status, int(wait_time))
            time.sleep(wait_time)
            return False

        return False

    def start_health_server(self) -> None:
        """Starts a minimal HTTP health server for Hugging Face Spaces on port 7860."""
        port = self.config.port
        tracker_ref = self

        class HealthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                status_payload = {
                    "status": "running",
                    "uptime_seconds": int(time.time() - getattr(tracker_ref, "_start_ts", time.time())),
                    "tracked_plans": list(tracker_ref.config.target_plans) if not tracker_ref.config.track_all else "ALL",
                    "cached_etag": tracker_ref.client.cached_etag,
                }
                self.wfile.write(json.dumps(status_payload).encode("utf-8"))

            def log_message(self, format, *args):
                pass  # Silence routine health check access logs

        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logging.info("Started cloud health check web server on port %d", port)
        except Exception as e:
            logging.warning("Could not bind health server to port %d: %s", port, e)

    def run_loop(self, dry_run: bool = False, enable_health_server: Optional[bool] = None) -> None:
        """Main continuous polling loop with jitter and error resilience."""
        self._start_ts = time.time()
        
        # Start health check server for cloud platforms (Koyeb, Docker, Render)
        if enable_health_server is None:
            # Enable if running in cloud/docker (PORT is present) or standalone
            enable_health_server = bool(os.getenv("PORT") or os.getenv("KOYEB_APP_NAME")) or (not os.getenv("SPACE_ID"))

        if enable_health_server:
            self.start_health_server()

        target_display = (
            "ALL PLANS (*)"
            if self.config.track_all
            else ", ".join(sorted(self.config.target_plans))
        )
        logging.info("=" * 65)
        logging.info("OnePlay Live Subscription Tracker Started")
        logging.info("Target Plans          : %s", target_display)
        logging.info(
            "Poll Interval         : %.1fs (±%.1fs jitter)",
            self.config.poll_interval,
            self.config.poll_jitter,
        )
        logging.info(
            "Single Backup Reminder: %s (%ds delay)",
            "Enabled" if self.config.send_backup_notification else "Disabled",
            int(self.config.backup_delay_seconds),
        )
        logging.info(
            "Sold-Out Notification : %s",
            "Enabled" if self.config.notify_on_out_of_stock else "Disabled",
        )
        logging.info(
            "Notifications         : Discord: %s | Telegram: %s",
            "Configured" if self.config.discord_webhook_url else "Disabled",
            "Configured" if self.config.telegram_bot_token else "Disabled",
        )
        if dry_run:
            logging.warning("DRY RUN MODE ENABLED: Notifications will not be dispatched.")
        logging.info("=" * 65)

        while self.running:
            start_time = time.time()
            success = self.poll_once(dry_run=dry_run)

            if not self.running:
                break

            if success:
                jitter_delta = random.uniform(-self.config.poll_jitter, self.config.poll_jitter)
                target_sleep = max(5.0, self.config.poll_interval + jitter_delta)

                elapsed = time.time() - start_time
                actual_sleep = max(1.0, target_sleep - elapsed)
                logging.debug("Poll completed in %.2fs. Sleeping %.2fs...", elapsed, actual_sleep)

                wake_target = time.time() + actual_sleep
                while self.running and time.time() < wake_target:
                    time.sleep(min(0.5, max(0.0, wake_target - time.time())))

        logging.info("OnePlay Subscription Tracker stopped cleanly.")


# ---------------------------------------------------------------------------
# CLI Entry Point & Utilities
# ---------------------------------------------------------------------------
def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)


def run_test_alert(config: TrackerConfig) -> None:
    """Dispatches a synthetic test alert to verify notification credentials."""
    dispatcher = NotificationDispatcher(config)
    sample_plan = PlanInfo(
        plan_id="test-prime-sample-id",
        plan_name="Prime (Test Alert)",
        category="OneSpace",
        price=1999,
        currency="INR",
        duration_days=30,
        is_sold_out=False,
        is_live_for_purchase=True,
        raw_plan={"value": 1599},
    )
    logging.info("Sending synthetic test alert through configured channels...")
    success = dispatcher.dispatch(sample_plan, alert_type=ALERT_TEST)
    if success:
        logging.info("✅ Test alert successfully dispatched!")
    else:
        logging.error("❌ Test alert failed. Verify tokens/webhooks in .env.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OnePlay Live Subscription Availability Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env configuration file (default: .env)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform a single poll and exit immediately",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Monitor without dispatching push notifications",
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Send a test notification to verify Discord/Telegram setup and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()

    config = TrackerConfig(env_path=args.env_file)
    if args.debug:
        config.log_level = "DEBUG"
    setup_logging(config.log_level)

    if args.test_alert:
        run_test_alert(config)
        sys.exit(0)

    tracker = OnePlayTracker(config)

    def sig_handler(signum, frame):
        logging.info("Shutdown signal received (%d). Terminating tracker...", signum)
        tracker.stop()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    if args.once:
        logging.info("Running single check against OnePlay API...")
        status, payload = tracker.client.fetch_plans()
        if status == 200 and payload:
            plans = tracker.client.parse_plans(payload)
            logging.info("Fetched %d plans successfully.", len(plans))
            print("\n" + "=" * 70)
            print(f"{'Category':<15} | {'Plan Name':<15} | {'Price':<12} | {'Duration':<8} | {'Status'}")
            print("-" * 70)
            for p in plans:
                target_marker = "⭐ " if config.is_target(p) else "   "
                status_str = "🟢 AVAILABLE" if p.is_available else "🔴 SOLD OUT"
                print(
                    f"{p.category:<15} | {target_marker + p.plan_name:<15} | {p.format_price():<12} | {str(p.duration_days) + 'd':<8} | {status_str}"
                )
            print("=" * 70 + "\n")
        else:
            logging.error("Failed to fetch plans. Status code: %d", status)
            sys.exit(1)
        sys.exit(0)

    tracker.run_loop(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
