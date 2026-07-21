"""
agents/execution_agent.py

The last stage: turn approved buy signals into Alpaca bracket orders.

Deliberately NOT a CrewAI agent — and that's the architecture lesson
of this file. An "agent" earns an LLM when the step requires judgment
over unstructured input. Execution is the opposite: the judgment was
already made upstream (signal + confidence + exit levels), and what
remains is arithmetic and API calls that must be *boringly
deterministic*. An LLM here could round a price creatively, size a
position generously, or hallucinate a symbol — and unlike a bad
opinion, a bad order costs money. The rule of thumb: LLMs decide,
code executes.

The asymmetry with the signal agent is the security model:
  signal agent    - judgment, no order access
  execution agent - order access, no judgment
Neither can do the other's job, so no single failure (bad prompt,
model outage, hallucination) can both invent and place a trade.

What "no judgment" still includes — mechanical policy, applied
uniformly:
  - only signal == "buy" with confidence >= MIN_CONFIDENCE
  - stop-loss ceiling: > MAX_STOP_LOSS_PCT skips the trade (never
    clamp a stop tighter); take-profit ceiling: > MAX_TAKE_PROFIT_PCT
    clamps down and proceeds
  - cash-based position budget: 20% of available cash, with a $200
    floor that never exceeds the cash left; whole shares only (Alpaca
    forbids fractional bracket orders); if one share busts the budget,
    skip
  - skip anything that can't be sized or quoted, rather than improvise
  - dry-run by default; pass submit=True (or --submit on the CLI) to
    actually submit (paper) orders
"""

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from agents.signal_agent import SignalDecision
from tools.broker import (
    get_account,
    get_open_buy_orders,
    get_positions,
    get_quote,
    place_bracket_order,
)

load_dotenv()

MIN_CONFIDENCE = 0.6

# Position sizing uses actual cash, never margin buying power. Normally a
# position may use 20% of the cash still available in this execution run.
# When that amount falls below $200, the budget floor is $200, capped by
# the cash actually left (e.g. $500 -> $200; $50 -> $50).
MAX_POSITION_PCT = 0.20
MIN_POSITION_BUDGET = 200.0

# Asymmetric exit ceilings, applied to the trader's numbers at the
# last moment before submission. Asymmetric on purpose:
#   TP > 12%  -> CLAMP to 12% and proceed. Capping upside never
#                makes a trade less safe.
#   SL > 5%   -> SKIP the trade entirely, never clamp tighter. A
#                wide stop is the bear agent's honest read of real
#                volatility; forcing it tighter just converts normal
#                noise into stop-outs, which defeats the stop.
MAX_TAKE_PROFIT_PCT = 12.0
MAX_STOP_LOSS_PCT = 5.0
ET = ZoneInfo("America/New_York")


def _client_order_id(symbol: str, now: datetime | None = None) -> str:
    """Identify this attempt without blocking a later failed-order retry."""
    now = now or datetime.now(ET)
    return f"ta-{now:%Y%m%d-%H%M%S}-{symbol.upper()}"


