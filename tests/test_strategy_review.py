import unittest
from datetime import datetime, timedelta, timezone

from tools.strategy_review import snapshot_diagnostic


class SnapshotDiagnosticTests(unittest.TestCase):
    def test_uses_first_signal_per_day_and_only_prices_after_decision(self):
        at = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)
        row = dict(date="2026-10-01", symbol="A", decision_at=at,
                   verified=True, stop_loss_pct=2, net_score=.2, analysis_price=100)
        scans = {(row["date"], "A"): [(at - timedelta(minutes=5), 500),
                                     (at + timedelta(minutes=30), 101),
                                     (at + timedelta(minutes=60), 102)]}
        result = snapshot_diagnostic([row, {**row, "decision_at": at + timedelta(minutes=30)}], scans, .2)
        self.assertEqual(result["selected_symbol_days"], 1)
        self.assertAlmostEqual(result["mean_next_snapshot_return_pct"], 1)
        self.assertAlmostEqual(result["mean_last_available_snapshot_return_pct"], 2)

    def test_missing_prices_are_reported_and_unverified_signals_excluded(self):
        row = dict(date="2026-10-01", symbol="A", decision_at=datetime.now(timezone.utc),
                   verified=True, stop_loss_pct=2, net_score=.2, analysis_price=100)
        result = snapshot_diagnostic([row, {**row, "symbol": "B", "verified": False}], {}, .2)
        self.assertEqual(result["selected_symbol_days"], 1)
        self.assertEqual(result["missing_later_snapshot"], 1)
        self.assertIsNone(result["mean_next_snapshot_return_pct"])
