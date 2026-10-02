# Trading review and parameter proposal — 2 October 2026

Repository: battersea-dynamics/fix-with-chatgpt. Inspected main commit:
`53eff3f436bef9cfdc93dc4aa4d03070795c702d` (daily records 1 October).
Review period: 10 September–1 October, 16 recorded market sessions.
All references concern Alpaca paper trading.

## Conclusion

The experiment is operating, but profitability remains unmeasured. GitHub
records submissions rather than a reconciled entry-to-exit ledger. The
archived price diagnostics are weak and incomplete; they do not justify
lowering the buy threshold or increasing position sizes.

Prepare a separate 1.5:1 minimum remaining reward/risk experiment. First
reconcile fills and exits, review the proposal, and explicitly end the old
evaluation period before activating a new version. Treat this floor as an
experimental payoff constraint, not an empirically optimized parameter.

## Operational record

| Session | Daytime attempts | Failed stages | Completed regular decisions | Paper submissions |
|---|---:|---:|---:|---:|
| 10 September | 12 | 1 | 165 | 1 |
| 11 September | 8 | 0 | 120 | 2 |
| 14 September | 12 | 0 | 166 | 8 |
| 15 September | 8 | 0 | 56 | 1 |
| 16 September | 12 | 0 | 180 | 4 |
| 17 September | 12 | 0 | 179 | 2 |
| 18 September | 12 | 0 | 180 | 3 |
| 21 September | 10 | 0 | 142 | 4 |
| 22 September | 12 | 0 | 92 | 4 |
| 23 September | 7 | 2 | 46 | 1 |
| 24 September | 5 | 0 | 3 | 0 |
| 25 September | 12 | 0 | 155 | 3 |
| 28 September | 9 | 0 | 57 | 1 |
| 29 September | 12 | 1 | 152 | 3 |
| 30 September | 12 | 0 | 174 | 1 |
| 1 October | 12 | 0 | 153 | 2 |
| **Total** | **167** | **4** | **2,020** | **40** |

A full 12-slot schedule across these sessions would yield 192 attempts;
25 are absent from the committed reports. The absence does not itself tell
us which scheduler, queue, cache or reporting failure caused the gap.
There are 2,640 archived regular scanner rows. Their difference from 2,020
decisions includes failed stages, held-name removal and incomplete evidence;
it must not be attributed entirely to quota exhaustion.

### Confirmed daily Gemini exhaustion

