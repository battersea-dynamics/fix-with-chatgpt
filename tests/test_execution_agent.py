import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def load_execution_agent():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None

    signal_agent = types.ModuleType("agents.signal_agent")
    signal_agent.SignalDecision = object

    broker = types.ModuleType("tools.broker")
    broker.get_account = lambda: None
    broker.get_market_clock = lambda: None
    broker.get_open_buy_orders = lambda: []
    broker.get_positions = lambda: []
    broker.get_quote = lambda symbol: None
    broker.place_bracket_order = lambda *args, **kwargs: None

    spec = importlib.util.spec_from_file_location(
        "execution_agent_under_test", ROOT / "agents" / "execution_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    stubs = {
        "dotenv": dotenv,
        "agents.signal_agent": signal_agent,
        "tools.broker": broker,
    }
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


def buy_decision(symbol="AAPL"):
    return SimpleNamespace(
        symbol=symbol,
        signal="buy",
        confidence=0.8,
        take_profit_pct=4.0,
        stop_loss_pct=2.0,
        numbers_verified=True,
    )


class DuplicateEntryGuardTests(unittest.TestCase):
    def setUp(self):
        self.execution = load_execution_agent()
        self.account = {
            "cash": 100_000.0,
            "portfolio_value": 100_000.0,
            "buying_power": 100_000.0,
        }

    def test_existing_position_blocks_entry_before_quote(self):
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(
                self.execution,
                "get_positions",
                return_value=[{"symbol": "AAPL"}],
            ),
            patch.object(
                self.execution, "get_open_buy_orders", return_value=[]
            ),
            patch.object(self.execution, "get_quote") as quote,
        ):
            report = self.execution.execute_signals([buy_decision()])

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("already held", report[0]["reason"])
        quote.assert_not_called()

    def test_open_buy_order_blocks_duplicate_entry(self):
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(
                self.execution,
                "get_open_buy_orders",
                return_value=[{"symbol": "AAPL"}],
            ),
            patch.object(self.execution, "get_quote") as quote,
        ):
            report = self.execution.execute_signals([buy_decision()])

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("duplicate entry blocked", report[0]["reason"])
        quote.assert_not_called()

    def test_failed_numeric_verification_blocks_entry(self):
        decision = buy_decision()
        decision.numbers_verified = False
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(
                self.execution, "get_open_buy_orders", return_value=[]
            ),
            patch.object(self.execution, "get_quote") as quote,
        ):
            report = self.execution.execute_signals([decision])

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("verification failed", report[0]["reason"])
        quote.assert_not_called()

    def test_submitted_order_uses_attempt_specific_client_id(self):
        et = ZoneInfo("America/New_York")
        clock = SimpleNamespace(
            is_open=True,
            timestamp=datetime(2026, 7, 23, 15, 0, tzinfo=et),
            next_close=datetime(2026, 7, 23, 16, 0, tzinfo=et),
        )
        submit = Mock(return_value={"id": "paper-order"})
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(
                self.execution, "get_open_buy_orders", return_value=[]
            ),
            patch.object(
                self.execution, "get_quote", return_value={"ask": 100.0}
            ),
            patch.object(
                self.execution, "get_market_clock", return_value=clock
            ),
            patch.object(self.execution, "place_bracket_order", submit),
        ):
            report = self.execution.execute_signals(
                [buy_decision()], submit=True
            )

        client_id = report[0]["order"]["client_order_id"]
        self.assertRegex(client_id, r"^ta-\d{8}-\d{6}-AAPL$")
        self.assertEqual(
            submit.call_args.kwargs["client_order_id"], client_id
        )

    def test_position_budget_uses_cash_with_200_dollar_floor(self):
        cases = (
            (10_000.0, 2_000.0),
            (500.0, 200.0),
            (200.0, 200.0),
            (50.0, 50.0),
        )
        for cash, expected_cost in cases:
            with self.subTest(cash=cash):
                account = {**self.account, "cash": cash}
                with (
                    patch.object(
                        self.execution, "get_account", return_value=account
                    ),
                    patch.object(
                        self.execution, "get_positions", return_value=[]
                    ),
                    patch.object(
                        self.execution, "get_open_buy_orders", return_value=[]
                    ),
                    patch.object(
                        self.execution,
                        "get_quote",
                        return_value={"ask": 10.0},
                    ),
                ):
                    report = self.execution.execute_signals([buy_decision()])

                self.assertEqual(
                    report[0]["order"]["est_cost"], expected_cost
                )

    def _execute_at_price(self, ask, **kwargs):
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(
                self.execution, "get_open_buy_orders", return_value=[]
            ),
            patch.object(
                self.execution, "get_quote", return_value={"ask": ask}
            ),
        ):
            return self.execution.execute_signals(
                [buy_decision()],
                reference_prices={"AAPL": 100.0},
                **kwargs,
            )

    def test_higher_live_price_keeps_original_target(self):
        report = self._execute_at_price(103.0)

        self.assertEqual(report[0]["action"], "dry_run")
        self.assertEqual(report[0]["order"]["take_profit"], 104.0)
        self.assertEqual(
            report[0]["order"]["remaining_take_profit_pct"], 0.97
        )
        self.assertEqual(report[0]["order"]["live_price_change_pct"], 3.0)

    def test_lower_live_price_shifts_target_by_same_percentage(self):
        report = self._execute_at_price(99.0)

        self.assertEqual(report[0]["action"], "dry_run")
        self.assertEqual(report[0]["order"]["take_profit"], 102.96)
        self.assertEqual(
            report[0]["order"]["remaining_take_profit_pct"], 4.0
        )
        self.assertEqual(report[0]["order"]["live_price_change_pct"], -1.0)

    def test_price_at_original_target_blocks_entry(self):
        report = self._execute_at_price(104.0)

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("reached or passed", report[0]["reason"])

    def test_more_than_two_percent_down_blocks_entry(self):
        report = self._execute_at_price(97.0)

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("fell 3.00%", report[0]["reason"])
        self.assertIn("2% max downside", report[0]["reason"])

    def test_required_missing_reference_price_blocks_entry(self):
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(
                self.execution, "get_open_buy_orders", return_value=[]
            ),
            patch.object(
                self.execution, "get_quote", return_value={"ask": 100.0}
            ),
        ):
            report = self.execution.execute_signals(
                [buy_decision()],
                require_reference_price=True,
            )

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("no reference price", report[0]["reason"])

    def test_submission_too_close_to_market_close_is_blocked(self):
        et = ZoneInfo("America/New_York")
        clock = SimpleNamespace(
            is_open=True,
            timestamp=datetime(2026, 7, 23, 15, 59, tzinfo=et),
            next_close=datetime(2026, 7, 23, 16, 0, tzinfo=et),
        )
        submit = Mock(return_value={"id": "paper-order"})
        with (
            patch.object(self.execution, "get_account", return_value=self.account),
            patch.object(self.execution, "get_positions", return_value=[]),
            patch.object(
                self.execution, "get_open_buy_orders", return_value=[]
            ),
            patch.object(
                self.execution, "get_quote", return_value={"ask": 100.0}
            ),
            patch.object(
                self.execution, "get_market_clock", return_value=clock
            ),
            patch.object(self.execution, "place_bracket_order", submit),
        ):
            report = self.execution.execute_signals(
                [buy_decision()],
                submit=True,
                reference_prices={"AAPL": 100.0},
            )

        self.assertEqual(report[0]["action"], "skipped")
        self.assertIn("to market close", report[0]["reason"])
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
