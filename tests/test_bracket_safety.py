import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_execution_agent import buy_decision, load_execution_agent
from tests.test_orchestrator import ET, load_orchestrator


class BracketSafetyTests(unittest.TestCase):
    def setUp(self):
        self.execution = load_execution_agent()
        self.account = {"cash": 100_000.0, "portfolio_value": 100_000.0,
                        "buying_power": 100_000.0}
        self.clock = SimpleNamespace(
            is_open=True,
            timestamp=datetime(2026, 7, 23, 15, 0, tzinfo=ET),
            next_close=datetime(2026, 7, 23, 16, 0, tzinfo=ET),
        )

    def run_exec(self, decisions, quotes, submit_fn=None, submit=True):
        submit_fn = submit_fn or Mock(return_value={"id": "x"})
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(self.execution, "get_open_buy_orders", return_value=[]),
            patch.object(self.execution, "get_quote", side_effect=lambda s: quotes[s]),
            patch.object(self.execution, "get_market_clock", return_value=self.clock),
            patch.object(self.execution, "place_bracket_order", submit_fn),
        ):
            return self.execution.execute_signals(decisions, submit=submit), submit_fn

    def test_wide_ask_is_skipped_before_order(self):
        # Like run 40714177142: ask far above where the stock trades.
        report, submit = self.run_exec(
            [buy_decision("AAPL")], {"AAPL": {"bid": 142.77, "ask": 150.0}})
        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("spread too wide", report[0]["reason"])
        submit.assert_not_called()

    def test_stop_is_below_midpoint(self):
        report, submit = self.run_exec(
            [buy_decision("AAPL")], {"AAPL": {"bid": 99.6, "ask": 100.0}})
        order = report[0]["order"]
        self.assertLessEqual(order["stop_loss"], 99.8 - 0.01)
        self.assertEqual(order["stop_loss"], round(99.8 * 0.98, 2))

    def test_invalid_bracket_legs_skipped(self):
        decision = buy_decision("AAPL")
        decision.stop_loss_pct = 0.001  # stop rounds onto the price
        report, submit = self.run_exec(
            [decision], {"AAPL": {"bid": 100.0, "ask": 100.0}})
        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("bracket legs invalid", report[0]["reason"])
        submit.assert_not_called()

    def test_broker_rejection_does_not_stop_next_decision(self):
        class APIError(Exception):
            pass

        calls = []

        def fake_submit(symbol, *a, **k):
            calls.append(symbol)
            if symbol == "AAPL":
                raise APIError("stop_loss.stop_price must be <= base_price - 0.01")
            return {"id": "ok"}

        quotes = {"AAPL": {"bid": 100.0, "ask": 100.0},
                  "MSFT": {"bid": 100.0, "ask": 100.0}}
        report, _ = self.run_exec(
            [buy_decision("AAPL"), buy_decision("MSFT")], quotes, fake_submit)
        self.assertEqual(calls, ["AAPL", "MSFT"])
        self.assertEqual(report[0]["action"], "rejected")
        self.assertIn("base_price", report[0]["reason"])
        self.assertEqual(report[1]["action"], "submitted")
        # cash was restored, so MSFT got the same size as AAPL would have
        self.assertEqual(report[1]["order"]["qty"], report[0]["order"]["qty"])


class TickFailureExitTests(unittest.TestCase):
    def _tick(self, fail):
        orch = load_orchestrator()
        open_ = datetime(2026, 7, 23, 9, 30, tzinfo=ET)
        close = datetime(2026, 7, 23, 16, 0, tzinfo=ET)
        state = {"day_complete": False, "session_recorded": True,
                 "premarket_done": True, "exec_done": True,
                 "last_daytime_slot": None, "daytime_cycles_attempted": 0}
        dr = types.ModuleType("tools.daily_report")
        dr.append_event = lambda *a, **k: None
        dr.summarize_execution = lambda r: {}
        tools_pkg = sys.modules.get("tools") or types.ModuleType("tools")

        def cycle(submit=False):
            if fail:
                raise RuntimeError("boom")
            return {}

        slots = orch._daytime_slots(open_, close)
        with (
            patch.dict(sys.modules, {"tools.daily_report": dr}),
            patch.object(tools_pkg, "daily_report", dr, create=True),
            patch.object(orch, "todays_session", return_value=(open_, close)),
            patch.object(orch, "_load_state", return_value=state),
            patch.object(orch, "_save_state") as save,
            patch.object(orch, "_now", return_value=slots[0] + timedelta(seconds=5)),
            patch.object(orch, "stage_daytime_cycle", cycle),
            patch.object(orch, "_log"),
        ):
            result = orch.run_tick()
        save.assert_called_once()  # state saved even when a stage failed
        return result

    def test_failed_stage_reported(self):
        self.assertEqual(self._tick(fail=True), ["daytime_cycle"])

    def test_success_reports_nothing(self):
        self.assertFalse(self._tick(fail=False))


if __name__ == "__main__":
    unittest.main()
