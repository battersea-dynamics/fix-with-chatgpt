"""Offline archive review. No API calls, credentials, LLMs or orders.

Snapshot returns are censored diagnostics, never a backtest or account P&L.
Usage: python -m tools.strategy_review --start 2026-09-10 --end 2026-10-01
"""

import argparse
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path


SCORE_PATTERN = re.compile(r"net ([+-]?[\d.]+) = bull ([\d.]+) - bear ([\d.]+)")


def snapshot_diagnostic(rows, scans, threshold):
    """First qualifying verified symbol/day; future scans after decision time only."""
    selected = {}
    for row in sorted(rows, key=lambda r: (r["date"], r["decision_at"])):
        if (row["verified"] and row["stop_loss_pct"] <= 5
                and row["net_score"] + 1e-9 >= threshold):
            selected.setdefault((row["date"], row["symbol"]), row)
    next_returns, last_returns = [], []
    for row in selected.values():
        later = sorted(
            (at, price) for at, price in scans.get((row["date"], row["symbol"]), [])
            if at > row["decision_at"]
        )
        if later:
            next_returns.append((later[0][1] / row["analysis_price"] - 1) * 100)
            last_returns.append((later[-1][1] / row["analysis_price"] - 1) * 100)
    return {
        "threshold": threshold,
        "selected_symbol_days": len(selected),
        "with_later_snapshot": len(last_returns),
        "missing_later_snapshot": len(selected) - len(last_returns),
        "positive_last_snapshot": sum(x > 0 for x in last_returns),
        "mean_next_snapshot_return_pct": statistics.mean(next_returns) if next_returns else None,
        "mean_last_available_snapshot_return_pct": statistics.mean(last_returns) if last_returns else None,
        "median_last_available_snapshot_return_pct": statistics.median(last_returns) if last_returns else None,
    }


def review(data_root, start, end, split):
    days, rows, orders = [], [], []
    scans = {}
    scanner_stats = Counter()
    repeats = []
    for report_path in sorted((data_root / "reports").glob("daily_report_*.json")):
        date = report_path.stem.removeprefix("daily_report_")
        if not start <= date <= end:
            continue
        events = json.loads(report_path.read_text())["events"]
        folder = data_root / "lists" / date
        by_cycle = {}
        for path in sorted(folder.glob("shortlist_*.json")):
            payload = json.loads(path.read_text())
            by_cycle[path.stem.removeprefix("shortlist_")] = payload
            at = datetime.fromisoformat(payload["generated_at"])
            for scan in payload["shortlist"]:
                scans.setdefault((date, scan["symbol"]), []).append((at, scan["close"]))
                scanner_stats["rows"] += 1
                scanner_stats["negative_day_change"] += scan["pct_change"] < 0
                scanner_stats["rel_volume_below_0_2"] += scan["rel_volume"] < 0.2
        count = 0
        for path in sorted(folder.glob("check_decisions_*.json")):
            payload = json.loads(path.read_text())
            count += len(payload["decisions"])
            cycle = by_cycle.get(path.stem.removeprefix("check_decisions_"))
            if cycle is None:
                continue
            by_symbol = {s["symbol"]: s for s in cycle["shortlist"]}
            for decision in payload["decisions"]:
                match = SCORE_PATTERN.match(decision["reasoning"])
                scan = by_symbol.get(decision["symbol"])
                if not match or scan is None:
                    continue
                rows.append({
                    "date": date, "symbol": decision["symbol"],
                    "decision_at": datetime.fromisoformat(payload["generated_at"]),
                    "net_score": float(match[1]),
                    "analysis_price": scan["close"],
                    "stop_loss_pct": decision["stop_loss_pct"],
                    "verified": decision.get("numbers_verified", True),
                    "signal": decision["signal"],
                })
        submitted = []
        for event in events:
            for entry in event.get("detail", {}).get("orders", []):
                if entry.get("action") != "submitted":
                    continue
                order = entry["order"]
                ask = order["entry_ref"]
                risk = ask - order["stop_loss"]
                ratio = (order["take_profit"] - ask) / risk if risk > 0 else None
                submitted.append({
                    "date": date, "symbol": entry["symbol"],
                    "entry_time_et": event["at_et"],
                    "qty": order["qty"], "reference_ask": ask,
                    "target": order["take_profit"], "stop": order["stop_loss"],
                    "reward_risk": ratio, "broker_status_at_submission": entry["broker"].get("status"),
                })
        orders.extend(submitted)
        repeats.extend({"date": date, "symbol": s, "submissions": n}
                       for s, n in Counter(o["symbol"] for o in submitted).items() if n > 1)
        days.append({
            "date": date,
            "daytime_cycles_attempted": sum(e["type"] == "daytime_cycle" for e in events),
            "failed_stages": sum(e.get("detail", {}).get("ok") is False for e in events),
            "archived_scanner_rows": sum(len(s["shortlist"]) for s in by_cycle.values()),
            "completed_regular_decisions": count,
            "paper_submissions": len(submitted),
        })
    thresholds = [0.10, 0.15, 0.20, 0.25, 0.30]
    periods = {"all": rows, "earlier": [r for r in rows if r["date"] < split],
               "later": [r for r in rows if r["date"] >= split]}
    return {
        "start": start, "end": end, "comparison_split": split,
        "limitations": [
            "No reconciled fills, exits, fees, account equity or actual P&L.",
            "Only archived shortlists; holdings disappear and other names drop out. Missingness is non-random.",
            "Later snapshot returns start at a delayed analysis price, not an executable fill.",
            "Last available snapshot is not necessarily the closing price; horizons vary.",
            "Snapshots cannot determine target-first/stop-first, excursions or overnight exits.",
            "Thresholds use rounded net scores embedded in historical reasoning.",
            "The retrospective date split is exploratory, not an untouched prospective test.",
            "Scanner rows minus decisions includes held-name removal, missing evidence and failed stages; it is not a quota-only loss count.",
        ],
        "days": days,
        "totals": {key: sum(day[key] for day in days) for key in (
            "daytime_cycles_attempted", "failed_stages", "archived_scanner_rows",
            "completed_regular_decisions", "paper_submissions")},
        "scanner": dict(scanner_stats), "repeated_submissions": repeats,
        "regular_buy_rows": sum(r["signal"] == "buy" for r in rows),
        "unique_regular_buy_symbol_days": len({(r["date"], r["symbol"]) for r in rows if r["signal"] == "buy"}),
        "threshold_snapshot_diagnostics": {
            period: [snapshot_diagnostic(selected, scans, t) for t in thresholds]
            for period, selected in periods.items()
        },
        "orders": orders,
        "proposed_reward_risk_floor": {
            "floor": 1.5, "submitted_orders_below_floor": [
                o for o in orders if o["reward_risk"] is None or o["reward_risk"] + 1e-9 < 1.5],
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--split", default="2026-09-28")
    args = parser.parse_args()
    print(json.dumps(review(args.data_root, args.start, args.end, args.split), indent=2))
