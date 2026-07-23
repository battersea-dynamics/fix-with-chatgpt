"""
orchestrator.py

The timing layer — and ONLY the timing layer. It knows what time it
is in New York and which entry point comes next; it contains no
trading logic, no thresholds, no data handling. Every stage below is
one import + one call into a component that already exists, already
gates itself on the market calendar, and already fails safe on bad
data. If this file were deleted, nothing about WHAT the system does
would change — only WHEN.

Timezone rule: every schedule computation happens in US Eastern
(the market's clock), via tools/market_calendar.py. Nick runs this
from the UK; neither he nor this file's logic ever reasons about the
UK offset — local time appears only in log lines, for readability.

Early-close days are handled by asking Alpaca for the session's real
open/close (session_times) instead of assuming 9:30-16:00: on the
day after Thanksgiving the close is 13:00, so "last cycle at
close-15min" correctly becomes 12:45, and the cycle loop shrinks.

The default day:

  open-45min   pre-market chain (scan -> news -> candles -> bulls
               -> bears -> trader) - produces decisions, spends no
               money
  open         poll Alpaca's clock until the session is actually
               open (bounded - a few minutes, then give up on the
               execution stage; a session that never opens means
               something is wrong and no orders is the right number
               of orders)
  open+0       pre-market execution (bracket orders; --submit only)
  open+45min   fresh scan -> list -> bull/bear -> possible paper buy
  every 30min  repeat that complete cycle, including one final cycle
               at close-15min

Manual override: any stage can be run immediately with
  python -m orchestrator --force premarket
  python -m orchestrator --force premarket_exec [--submit]
  python -m orchestrator --force daily_scan
  python -m orchestrator --force check [--submit]
  python -m orchestrator --force daytime_cycle [--submit]
so testing never waits for the clock. (Component-level calendar
gates still apply on non-trading days.) A future GitHub Actions
workflow calls these same entry points on its own cron — this file
is structured so the schedule constants below translate directly
into cron lines.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from tools.market_calendar import ET, todays_session

# ----- schedule configuration (minutes are relative to session) -----
PREMARKET_LEAD_MIN = 45        # pre-market chain starts open-45min
DAYTIME_START_DELAY_MIN = 45   # first complete cycle at open+45min
DAYTIME_INTERVAL_MIN = 30      # fresh scan/debate/buy cadence
LAST_CYCLE_BEFORE_CLOSE_MIN = 15  # final cycle starts close-15min
CYCLE_START_GRACE_MIN = 5      # tolerate Cloudflare/GitHub queue jitter
OPEN_POLL_INTERVAL_SEC = 20    # how often to ask "is it open yet?"
OPEN_POLL_MAX_MIN = 5          # give up on execution this long after
                               # the scheduled open if still closed
SLEEP_CHUNK_SEC = 60           # wake at least this often while waiting

# ----- tick mode (scheduler-driven, e.g. GitHub Actions cron) -----
# A tick wakes up, runs whatever stage is due NOW, and exits. The
# cron only needs to fire more often than the narrowest window below;
# all precise timing stays in here, in ET, where it already lives.
EXEC_WINDOW_MIN = 15           # premarket exec must start within this
                               # after open - later, the gap thesis is
                               # stale and no orders is the safe answer
STATE_PATH = Path("data/orchestrator_state.json")


# ----- stages: one import + one call each, lazily imported so the -----
# ----- orchestrator starts instantly and LLM deps load only if used -----

def stage_premarket():
    from tools.premarket_scanner import run_premarket_scan
    from tools.premarket_news import run_premarket_news
    from agents.candle_agent import run_candle_agent
    from agents.premarket_bull_agent import run_premarket_bulls
    from agents.premarket_bear_agent import run_premarket_bears
    from tools.premarket_trader import decide_premarket_trades

    run_premarket_scan()
    run_premarket_news()
    run_candle_agent()
    run_premarket_bulls()
    run_premarket_bears()
    decisions = decide_premarket_trades()
    return {
        "decisions": len(decisions),
        "buys": sum(1 for d in decisions if d.signal == "buy"),
    }


def stage_premarket_exec(submit: bool = False):
    from tools.premarket_execution import execute_premarket_decisions
    return execute_premarket_decisions(submit=submit)


def stage_daily_scan():
    from pipeline import daily_scan
    shortlist = daily_scan()
    return {"shortlist": [r.symbol for r in shortlist]}


def stage_check(submit: bool = False):
    from pipeline import check_shortlist
    return check_shortlist(submit=submit)


def stage_daytime_cycle(submit: bool = False):
    from pipeline import daytime_cycle
    return daytime_cycle(submit=submit)


STAGES = {
    "premarket": lambda submit: stage_premarket(),
    "premarket_exec": stage_premarket_exec,
    "daily_scan": lambda submit: stage_daily_scan(),
    "check": stage_check,
    "daytime_cycle": stage_daytime_cycle,
}


# ----- timing helpers -----

def _now() -> datetime:
    return datetime.now(ET)


def _log(message: str):
    now = _now()
    print(f"[orchestrator] {now:%H:%M:%S} ET "
          f"({now.astimezone():%H:%M} local)  {message}", flush=True)


def _sleep_until(target: datetime, label: str):
    """Chunked sleep so Ctrl+C stays responsive and logs show life."""
    while (remaining := (target - _now()).total_seconds()) > 0:
        if remaining > SLEEP_CHUNK_SEC * 5:
            _log(f"waiting for {label} at {target:%H:%M} ET "
                 f"({remaining / 60:.0f} min)")
        time.sleep(min(remaining, SLEEP_CHUNK_SEC))


def _poll_until_open(scheduled_open: datetime) -> bool:
    """
    True once Alpaca's own clock says the session is open. Bounded:
    if it still isn't open OPEN_POLL_MAX_MIN after the scheduled
    time, something unusual is happening (delayed open, halt) and we
    refuse to run the execution stage rather than trade into it.
    """
    from tools.broker import trading_client

    deadline = scheduled_open + timedelta(minutes=OPEN_POLL_MAX_MIN)
    while True:
        # Always ask Alpaca at least once. GitHub's scheduled jobs can
        # start several minutes late; the old loop checked the deadline
        # first, so a 09:39 tick with a 09:35 polling deadline returned
        # False without ever checking an already-open market.
        if trading_client.get_clock().is_open:
            return True
        if _now() >= deadline:
            return False
        _log(f"market not open yet, polling every "
             f"{OPEN_POLL_INTERVAL_SEC}s")
        time.sleep(OPEN_POLL_INTERVAL_SEC)


# ----- tick mode: one bounded wake-up for external schedulers -----

def _load_state(today) -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        if state.get("date") == today.isoformat():
            # Safe forward migration if code is deployed after an earlier
            # tick already created today's old-format state file.
            state.setdefault("last_daytime_slot", None)
            state.setdefault("daytime_cycles_attempted", 0)
            return state
    return {"date": today.isoformat(), "premarket_done": False,
            "exec_done": False, "last_daytime_slot": None,
            "daytime_cycles_attempted": 0, "session_recorded": False,
            "day_complete": False}


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _daytime_slots(
    session_open: datetime,
    session_close: datetime,
) -> list[datetime]:
    """All scheduled regular-session cycle starts for this session."""
    slot = session_open + timedelta(minutes=DAYTIME_START_DELAY_MIN)
    last = session_close - timedelta(minutes=LAST_CYCLE_BEFORE_CLOSE_MIN)
    slots = []
    while slot <= last:
        slots.append(slot)
        slot += timedelta(minutes=DAYTIME_INTERVAL_MIN)
    return slots


def _due_daytime_slot(
    now: datetime,
    slots: list[datetime],
    completed_slot: str | None,
) -> datetime | None:
    """
    Return the exact slot this wake-up may service.

    Matching an anchored slot, rather than merely waiting 25 minutes since
    the previous run, prevents extra pre-market/open Cloudflare wakes from
    shifting the whole daytime cadence.
    """
    grace = timedelta(minutes=CYCLE_START_GRACE_MIN)
    for slot in reversed(slots):
        if slot <= now <= slot + grace:
            if completed_slot != slot.isoformat(timespec="seconds"):
                return slot
            return None
    return None


def run_tick(submit: bool = False):
    """
    Scheduler entry point (GitHub Actions cron, or any cron): decide
    what's due right now, run it, record it, exit. Never sleeps
    between stages - the next cron firing is the next wake-up. State
    lives in data/orchestrator_state.json so a fleet of short-lived
    runners behaves like one long-running orchestrator, as long as
    data/ is carried between runs (the workflow uses actions/cache).

    Failure policy per stage: the pre-market chain is idempotent, so it is
    only marked done on success. A daytime slot is marked attempted even
    if its cycle fails; the next scheduled slot starts with a completely
    fresh scan instead of hammering APIs repeatedly inside one window.
    Execution is marked done on ATTEMPT: retrying a
    possibly-half-submitted order loop is how you double-order, and
    "no more orders today" is always the safe failure mode.
    """
    from tools import daily_report

    session = todays_session()
    if session is None:
        _log("tick: no trading session today - exiting")
        return
    session_open, session_close = session
    today = _now().date()

    state = _load_state(today)
    if state["day_complete"]:
        _log("tick: trading day already complete - exiting")
        return

    premarket_at = session_open - timedelta(minutes=PREMARKET_LEAD_MIN)
    daytime_slots = _daytime_slots(session_open, session_close)
    last_daytime_slot = daytime_slots[-1]
    exec_deadline = session_open + timedelta(minutes=EXEC_WINDOW_MIN)

    if not state["session_recorded"]:
        daily_report.append_event(today, "session", {
            "open_et": f"{session_open:%H:%M}",
            "close_et": f"{session_close:%H:%M}",
            "submit_mode": submit,
        })
        state["session_recorded"] = True

    def record(stage_name: str, fn) -> bool:
        try:
            result = fn()
            if (isinstance(result, dict)
                    and isinstance(result.get("execution_report"), list)):
                detail = {
                    key: value for key, value in result.items()
                    if key != "execution_report"
                }
                detail.update(daily_report.summarize_execution(
                    result["execution_report"]
                ))
            else:
                detail = (daily_report.summarize_execution(result)
                          if isinstance(result, list) else result)
            daily_report.append_event(today, stage_name,
                                      {"ok": True, **(detail or {})})
            return True
        except SystemExit as exc:
            # Components use SystemExit for controlled fail-safe stops
            # (missing/stale files). SystemExit is not an Exception, so
            # without this branch the tick terminated before saving state
            # or recording the reason in the daily report.
            daily_report.append_event(today, stage_name, {
                "ok": False, "error": f"SystemExit: {exc}"})
            _log(f"tick stage {stage_name} STOPPED: {exc}")
            return False
        except Exception as exc:  # report the error, keep the day alive
            daily_report.append_event(today, stage_name, {
                "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            _log(f"tick stage {stage_name} FAILED: {exc}")
            return False

    now = _now()

    if not state["premarket_done"] and premarket_at <= now < session_open:
        _log("tick stage: premarket chain")
        state["premarket_done"] = record("premarket_chain", stage_premarket)

    now = _now()
    if not state["exec_done"] and session_open <= now <= exec_deadline:
        if _poll_until_open(session_open):
            _log("tick stage: premarket execution")
            record("premarket_execution",
                   lambda: stage_premarket_exec(submit=submit))
        else:
            daily_report.append_event(today, "premarket_execution", {
                "ok": False,
                "error": "market not open at scheduled time - skipped"})
        state["exec_done"] = True  # attempted = done, never retry orders

    now = _now()
    due_slot = _due_daytime_slot(
        now, daytime_slots, state["last_daytime_slot"]
    )
    if due_slot is not None:
        _log(f"tick stage: complete daytime cycle for {due_slot:%H:%M}")
        record("daytime_cycle",
               lambda: stage_daytime_cycle(submit=submit))
        # Attempted means done for this exact slot. Broker/position/order
        # guards make a later slot safe, but retrying immediately could
        # duplicate API work or an ambiguous submission.
        state["last_daytime_slot"] = due_slot.isoformat(timespec="seconds")
        state["daytime_cycles_attempted"] += 1

    final_attempted = (
        state["last_daytime_slot"]
        == last_daytime_slot.isoformat(timespec="seconds")
    )
    if (final_attempted
            or _now() > last_daytime_slot
            + timedelta(minutes=CYCLE_START_GRACE_MIN)):
        state["day_complete"] = True
        daily_report.append_event(today, "day_complete", {
            "premarket_done": state["premarket_done"],
            "exec_done": state["exec_done"],
            "daytime_cycles_attempted": state["daytime_cycles_attempted"],
            "final_daytime_slot_attempted": final_attempted,
        })
        _log("tick: trading day complete")

    _save_state(state)


# ----- the scheduled day -----

def run_day(submit: bool = False):
    session = todays_session()
    if session is None:
        _log("no trading session today - exiting")
        return
    session_open, session_close = session

    premarket_at = session_open - timedelta(minutes=PREMARKET_LEAD_MIN)
    daytime_slots = _daytime_slots(session_open, session_close)

    _log(f"session {session_open:%H:%M}-{session_close:%H:%M} ET | "
         f"premarket {premarket_at:%H:%M}, exec ~{session_open:%H:%M}, "
         f"daytime cycles {daytime_slots[0]:%H:%M}-"
         f"{daytime_slots[-1]:%H:%M} every {DAYTIME_INTERVAL_MIN}min"
         + (" | SUBMITTING paper orders" if submit else " | dry-run"))

    if _now() < premarket_at:
        _sleep_until(premarket_at, "pre-market chain")
    _log("stage: premarket chain")
    stage_premarket()

    _sleep_until(session_open, "market open")
    if _poll_until_open(session_open):
        _log("stage: premarket execution")
        stage_premarket_exec(submit=submit)
    else:
        _log(f"market still closed {OPEN_POLL_MAX_MIN}min after "
             f"scheduled open - SKIPPING premarket execution")

    for cycle_at in daytime_slots:
        _sleep_until(cycle_at, "daytime cycle")
        _log("stage: fresh scan -> debate -> possible paper buy")
        stage_daytime_cycle(submit=submit)

    _log("trading day schedule complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Timing layer for the trading pipeline")
    parser.add_argument("--force", choices=sorted(STAGES),
                        help="run one stage immediately, skip all waiting")
    parser.add_argument("--tick", action="store_true",
                        help="one bounded scheduler wake-up: run whatever "
                             "is due now, then exit (for cron/CI)")
    parser.add_argument("--submit", action="store_true",
                        help="execution stages submit paper orders "
                             "(default: dry-run; paper account only)")
    args = parser.parse_args()

    if args.force:
        _log(f"forced stage: {args.force}"
             + (" | SUBMITTING paper orders" if args.submit else " | dry-run"))
        STAGES[args.force](args.submit)
    elif args.tick:
        run_tick(submit=args.submit)
    else:
        try:
            run_day(submit=args.submit)
        except KeyboardInterrupt:
            _log("interrupted - exiting")
            sys.exit(130)
