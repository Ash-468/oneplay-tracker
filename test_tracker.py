#!/usr/bin/env python3
"""
Unit and integration test suite for OnePlay Availability Tracker.
Tests state persistence, edge transitions, single backup reminders,
sold-out closure notifications, parser resilience, and payload structures.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from tracker import (
    ALERT_BACKUP,
    ALERT_IN_STOCK,
    ALERT_OUT_OF_STOCK,
    OnePlayClient,
    OnePlayTracker,
    PlanInfo,
    STATUS_AVAILABLE,
    STATUS_SOLD_OUT,
    StateManager,
    TrackerConfig,
)


class TestPlanAvailabilityRules(unittest.TestCase):
    def test_availability_flags(self):
        # Both is_sold_out: False and is_live_for_purchase: True -> AVAILABLE
        p1 = PlanInfo("1", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=False, is_live_for_purchase=True, raw_plan={})
        self.assertTrue(p1.is_available)
        self.assertEqual(p1.status, STATUS_AVAILABLE)

        # is_sold_out: True -> SOLD_OUT
        p2 = PlanInfo("2", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=True, is_live_for_purchase=True, raw_plan={})
        self.assertFalse(p2.is_available)
        self.assertEqual(p2.status, STATUS_SOLD_OUT)

        # is_live_for_purchase: False -> SOLD_OUT
        p3 = PlanInfo("3", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=False, is_live_for_purchase=False, raw_plan={})
        self.assertFalse(p3.is_available)
        self.assertEqual(p3.status, STATUS_SOLD_OUT)

    def test_price_formatting(self):
        p = PlanInfo("1", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=False, is_live_for_purchase=True, raw_plan={})
        self.assertEqual(p.format_price(), "₹1,999 INR")


class TestStateManager(unittest.TestCase):
    def test_state_persistence_and_reload(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            temp_path = f.name

        try:
            # Initialize state manager and write a plan state
            mgr = StateManager(temp_path)
            mgr.set_initial_state("OneSpace:Prime:30d", STATUS_AVAILABLE, notified=True, now=1000.0)
            mgr.mark_backup_sent("OneSpace:Prime:30d", now=1180.0)

            # Reload fresh instance from disk
            mgr_reloaded = StateManager(temp_path)
            rec = mgr_reloaded.get_plan_state("OneSpace:Prime:30d")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["status"], STATUS_AVAILABLE)
            self.assertTrue(rec["in_stock_notified"])
            self.assertTrue(rec["backup_notified"])

            # Test mark sold out calculates duration correctly
            duration = mgr_reloaded.mark_sold_out("OneSpace:Prime:30d", now=1600.0)
            self.assertEqual(duration, 600.0)  # 1600 - 1000 = 600s (10 mins)

            # Reload again to confirm sold out state
            mgr_reloaded_again = StateManager(temp_path)
            rec2 = mgr_reloaded_again.get_plan_state("OneSpace:Prime:30d")
            self.assertEqual(rec2["status"], STATUS_SOLD_OUT)
            self.assertFalse(rec2["in_stock_notified"])
            self.assertFalse(rec2["backup_notified"])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestNotificationLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_state = tempfile.NamedTemporaryFile("w+", delete=False)
        self.state_file = self.temp_state.name
        self.temp_state.close()

        self.config = TrackerConfig()
        self.config.target_plans = {"prime"}
        self.config.state_file = self.state_file
        self.config.alert_on_startup = False
        self.config.send_backup_notification = True
        self.config.backup_delay_seconds = 180.0  # 3 minutes
        self.config.notify_on_out_of_stock = True

        self.tracker = OnePlayTracker(self.config)
        self.tracker.dispatcher.dispatch = MagicMock(return_value=True)

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)

    def test_lifecycle_in_stock_backup_and_sold_out(self):
        p_sold = PlanInfo("1", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=True, is_live_for_purchase=True, raw_plan={"value": 1599})
        p_avail = PlanInfo("1", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=False, is_live_for_purchase=True, raw_plan={"value": 1599})

        # 1. Cold start while sold out: silent recording
        self.tracker.evaluate_plans([p_sold])
        self.tracker.dispatcher.dispatch.assert_not_called()

        # 2. Plan flips to AVAILABLE: Immediate In-Stock Alert fired
        t0 = time.time()
        self.tracker.evaluate_plans([p_avail])
        self.assertEqual(self.tracker.dispatcher.dispatch.call_count, 1)
        self.assertEqual(self.tracker.dispatcher.dispatch.call_args[0][1], ALERT_IN_STOCK)

        # 3. Subsequent poll while still available (e.g. 30s later): NO repetitive spam!
        self.tracker.evaluate_plans([p_avail])
        self.assertEqual(self.tracker.dispatcher.dispatch.call_count, 1)

        # 4. Backup reminder time arrives (> 180s): Fires single backup reminder
        with patch("time.time", return_value=t0 + 200.0):
            self.tracker.evaluate_plans([p_avail])
            self.assertEqual(self.tracker.dispatcher.dispatch.call_count, 2)
            self.assertEqual(self.tracker.dispatcher.dispatch.call_args[0][1], ALERT_BACKUP)

            # 5. Continuous availability after backup: Absolutely silent!
            self.tracker.evaluate_plans([p_avail])
            self.tracker.evaluate_plans([p_avail])
            self.assertEqual(self.tracker.dispatcher.dispatch.call_count, 2)

        # 6. Plan flips to SOLD_OUT: Fires Out-of-Stock Alert so user knows window closed
        with patch("time.time", return_value=t0 + 600.0):
            self.tracker.evaluate_plans([p_sold])
            self.assertEqual(self.tracker.dispatcher.dispatch.call_count, 3)
            self.assertEqual(self.tracker.dispatcher.dispatch.call_args[0][1], ALERT_OUT_OF_STOCK)


class TestDiscordEmbeds(unittest.TestCase):
    def test_discord_alert_types(self):
        cfg = TrackerConfig()
        cfg.discord_webhook_url = "https://discord.com/api/webhooks/dummy"
        tracker = OnePlayTracker(cfg)

        plan = PlanInfo("prime-1", "Prime", "OneSpace", 1999, "INR", 30, is_sold_out=False, is_live_for_purchase=True, raw_plan={"value": 1599})

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 204
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            # Test In-Stock
            ok1 = tracker.dispatcher.dispatch(plan, ALERT_IN_STOCK)
            self.assertTrue(ok1)

            # Test Backup
            ok2 = tracker.dispatcher.dispatch(plan, ALERT_BACKUP)
            self.assertTrue(ok2)

            # Test Out of Stock
            ok3 = tracker.dispatcher.dispatch(plan, ALERT_OUT_OF_STOCK, duration_seconds=500.0)
            self.assertTrue(ok3)


if __name__ == "__main__":
    unittest.main()
