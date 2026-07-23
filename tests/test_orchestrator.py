import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def load_orchestrator():
    market_calendar = types.ModuleType("tools.market_calendar")
    market_calendar.ET = ET
    market_calendar.todays_session = lambda: None
    spec = importlib.util.spec_from_file_location(
        "orchestrator_under_test", ROOT / "orchestrator.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"tools.market_calendar": market_calendar}):
        spec.loader.exec_module(module)
    return module


class MarketOpenPollingTests(unittest.TestCase):
    def test_late_tick_still_checks_alpaca_clock(self):
        orchestrator = load_orchestrator()
        scheduled_open = datetime(2026, 7, 20, 9, 30, tzinfo=ET)
        orchestrator._now = lambda: datetime(2026, 7, 20, 9, 39, tzinfo=ET)

        trading_client = Mock()
        trading_client.get_clock.return_value.is_open = True
        broker = types.ModuleType("tools.broker")
        broker.trading_client = trading_client

        with patch.dict(sys.modules, {"tools.broker": broker}):
            self.assertTrue(orchestrator._poll_until_open(scheduled_open))

        trading_client.get_clock.assert_called_once_with()

    def test_late_tick_returns_false_after_one_real_closed_check(self):
        orchestrator = load_orchestrator()
        scheduled_open = datetime(2026, 7, 20, 9, 30, tzinfo=ET)
        orchestrator._now = lambda: datetime(2026, 7, 20, 9, 39, tzinfo=ET)

        trading_client = Mock()
        trading_client.get_clock.return_value.is_open = False
        broker = types.ModuleType("tools.broker")
        broker.trading_client = trading_client

        with patch.dict(sys.modules, {"tools.broker": broker}):
            self.assertFalse(orchestrator._poll_until_open(scheduled_open))

        trading_client.get_clock.assert_called_once_with()


class DaytimeScheduleTests(unittest.TestCase):
    def test_normal_session_has_twelve_complete_cycles(self):
        orchestrator = load_orchestrator()
        session_open = datetime(2026, 7, 23, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 7, 23, 16, 0, tzinfo=ET)

        slots = orchestrator._daytime_slots(session_open, session_close)

        self.assertEqual(len(slots), 12)
        self.assertEqual(slots[0].strftime("%H:%M"), "10:15")
        self.assertEqual(slots[-1].strftime("%H:%M"), "15:45")

    def test_tick_only_services_anchored_slot_with_grace(self):
        orchestrator = load_orchestrator()
        session_open = datetime(2026, 7, 23, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 7, 23, 16, 0, tzinfo=ET)
        slots = orchestrator._daytime_slots(session_open, session_close)

        self.assertIsNone(orchestrator._due_daytime_slot(
            datetime(2026, 7, 23, 10, 0, tzinfo=ET), slots, None
        ))
        first = orchestrator._due_daytime_slot(
            datetime(2026, 7, 23, 10, 16, tzinfo=ET), slots, None
        )
        self.assertEqual(first, slots[0])
        self.assertIsNone(orchestrator._due_daytime_slot(
            datetime(2026, 7, 23, 10, 30, tzinfo=ET), slots, None
        ))

    def test_final_slot_tolerates_scheduler_seconds(self):
        orchestrator = load_orchestrator()
        session_open = datetime(2026, 7, 23, 9, 30, tzinfo=ET)
        session_close = datetime(2026, 7, 23, 16, 0, tzinfo=ET)
        slots = orchestrator._daytime_slots(session_open, session_close)

        due = orchestrator._due_daytime_slot(
            datetime(2026, 7, 23, 15, 45, 30, tzinfo=ET), slots, None
        )
        self.assertEqual(due, slots[-1])


if __name__ == "__main__":
    unittest.main()
