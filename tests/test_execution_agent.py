import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_execution_agent():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None

    signal_agent = types.ModuleType("agents.signal_agent")
    signal_agent.SignalDecision = object

    broker = types.ModuleType("tools.broker")
    broker.get_account = lambda: None
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


if __name__ == "__main__":
    unittest.main()
