# Independent review — `battersea-dynamics/fix-with-chatgpt`

**Reviewed:** 14 August 2026 · commit on `main` · ~6,500 lines of Python across 44 files, plus 16 sessions of committed dry-run archive (2,593 regular-session decisions, 216 pre-market decisions, 67 generated orders).
**Method:** full read of the code, the README, and `docs/trading-agent-repo-review.md`; the test suite run offline; and a statistical pass over the committed `data/lists/` archive. **No files were changed.**
**Not covered:** live behaviour (the archive is almost entirely dry-run), and anything that depends on real fills. This is engineering and design feedback, not investment advice.

---

## 1. The short version

This is a genuinely good piece of engineering. The architecture is right, the safety posture is right, and the documentation is better than most commercial systems. The third-party review says the same and I agree with it.

What that review did not do — and what I've done here — is **test the design against the data the system has already produced**. That changes the picture in one important way:

> The bull/bear debate is not currently working as a debate. The bear's score is very nearly a constant. In 2,593 decisions it never went below 0.50, sat at exactly 0.85 half the time, and accounts for only **14% of the variance** in the net score. Every one of the 71 buy signals required the bear to break away from 0.85.

So the elegant `net = bull − bear ≥ 0.2` policy is, in practice, executing a much simpler and unintended rule: **buy when bull ≥ 0.85 and bear happens to drop below 0.85.** 90% of buys came from `bull ≥ 0.85`; 100% came from `bear < 0.85`.

That is not a reason to stop. It's a reason to fix the scoring layer *before* Monday's submission trial, or at minimum to record it as a known condition of the trial, because it changes what the trial can tell you.

Everything else below is ordered by how much it would change outcomes.

---

## 2. What is solidly built (and worth protecting)

I want to be specific rather than complimentary in general terms, because these are the parts that shouldn't be touched during tuning.

- **The separation of concerns actually holds.** `orchestrator.py` really does contain no trading logic. The signal layer has no order access; the execution layer has no judgment. That asymmetry is a real security model, not a diagram — no single failure can both invent and place a trade.
- **The test suite is hermetic and fast.** 46 tests, running in 0.03 seconds with no API keys and no network. They cover the safety paths specifically (guards, duplicate entry, budget floor, close-time gate, broker reconciliation, catalyst resilience). That is the correct thing to spend test budget on and it's rare in hobby projects.
- **Fail-safe degradation is consistent.** Missing bull or bear case → no trade. Rate limit → `None` → no trade. Incomplete catalyst evidence → symbol skipped. Ambiguous submission → one read-only lookup, never a resubmit. Execution marked done on *attempt*. Every degradation lands on "fewer orders", which is the only correct direction.
- **`tools/market_data.py`** is the best file in the repo. It documents a real bug, the measurement that ruled out the obvious alternative (IEX carrying 0.1% of pre-market volume), and the reason the chosen fix is cheap. That's how a decision record should read.
- **The honesty of the README.** Listing "no calibration" and "numbers-only fact-checking" as known gaps, unprompted, is worth more than any feature in the repo.

---

## 3. Where I differ from the third-party review

I agree with all six of its "statistically weak" points. Three refinements:

| Their point | My refinement |
|---|---|
| "LLM scores are not calibrated probabilities" | Correct but understated. It's not that the two scores are *miscalibrated*; it's that **one of them barely varies**. Calibrating a near-constant won't help — the bear prompt needs to produce a distribution first. |
| "Fact-checker only catches numeric drift" | Correct, and now measured: a fabricated percentage in the plausible 0.5–15% range **passes the verifier ~35% of the time by chance alone** (median evidence pool = 17 numbers, ±6% tolerance, flat matching). It's a coarse tripwire, not a filter. |
| "Log everything into a structured dataset" | Half-done already, and the missing half is narrower than they imply. The dated JSON archive *is* the decision log — what's missing is only the **outcome label** (did the TP or SL get hit first, and when). That's a small addition, not a database migration. |

One thing the review missed entirely is the next item, which I'd put above everything on its list except portfolio risk.

---

## 4. Findings from the archive

### 4.1 The bear agent's score carries almost no information

Distribution across 2,593 regular-session decisions:

| | min | p25 | median | p75 | max | variance |
|---|---|---|---|---|---|---|
| `bull_confidence` | 0.05 | 0.30 | 0.40 | 0.60 | 0.95 | 0.0476 |
| `bear_risk` | **0.50** | 0.75 | **0.85** | 0.85 | 0.95 | **0.0079** |

`bear_risk` = exactly 0.85 in 49.5% of cases, and is in {0.85, 0.95} in 62.6%. It never used the bottom half of its own scale — despite the prompt defining `<0.3` as "strained, you had to stretch". Pre-market is worse: minimum 0.70, and 0.85 in 62% of cases.