def execute_signals(
    decisions: list[SignalDecision],
    submit: bool = False,
) -> list[dict]:
    """
    Filter, size, and (if submit) submit one bracket order per approved
    buy. Returns a report of what was done or would be done — the
    dry-run output is the exact order that submit mode would place.
    (Paper account only — 'submit' means a paper order, never real money.)
    """
    account = get_account()
    available_cash = account["cash"]
    now_et = datetime.now(ET)
    held_symbols = {p["symbol"] for p in get_positions()}
    ordered_symbols = {o["symbol"] for o in get_open_buy_orders()}
    report = []

    for decision in decisions:
        entry = {"symbol": decision.symbol, "action": "skipped"}
        report.append(entry)

        if decision.signal != "buy":
            entry["reason"] = f"signal is '{decision.signal}'"
            continue
        if decision.confidence < MIN_CONFIDENCE:
            entry["reason"] = (
                f"confidence {decision.confidence:.2f} < {MIN_CONFIDENCE}"
            )
            continue
        if not getattr(decision, "numbers_verified", True):
            entry["reason"] = (
                "numeric evidence verification failed; refusing to trade"
            )
            continue

        if decision.symbol in held_symbols:
            entry["reason"] = "position already held; adding is disabled"
            continue
        if decision.symbol in ordered_symbols:
            entry["reason"] = (
                "buy order is still active; duplicate entry blocked"
            )
            continue

        # Stop-loss ceiling: skip, never tighten (see constants).
        if decision.stop_loss_pct > MAX_STOP_LOSS_PCT:
            entry["reason"] = (
                f"stop-loss ceiling exceeded, skipped: trader set "
                f"{decision.stop_loss_pct:.1f}% > {MAX_STOP_LOSS_PCT:.0f}% "
                f"max (wide stop = honest volatility read; not clamping)"
            )
            continue

        # Take-profit ceiling: clamp and proceed (capping upside
        # never makes a trade less safe).
        take_profit_pct = decision.take_profit_pct
        tp_clamped = take_profit_pct > MAX_TAKE_PROFIT_PCT
        if tp_clamped:
            take_profit_pct = MAX_TAKE_PROFIT_PCT

        # Reference price for converting the agent's percentages into
        # absolute bracket prices: the current ask, i.e. roughly what
        # a market buy would actually pay right now.
        quote = get_quote(decision.symbol)
        ask = quote["ask"]
        if not ask or ask <= 0:
            entry["reason"] = f"no usable ask price (got {ask!r})"
            continue

        position_budget = min(
            available_cash,
            max(available_cash * MAX_POSITION_PCT, MIN_POSITION_BUDGET),
        )
        if ask > position_budget:
            entry["reason"] = (
                f"position size cap exceeded, skipped: 1 share at "
                f"${ask:.2f} > current cash-based budget "
                f"(${position_budget:.2f})"
            )
            continue
        qty = math.floor(position_budget / ask)
        if qty < 1:
            entry["reason"] = (
                f"can't afford 1 share at ${ask:.2f} with remaining "
                f"cash (${available_cash:.2f})"
            )
            continue

        take_profit = ask * (1 + take_profit_pct / 100)
        stop_loss = ask * (1 - decision.stop_loss_pct / 100)
        available_cash -= qty * ask

        order = {
            "symbol": decision.symbol,
            "qty": qty,
            "est_cost": round(qty * ask, 2),
            "entry_ref": ask,
            "take_profit": round(take_profit, 2),
            "stop_loss": round(stop_loss, 2),
            "confidence": decision.confidence,
            "client_order_id": _client_order_id(decision.symbol, now_et),
        }
        if tp_clamped:
            order["take_profit_clamped"] = (
                f"trader wanted {decision.take_profit_pct:.1f}%, "
                f"capped at {MAX_TAKE_PROFIT_PCT:.0f}%"
            )

        if submit:
            result = place_bracket_order(
                decision.symbol, qty, take_profit, stop_loss,
                client_order_id=order["client_order_id"],
            )
            entry.update(action="submitted", order=order, broker=result)
            ordered_symbols.add(decision.symbol)
        else:
            entry.update(action="dry_run", order=order)

    return report


if __name__ == "__main__":
    import sys

    from tools.catalysts import build_catalyst_report
    from tools.scanner import scan
    from agents.signal_agent import analyze_shortlist

    submit = "--submit" in sys.argv
    top_n = 5

    shortlist = scan(top_n=top_n)
    catalysts = build_catalyst_report([s.symbol for s in shortlist])
    decisions = analyze_shortlist(shortlist, catalysts)

    report = execute_signals(decisions, submit=submit)
    print(json.dumps(report, indent=2))
    if not submit:
        print("\n(dry run - re-run with --submit to submit paper orders)",
              file=sys.stderr)
