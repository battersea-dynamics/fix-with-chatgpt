import importlib.util
import os
import tempfile
import unittest


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
        from agents.llm_runner import GEMINI_MODEL, gemini_llm

        llm = gemini_llm()
        self.assertEqual(GEMINI_MODEL, "gemini/gemini-3.1-flash-lite")
        self.assertEqual(llm.provider, "gemini")


if __name__ == "__main__":
    unittest.main()