Correlation between the two scores is only −0.28, so they aren't even moving as opposites.

**Why it probably happens.** The bear prompt is asked to argue one-sidedly *and* is handed a checklist of failure modes, on a shortlist selected precisely for being extreme. An instruction-tuned model given "find how this trade dies" on a stock that's +28% and 29% above its moving average will always find something, and "0.85" is the natural expression of "serious, but I'm not naming an imminent event". The calibration rules added later (0.80+ requires a named event) tightened the top but left no pressure at the bottom.

**What I'd try, in order:**
1. Give the bear a **forced-distribution instruction** — the same trick that fixed the original single-analyst hedging problem. e.g. "across a typical shortlist of 15, roughly a third of setups are ordinary and should score 0.3–0.5."
2. Add worked **few-shot anchors** for the low end specifically: one example of a clean setup scored 0.25, one at 0.45. The bull has effective anchors because the tape-only cap forces the low end; the bear has none.
3. Measure again on the *same* archived shortlists before changing anything else. You can re-run the debate on historical scan rows with no market-data cost — only Gemini calls.

Until the bear varies, the "A/B test scanner vs scanner+debate" on the roadmap will effectively be testing *scanner vs scanner + bull agent*, and it's worth knowing that going in.

### 4.2 The buy threshold sits exactly on the modal value

Of 71 buys, **49 (69%) landed on `net = 0.20` exactly** — the threshold itself. Another 14 at 0.25.

Because both agents quantise to 0.05, a single 0.05 tick in either score flips roughly 70% of all buys. This isn't the usual "uncalibrated threshold" complaint — the threshold has been placed at the single most crowded point in the distribution, which is the least stable place it could be. Any prompt edit, any model update, any temperature drift will move the trade count sharply, and you'll misread it as a strategy effect.

Before the trial, I'd record the full net-score histogram as a baseline so you can tell "the market changed" from "the scores shifted 0.05".

### 4.3 Roughly half of every LLM budget is spent on candidates that can never be bought

The scanner is direction-agnostic by design ("a crash is as much of a candidate as a spike"). The system is long-only.

- **48.8%** of shortlist rows are stocks that are **down** on the day.
- **0 of 69** matched buys came from one. Not a low rate — zero, across 16 sessions.

That's ~180 Gemini calls per day, half the shortlist capacity, and half the debate wall-clock time, spent on a population with a 0% historical conversion rate. It's also the reason the daily call ceiling feels tight.

Three ways out, cheapest first:
1. **Sign-aware ranking** — keep `|z|` for `rel_volume` (direction-free by nature) but use signed `z` for `pct_change`/`ma_distance`. Frees half the budget immediately; costs nothing.
2. Keep the full scan but **split the shortlist quota** (e.g. 12 up-movers, 3 down-movers) so you retain a control group for later analysis.
3. Add a short side. Much bigger project; not for now.

Option 1 alone would let you either double the depth of coverage or halve the run time — and run time matters (see 4.7).

### 4.4 The volume kicker is a time-of-day artefact, not a signal

`tools/scanner.py` uses **daily** bars. During the session the latest daily bar is partial, so `rel_volume` is "volume so far today ÷ 20-day full-day average" — a number that mechanically grows from open to close. The docstring flags this caveat for manual runs, but the daytime cycle runs on it 12 times a day.

Measured on the archive (median `rel_volume` of shortlisted names, by cycle):

| Cycle (ET) | 10:16 | 11:16 | 12:16 | 13:16 | 14:16 | 15:16 | 15:46 |
|---|---|---|---|---|---|---|---|
| Median `rel_volume` | 1.61 | 2.63 | 2.97 | 3.43 | 3.95 | 4.89 | 5.02 |
| % above kicker floor (1.2) | 64% | 82% | 85% | 83% | 91% | 94% | 95% |

The kicker's parameters are absolute (`FLOOR=1.2`, `CAP=4.0`), so:

- **In the morning** it barely fires — a stock needs ~6× normal *pace* to clear a floor calibrated on full days.
- **In the afternoon** nearly everything is past the 4.0 cap, so the kicker pays a flat +4.2 to almost every candidate and stops discriminating entirely.

The median shortlist score also drifts up through the day (15.2 → 17.6), consistent with that.

The fix is to normalise by session progress: compare today's volume-so-far against the *same fraction* of the 20-day average day (or switch to minute bars for the intraday leg, which the pre-market scanner already does). Until then, `rel_volume` is not comparable across cycles, and any cross-cycle statistic you compute during the trial inherits the bias.

### 4.5 Take-profit is anchored to the analysis price; stop-loss is not

In `execution_agent.py`, when the live ask has risen above the scan price:

```
take_profit = reference_price * (1 + tp_pct/100)     # anchored to the old price
stop_loss   = ask * (1 - sl_pct/100)                 # computed from the new price
```

