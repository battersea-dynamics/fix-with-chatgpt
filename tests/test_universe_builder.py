import os
import unittest
from unittest import mock

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from tools import universe_builder


FAKE_ASSETS = [
    {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "TXCO", "name": "Texas Co", "exchange": "TXSE", "tradable": True},
    {"symbol": "FUTR", "name": "Future Co", "exchange": "ZZZZ", "tradable": True},
    {"symbol": "DEAD", "name": "Dead Co", "exchange": "NYSE", "tradable": False},
    {"symbol": "MS.PRO", "name": "Pref", "exchange": "NYSE", "tradable": True},
    {"symbol": "SPYX", "name": "Some ETF", "exchange": "NYSE", "tradable": True},
]


class ListCandidateSymbolsTest(unittest.TestCase):
    def test_unknown_exchanges_do_not_crash_and_txse_kept(self):
        with mock.patch.object(universe_builder, "_trading_client") as client:
            client.get.return_value = FAKE_ASSETS
            result = universe_builder.list_candidate_symbols()
        self.assertEqual(result, ["AAPL", "TXCO"])
        client.get.assert_called_once()

    def test_empty_response(self):
        with mock.patch.object(universe_builder, "_trading_client") as client:
            client.get.return_value = None
            self.assertEqual(universe_builder.list_candidate_symbols(), [])


if __name__ == "__main__":
    unittest.main()