[The final 1 October workflow](https://github.com/battersea-dynamics/fix-with-chatgpt/actions/runs/36916680938)
completed successfully, but job `110552261659` explicitly records a daily
Gemini request-quota error with limit 500. Only 3 decisions were produced in
the 15:16 ET cycle and 2 in the 15:47 ET cycle. These are incomplete debates,
not 30 hold decisions. The new proposal records `analysis_coverage` and
identifies missing symbols even when workflow completion is successful.

The runner reserves logical calls, while CrewAI/provider retries can produce
additional underlying requests. The archive cannot establish their exact
count or exclude other usage of the same Google project.
[Google documents project-wide quotas, with daily reset at midnight Pacific](https://ai.google.dev/gemini-api/docs/rate-limits).
Do not raise the internal ceiling. Instrument actual requests/retries and
cache/provider behaviour before changing the shortlist length.

Simply shrinking 15 candidates to 12 would lower the planned daily logical
calls from 396 to 324, but would omit 8 of the 35 archived regular submissions
whose shortlist rank is available. That is a material opportunity tradeoff;
it is not automatically a quality improvement. This proposal retains 15.

### Execution failures and repeat submissions

The four recorded failures include an Alpaca stop-price rejection on
10 September, two server/protocol failures on 23 September and a connection
reset on 29 September. Several transient error types are not recognized by
the LLM runner's current bounded retry classifier. Investigate these and
missing slots separately from strategy tuning.

CRBP had two submissions on 14 September; SECZ had two on 18 September.
The records alone do not establish their exit reasons or prove duplicates.
Separate same-session re-entry outcomes in the broker reconciliation.

## Latest session

| 1 October order | Approximate UK submission time | Quantity | Reference ask | Target | Stop |
|---|---|---:|---:|---:|---:|
| INFY | 14:31 BST | 24 | $11.64 | $12.00 | $11.46 |
| ACN | 15:24 BST | 1 | $227.50 | $238.50 | $221.59 |

Both broker responses were `pending_new`, not confirmed fills. Subsequent
ACN snapshots fell to $220.53 and ultimately $213.98, below the submitted
stop. This makes ACN a concerning entry, but neither its actual stop fill nor
realized loss can be inferred from the snapshots. INFY has no subsequent
regular shortlist price in the archive. Obtain Alpaca fill/activity history
and current positions/open orders to establish both outcomes.

## Threshold diagnostics

Run `python -m tools.strategy_review --start 2026-09-10 --end 2026-10-01`.
The evaluator selects the first verified qualifying decision per symbol/day,
requires the proposed stop to be at most 5%, and only uses snapshots generated
after that decision completed. Returns begin at the delayed scan price.

| Net threshold | Qualifying symbol-days | With later snapshot | Missing later snapshot | Positive at last available snapshot | Mean next snapshot | Mean last available snapshot |
|---|---:|---:|---:|---:|---:|---:|
| 0.10 | 72 | 54 | 18 | 20 | −0.16% | −0.52% |
| 0.15 | 63 | 45 | 18 | 16 | −0.35% | −1.02% |
| **0.20 current** | **43** | **23** | **20** | **8** | **−0.09%** | **−0.67%** |
| 0.25 | 16 | 6 | 10 | 3 | +1.16% | −0.28% |
| 0.30 | 5 | 3 | 2 | 1 | −0.98% | −2.42% |

These comparisons are exploratory and strongly censored: held symbols leave
the shortlist, other stocks fall out of the top 15, observation horizons vary,
and last available prices are not necessarily closes. Snapshots cannot
determine which bracket leg was touched first. These are **not account
returns, trade win rates, or a valid strategy backtest**.

The retrospective 28 September split is also not an untouched validation
set. For example, the later-period 0.25 result has just one observed candidate.
There is no convincing support here for loosening 0.20 to 0.15 or claiming
0.25 is better. Preserve 0.20 while improving measurement.

## Proposed execution parameter

Require remaining reward/risk >= 1.5 at the live ask, after the existing
delayed-price policy and cent rounding. Reward is target minus ask; risk is
ask minus stop. Skip inadequate ratios; do not raise targets or tighten stops
to force acceptance. The shared execution path applies this to both regular
and premarket decisions. A 1.5:1 payoff has a simplified break-even win rate
of 40%, before costs and assuming exact target/stop fills. This says nothing
about the strategy's actual win probability or realized payoff.

| Historical submission | Remaining reward/risk | Proposed result |
|---|---:|---|
| CRWD, 14 September | 1.31 | Skip |
| BWIN, 14 September | 0.71 | Skip |
| IOVA, 29 September | 1.09 | Skip |

The other 37 submitted orders pass the floor at their recorded ask/exit
prices. A mocked dry-run replay confirms these 37/3 eligibility outcomes;
this replay does not model fills, account evolution or subsequent returns.
Whether the three rejected cases won or lost remains unknown.

## Other tuning candidates

The scanner ranks absolute deviations in either direction. Of 2,640 archive
rows, 1,342 had negative daily change and 357 had relative volume below 0.2.
FHTX and CTVA occupied the top two slots throughout all 12 scans on 1 October.
This is consistent with extreme downside and structural moves consuming
analysis capacity, but cannot establish the best replacement candidates
because the full ranked universe is not archived.

Test a continuation-oriented scanner in shadow mode after capturing the
full ranking or an enlarged deterministic candidate pool. In particular,
compare a positive-only volume z-score contribution against the current
absolute-volume z-score. Normalize relative volume by elapsed session time
before setting morning volume thresholds. Preserve the ability to assess
confirmed rebounds separately; do not blindly exclude low-priced stocks.

Retain cash-based 20% sizing, the $200 floor, the 5% stop ceiling, the 12%
target ceiling and the 2% downside guard for this proposed experiment.
Portfolio-wide combined stop risk remains a separate missing control.

## Validation and activation

Local verification: 53 tests run, 51 pass, 2 existing CrewAI dependency tests
skip because CrewAI is unavailable locally. Added cases cover late-entry
reward erosion, exact 1.5 acceptance, broker-side non-submission below the
floor, cent rounding, incomplete coverage, symbol/day deduplication,
post-decision timing and missing outcome reporting. No API/order calls are
made by the offline reviewer or the mocked historical replay.

The working main branch and paper-submission configuration remain unchanged
while this is a draft proposal. Before activation, reconcile old fills/exits,
review CI, close the previous evaluation window and observe the new behaviour
in dry-run mode. Then freeze a forward paper comparison with identical
reporting: completed trades, net P&L, R-multiples, drawdown, re-entry outcomes,
entry-time groups and analysis coverage. Extend the evidence window when
few independent completed trades are available; a calendar duration alone
does not validate profitability.
