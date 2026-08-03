"""
tools/catalysts.py

Stage 2 of the daily pipeline: context for the shortlist only.

The scanner (stage 1) answers "what is moving?" — this module answers
"is there a *reason* it's moving, or about to?" Three sources:

  earnings   - Finnhub earnings calendar. An earnings date inside the
               next few days is the single most common cause of a big
               overnight gap — for or against you. The signal agent
               needs to know it's there.
  dividends  - Alpaca's corporate-actions endpoint. An ex-dividend
               date matters for an intraday system because the price
               mechanically drops by the dividend on the ex date, which
               can trip a stop-loss that had nothing to do with the
               trade thesis.
  news       - Finnhub company news headlines. Raw text — no scoring
               here; interpreting headlines is precisely the judgment
               call we're saving the LLM for.

This runs only on the 10-20 shortlisted names, not the whole universe.
Earnings are fetched once in bulk for the complete shortlist window; news
still needs one call per symbol. Finnhub traffic is paced and retried centrally.

Plain dicts out. Like the scanner, there is deliberately no LLM here —
gathering evidence and judging evidence are separate stages.
"""

import os
import sys
import time
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest

load_dotenv()

FINNHUB_BASE = "https://finnhub.io/api/v1"
EARNINGS_AHEAD_DAYS = 14
NEWS_BACK_DAYS = 7
MAX_HEADLINES = 5

_corp_actions_client = CorporateActionsClient(
    os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
)


# Finnhub's free tier allows 60 calls/min but also enforces a burst
# limit - fire 30 calls back-to-back and it starts resetting
# connections (found empirically: WinError 10054 mid-report). All
# Finnhub traffic funnels through this one helper, so pacing and
# retries live here and every caller inherits them.
_MIN_CALL_INTERVAL = 1.1   # seconds; ~55 calls/min, under the 60 cap
_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 5
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_last_call_time = 0.0
_session = requests.Session()


class FinnhubUnavailableError(RuntimeError):
    """Finnhub remained temporarily unavailable after bounded retries."""


