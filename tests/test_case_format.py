import importlib.util
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agents.case_format import build_evidence_facts, format_evidence


ET = ZoneInfo("America/New_York")


def scan():
    return SimpleNamespace(
        symbol="TEST",
        sector="Technology",
        close=10.0,
        rel_volume=2.5,
        pct_change=12.0,
        ma_distance=8.0,
    )


class DebateEvidenceFormattingTests(unittest.TestCase):
    def test_dates_are_anchored_to_the_us_session(self):
        catalysts = {
            "earnings": [{"date": "2026-08-05", "hour": "bmo"}],
            "dividends": [],
            "news": [
                {"headline": "Same-day update", "date": "2026-08-05"},
                {"headline": "Earlier update", "date": "2026-08-03"},
            ],
        }

        evidence = format_evidence(
            scan(),
            catalysts,
            session_date=datetime(2026, 8, 5, 10, 15, tzinfo=ET),
        )

        self.assertIn("US trading-session date 2026-08-05", evidence)
        self.assertIn('"days_from_session": 0', evidence)
        self.assertIn('"days_from_session": -2', evidence)
        self.assertIn("same US trading-session date", evidence)
        self.assertNotIn("days_from_session", catalysts["earnings"][0])

    def test_compact_momentum_history_is_visible_to_both_agents(self):
        history = {
            "observations": 3,
            "consecutive_scans": 3,
            "return_since_previous_pct": 1.25,
            "return_since_first_seen_pct": 4.5,
            "drawdown_from_observed_high_pct": 0.0,
            "rel_volume_rate_per_30m": 0.4,
            "setup": "sustained_continuation",
            "unused_internal_field": "must not leak",
        }

        evidence = format_evidence(
            scan(),
            {"earnings": [], "dividends": [], "news": []},
            momentum_context=history,
            session_date="2026-08-05",
        )

        self.assertIn("observed 30-minute shortlist snapshots", evidence)
        self.assertIn('"consecutive_scans": 3', evidence)
        self.assertIn('"return_since_first_seen_pct": 4.5', evidence)
        self.assertNotIn("unused_internal_field", evidence)

    def test_missing_history_is_explicitly_unknown(self):
        evidence = format_evidence(
            scan(),
            {"earnings": [], "dividends": [], "news": []},
            session_date="2026-08-05",
        )

        self.assertIn("persistence as unknown", evidence)

    @unittest.skipUnless(
        importlib.util.find_spec("alpaca"),
        "Alpaca dependencies are installed in CI, not this workspace",
    )
    def test_new_derived_numbers_are_available_to_numeric_verification(self):
        from tools.case_verifier import verify_text

        facts = build_evidence_facts(
            {
                "earnings": [],
                "dividends": [],
                "news": [{"headline": "Update", "date": "2026-08-03"}],
            },
            momentum_context={
                "return_since_first_seen_pct": 4.5,
                "rel_volume_rate_per_30m": 0.4,
            },
            session_date="2026-08-05",
        )

        ok, unmatched = verify_text(
            "Price improved 4.5% while relative-volume pace added 0.4x.",
            facts,
        )

        self.assertTrue(ok)
        self.assertEqual(unmatched, [])
        self.assertEqual(facts["catalysts"]["news"][0]["days_from_session"], -2)


if __name__ == "__main__":
    unittest.main()
