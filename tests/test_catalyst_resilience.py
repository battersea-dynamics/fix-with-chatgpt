import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_catalysts():
    class ConnectionError(Exception):
        pass

    class Timeout(Exception):
        pass

    class ReadTimeout(Timeout):
        pass

    class HTTPError(Exception):
        pass

    requests = types.ModuleType("requests")
    requests.exceptions = types.SimpleNamespace(
        ConnectionError=ConnectionError,
        Timeout=Timeout,
        ReadTimeout=ReadTimeout,
        HTTPError=HTTPError,
    )
    requests.HTTPError = HTTPError
    requests.Session = Mock

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None

    enums = types.ModuleType("alpaca.data.enums")
    enums.CorporateActionsType = types.SimpleNamespace(CASH_DIVIDEND="cash")

    historical = types.ModuleType(
        "alpaca.data.historical.corporate_actions"
    )
    client = Mock()
    historical.CorporateActionsClient = lambda *args, **kwargs: client

    requests_module = types.ModuleType("alpaca.data.requests")
    requests_module.CorporateActionsRequest = lambda **kwargs: kwargs

    spec = importlib.util.spec_from_file_location(
        "catalysts_under_test", ROOT / "tools" / "catalysts.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "requests": requests,
        "dotenv": dotenv,
        "alpaca.data.enums": enums,
        "alpaca.data.historical.corporate_actions": historical,
        "alpaca.data.requests": requests_module,
    }):
        spec.loader.exec_module(module)
    module._MIN_CALL_INTERVAL = 0
    module._RETRY_BACKOFF_SECONDS = 0
    return module


def successful_response(payload):
    response = Mock(status_code=200, headers={})
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class FinnhubRetryTests(unittest.TestCase):
    def setUp(self):
        self.catalysts = load_catalysts()
        self.token = patch.dict(os.environ, {"FINNHUB_API_KEY": "test"})
        self.token.start()
        self.addCleanup(self.token.stop)

    def test_read_timeout_is_retried(self):
        response = successful_response({"ok": True})
        self.catalysts._session.get = Mock(side_effect=[
            self.catalysts.requests.exceptions.ReadTimeout("slow"),
            response,
        ])

        with patch.object(self.catalysts.time, "sleep"):
            result = self.catalysts._finnhub_get("test", {})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.catalysts._session.get.call_count, 2)

    def test_temporary_http_error_is_retried(self):
        unavailable = Mock(status_code=503, headers={})
        response = successful_response({"ok": True})
        self.catalysts._session.get = Mock(
            side_effect=[unavailable, response]
        )

        with patch.object(self.catalysts.time, "sleep"):
            result = self.catalysts._finnhub_get("test", {})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.catalysts._session.get.call_count, 2)

    def test_exhausted_timeouts_raise_temporary_unavailable(self):
        self.catalysts._session.get = Mock(
            side_effect=self.catalysts.requests.exceptions.ReadTimeout("slow")
        )

        with patch.object(self.catalysts.time, "sleep"):
            with self.assertRaises(self.catalysts.FinnhubUnavailableError):
                self.catalysts._finnhub_get("test", {})

        self.assertEqual(
            self.catalysts._session.get.call_count,
            self.catalysts._RETRIES,
        )

    def test_authentication_error_is_not_hidden_or_retried(self):
        response = Mock(status_code=401, headers={})
        response.raise_for_status.side_effect = (
            self.catalysts.requests.HTTPError("401")
        )
        self.catalysts._session.get = Mock(return_value=response)

        with self.assertRaises(self.catalysts.requests.HTTPError):
            self.catalysts._finnhub_get("test", {})

        self.catalysts._session.get.assert_called_once()


