import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def _module(name):
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def load_broker():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None

    requests = _module("requests")
    request_exceptions = types.ModuleType("requests.exceptions")

    class RequestException(Exception):
        pass

    class ChunkedEncodingError(RequestException):
        pass

    class RequestsConnectionError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    request_exceptions.RequestException = RequestException
    request_exceptions.ChunkedEncodingError = ChunkedEncodingError
    request_exceptions.ConnectionError = RequestsConnectionError
    request_exceptions.Timeout = Timeout
    requests.exceptions = request_exceptions

    alpaca = _module("alpaca")
    alpaca_common = _module("alpaca.common")
    alpaca_exceptions = types.ModuleType("alpaca.common.exceptions")

    class APIError(Exception):
        def __init__(self, message, status_code=None):
            super().__init__(message)
            self.status_code = status_code

    alpaca_exceptions.APIError = APIError

    trading_client = Mock()
    data_client = Mock()

    alpaca_trading = _module("alpaca.trading")
    trading_client_module = types.ModuleType("alpaca.trading.client")
    trading_client_module.TradingClient = Mock(return_value=trading_client)
    trading_requests = types.ModuleType("alpaca.trading.requests")

    class Request:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    trading_requests.GetOrdersRequest = Request
    trading_requests.MarketOrderRequest = Request
    trading_requests.StopLossRequest = Request
    trading_requests.TakeProfitRequest = Request

    trading_enums = types.ModuleType("alpaca.trading.enums")
    trading_enums.OrderClass = SimpleNamespace(BRACKET="bracket")
    trading_enums.OrderSide = SimpleNamespace(BUY="buy", SELL="sell")
    trading_enums.QueryOrderStatus = SimpleNamespace(OPEN="open")
    trading_enums.TimeInForce = SimpleNamespace(DAY="day", GTC="gtc")

    alpaca_data = _module("alpaca.data")
    data_historical = types.ModuleType("alpaca.data.historical")
    data_historical.StockHistoricalDataClient = Mock(return_value=data_client)
    data_requests = types.ModuleType("alpaca.data.requests")
    data_requests.StockLatestQuoteRequest = Request

    stubs = {
        "dotenv": dotenv,
        "requests": requests,
        "requests.exceptions": request_exceptions,
        "alpaca": alpaca,
        "alpaca.common": alpaca_common,
        "alpaca.common.exceptions": alpaca_exceptions,
        "alpaca.trading": alpaca_trading,
        "alpaca.trading.client": trading_client_module,
        "alpaca.trading.requests": trading_requests,
        "alpaca.trading.enums": trading_enums,
        "alpaca.data": alpaca_data,
        "alpaca.data.historical": data_historical,
        "alpaca.data.requests": data_requests,
    }

    spec = importlib.util.spec_from_file_location(
        "broker_under_test", ROOT / "tools" / "broker.py"
    )
    module = importlib.util.module_from_spec(spec)
    credentials = {
        "ALPACA_API_KEY": "paper-key",
        "ALPACA_SECRET_KEY": "paper-secret",
    }
    with (
        patch.dict(sys.modules, stubs),
        patch.dict(os.environ, credentials, clear=False),
    ):
        spec.loader.exec_module(module)
    return module


def accepted_order(client_order_id="ta-test-AAPL"):
    return SimpleNamespace(
        id="paper-order-id",
        client_order_id=client_order_id,
        symbol="AAPL",
        qty="2",
        side=SimpleNamespace(value="buy"),
        status=SimpleNamespace(value="accepted"),
        legs=[],
    )


class BracketOrderReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.broker = load_broker()
        self.client_id = "ta-test-AAPL"

    def place(self):
        return self.broker.place_bracket_order(
            "AAPL",
            2,
            105.0,
            98.0,
            client_order_id=self.client_id,
        )

    def test_normal_submission_adds_no_lookup_call(self):
        self.broker.trading_client.submit_order.return_value = accepted_order()

        result = self.place()

        self.assertNotIn("submission_reconciled", result)
        self.broker.trading_client.submit_order.assert_called_once()
        self.broker.trading_client.get_order_by_client_id.assert_not_called()

    def test_connection_failure_recovers_existing_order_once(self):
        self.broker.trading_client.submit_order.side_effect = (
            self.broker.RequestsConnectionError("connection dropped")
        )
        self.broker.trading_client.get_order_by_client_id.return_value = (
            accepted_order()
        )

        result = self.place()

        self.assertTrue(result["submission_reconciled"])
        self.broker.trading_client.submit_order.assert_called_once()
        self.broker.trading_client.get_order_by_client_id.assert_called_once_with(
            self.client_id
        )

    def test_server_error_recovers_existing_order_once(self):
        self.broker.trading_client.submit_order.side_effect = (
            self.broker.APIError("service unavailable", status_code=503)
        )
        self.broker.trading_client.get_order_by_client_id.return_value = (
            accepted_order()
        )

        result = self.place()

        self.assertTrue(result["submission_reconciled"])
        self.broker.trading_client.get_order_by_client_id.assert_called_once_with(
            self.client_id
        )

    def test_definitive_rejection_is_not_looked_up(self):
        rejected = self.broker.APIError("order rejected", status_code=422)
        self.broker.trading_client.submit_order.side_effect = rejected

        with self.assertRaises(self.broker.APIError) as raised:
            self.place()

        self.assertIs(raised.exception, rejected)
        self.broker.trading_client.get_order_by_client_id.assert_not_called()

    def test_failed_lookup_preserves_original_submission_error(self):
        original = self.broker.Timeout("submit timed out")
        self.broker.trading_client.submit_order.side_effect = original
        self.broker.trading_client.get_order_by_client_id.side_effect = (
            self.broker.APIError("order not found", status_code=404)
        )

        with self.assertRaises(self.broker.Timeout) as raised:
            self.place()

        self.assertIs(raised.exception, original)
        self.broker.trading_client.submit_order.assert_called_once()
        self.broker.trading_client.get_order_by_client_id.assert_called_once_with(
            self.client_id
        )


if __name__ == "__main__":
    unittest.main()
