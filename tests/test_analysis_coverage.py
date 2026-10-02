import unittest

from tools.analysis_coverage import summarize_analysis


class AnalysisCoverageTests(unittest.TestCase):
    def test_missing_debate_is_degraded_and_held_stock_is_separate(self):
        result = summarize_analysis(["HELD", "A", "B"], ["A", "B"], ["A"], ["HELD"], [])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["missing_decision_symbols"], ["B"])
        self.assertEqual(result["held_removed"], ["HELD"])

    def test_all_hold_decisions_are_complete_but_missing_evidence_is_degraded(self):
        result = summarize_analysis(["A"], ["A"], ["A"], [], [])
        self.assertEqual(result["status"], "complete")
        result = summarize_analysis(["A", "B"], ["A"], ["A"], [], [{"symbol": "B"}])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["missing_decision_symbols"], [])
        self.assertEqual(result["evidence_skips"], ["B"])
