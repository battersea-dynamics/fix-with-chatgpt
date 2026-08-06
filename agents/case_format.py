"""
agents/case_format.py

The one shared evidence block both debate agents receive. Kept in a
single function so the bull and bear literally see the same bytes —
if each agent formatted its own view of the data, a score difference
between them could come from presentation (one saw the headlines
first, one saw them last) rather than judgment. Symmetric inputs are
what make the bull-minus-bear subtraction meaningful.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from tools.scanner import ScanResult


ET = ZoneInfo("America/New_York")

MOMENTUM_FIELDS = (
    "observations",
    "consecutive_scans",
    "first_seen_et",
    "minutes_since_first_seen",
    "return_since_previous_pct",
    "return_since_first_seen_pct",
    "positive_observed_intervals",
    "observed_intervals",
    "current_rank",
    "previous_rank",
    "rank_improvement",
    "score_change",
    "observed_high",
    "drawdown_from_observed_high_pct",
    "rel_volume_rate_per_30m",
    "rel_volume_rate_change",
    "price_acceleration_pct_points",
    "setup",
)


def _session_day(value: date | datetime | str | None) -> date:
    """Normalize the evidence timestamp to a US Eastern session date."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ET)
        return value.astimezone(ET).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return datetime.now(ET).date()


def _dated_catalysts(catalysts: dict, session_day: date) -> dict:
    """Add deterministic ages so the LLM never guesses what "today" is."""
    annotated = copy.deepcopy(catalysts)
    date_fields = {
        "earnings": "date",
        "dividends": "ex_date",
        "news": "date",
    }
    for section, date_field in date_fields.items():
        events = annotated.get(section, [])
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict) or not event.get(date_field):
                continue
            try:
                event_day = date.fromisoformat(str(event[date_field])[:10])
            except ValueError:
                continue
            days = (event_day - session_day).days
            event["days_from_session"] = days
            if days == 0:
                relation = "same US trading-session date"
            elif days < 0:
                relation = f"{abs(days)} calendar day(s) before the session"
            else:
                relation = f"{days} calendar day(s) after the session"
            event["session_relation"] = relation
    return annotated


def _compact_momentum(momentum_context: dict | None) -> dict:
    if not isinstance(momentum_context, dict):
        return {}
    return {
        key: momentum_context.get(key)
        for key in MOMENTUM_FIELDS
        if key in momentum_context
    }


def build_evidence_facts(
    catalysts: dict,
    momentum_context: dict | None = None,
    session_date: date | datetime | str | None = None,
) -> dict:
    """Return the exact derived facts shown to and verified for both agents."""
    session_day = _session_day(session_date)
    return {
        "us_session_date": session_day.isoformat(),
        "momentum_history": _compact_momentum(momentum_context),
        "catalysts": _dated_catalysts(catalysts, session_day),
    }


def _momentum_evidence(momentum_context: dict | None) -> str:
    compact = _compact_momentum(momentum_context)
    if not compact:
        return (
            "Same-session momentum history (US Eastern): unavailable; "
            "treat persistence as unknown, not positive or negative."
        )

    return (
        "Same-session momentum history (US Eastern; observed 30-minute "
        "shortlist snapshots, not continuous bars):\n"
        f"{json.dumps(compact, indent=2)}\n"
        "Use the numerical development, not the provisional setup label "
        "alone. Do not invent what happened between observations."
    )


def format_evidence(
    scan: ScanResult,
    catalysts: dict,
    momentum_context: dict | None = None,
    session_date: date | datetime | str | None = None,
) -> str:
    facts = build_evidence_facts(
        catalysts,
        momentum_context=momentum_context,
        session_date=session_date,
    )
    return (
        f"Evidence for {scan.symbol} ({scan.sector}) on US trading-session "
        f"date {facts['us_session_date']}:\n\n"
        f"Scanner metrics (vs its own 20-day history):\n"
        f"  Close: ${scan.close:.2f}\n"
        f"  Relative volume: {scan.rel_volume:.2f}x normal\n"
        f"  Day change: {scan.pct_change:+.2f}%\n"
        f"  Distance from 20-day MA: {scan.ma_distance:+.2f}%\n\n"
        f"{_momentum_evidence(momentum_context)}\n\n"
        f"Catalyst report (earnings within 14 days, ex-dividend dates "
        f"within 14 days, headlines from the last 7 days):\n"
        f"{json.dumps(facts['catalysts'], indent=2)}\n\n"
        "Date rule: use days_from_session and session_relation exactly. "
        "An event marked same US trading-session date is fresh today and "
        "must never be called stale, old, or several days old. Do not use "
        "your internal clock to reinterpret these dates."
    )
