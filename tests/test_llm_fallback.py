import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


@unittest.skipUnless(importlib.util.find_spec("crewai"), "CrewAI not installed")
class GeminiFallbackTests(unittest.TestCase):
    def setUp(self):
        from agents import llm_runner
        self.r = llm_runner
        self.tmp = tempfile.TemporaryDirectory()
        self.sleeps = []
        self.models = []
        self.outcomes = []  # per kickoff: Exception or output
        r = self.r

        def fake_crew(agents, tasks, verbose):
            crew = MagicMock()

            def kickoff():
                self.models.append(agents[0].llm)
                o = self.outcomes.pop(0)
                if isinstance(o, Exception):
                    raise o
                return SimpleNamespace(tasks_output=[SimpleNamespace(pydantic=o)])
            crew.kickoff.side_effect = kickoff
            return crew

        self.patches = [
            patch.object(r, "USAGE_PATH", Path(self.tmp.name) / "u.json"),
            patch.object(r, "Crew", side_effect=fake_crew),
            patch.object(r, "gemini_llm", side_effect=lambda m=r.GEMINI_MODEL: m),
            patch.object(r.time, "sleep", side_effect=self.sleeps.append),
            patch.object(r, "_exhausted_models", set()),
            patch.object(r, "_daily_quota_exhausted", False),
            patch.object(r, "_run_started_at", r.time.monotonic()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_task(self):
        return self.r.run_task(SimpleNamespace(llm=None), MagicMock(), "bull", "AAPL")

    def assert_no_long_sleeps(self):
        self.assertTrue(all(s <= 10 for s in self.sleeps), self.sleeps)

    def test_model_order(self):
        self.assertEqual(self.r.GEMINI_MODELS, [
            "gemini/gemini-3.5-flash-lite",
            "gemini/gemini-3.1-flash-lite",
            "gemini/gemini-3.6-flash",
        ])

    def test_falls_back_to_second_model_on_503(self):
        out = SimpleNamespace(symbol="AAPL")
        self.outcomes = [Exception("503 UNAVAILABLE high demand"), out]
        self.assertIs(self.run_task(), out)
        self.assertEqual(self.models, self.r.GEMINI_MODELS[:2])
        self.assert_no_long_sleeps()

    def test_daily_quota_model_skipped_for_rest_of_run(self):
        out = SimpleNamespace(symbol="AAPL")
        self.outcomes = [Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDay"), out, out]
        self.run_task()
        self.run_task()
        first = self.r.GEMINI_MODELS[0]
        self.assertEqual(self.models.count(first), 1)
        self.assertEqual(self.models[-1], self.r.GEMINI_MODELS[1])

    def test_returns_none_when_all_models_fail(self):
        n = len(self.r.GEMINI_MODELS)
        self.outcomes = [Exception("RateLimitError 429")] * (2 * n)
        self.assertIsNone(self.run_task())
        self.assertEqual(len(self.models), 2 * n)
        self.assertNotIn(45, self.sleeps)
        self.assert_no_long_sleeps()
        self.assertEqual(self.sleeps.count(self.r.ALL_MODELS_BACKOFF_SECONDS), 1)

    def test_non_retryable_error_raises(self):
        self.outcomes = [ValueError("bad schema")]
        with self.assertRaises(ValueError):
            self.run_task()

    def test_time_budget_guard_skips_calls(self):
        with patch.object(self.r, "_run_started_at",
                          self.r.time.monotonic() - self.r.RUN_TIME_BUDGET_SECONDS - 1):
            self.assertIsNone(self.run_task())
        self.assertEqual(self.models, [])

    def test_usage_file_backward_compatible(self):
        import json
        from datetime import datetime
        today = datetime.now(self.r.ET).date().isoformat()
        self.r.USAGE_PATH.write_text(json.dumps({"date": today, "attempts": 5}))
        self.assertTrue(self.r._reserve_daily_call("gemini/x"))
        data = json.loads(self.r.USAGE_PATH.read_text())
        self.assertEqual(data["attempts"], 6)
        self.assertEqual(data["per_model"], {"gemini/x": 1})


if __name__ == "__main__":
    unittest.main()
