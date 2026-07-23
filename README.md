# trading-agent

A multi-agent intraday trading system built from the ground up as a learning project — each piece is added and understood individually rather than pulled in as a black box. Runs against Alpaca's **paper trading** account only; nothing here places live trades.

**Stack:** Python, [alpaca-py](https://github.com/alpacahq/alpaca-py) (paper only), [CrewAI](https://github.com/crewAIInc/crewAI) for agent orchestration, Google Gemini as the LLM (pinned to `gemini-3.1-flash-lite` through CrewAI's native Gemini provider — a rolling alias once silently moved us onto a model with a 20-requests/day quota, so the model is pinned on purpose), Finnhub for earnings/news.

## Branches

- **`main`** — the current working version. Larger fixes should use a
  temporary branch and pass the test workflow before merging. This copied
  repository is independent and does not carry the original repository's
  `previous` branch.

## The trading day

`orchestrator.py` is a pure timing layer — it decides *when*, never *what*. Every stage below is independently runnable; the orchestrator just sequences them on the market's clock (all times ET, from Alpaca's own calendar, so early-close days shrink the schedule automatically):

```
open-45min   pre-market chain
open         poll Alpaca's clock until actually open (bounded), then
             pre-market execution
open+45min   fresh scan -> archived 15-stock list -> bull/bear debate
             -> possible paper buy, all in the same cycle
every 30min  repeat the complete cycle; final start at close-15min
```

### Pre-market chain (six components, file seams between them)

```
market_calendar    gate: is there a session today? (every component checks)
premarket_scanner  whole dynamic universe (~2,300 stocks) -> pre-market
                   rel volume + gap vs a PRE-MARKET baseline -> top 12
                   -> data/premarket_scan.json
premarket_news     Finnhub headlines for the shortlist
                   -> data/premarket_news.json
candle_agent       LLM reads yesterday's daily + today's PM candle
                   (pre-computed ratios; code divides, model judges)
                   -> data/premarket_candles.json
premarket bull /   adversarial debate per stock: strongest genuine case
bear agents        for and against, on identical evidence, anchored 0-1
                   scores; must cite headlines or note their absence;
                   auto fact-checked -> data/premarket_{bull,bear}_cases.json
premarket_trader   no LLM: net = bull - bear, buy at net >= 0.2,
                   bear-risk-tempered TP/SL -> data/premarket_decisions.json
premarket_execution reads decisions, applies guards (below), submits GTC
                   bracket orders - dry-run unless --submit
```

### Regular-session pipeline

```
stage 0: portfolio state     (start of every check run)
  snapshot_portfolio - cash, buying power, holdings
                       -> data/portfolio_state.json (audit record)

stage 1: daily_scan          python pipeline.py scan
  held-symbol filter - shortlist slots never wasted on stocks already owned
  universe_builder   - all tradable US equities: price >= $3, avg volume
                       >= 500k, real stocks only (no ETPs/OTC/preferred)
  catalysts prescan  - one bulk Finnhub call: earnings in the next 1-3 days?
  scanner            - rel volume + % change + MA distance, z-scored, plus
                       an absolute-volume kicker and a catalyst boost
  -> data/lists/<date>/shortlist_<HHMM>.json

stage 2: check_shortlist     python pipeline.py check [--submit]
  catalysts          - per-symbol earnings/dividends/news for the shortlist
  bull/bear debate   - same adversarial structure as pre-market (committed
                       one-sided cases, honest anchored scores, must cite
                       dated evidence or admit there is none)
  case verifier      - deterministic numeric fact-check of both case texts
  trader             - no LLM: net score -> buy/hold + tempered TP/SL
  execution agent    - no LLM: filters, sizes, submits GTC bracket orders
                       (dry-run unless --submit)
```

During the regular session those two stages run back-to-back as one
`daytime_cycle`: every cycle produces a new timestamped shortlist,
immediately debates it, and immediately reaches the deterministic execution
decision. The JSON files are both scheduling seams and the audit trail. Exits
are enforced broker-side via bracket orders (attached take-profit + stop-loss,
one-cancels-other) — nothing watches positions after entry; the broker does.

## Safety guards (all in one place)

