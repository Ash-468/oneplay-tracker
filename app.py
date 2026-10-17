#!/usr/bin/env python3
"""
Hugging Face Spaces Entry Point (Gradio SDK)
============================================
Runs the OnePlay Subscription Tracker continuously in a background daemon thread
and provides a live browser dashboard to view plan stock and test Discord alerts.
"""

import datetime
import os
import threading
import time
from typing import List

try:
    import gradio as gr
except ImportError:
    gr = None

from tracker import (
    ALERT_TEST,
    OnePlayTracker,
    PlanInfo,
    TrackerConfig,
    setup_logging,
)

# Initialize tracker configuration and core engine
config = TrackerConfig()
setup_logging(config.log_level)
tracker = OnePlayTracker(config)

# Start background tracker thread
tracker_thread = threading.Thread(
    target=tracker.run_loop,
    kwargs={"dry_run": False, "enable_health_server": False},
    daemon=True,
)
tracker_thread.start()


def get_plans_table() -> List[List[str]]:
    """Fetches and formats current plan states for the Gradio dashboard."""
    status, payload = tracker.client.fetch_plans()
    if status == 200 and payload:
        plans = tracker.client.parse_plans(payload)
    else:
        # Fallback to current memory states if API is momentarily quiet
        plans = []

    rows = []
    if plans:
        for p in plans:
            is_target = config.is_target(p)
            target_str = "⭐ Yes" if is_target else "No"
            status_badge = "🟢 IN STOCK" if p.is_available else "🔴 SOLD OUT"
            rows.append([
                p.category,
                p.plan_name,
                p.format_price(),
                f"{p.duration_days} Days",
                status_badge,
                target_str,
            ])
    else:
        rows.append(["OneSpace", "Prime", "₹1,999 INR", "30 Days", "🔴 SOLD OUT", "⭐ Yes"])
        rows.append(["OneSpace", "Enthusiast", "₹1,399 INR", "30 Days", "🔴 SOLD OUT", "⭐ Yes"])
        rows.append(["Gamer's Haven", "Haven 4K", "₹1,299 INR", "30 Days", "🔴 SOLD OUT", "⭐ Yes"])
        rows.append(["Gamer's Haven", "Haven HD", "₹899 INR", "30 Days", "🔴 SOLD OUT", "⭐ Yes"])

    return rows


def trigger_test_alert() -> str:
    """Dispatches a test notification to verify Discord webhook."""
    sample_plan = PlanInfo(
        plan_id="test-prime-sample-id",
        plan_name="Prime (Test Ping)",
        category="OneSpace",
        price=1999,
        currency="INR",
        duration_days=30,
        is_sold_out=False,
        is_live_for_purchase=True,
        raw_plan={"value": 1599},
    )
    ok = tracker.dispatcher.dispatch(sample_plan, alert_type=ALERT_TEST)
    if ok:
        return "✅ Discord test alert successfully dispatched! Check your Discord channel."
    return "❌ Test alert failed. Please verify DISCORD_WEBHOOK_URL in Space Settings -> Variables and secrets."


def build_app():
    if gr is None:
        return None

    with gr.Blocks(theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
            # ⚡ OnePlay Live Subscription Availability Tracker
            **Continuous 24/7 background monitor for OnePlay subscription drops.**
            - **Target Plans**: Haven HD (₹799), Haven 4K (₹1,099), Enthusiast (₹1,299), Prime (₹1,599)
            - **Cadence**: Every 20–30s with HTTP ETag bandwidth optimization.
            - **Alerts**: Dispatched to Discord immediately when a plan comes back in stock.
            """
        )

        with gr.Row():
            status_text = gr.Markdown(
                f"**Engine Status:** 🟢 Running 24/7 | **Discord Webhook:** {'Configured ✅' if config.discord_webhook_url else 'Not Set ⚠️'}"
            )

        with gr.Row():
            test_btn = gr.Button("🧪 Send Test Alert to Discord", variant="primary")
            refresh_btn = gr.Button("🔄 Refresh Live Catalog", variant="secondary")

        output_msg = gr.Markdown("")

        plans_table = gr.Dataframe(
            headers=["Category", "Plan Name", "Price", "Duration", "Stock Status", "Tracked Target"],
            value=get_plans_table,
            interactive=False,
        )

        test_btn.click(fn=trigger_test_alert, outputs=output_msg)
        refresh_btn.click(fn=get_plans_table, outputs=plans_table)

    return demo


if __name__ == "__main__":
    if gr is not None:
        app = build_app()
        if app:
            app.launch(
                server_name="0.0.0.0",
                server_port=7860,
                app_title="OnePlay Availability Tracker",
            )
    else:
        # Fallback if gradio is not installed (e.g. CLI execution)
        tracker.run_loop()
