"""
agents/llm_runner.py

Shared plumbing for every LLM-backed agent: the Gemini config, the
call pacing, and the retry/skip policy. Extracted from signal_agent.py
when the signal stage split into bull + bear — with multiple modules
making LLM calls, pacing has to be enforced in ONE place or the limit
gets blown by the sum of callers each individually behaving.

The numbers, and where they come from:

  The active broker-agent Google project allows 15 requests/minute and
  500 requests/day for Gemini 3.1 Flash Lite. Eight-second spacing
  targets at most 7.5 logical calls/minute, leaving room for provider
  retries. The normal plan is 396 calls/day (360 regular-session +
  36 pre-market); a 450-attempt local ceiling preserves 50 calls of
  headroom for provider-side behavior and manual use.

  The throttle is a module-global "time since last call anywhere",
  not a sleep inside any one loop: bull then bear on the same stock
  are two calls from two modules, and both must count.

  Retries handle the 429s that slip through anyway (CrewAI can make
  more than one request per task, e.g. schema-validation retries).
  Anything non-retryable surfaces immediately — a bug should crash,
  not be silently retried.

  After all retries fail: return None. The caller treats a missing
  opinion as "no trade" — degradation must always land on the safe
  side of the ledger.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from crewai import LLM, Agent, Crew, Task
from dotenv import load_dotenv

load_dotenv()

CALL_SPACING_SECONDS = 8
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 45
MAX_DAILY_CALL_ATTEMPTS = 450
USAGE_PATH = Path("data/llm_usage.json")
ET = ZoneInfo("America/New_York")

# Pinned GA model, deliberately NOT a rolling alias and NOT -preview.
# History: we used gemini-flash-latest, and overnight it started
# resolving to gemini-3.5-flash - whose free tier allows 20 requests
# per DAY, less than one debate run. Rolling aliases let Google change
# our quota out from under us; a pinned model can only break loudly.
# The lite tier carries the highest free-tier daily quotas, which is
# what a 30+-calls-per-run pipeline actually needs. (gemini-2.5-flash
# itself now 404s: "no longer available to new users".)
GEMINI_MODEL = "gemini/gemini-3.1-flash-lite"

_last_call_at = 0.0
_daily_quota_exhausted = False


def gemini_llm() -> LLM:
    # CrewAI 1.15 routes Gemini models through its native provider.
    # requirements.txt installs the matching google-genai extra; keeping
    # the model on that supported path avoids a startup-only ImportError.
    return LLM(model=GEMINI_MODEL)


def _throttle():
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < CALL_SPACING_SECONDS:
        time.sleep(CALL_SPACING_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def _reserve_daily_call() -> bool:
    """
    Persistently reserve one logical Gemini attempt before making it.

    GitHub runs are separate processes, so an in-memory counter would reset
    every 30 minutes. This file is carried in the existing data cache and
    keeps all pre-market, regular-session, retry, and manual attempts under
    the project's 500-request daily allowance.
    """
    today = datetime.now(ET).date().isoformat()
    usage = {"date": today, "attempts": 0}
    if USAGE_PATH.exists():
        loaded = json.loads(USAGE_PATH.read_text())
        if loaded.get("date") == today:
            usage = loaded
    if usage["attempts"] >= MAX_DAILY_CALL_ATTEMPTS:
        return False
    usage["attempts"] += 1
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage, indent=2))
    return True


def run_task(agent: Agent, task: Task, label: str, symbol: str):
    """
    Run one single-task Crew with pacing and rate-limit retries.
    Returns the task's pydantic output, or None if rate limits
    persisted through all retries (caller must treat None as
    "no opinion -> no trade").

    Daily-quota 429s are handled differently from per-minute ones:
    a per-minute limit passes with a pause; a per-day limit means
    every further call today is a guaranteed failure. Retrying those
    would turn an already-dead run into an hour of sleeps, so the
    first daily-quota error latches _daily_quota_exhausted and every
    subsequent call fast-fails to None.
    """
    global _daily_quota_exhausted
    if _daily_quota_exhausted:
        print(f"[{label}] {symbol}: skipped - daily Gemini quota exhausted",
              file=sys.stderr)
        return None

    crew = Crew(agents=[agent], tasks=[task], verbose=True)

    for attempt in range(RATE_LIMIT_RETRIES):
        if not _reserve_daily_call():
            print(f"[{label}] {symbol}: skipped - internal daily Gemini "
                  f"safety ceiling ({MAX_DAILY_CALL_ATTEMPTS}) reached",
                  file=sys.stderr)
            _daily_quota_exhausted = True
            return None
        _throttle()
        try:
            result = crew.kickoff()
            output = result.tasks_output[0].pydantic
            # Trust nothing that crosses a process boundary: the LLM
            # fills the symbol field itself, so pin it to the stock we
            # actually asked about.
            if getattr(output, "symbol", symbol) != symbol:
                output.symbol = symbol
            return output
        except Exception as exc:
            # APIConnectionError ("Server disconnected") observed in
            # live pre-market runs: a transient network drop, not a
            # bug - retry it like a 503 rather than crashing the run.
            retryable = ("RateLimitError", "ServiceUnavailable",
                         "APIConnectionError")
            marker = type(exc).__name__ + str(exc)
            if "PerDay" in marker or "per day" in marker:
                print(f"[{label}] {symbol}: DAILY quota exhausted - "
                      f"skipping all remaining LLM calls this run",
                      file=sys.stderr)
                _daily_quota_exhausted = True
                return None
            if not any(m in marker for m in (*retryable, "429", "503")):
                raise
            print(f"[{label}] {symbol}: rate-limited, waiting "
                  f"{RATE_LIMIT_BACKOFF_SECONDS}s "
                  f"(attempt {attempt + 1}/{RATE_LIMIT_RETRIES})",
                  file=sys.stderr)
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)

    print(f"[{label}] {symbol}: skipped - rate limit persisted",
          file=sys.stderr)
    return None