| Guard | Where | What it protects against |
|---|---|---|
| Calendar gate | every pre-market component + orchestrator | running against a closed market (weekends, holidays, half-days) |
| One-sided-evidence skip | signal orchestrators + premarket trader | a stock with only a bull case (or only a bear case) can never become a trade |
| Confidence threshold (>= 0.6) | execution agent | buys below the trader's net-score bar never execute (thresholds aligned by construction) |
| Numeric fact-checker | after every debate + execution agent | cited numbers that don't trace to source data block the trade (`numbers_verified`) — numbers only; an invented *qualitative* claim can still pass |
| Duplicate-entry guard | portfolio filter + execution agent | a currently held symbol is removed before regular-session analysis and checked again before execution; an active buy order is also blocked, while failed/cancelled/expired attempts may be retried on a later scan |
| Cash-based position cap | execution agent (shared by both pipelines) | max 20% of currently available cash per position, with a $200 minimum budget that can never exceed the cash left (`$10,000 -> $2,000`, `$500 -> $200`, `$200 -> $200`, `$50 -> $50`); whole shares only and no margin buying power |
| Exit ceilings (asymmetric) | execution agent (shared by both pipelines) | take-profit above 12% is clamped down and proceeds (capping upside is safe); stop-loss above 5% skips the trade entirely — a wide stop is the bear's honest volatility read, and tightening it would convert noise into stop-outs |
| Dead-quote guard | execution agent | market buys are never sized off a 0/absent ask (closed market, thin tape) |
| Delayed-price exit guard | regular execution | a live ask more than 2% below the analysed price invalidates the thesis; an ask above it keeps the original absolute take-profit target, so already-realised movement reduces the remaining upside and reaching the target skips the trade |
| Closing-time guard | regular execution | immediately before submission Alpaca must report the market open with at least two minutes remaining |
| Gemini daily-call ceiling | LLM runner | stops at 450 logical attempts, preserving headroom below the broker-agent project's 500-request daily limit |
| Delayed-price exit guard | premarket execution | uses the same policy as regular execution: downside beyond 2% skips; a lower accepted entry shifts the target down by the same percentage, while a higher entry leaves the original target fixed |
| Stale-decisions guard | premarket execution | yesterday's gap thesis can never execute today |
| GTC bracket orders | broker | exit legs never expire at the close, leaving an unprotected overnight position (Alpaca caps GTC at 90 days) |
| Dry-run by default | both execution paths | orders are only submitted with an explicit `--submit` (paper account only — even "submit" is a paper order, never real money; the flag is named `--submit`, not `--live`, so it never reads as real money) |
| Daily-quota latch | LLM runner | a burned Gemini daily quota fast-fails the run instead of retry-sleeping through guaranteed failures |

## Running

**Orchestrated (the normal way):**

```
.venv\Scripts\python.exe -m orchestrator            # full scheduled day, dry-run
.venv\Scripts\python.exe -m orchestrator --submit   # ...submitting paper orders
```

Start it before open−45min (08:45 US Eastern). That is 13:45 UK time
for most of the year and 12:45 UK time during the short weeks when the US
and UK change daylight-saving time on different dates. All program schedule
math remains US Eastern and uses Alpaca's market calendar; UK times are for
the operator's convenience only, so no manual time conversion belongs in the
trading logic.

**Any stage manually (testing never waits for the clock):**

```
.venv\Scripts\python.exe -m orchestrator --force premarket
.venv\Scripts\python.exe -m orchestrator --force premarket_exec [--submit]
.venv\Scripts\python.exe -m orchestrator --force daily_scan
.venv\Scripts\python.exe -m orchestrator --force check [--submit]
.venv\Scripts\python.exe -m orchestrator --force daytime_cycle [--submit]
```

**Or the underlying entry points directly:**

```
.venv\Scripts\python.exe pipeline.py scan | check [--submit] | all [--submit]
.venv\Scripts\python.exe -m tools.premarket_scanner [YYYY-MM-DD]
.venv\Scripts\python.exe -m tools.case_verifier
```

## Automation (GitHub Actions)

`.github/workflows/automatic-trading.yml` receives dispatches from the
Cloudflare Worker for pre-market, market-open execution, and anchored
30-minute daytime slots. Off-window
ticks exit in seconds; the orchestrator's ET logic remains the only clock that
decides which stage, if any, is due. Each tick is a fresh VM: `data/` (state file +
all pipeline file seams) is carried between ticks via `actions/cache`, so
short-lived runners behave like one long-running orchestrator. Execution is
marked done on *attempt*, never retried — a possibly-half-submitted order loop
must not double-order.

- **Dry-run by default, everywhere.** Scheduled runs submit orders only if the repo *variable* `TRADING_SUBMIT` is exactly `true`; manual runs only if the `submit` checkbox is ticked. Neither exists by accident. (Everything is a paper account — the flag/variable are named `submit`, not `live`, so nothing ever reads as "real money".)
- **Manual testing:** Actions → trading → "Run workflow" — pick a stage (`tick`, `premarket`, `daily_scan`, ...) and run it immediately.
- **Daily report:** tick mode appends every stage outcome, order, guard trigger, and error to `data/reports/daily_report_<date>.json` (deterministic, no LLM); the workflow commits each day's full record (report + lists) once at end of day, so the audit trail is readable on GitHub without opening Action logs.

### Scheduler reliability and Cloudflare operation (July 2026)

GitHub's native scheduled events were intermittent, although manual and
push-triggered runs worked normally. The production clock is therefore a
Cloudflare Worker Cron Trigger; GitHub's native trading schedule is disabled.

Cloudflare uses three UTC Cron Triggers:

```
45 12,13 * * MON-FRI
30 13,14 * * MON-FRI
15,45 14-20 * * MON-FRI
```

They cover the pre-market wake, market-open execution, and daytime cycle
slots across both US daylight-saving regimes. The union deliberately creates
a few extra off-season wakes; the orchestrator accepts only exact US Eastern
slots (with five minutes of queue grace), so every extra wake exits without
running a pipeline stage.

Cloudflare only dispatches the existing GitHub workflow:

1. Cloudflare sends an authenticated `workflow_dispatch` request to GitHub.
2. GitHub restores state and runs the orchestrator, trading logic, reports,
   and paper-order code.
3. Alpaca, Gemini, and Finnhub credentials remain GitHub Actions secrets.
   Cloudflare stores only the repository-scoped token used for dispatch.
4. `TRADING_SUBMIT` remains the master switch for automatic paper-account
   orders. The broker clients use `paper=True`; this project never targets
   Alpaca live trading.

### Data layout (temporary, for the active review period)

Per-run artifacts are partitioned by ET session date instead of being overwritten: `data/lists/<date>/` holds every list/case/decision file (per-cycle `shortlist_<HHMM>.json` and `check_decisions_<HHMM>.json`,
pre-market scan/news/candles/cases/decisions), `data/reports/` holds the daily reports, and `data/weekly/` is reserved for the future weekly summary. These dated folders are **committed** — they're the record being actively reviewed over the coming weeks. This is explicitly **not a permanent design**: once the review period ends, revisit (likely revert to plain overwritten files, or add retention) — see the note in `tools/datapaths.py`. Loose files in `data/` (universe cache, orchestrator state, portfolio snapshot) remain runtime-only and gitignored.
- Credentials live in encrypted repository secrets (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `GEMINI_API_KEY`, `FINNHUB_API_KEY`), referenced by name in the workflow only.

## Roadmap / known gaps

Nothing in this section is built yet — it exists so the README never implies more safety or automation than exists, and so the ordering below has a recorded rationale.

**On the external review (July 2026):** an outside architecture review validated the core design — the separation of *evidence gathering → judgment → decision → execution* into distinct stages, the deterministic (no-LLM) trader and execution layers, and the dry-run-by-default posture — and flagged **portfolio-level risk as the main missing piece**. The priority order below reflects that review together with our own assessment; it's why the list is ordered the way it is, not just what's on it.

**Immediate operational sequence:** the Cloudflare scheduler has delivered
a complete US trading session reliably. The current step is to validate the
new complete daytime cycles in dry-run mode, then review and tune the
pre-market scanner, regular-session shortlist, bull/bear evidence and scores,
and final stock-selection rules using the dated paper-trading audit records. These operational and evaluation steps
come before adding another major subsystem; they do not remove the
portfolio-risk priority below.

**Priority order for what comes next:**

1. **Portfolio-level risk manager** — limits on total simultaneous exposure, maximum number of open positions, and sector/correlation concentration. Today every trade is judged alone; nothing sees the portfolio as a whole. Confirmed as the next major build both independently and by the review — the planned "risk agent" slot between signal and execution.
2. **A/B evaluation: scanner-alone vs. scanner + bull/bear debate** — measure whether the LLM debate layer actually improves outcomes over the deterministic scanner on its own, *before* investing further in it.
3. **Full broker reconciliation** — current positions and active buy orders
   are checked before execution, but the system still needs explicit
   reconciliation of ambiguous submissions, partial fills, and protective
   legs before treating broker state as fully transactional.
4. **Protective-order integrity monitoring** — actively confirm every open position's bracket legs (take-profit + stop-loss) are intact, rather than assuming GTC silently handles everything. A dropped or cancelled leg would currently go unnoticed.
5. **Persistent transactional state (e.g. SQLite)** — for operational state specifically, alongside the existing JSON audit files. Not urgent at current scale; noted as a future consideration.

**Other known limitations (documented for honesty, not necessarily on the build path):**

- **No calibration.** Every threshold (net-score 0.2, confidence 0.6, 2% downside deviation, 20%-of-cash position cap with a $200 floor, 12%/5% exit ceilings, TP/SL tempering) is a reasoned first guess, deliberately deferred until there's real paper-trading history to calibrate against.
- **Numbers-only fact-checking.** The case verifier cannot catch an invented *qualitative* claim (a fabricated catalyst) — only numeric drift.

## Setup

Requires Python 3.12 (3.14 doesn't yet have prebuilt wheels for some dependencies — a `.venv` on 3.12 is recommended).

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your keys — all four variables are required:

| Variable | Source |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | paper keys from the [Alpaca dashboard](https://app.alpaca.markets/paper/dashboard/overview) |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) |
| `FINNHUB_API_KEY` | free tier at [finnhub.io](https://finnhub.io/register) |

`.env` is gitignored; never commit real keys. Runtime artifacts live in `data/` (also gitignored).
