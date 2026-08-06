"""Build same-session momentum context from archived shortlist snapshots.

This module is deliberately deterministic and read-only with respect to prior
archives. Its compact output is shared symmetrically with the bull and bear
agents; it does not itself change the shortlist or make a trading decision.

The source observations are the regular-session ``shortlist_<HHMM>.json``
files already produced every cycle.  Consequently this adds no Alpaca request
and no Gemini call.  The snapshots are sparse by design, so the output uses
"observed" language and never claims to know the path between scans.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from tools.scanner import ScanResult


ET = ZoneInfo("America/New_York")


def _aware_et(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ET)
    return value.astimezone(ET)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _pct_change(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100


def _load_prior_scans(
    lists_dir: Path,
    generated_at: datetime,
    exclude_path: Path | None,
) -> list[dict[str, Any]]:
    """Load valid earlier shortlist archives from the same ET session."""
    current_time = _aware_et(generated_at)
    excluded = exclude_path.resolve() if exclude_path is not None else None
    scans: list[dict[str, Any]] = []

    if not lists_dir.exists():
        return scans

    for path in lists_dir.glob("shortlist_*.json"):
        if excluded is not None and path.resolve() == excluded:
            continue
        try:
            payload = json.loads(path.read_text())
            scan_time = _aware_et(datetime.fromisoformat(payload["generated_at"]))
            rows = payload["shortlist"]
            if not isinstance(rows, list):
                continue
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # A partially written or old malformed archive must not break a
            # trading cycle.  It simply cannot contribute history.
            continue

        if scan_time.date() != current_time.date() or scan_time >= current_time:
            continue

        by_symbol: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(rows, start=1):
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            try:
                by_symbol[str(row["symbol"])] = {
                    "at": scan_time,
                    "rank": rank,
                    "close": float(row["close"]),
                    "rel_volume": float(row["rel_volume"]),
                    "score": float(row["score"]),
                    "pct_change": float(row["pct_change"]),
                    "ma_distance": float(row["ma_distance"]),
                }
            except (KeyError, TypeError, ValueError):
                continue

        scans.append({"at": scan_time, "symbols": by_symbol})

    scans.sort(key=lambda scan: scan["at"])
    return scans


def _classify_setup(context: dict[str, Any], current: "ScanResult") -> str:
    """Assign a provisional descriptive label for shadow evaluation.

    The thresholds are intentionally conservative first guesses.  The label
    has no trading authority and will be calibrated against later outcomes.
    """
    consecutive = context["consecutive_scans"]
    last_return = context["return_since_previous_pct"]
    since_first = context["return_since_first_seen_pct"]
    drawdown = context["drawdown_from_observed_high_pct"]
    rank_improvement = context["rank_improvement"]
    price_acceleration = context["price_acceleration_pct_points"]
    volume_rate = context["rel_volume_rate_per_30m"]

    if consecutive < 2 or last_return is None:
        return "insufficient_history"

    # A bounce while the stock remains negative both on the day and versus its
    # moving average is not yet evidence of a durable reversal.
    if (
        current.pct_change < 0
        and current.ma_distance < 0
        and last_return > 0
        and consecutive <= 2
    ):
        return "falling_knife_rebound"

    # Loss of the observed high or a clearly negative latest interval after an
    # earlier rise is treated as deterioration, not generic "chasing".
    if drawdown <= -3 or (
        since_first is not None
        and since_first > 0
        and last_return <= -1
        and (price_acceleration is None or price_acceleration < 0)
    ):
        return "exhausted_or_reversing"

    if (
        consecutive >= 3
        and since_first is not None
        and since_first > 0
        and last_return > 0
        and drawdown > -2
        and (volume_rate is None or volume_rate >= 0)
    ):
        return "sustained_continuation"

    if (
        consecutive == 2
        and last_return > 0
        and drawdown > -2
        and (rank_improvement is None or rank_improvement >= 0)
    ):
        return "early_breakout"

    return "mixed"


def build_momentum_shadow(
    current: list["ScanResult"],
    generated_at: datetime,
    lists_dir: Path,
    current_output_path: Path | None = None,
) -> dict[str, Any]:
    """Return compact same-session history for each current candidate."""
    now = _aware_et(generated_at)
    prior_scans = _load_prior_scans(
        lists_dir=lists_dir,
        generated_at=now,
        exclude_path=current_output_path,
    )
    current_by_symbol = {row.symbol: row for row in current}
    current_scan = {
        "at": now,
        "symbols": {
            row.symbol: {
                "at": now,
                "rank": rank,
                "close": float(row.close),
                "rel_volume": float(row.rel_volume),
                "score": float(row.score),
                "pct_change": float(row.pct_change),
                "ma_distance": float(row.ma_distance),
            }
            for rank, row in enumerate(current, start=1)
        },
    }
    all_scans = [*prior_scans, current_scan]
    symbol_context: dict[str, dict[str, Any]] = {}

    for symbol, scan_result in current_by_symbol.items():
        observations = [
            scan["symbols"][symbol]
            for scan in all_scans
            if symbol in scan["symbols"]
        ]
        latest = observations[-1]
        previous_scan = prior_scans[-1] if prior_scans else None
        previous = (
            previous_scan["symbols"].get(symbol)
            if previous_scan is not None
            else None
        )

        consecutive = 1
        for scan in reversed(prior_scans):
            if symbol not in scan["symbols"]:
                break
            consecutive += 1

        first = observations[0]
        observed_high = max(item["close"] for item in observations)
        positive_intervals = sum(
            newer["close"] > older["close"]
            for older, newer in zip(observations, observations[1:])
        )

        elapsed_minutes: float | None = None
        last_return: float | None = None
        rank_improvement: int | None = None
        score_change: float | None = None
        volume_rate: float | None = None
        if previous is not None:
            elapsed_minutes = (now - previous["at"]).total_seconds() / 60
            last_return = _pct_change(latest["close"], previous["close"])
            rank_improvement = previous["rank"] - latest["rank"]
            score_change = latest["score"] - previous["score"]
            if elapsed_minutes > 0:
                volume_rate = (
                    (latest["rel_volume"] - previous["rel_volume"])
                    / elapsed_minutes
                    * 30
                )

        previous_return: float | None = None
        previous_volume_rate: float | None = None
        if len(prior_scans) >= 2:
            older = prior_scans[-2]["symbols"].get(symbol)
            newer = prior_scans[-1]["symbols"].get(symbol)
            if older is not None and newer is not None:
                previous_return = _pct_change(newer["close"], older["close"])
                prior_elapsed = (
                    newer["at"] - older["at"]
                ).total_seconds() / 60
                if prior_elapsed > 0:
                    previous_volume_rate = (
                        (newer["rel_volume"] - older["rel_volume"])
                        / prior_elapsed
                        * 30
                    )

        context: dict[str, Any] = {
            "observations": len(observations),
            "consecutive_scans": consecutive,
            "first_seen_et": first["at"].strftime("%H:%M"),
            "minutes_since_first_seen": round(
                (now - first["at"]).total_seconds() / 60
            ),
            "minutes_since_previous_scan": (
                round(elapsed_minutes) if elapsed_minutes is not None else None
            ),
            "return_since_previous_pct": _round(last_return),
            "return_since_first_seen_pct": _round(
                _pct_change(latest["close"], first["close"])
            ),
            "positive_observed_intervals": positive_intervals,
            "observed_intervals": max(0, len(observations) - 1),
            "current_rank": latest["rank"],
            "previous_rank": previous["rank"] if previous is not None else None,
            "rank_improvement": rank_improvement,
            "score_change": _round(score_change),
            "observed_high": _round(observed_high),
            "drawdown_from_observed_high_pct": _round(
                _pct_change(latest["close"], observed_high)
            ),
            "rel_volume_rate_per_30m": _round(volume_rate),
            "rel_volume_rate_change": _round(
                volume_rate - previous_volume_rate
                if volume_rate is not None and previous_volume_rate is not None
                else None
            ),
            "price_acceleration_pct_points": _round(
                last_return - previous_return
                if last_return is not None and previous_return is not None
                else None
            ),
        }
        context["setup"] = _classify_setup(context, scan_result)
        symbol_context[symbol] = context

    return {
        "mode": "active_evidence",
        "affects_decisions": True,
        "source": "same-session shortlist snapshots",
        "prior_scans_available": len(prior_scans),
        "symbols": symbol_context,
    }