The README explains the anchoring correctly ("already-realised movement reduces the remaining upside") — but the stop keeps its **full percentage from the higher entry**. So a run-up between scan and execution shrinks the reward and leaves the risk untouched.

Observed in the archive (67 orders):

| Reward:risk | Count |
|---|---|
| < 1.0 | 3 |
| < 1.5 | 6 |
| median | 2.02 |

The worst cases: **IQV at 0.33** (+0.87% target vs −2.60% stop — needs a 75% hit rate to break even) and **QNST at 0.36**, both after ~5–7% drift between scan and execution. The only guard is "is there *any* upside left at cent precision", which 0.87% passes.

**Fix, one line, in the spirit of the existing guards:** skip the trade if `remaining_TP < k × SL` (k ≈ 1.5). Don't tighten the stop — that would violate the (correct) rule about not converting noise into stop-outs. Skipping is the right failure mode and matches everything else in that file.

Median across all orders is R:R 2.02, i.e. a break-even hit rate around 33%. Worth writing down as the number the trial has to beat.

### 4.6 What the system actually trades

Independent of intent, the realised profile of the 69 buy candidates is:

- median day change at entry **+27.8%**
- median distance above the 20-day MA **+29.3%**
- median price **$12.29**; 19% of all shortlist rows are under $5
- 61% carried an earnings catalyst flag (vs 29% base rate)
- 25 unique symbols across 16 sessions

That is **momentum continuation in extended small caps** — which is fine as a strategy, but it sits in some tension with a bear agent whose primary named failure mode is "chasing". Worth stating explicitly in the README so the trial is evaluated against what the system does, not what it was framed as.

Two related notes:
- 4.7% of shortlist rows are priced below the universe's own $3 floor. The floor is applied when the daily universe cache is built, not as an ongoing invariant. On a system trading sub-$5 names, that matters for spread and slippage.
- The scanner's score is an uncapped sum of `|z|` values. Median span from rank 1 to rank 15 is **42.5**, with a **13.6** gap between rank 1 and rank 2 — one metric on one outlier dominates the top of the list. Meanwhile the rank 14→15 gap is **0.22**, so the flat `CATALYST_BOOST = 2.0` is decisive at the margin: ~12% of shortlist rows are catalyst names that wouldn't otherwise make the cut. That may well be what you want — but it's a much stronger lever than "flat bonus" suggests.

### 4.7 The last cycle of the day systematically creates overnight positions

`LAST_CYCLE_BEFORE_CLOSE_MIN = 15`, so the final cycle starts at 15:45 ET. Measured from the archive, a full cycle takes **~7 minutes** (15 stocks × 2 calls × 8s spacing, plus the scan) — the 15:46 cycle on 13 August finished at 15:53.

`MIN_SECONDS_TO_CLOSE = 120` then permits submission with 7 minutes left. A bracket entered at 15:53 on a stock with a 3% stop and a 6% target will essentially never resolve intraday, so it becomes an overnight hold — and a stop-loss does not protect against a gap. The README describes the horizon as "hours to a few days", so this may be intended; but it means the final cycle has a materially different risk profile from the other eleven, and it's the cycle least likely to be noticed.

Options: move the last cycle to close−45min, raise `MIN_SECONDS_TO_CLOSE` to something like 1,800, or accept it explicitly and size that cycle differently.

### 4.8 The `$200` position floor inverts the risk control exactly when it matters

`min(cash, max(cash × 0.20, 200))` is correctly documented and correctly tested. But read it as a percentage of remaining cash:

| Cash left | Budget | % of remaining cash |
|---|---|---|
| $10,000 | $2,000 | 20% |
| $1,000 | $200 | 20% |
| $500 | $200 | **40%** |
| $200 | $200 | **100%** |

The 20% cap is the risk control; the floor is a usability convenience so small accounts can still trade. As written, the convenience overrides the control precisely when the account is most depleted. Median observed order size is $383, so on a ~$2,000 account you are already close to the region where the floor starts binding.

I'd either make the floor a hard *stop* ("if 20% of cash is under $200, stop trading for the day") or accept a smaller minimum. This interacts directly with the missing portfolio-risk layer and is arguably a cheaper first step.

### 4.9 There is no "already traded today" ledger

The duplicate-entry guard checks *currently held* positions and *currently open* buy orders. If a position stops out at 13:00, the symbol is no longer held and no order is open — so the same name can be re-entered at 13:46 with no memory that the thesis just failed. With 25 unique symbols across 69 recommendations (up to 7 repeats of the same name), this will happen in submit mode.

A per-session traded-symbol set, persisted alongside the orchestrator state, is a small change and closes the loop.

### 4.10 The trading policy exists twice

