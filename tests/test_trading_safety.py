import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[1]


class SignalDecision(BaseModel):
    symbol: str
    signal: str
    confidence: float
    take_profit_pct: float
    stop_loss_pct: float
    reasoning: str
    numbers_verified: bool = True
    unverified_numbers: list[str] = Field(default_factory=list)


class BullCase(BaseModel):
    symbol: str
    bull_case: str
    bull_confidence: float
    take_profit_pct: float
    stop_loss_pct: float


class BearCase(BaseModel):
    symbol: str
    bear_case: str
    bear_risk: float


def load_regular_trader():
    bull_module = types.ModuleType("agents.bull_agent")
    bull_module.BullCase = BullCase
    bear_module = types.ModuleType("agents.bear_agent")
    bear_module.BearCase = BearCase
    signal_module = types.ModuleType("agents.signal_agent")
    signal_module.SignalDecision = SignalDecision
    spec = importlib.util.spec_from_file_location(
        "regular_trader_under_test", ROOT / "tools" / "trader.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "agents.bull_agent": bull_module,
        "agents.bear_agent": bear_module,
        "agents.signal_agent": signal_module,
    }):
        spec.loader.exec_module(module)
    return module


def load_premarket_trader(data_dir: Path):
    signal_module = types.ModuleType("agents.signal_agent")
    signal_module.SignalDecision = SignalDecision
    datapaths = types.ModuleType("tools.datapaths")
    datapaths.list_path = lambda name: data_dir / name
    calendar = types.ModuleType("tools.market_calendar")
    calendar.ET = ZoneInfo("America/New_York")
    calendar.is_market_open_today = lambda: True
    spec = importlib.util.spec_from_file_location(
        "premarket_trader_under_test",
        ROOT / "tools" / "premarket_trader.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "agents.signal_agent": signal_module,
        "tools.datapaths": datapaths,
        "tools.market_calendar": calendar,
    }):
        spec.loader.exec_module(module)
    return module


class TradingSafetyTests(unittest.TestCase):
    def test_regular_exact_threshold_is_buy(self):
        trader = load_regular_trader()
        decision = trader.decide(
            BullCase(
                symbol="TEST",
                bull_case="bull",
                bull_confidence=0.95,
                take_profit_pct=5.0,
                stop_loss_pct=3.0,
            ),
            BearCase(symbol="TEST", bear_case="bear", bear_risk=0.75),
        )
        self.assertEqual(decision.signal, "buy")
        self.assertEqual(decision.confidence, 0.6)

    def test_premarket_exact_threshold_is_buy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_cases(root, bull_verified=True, bear_verified=True)
            trader = load_premarket_trader(root)
            decisions = trader.decide_premarket_trades(
                root / "premarket_decisions.json"
            )
        self.assertEqual(decisions[0].signal, "buy")
        self.assertEqual(decisions[0].confidence, 0.6)

    def test_premarket_verification_failure_reaches_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_cases(
                root,
                bull_verified=True,
                bear_verified=False,
                bear_bad=["cited 4e+08 - no matching value"],
            )
            trader = load_premarket_trader(root)
            decisions = trader.decide_premarket_trades(
                root / "premarket_decisions.json"
            )
        self.assertFalse(decisions[0].numbers_verified)
        self.assertEqual(
            decisions[0].unverified_numbers,
            ["bear: cited 4e+08 - no matching value"],
        )

    def test_missing_verification_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_cases(root, bull_verified=None, bear_verified=None)
            trader = load_premarket_trader(root)
            decisions = trader.decide_premarket_trades(
                root / "premarket_decisions.json"
            )
        self.assertFalse(decisions[0].numbers_verified)

    @staticmethod
    def _write_cases(
        root: Path,
        bull_verified: bool | None,
        bear_verified: bool | None,
        bear_bad: list[str] | None = None,
    ):
        bull = {
            "symbol": "TEST",
            "bull_case": "bull",
            "bull_confidence": 0.95,
            "take_profit_pct": 5.0,
            "stop_loss_pct": 3.0,
        }
        bear = {
            "symbol": "TEST",
            "bear_case": "bear",
            "bear_risk": 0.75,
        }
        if bull_verified is not None:
            bull["numbers_verified"] = bull_verified
            bull["unverified_numbers"] = []
        if bear_verified is not None:
            bear["numbers_verified"] = bear_verified
            bear["unverified_numbers"] = bear_bad or []
        (root / "premarket_bull_cases.json").write_text(
            json.dumps({"cases": {"TEST": bull}})
        )
        (root / "premarket_bear_cases.json").write_text(
            json.dumps({"cases": {"TEST": bear}})
        )


if __name__ == "__main__":
    unittest.main()
