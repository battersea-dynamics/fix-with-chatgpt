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

  Busy/rate-limit errors (503 high demand, 429) fall through to the
  next free model in GEMINI_MODELS after a ~2s pause instead of a 45s
  sleep (CrewAI can also make more than one request per task).
  Anything non-retryable surfaces immediately — a bug should crash,
  not be silently retried.

  After every model fails (twice): return None. The caller treats a missing
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
# Short pause before trying the NEXT model after a busy/rate-limited
# reply. Different models have separate quotas, so no long wait needed.
MODEL_SWITCH_PAUSE_SECONDS = 2
# If every model in the list failed on one call: one short backoff,
# then one more pass over the list, then give up (None = no trade).
ALL_MODELS_BACKOFF_SECONDS = 10
ALL_MODELS_PASSES = 2
MAX_DAILY_CALL_ATTEMPTS = 450
# Stop calling the LLM once the run has used this long, so the 45-min
# GitHub job limit never kills the state-saving step after the debate.
RUN_TIME_BUDGET_SECONDS = 35 * 60
USAGE_PATH = Path("data/llm_usage.json")
ET = ZoneInfo("America/New_York")

# Pinned GA models, deliberately NOT rolling aliases and NOT -preview.
# History: we used gemini-flash-latest, and overnight it started
# resolving to gemini-3.5-flash - whose free tier allows 20 requests
# per DAY, less than one debate run. Rolling aliases let Google change
# our quota out from under us; a pinned model can only break loudly.
# (gemini-2.5-flash now 404s: "no longer available to new users"; the
# models page says all 2.5 models are limited to past users, so the
# 2.5 family is excluded.)
#
# Ordered fallback list - edit here. Primary first. All three are
# listed as "Free of charge" on the Free Tier at
# https://ai.google.dev/gemini-api/docs/pricing and as stable names at
# https://ai.google.dev/gemini-api/docs/models (checked 2026-10-28).
# Per-model free RPM/RPD are NOT published in the docs any more (only
# in AI Studio), so lite models come first (historically the highest
# free quotas) and the full Flash model is last resort.
GEMINI_MODELS = [
    "gemini/gemini-3.1-flash-lite",
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.6-flash",
]
GEMINI_MODEL = GEMINI_MODELS[0]

_RETRYABLE_MARKERS = ("RateLimitError", "ServiceUnavailable",
                      "APIConnectionError", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                      "high demand", "overloaded", "429", "503")

_run_started_at = time.monotonic()
_last_call_at = 0.0
_daily_quota_exhausted = False      # local 450 ceiling hit, or all models dead
_exhausted_models: set = set()      # models that returned a per-day 429


def gemini_llm(model: str = GEMINI_MODEL) -> LLM:
    # CrewAI 1.15 routes Gemini models through its native provider.
    # requirements.txt installs the matching google-genai extra; keeping
    # the model on that supported path avoids a startup-only ImportError.
    return LLM(model=model)


def _throttle():
    # Global spacing across all models: 8s => <=7.5 calls/min overall,
    # within the primary's 15 RPM even if every call lands on one model.
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < CALL_SPACING_SECONDS:
        time.sleep(CALL_SPACING_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def _reserve_daily_call(model: str = GEMINI_MODEL) -> bool:
    """
    Persistently reserve one Gemini attempt before making it.

    GitHub runs are separate processes, so an in-memory counter would reset
    every 30 minutes. "attempts" counts every request on ANY model (each
    fallback hop is one attempt) and is capped at MAX_DAILY_CALL_ATTEMPTS:
    a conservative overall ceiling, since each model actually has its own
    quota. "per_model" is informational. Old files without "per_model"
    are read fine; everything resets when the ET date changes.
    """
    today = datetime.now(ET).date().isoformat()
    usage = {"date": today, "attempts": 0, "per_model": {}}
    if USAGE_PATH.exists():
        loaded = json.loads(USAGE_PATH.read_text())
        if loaded.get("date") == today:
            usage.update(loaded)
            usage.setdefault("per_model", {})
    if usage["attempts"] >= MAX_DAILY_CALL_ATTEMPTS:
        return False
    usage["attempts"] += 1
    usage["per_model"][model] = usage["per_model"].get(model, 0) + 1
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage, indent=2))
    return True


def _over_time_budget() -> bool:
    return time.monotonic() - _run_started_at > RUN_TIME_BUDGET_SECONDS


def run_task(agent: Agent, task: Task, label: str, symbol: str):
    """
    Run one single-task Crew with pacing and model fallback.
    Returns the task's pydantic output, or None (caller must treat None
    as "no opinion -> no trade").

    On a busy/rate-limit error the next model in GEMINI_MODELS is tried
    after a ~2s pause. A per-day 429 marks that model exhausted for the
    rest of the run. If every model fails, one <=10s backoff and one
    more pass; then None. Non-retryable errors raise immediately.
    Candidates are processed in the caller's order, so the time-budget
    guard only ever drops the lowest-ranked ones.
    """
    global _daily_quota_exhausted
    if _daily_quota_exhausted:
        print(f"[{label}] {symbol}: skipped - daily Gemini quota exhausted",
              file=sys.stderr)
        return None

    for pass_no in range(ALL_MODELS_PASSES):
        if pass_no:
            print(f"[{label}] {symbol}: all models busy, backing off "
                  f"{ALL_MODELS_BACKOFF_SECONDS}s then one more pass",
                  file=sys.stderr)
            time.sleep(ALL_MODELS_BACKOFF_SECONDS)
        tried_any = False
        for model in GEMINI_MODELS:
            if model in _exhausted_models:
                continue
            if _over_time_budget():
                print(f"[{label}] {symbol}: skipped - run time budget "
                      f"({RUN_TIME_BUDGET_SECONDS // 60} min) used up; "
                      f"no more LLM calls this run", file=sys.stderr)
                return None
            if not _reserve_daily_call(model):
                print(f"[{label}] {symbol}: skipped - internal daily Gemini "
                      f"safety ceiling ({MAX_DAILY_CALL_ATTEMPTS}) reached",
                      file=sys.stderr)
                _daily_quota_exhausted = True
                return None
            if tried_any:
                time.sleep(MODEL_SWITCH_PAUSE_SECONDS)
            tried_any = True
            _throttle()
            agent.llm = gemini_llm(model)
            crew = Crew(agents=[agent], tasks=[task], verbose=True)
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
                marker = type(exc).__name__ + str(exc)
                if "PerDay" in marker or "per day" in marker:
                    print(f"[{label}] {symbol}: {model} DAILY quota exhausted "
                          f"- skipping it for the rest of this run",
                          file=sys.stderr)
                    _exhausted_models.add(model)
                    continue
                if not any(m in marker for m in _RETRYABLE_MARKERS):
                    raise
                print(f"[{label}] {symbol}: {model} busy/rate-limited "
                      f"({type(exc).__name__}) - trying next model",
                      file=sys.stderr)
        if not tried_any:
            print(f"[{label}] {symbol}: skipped - every Gemini model's daily "
                  f"quota exhausted", file=sys.stderr)
            _daily_quota_exhausted = True
            return None

    print(f"[{label}] {symbol}: skipped - all models failed twice",
          file=sys.stderr)
    return None