`BUY_THRESHOLD = 0.2`, `MAX_TEMPERING = 0.5`, the epsilon and the clamp logic are declared independently in `tools/trader.py` **and** `tools/premarket_trader.py`. They agree today. Nothing enforces that they agree tomorrow, and a threshold tuned in one place while the other silently diverges is exactly the kind of bug that's invisible in the audit trail. One `policy.py` imported by both.

### 4.11 Operational hygiene

- **The Actions cache caches your git history.** `path: data` now includes `data/lists/` and `data/reports/`, which are *also committed* (~9 MB and growing). A new cache entry is written on every tick — ~12 per day — each carrying the whole archive. Restrict the cache to the runtime files (`orchestrator_state.json`, `universe.json`, `llm_usage.json`, `portfolio_state.json`) and let git own the archive.
- **The LLM ceiling has thin headroom.** Planned usage is 396 calls/day against `MAX_DAILY_CALL_ATTEMPTS = 450`. Retries consume reservations too, so a day with a handful of 429s can hit the ceiling — and the failure is not graceful degradation but a latch that zeroes out every remaining cycle. Fixing 4.3 roughly halves demand and makes this a non-issue.
- **Dependency pinning is inconsistent.** `alpaca-py` and `crewai` are pinned; `python-dotenv` and `requests` are not. For a week where the stated protocol is "freeze all trading logic", pin all four.
- **Minor doc drift.** `signal_agent.py` still says calls are spaced 13s; `llm_runner.py` uses 8s. `scanner.py`'s docstring still describes a 143-ticker universe.
- **Historical timestamp bug, already fixed, still in the data.** `check_decisions_1738.json` from 21 July carries `generated_at: 2026-07-21T17:38:21` with no offset, while the order inside it is stamped 13:38:21 ET — a UTC/ET mix-up on the runner. Current files carry proper `-04:00` offsets, so this is resolved, but anything you compute across the full archive should treat pre-22-July timestamps as suspect.
- **Workflow interpolation.** `${{ inputs.stage }}` is interpolated directly into `run:`. It's constrained to a `choice` enum so it's safe in practice; using an `env:` variable instead is the standard hardening and costs nothing.

---

## 5. On the trial starting Monday

The protocol in the README is well designed — freeze the logic, don't interfere, review every session. Two additions I'd make before Monday:

1. **Record the baseline distributions first** (net-score histogram, bear-score histogram, buy count per session, R:R per order). Without them, any change over the week is unattributable.
2. **Add outcome labelling now, not after.** For each filled order, record: fill price vs `entry_ref` (slippage — currently invisible because both legs are computed off the pre-trade ask, not the fill), which leg hit first, time to resolution, and realised R-multiple. This is the one genuinely missing piece of the audit trail and it's ~50 lines against the existing order records.

And one caution about what the week can prove. Five sessions at the current rate is roughly 20 filled trades. At a 33% break-even hit rate, that sample cannot distinguish a real edge from noise — the confidence interval on a 20-trade hit rate is roughly ±20 percentage points. The trial is a **systems test** (do brackets attach, do guards fire, does sizing behave, does the duplicate guard hold, does anything double-order) and should be read as one. Your own README already says this; I'm underlining it because a good week is psychologically much harder to discount than a bad one.

---

## 6. If I were ordering the work

1. **Fix the bear score distribution** (4.1) — until this varies, everything downstream, including the planned A/B test, is measuring one agent. Re-runnable against archived shortlists at Gemini cost only.
2. **Minimum reward:risk gate on execution** (4.5) — one condition, removes the worst orders in the archive, ships before Monday.
3. **Traded-today ledger** (4.9) and **the floor/stop interaction** (4.8) — both small, both matter only once submissions are live, so both belong before Monday.
4. **Outcome labelling** (§5.2) — the thing that turns the archive into evidence.
5. **Sign-aware scanning** (4.3) — halves the LLM bill and the run time; enables everything else.
6. **Session-progress volume normalisation** (4.4) — makes cross-cycle statistics valid.
7. **Portfolio risk layer** — as both the README and the outside review say. My only amendment is that items 2, 3 and 6 are cheaper and are prerequisites for measuring whether the risk layer helped.

---

## 7. Closing

The thing I'd most want you to take from this: **the design is sound and the data disagrees with the design in one specific, fixable place.** Two independent reviews have now told you the architecture is right, which is reassuring but not actionable. What's actionable is that the archive you've been diligently committing for four weeks contains a clear, measurable answer about the debate layer — and it says the debate currently has one voice.

That the archive could answer the question at all is a credit to the design. Most systems at this stage can't be interrogated like this.

*Reviewed on the code as of 14 August 2026. All statistics are computed from the committed `data/lists/` archive; every figure above is reproducible from the repo as it stands. I'm not a financial adviser and nothing here is a recommendation about trading — it's a read of the code and the records it produced.*