def _retry_delay(attempt: int, response=None) -> float:
    """Bounded linear backoff, honouring Retry-After when supplied."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
    return _RETRY_BACKOFF_SECONDS * (attempt + 1)


def _finnhub_get(path: str, params: dict) -> dict | list:
    global _last_call_time
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        raise RuntimeError(
            "Missing FINNHUB_API_KEY - get a free key at "
            "https://finnhub.io/register and add it to .env"
        )

    for attempt in range(_RETRIES):
        wait = _MIN_CALL_INTERVAL - (time.monotonic() - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()

        try:
            response = _session.get(
                f"{FINNHUB_BASE}/{path}",
                params={**params, "token": key},
                timeout=(5, 15),
            )
            if response.status_code in _RETRYABLE_STATUS_CODES:
                if attempt == _RETRIES - 1:
                    raise FinnhubUnavailableError(
                        f"finnhub/{path} returned HTTP "
                        f"{response.status_code} after {_RETRIES} attempts"
                    )
                delay = _retry_delay(attempt, response)
                print(
                    f"[catalysts] HTTP {response.status_code} from "
                    f"finnhub/{path}; retrying in {delay:g}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt == _RETRIES - 1:
                raise FinnhubUnavailableError(
                    f"finnhub/{path} unavailable after {_RETRIES} attempts "
                    f"({type(exc).__name__})"
                ) from exc
            delay = _retry_delay(attempt)
            print(
                f"[catalysts] {type(exc).__name__} on finnhub/{path}; "
                f"retrying in {delay:g}s",
                file=sys.stderr,
            )
            time.sleep(delay)


def get_upcoming_earnings(symbol: str) -> list[dict]:
    """Earnings events for `symbol` in the next EARNINGS_AHEAD_DAYS."""
    today = date.today()
    data = _finnhub_get("calendar/earnings", {
        "symbol": symbol,
        "from": today.isoformat(),
        "to": (today + timedelta(days=EARNINGS_AHEAD_DAYS)).isoformat(),
    })
    return [
        {
            "date": e.get("date"),
            "hour": e.get("hour"),          # bmo = before open, amc = after close
            "eps_estimate": e.get("epsEstimate"),
            "revenue_estimate": e.get("revenueEstimate"),
        }
        for e in data.get("earningsCalendar", [])
    ]


def get_upcoming_earnings_bulk(
    symbols: list[str],
) -> dict[str, list[dict]]:
    """One Finnhub request for all shortlisted symbols' earnings.

    The calendar endpoint without a symbol returns the complete date window.
    Intersecting locally replaces one request per stock with one request for
    the cycle, reducing both latency and exposure to transient failures.
    """
    today = date.today()
    data = _finnhub_get("calendar/earnings", {
        "from": today.isoformat(),
        "to": (today + timedelta(days=EARNINGS_AHEAD_DAYS)).isoformat(),
    })
    requested = set(symbols)
    earnings = {symbol: [] for symbol in symbols}
    for event in data.get("earningsCalendar", []):
        symbol = event.get("symbol")
        if symbol not in requested:
            continue
        earnings[symbol].append({
            "date": event.get("date"),
            "hour": event.get("hour"),
            "eps_estimate": event.get("epsEstimate"),
            "revenue_estimate": event.get("revenueEstimate"),
        })
    return earnings


def get_recent_news(symbol: str) -> list[dict]:
    """Most recent headlines for `symbol`, newest first, capped."""
    today = date.today()
    articles = _finnhub_get("company-news", {
        "symbol": symbol,
        "from": (today - timedelta(days=NEWS_BACK_DAYS)).isoformat(),
        "to": today.isoformat(),
    })
    articles = sorted(articles, key=lambda a: a.get("datetime", 0), reverse=True)
    return [
        {
            "headline": a.get("headline"),
            "source": a.get("source"),
            "date": date.fromtimestamp(a["datetime"]).isoformat()
            if a.get("datetime") else None,
        }
        for a in articles[:MAX_HEADLINES]
    ]


def get_sector(symbol: str) -> str:
    """
    Finnhub's industry classification for `symbol` (e.g. "Technology",
    "Biotechnology"), or "unknown" if Finnhub has no profile for it or
    the call fails. Best-effort: a missing sector is a labelling gap,
    never a reason to abort the scan.

    Why Finnhub and not Alpaca: Alpaca's asset metadata carries no
    sector/industry field at all (only symbol, name, exchange,
    tradable...), so the dynamic universe has no sector attached. This
    is the one source that fills it, at one call per symbol — which is
    why sector is resolved on the ~15-name shortlist, not the ~2,300
    symbol universe (that would be ~2,300 calls / ~40 min, well past
    the free tier).
    """
    try:
        profile = _finnhub_get("stock/profile2", {"symbol": symbol})
    except Exception:
        return "unknown"
    return (profile or {}).get("finnhubIndustry") or "unknown"


def get_upcoming_dividends(symbols: list[str]) -> dict[str, list[dict]]:
    """
    Cash dividends with ex-dates in the next two weeks, for all
    shortlisted symbols in one batched call (this endpoint accepts a
    symbol list, unlike the Finnhub ones).

    Subtlety found by testing: Alpaca's start/end filter applies to the
    *process date* (roughly the payable date), which trails the ex-date
    by weeks. So we query a wide future process window and filter on
    ex_date ourselves — querying start=today, end=today+14 directly
    would miss nearly every upcoming ex-date.
    """
    today = date.today()
    request = CorporateActionsRequest(
        symbols=symbols,
        types=[CorporateActionsType.CASH_DIVIDEND],
        start=today,
        end=today + timedelta(days=75),
    )
    data = _corp_actions_client.get_corporate_actions(request).data
    dividends: dict[str, list[dict]] = {}
    for action in data.get("cash_dividends", []):
        if action.ex_date is None or not (
            today <= action.ex_date <= today + timedelta(days=14)
        ):
            continue
        dividends.setdefault(action.symbol, []).append({
            "ex_date": action.ex_date.isoformat() if action.ex_date else None,
            "payable_date": action.payable_date.isoformat()
            if action.payable_date else None,
            "rate": action.rate,
        })
    return dividends


def prescan_earnings(
    universe: list[str],
    days_ahead: int = 3,
    diagnostics: list[dict] | None = None,
) -> dict[str, dict]:
    """
    PRE-SCAN mode: which of the (possibly thousands of) universe
    symbols report earnings in the next `days_ahead` days?

    This is a different animal from get_upcoming_earnings above, and
    the difference is why both exist. The shortlist-stage functions ask
    Finnhub about ONE symbol in depth — fine for 15 names, impossible
    for 2,000+ on a 60-calls/min free tier. But the earnings-calendar
    endpoint called WITHOUT a symbol returns every company reporting
    in the window in a single response, so the pre-scan is: one bulk
    call, then intersect with our universe in memory. Rate limits stop
    being a concern entirely.

    Returns {symbol: {"date", "hour", "days_until"}} for flagged
    symbols only. The scanner uses the keys as its boost set; the
    values ride along into the shortlist file so the signal agent can
    later see *why* something was flagged.
    """
    today = date.today()
    try:
        data = _finnhub_get("calendar/earnings", {
            "from": today.isoformat(),
            "to": (today + timedelta(days=days_ahead)).isoformat(),
        })
    except FinnhubUnavailableError as exc:
        warning = {
            "source": "finnhub",
            "operation": "earnings_prescan",
            "error": str(exc),
            "fallback": "scan continued without catalyst boost",
        }
        if diagnostics is not None:
            diagnostics.append(warning)
        print(f"[catalysts] {warning['error']}; "
              f"{warning['fallback']}", file=sys.stderr)
        return {}
    universe_set = set(universe)
    flagged: dict[str, dict] = {}
    for event in data.get("earningsCalendar", []):
        symbol = event.get("symbol")
        if symbol not in universe_set or not event.get("date"):
            continue
        event_date = date.fromisoformat(event["date"])
        # keep the soonest event per symbol
        existing = flagged.get(symbol)
        if existing and existing["date"] <= event["date"]:
            continue
        flagged[symbol] = {
            "date": event["date"],
            "hour": event.get("hour"),  # bmo/amc/dmh or blank
            "days_until": (event_date - today).days,
        }
    return flagged


def build_catalyst_report(symbols: list[str]) -> dict[str, dict]:
    """
    One dict per symbol with evidence and explicit source status. Temporary
    Finnhub failure is represented as incomplete data, never as an empty but
    successful "no catalyst" result. The pipeline deterministically skips
    incomplete symbols before the debate, so no order can be based on missing
    evidence while healthy symbols continue through the cycle.
    """
    dividends = get_upcoming_dividends(symbols)
    report = {
        symbol: {
            "earnings": [],
            "dividends": dividends.get(symbol, []),
            "news": [],
            "data_status": {
                "earnings": "pending",
                "dividends": "ok",
                "news": "pending",
            },
            "data_complete": True,
            "data_errors": [],
        }
        for symbol in symbols
    }

    try:
        earnings = get_upcoming_earnings_bulk(symbols)
    except FinnhubUnavailableError as exc:
        for symbol in symbols:
            report[symbol]["data_status"]["earnings"] = "unavailable"
            report[symbol]["data_complete"] = False
            report[symbol]["data_errors"].append(str(exc))
    else:
        for symbol in symbols:
            report[symbol]["earnings"] = earnings.get(symbol, [])
            report[symbol]["data_status"]["earnings"] = "ok"

    for symbol in symbols:
        try:
            report[symbol]["news"] = get_recent_news(symbol)
            report[symbol]["data_status"]["news"] = "ok"
        except FinnhubUnavailableError as exc:
            report[symbol]["data_status"]["news"] = "unavailable"
            report[symbol]["data_complete"] = False
            report[symbol]["data_errors"].append(str(exc))
    return report


def partition_complete_evidence(
    candidates: list,
    catalyst_report: dict[str, dict],
) -> tuple[list, list[dict]]:
    """Separate debate-ready candidates from deterministic safety skips."""
    ready = []
    skips = []
    for candidate in candidates:
        evidence = catalyst_report.get(candidate.symbol, {})
        if evidence.get("data_complete", False):
            ready.append(candidate)
            continue
        errors = evidence.get("data_errors") or [
            "Finnhub catalyst evidence unavailable"
        ]
        skips.append({
            "symbol": candidate.symbol,
            "action": "skipped",
            "reason": (
                "incomplete catalyst evidence; debate and order skipped: "
                + "; ".join(errors)
            ),
            "source": "catalyst_evidence",
        })
    return ready, skips


if __name__ == "__main__":
    import json
    import sys

    symbols = sys.argv[1:] or ["AAPL", "NVDA"]
    print(json.dumps(build_catalyst_report(symbols), indent=2))