class CatalystFallbackTests(unittest.TestCase):
    def setUp(self):
        self.catalysts = load_catalysts()

    def test_prescan_continues_without_boost_after_retries(self):
        diagnostics = []
        error = self.catalysts.FinnhubUnavailableError("temporary outage")
        with patch.object(self.catalysts, "_finnhub_get", side_effect=error):
            result = self.catalysts.prescan_earnings(
                ["AAA"], diagnostics=diagnostics
            )

        self.assertEqual(result, {})
        self.assertEqual(diagnostics[0]["operation"], "earnings_prescan")
        self.assertIn("without catalyst boost", diagnostics[0]["fallback"])

    def test_bulk_earnings_uses_one_request(self):
        payload = {
            "earningsCalendar": [
                {"symbol": "AAA", "date": "2026-08-05", "hour": "bmo"},
                {"symbol": "OTHER", "date": "2026-08-05"},
            ]
        }
        with patch.object(
            self.catalysts, "_finnhub_get", return_value=payload
        ) as request:
            result = self.catalysts.get_upcoming_earnings_bulk(["AAA", "BBB"])

        request.assert_called_once()
        self.assertEqual(len(result["AAA"]), 1)
        self.assertEqual(result["BBB"], [])
        self.assertNotIn("symbol", request.call_args.args[1])

    def test_one_news_failure_marks_only_that_symbol_incomplete(self):
        error = self.catalysts.FinnhubUnavailableError("news unavailable")
        with (
            patch.object(
                self.catalysts,
                "get_upcoming_dividends",
                return_value={},
            ),
            patch.object(
                self.catalysts,
                "get_upcoming_earnings_bulk",
                return_value={"AAA": [], "BBB": []},
            ),
            patch.object(
                self.catalysts,
                "get_recent_news",
                side_effect=[[], error],
            ),
        ):
            report = self.catalysts.build_catalyst_report(["AAA", "BBB"])

        self.assertTrue(report["AAA"]["data_complete"])
        self.assertFalse(report["BBB"]["data_complete"])
        self.assertEqual(report["BBB"]["data_status"]["news"], "unavailable")
        self.assertIn("news unavailable", report["BBB"]["data_errors"])

    def test_bulk_earnings_failure_marks_every_symbol_incomplete(self):
        error = self.catalysts.FinnhubUnavailableError(
            "earnings unavailable"
        )
        with (
            patch.object(
                self.catalysts,
                "get_upcoming_dividends",
                return_value={},
            ),
            patch.object(
                self.catalysts,
                "get_upcoming_earnings_bulk",
                side_effect=error,
            ),
            patch.object(
                self.catalysts,
                "get_recent_news",
                return_value=[],
            ),
        ):
            report = self.catalysts.build_catalyst_report(["AAA", "BBB"])

        self.assertFalse(report["AAA"]["data_complete"])
        self.assertFalse(report["BBB"]["data_complete"])
        self.assertEqual(
            report["AAA"]["data_status"]["earnings"],
            "unavailable",
        )

    def test_incomplete_symbol_is_skipped_while_healthy_symbol_continues(self):
        candidates = [
            types.SimpleNamespace(symbol="AAA"),
            types.SimpleNamespace(symbol="BBB"),
        ]
        report = {
            "AAA": {"data_complete": True, "data_errors": []},
            "BBB": {
                "data_complete": False,
                "data_errors": ["finnhub/company-news timed out"],
            },
        }

        ready, skips = self.catalysts.partition_complete_evidence(
            candidates,
            report,
        )

        self.assertEqual([candidate.symbol for candidate in ready], ["AAA"])
        self.assertEqual(skips[0]["symbol"], "BBB")
        self.assertEqual(skips[0]["action"], "skipped")
        self.assertIn("debate and order skipped", skips[0]["reason"])

    def test_missing_report_entry_fails_closed(self):
        candidate = types.SimpleNamespace(symbol="AAA")

        ready, skips = self.catalysts.partition_complete_evidence(
            [candidate],
            {},
        )

        self.assertEqual(ready, [])
        self.assertEqual(skips[0]["symbol"], "AAA")
        self.assertIn("unavailable", skips[0]["reason"])


if __name__ == "__main__":
    unittest.main()
