# Momentum Debate Tuning Plan

## Objective

Improve the regular-session bull/bear dispute so it can distinguish sustained
momentum from falling-knife rebounds and exhausted entries without making the
system indiscriminately more bullish.

The scanner already finds many stocks that later rise substantially. The main
weakness is that the debate currently sees only one snapshot and treats missing
company-specific news too strongly as evidence against buying.

## Safety constraints

- Keep all current holding, available-cash, position-sizing, late-entry,
  take-profit and stop-loss safeguards.
- Do not change order submission merely by adding analysis features.
- Build and review new behaviour in shadow mode before using it for submitted
  paper orders; keep submission disabled during initial active-evidence runs.
- Keep one bull call and one bear call per shortlisted symbol; do not add a
  second live debate that could exceed the Gemini daily request allowance.
- Do not send raw market bars to Gemini. Python must turn them into a compact
  numerical evidence block.
- Use US Eastern time for market-session calculations.

## Phase 1 — same-session history

Status: implemented and initially recorded in shadow mode.

For every stock in the current regular-session shortlist, read earlier
`shortlist_<HHMM>.json` files from the same US session and calculate:

- number of observations and consecutive shortlist appearances;
- first-seen time and elapsed minutes;
- return since the previous scan and since first appearance;
- number of positive observed intervals;
- current and previous rank, plus rank improvement;
- scanner-score change;
- observed high and drawdown from that high;
- relative-volume accumulation rate between scans;
- change in that accumulation rate;
- price acceleration between observed intervals;
- a provisional setup label: insufficient history, early breakout, sustained
  continuation, falling-knife rebound, exhausted/reversing, or mixed.

These features are written into every timestamped shortlist archive. They were
first collected with `affects_decisions: false`; after reviewing the 180 holds
from the 2026-08-05 session, the same compact block became active evidence for
both regular-session agents.

## Phase 2 — retrospective and live-shadow evaluation

- Replay the saved regular-session scans from the review period.
- Compare feature patterns across target-first, stop-first and neither cases.
- Record the shadow setup classification for at least five new sessions.
- Measure target-first rate, stop-first rate, maximum favourable excursion,
  maximum adverse excursion, time to target and late-entry rejection rate.
- Adjust provisional classification thresholds only when the data supports the
  change; avoid fitting rules to one exceptional stock or one day.

## Phase 3 — debate evidence and catalyst-policy correction

Status: active for dry-run evaluation after the 2026-08-05 review exposed a
structural score deadlock: 153/180 bear scores were at least 0.85, the maximum
net score was +0.10, and same-day news was sometimes described as stale. The
buy threshold was deliberately not lowered.

- pass the same compact history block to both bull and bear agents;
- include current scanner rank and score development;
- remove the bull's automatic confidence ceiling caused solely by missing news;
- treat no catalyst as uncertainty, not automatic proof of mean reversion;
- allow a tape-only bull case only when persistence, price development, volume
  pace and limited drawdown provide independent confirmation;
- require deterioration evidence before the bear labels a move as chasing or
  exit liquidity;
- keep negative news, imminent events and genuine loss of momentum bearish;
- keep the deterministic bull-minus-bear decision threshold initially
  unchanged so the prompt/evidence change can be measured separately.

The evidence now includes the explicit US trading-session date and calculated
event ages. Same-day events cannot be labelled stale, and bear risk of 0.80 or
higher requires a named imminent/adverse event or measured deterioration in
the same-session history. Labels such as chasing, exit liquidity and exhaustion
must cite that deterioration rather than relying only on a large day gain or
distance from the moving average.

This phase must reuse the existing bull and bear calls rather than run a second
complete live debate.

## Phase 4 — targeted intraday bars, if needed

If 30-minute scan history cannot reliably distinguish continuation from
reversal:

- request five-minute Alpaca bars only for the current shortlist, batched in a
  multi-symbol request;
- calculate recent returns, price slope, acceleration, higher highs/lows,
  session-high drawdown, recovery after pullback, volume pace, volume
  acceleration, volatility and liquidity in Python;
- pass only the compact derived features to the agents;
- respect the Basic-plan SIP delay by ending historical requests at the safe
  delayed-data time;
- do not fetch intraday bars for the complete scanning universe.

## Phase 5 — accurate after-close evaluation

- Fetch one-minute bars once after the complete regular session is available.
- Fetch only symbols shortlisted during that session.
- Use the bars to replace provisional 30-minute outcome labels with accurate
  target-first/stop-first order, maximum favourable/adverse excursion and time
  to target.
- Store compact evaluation results rather than committing repeated raw bar
  downloads unless raw data is temporarily required for a specific review.

## Phase 6 — activation and monitoring

- Keep order submission disabled while the revised evidence and prompts are
  first observed in live dry-run decisions.
- Keep the late-entry guard and all execution safeguards unchanged.
- Compare revised buy precision, missed-winner rate and stop-first rate over at
  least five to ten sessions.
- Change the buy threshold only as a separate later experiment, not at the same
  time as the evidence and prompt changes.

## Testing and documentation required throughout

- Unit-test history calculations, session isolation, missing scans, malformed
  archives and first-scan behaviour.
- Confirm shadow data cannot alter the shortlist, agent input, decision or
  execution path.
- Add tests before activating each later phase.
- Update the README whenever a phase becomes active production behaviour.
