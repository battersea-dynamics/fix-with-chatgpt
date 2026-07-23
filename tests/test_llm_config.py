import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(
    importlib.util.find_spec("crewai"),
    "CrewAI is installed in CI/GitHub Actions, not this lightweight workspace",
)
class LLMConfigurationTests(unittest.TestCase):
    def test_native_gemini_provider_is_available(self):
        isolated_home = tempfile.mkdtemp()
        os.environ["HOME"] = isolated_home
        os.environ["XDG_DATA_HOME"] = isolated_home
        # Do not inherit a developer machine's proxy configuration while
        # constructing the provider client; this test performs no API call.
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        os.environ.setdefault("GEMINI_API_KEY", "configuration-test-key")
        from agents.llm_runner import (
            CALL_SPACING_SECONDS,
            GEMINI_MODEL,
            MAX_DAILY_CALL_ATTEMPTS,
            gemini_llm,
        )

        llm = gemini_llm()
        self.assertEqual(GEMINI_MODEL, "gemini/gemini-3.1-flash-lite")
        self.assertEqual(CALL_SPACING_SECONDS, 8)
        self.assertEqual(MAX_DAILY_CALL_ATTEMPTS, 450)
        self.assertEqual(llm.provider, "gemini")

    def test_daily_safety_ceiling_is_persisted(self):
        from agents import llm_runner

        with tempfile.TemporaryDirectory() as folder:
            usage_path = Path(folder) / "llm_usage.json"
            with (
                patch.object(llm_runner, "USAGE_PATH", usage_path),
                patch.object(llm_runner, "MAX_DAILY_CALL_ATTEMPTS", 2),
            ):
                self.assertTrue(llm_runner._reserve_daily_call())
                self.assertTrue(llm_runner._reserve_daily_call())
                self.assertFalse(llm_runner._reserve_daily_call())


if __name__ == "__main__":
    unittest.main()
