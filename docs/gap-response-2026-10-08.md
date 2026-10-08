# BASIS gap-response reset — 2026-10-08

This is exploratory historical research, not untouched prospective evidence. No global information edge or profitable trading rule has been established.

Read 948,423 candidate frames across 13 deterministic chronology windows, 2026-09-26T18:49:35.663000+00:00 through 2026-10-08T21:18:06.420000+00:00. Each window contains continuous usable gaps; no 4 pp episode gate. This is a bounded chronology sample, not a full replay of every historical frame. Two windows contained no usable paired probabilities.

N is an event/UTC-day block. Rates and mean movements weight these blocks equally; confidence intervals resample blocks. Repeated adjacent frames are not independent evidence. Strata can share blocks. The report retains missing horizons, sparse-stratum reasons and exact window boundaries.

| Horizon | N blocks | OPT toward opening PM | PM toward opening OPT | Mean OPT move pp | Mean gap closure pp |
|---|---:|---:|---:|---:|---:|
| 1s | 367 | 16.5% | 1.2% | 0.0014 | 0.0005 |
| 5s | 364 | 38.8% | 5.8% | 0.0053 | 0.0010 |
| 15s | 366 | 40.8% | 8.1% | 0.0075 | 0.0054 |
| 30s | 366 | 39.8% | 9.3% | 0.0078 | 0.0049 |
| 60s | 366 | 39.2% | 11.4% | 0.0121 | 0.0018 |
| 5m | 365 | 37.7% | 16.1% | 0.0880 | 0.0079 |
| 30m | 354 | 30.5% | 24.1% | 0.3841 | 0.0242 |

The toward rates include unchanged prices as non-successes. Convergence and which leg moved are separate measurements. Source cadences and model theta matter, particularly at short horizons and while stock markets are closed.

| Opening absolute gap, at 30m | N blocks | OPT toward PM | Mean closure pp |
|---|---:|---:|---:|
| 0-1pp | 309 | 29.3% | -0.0700 |
| 1-2pp | 72 | 47.3% | 0.0107 |
| 2-4pp | 46 | 46.7% | 0.3683 |
| 4-8pp | 13 | 59.3% | 1.8035 |
| 8pp+ | 4 | 72.7% | 1.3089 |

Larger gaps have higher descriptive following rates, but the two largest bins have 13 and 4 blocks. That is insufficient to establish a scalable edge. Log-odds bins, all seven horizons and asset/event/direction/probability/expiry/session strata are available in the JSON report.

Error correction uses opening log-odds disagreement, prior 60s spot return, IV, time to expiry, threshold distance and direction. A separate ex-post regression adds spot return over the response horizon; it is attribution, not a prediction-time feature. Constant controls are dropped explicitly and uncertainty clusters on event/day. Raw interior tail probabilities remain raw; only exact endpoints use half the assumed .001 quote tick.

Raw-tail 30m, after spot: N=346. OPT beta 0.1549, 95% interval [-0.13177032645595013, 0.4415883082568045]; PM reversion b 0.0001, interval [-0.0008881297626170076, 0.0010689536945226211].
Separate .1–99.9% probability sensitivity: N=153. OPT beta 0.0425, 95% interval [-0.026527758422923042, 0.11152468029396922]; PM reversion b 0.0691, interval [0.01665555314710461, 0.1216355924707125].

The global OPT-following interval includes zero, including the quote-scale sensitivity fit. PM reversion is clearer in the quote-scale subset, but that is a sensitivity result, not proof of universal leadership. Extreme model tails can dominate raw-logit regression; do not promote a huge odds ratio to a strong trade.

Half-life: 1232 complete openings, 81 blocks, requiring no valid-frame gap exceeding five seconds. 35.7% reached half-gap. The global median was not reached within 30 minutes. P(full close before first widening) 0.61%; P(gap doubles) 18.5%. Median block time to maximum divergence 13.7 minutes. Missing continuity is not treated as convergence.

Exact v2 native salient jump timestamps/raw boundaries. Other leg uses causal preceding frame (<=2s); outcomes use as-of frames. Legacy has no salient stream and is excluded from native studies.
| Native jump study | Horizon | N blocks | Other leg follows jump | Mean other-leg move pp |
|---|---:|---:|---:|---:|
| PM jump -> OPT | 5s | 76 | 52.6% | 0.0169 |
| PM jump -> OPT | 30s | 76 | 49.7% | 0.0180 |
| PM jump -> OPT | 5m | 74 | 46.6% | 0.0089 |
| OPT jump -> PM | 5s | 36 | 24.8% | 0.0398 |
| OPT jump -> PM | 30s | 35 | 41.5% | 0.1520 |
| OPT jump -> PM | 5m | 36 | 46.1% | 0.1785 |

These are descriptive as-of response rates, including unchanged prices. Different source cadences, spot/theta-driven probability updates and overlapping events prevent interpreting the asymmetry as causal market leadership.

Historical structures use actual source surfaces available at opening, then actual exit bid/ask available by the horizon. USD conversions use observed spot; no midpoint fills. Fees, slippage and impact are itemized. First eligible positive modeled-ROI opening per event/day, no outcome-based entry selection. Terminal entries are bracketing verticals; touch entries use conditional payoffs. A candidate whose Q-only modeled payoff lies outside its actual quote band beyond numerical uncertainty is excluded from suggestions and retained for inspection. This avoids treating model miscalibration as PM information.

| Exit horizon | N | Win rate | Mean P&L | Median P&L | Mean ROI | Total spread/fees/slippage |
|---|---:|---:|---:|---:|---:|---:|
| 1s | 51 | 0.0% | $-28.70 | $-9.80 | -68.3% | $1466.35 |
| 5s | 50 | 0.0% | $-21.58 | $-9.80 | -69.3% | $1119.75 |
| 15s | 47 | 0.0% | $-28.95 | $-9.80 | -69.6% | $1375.06 |
| 30s | 50 | 0.0% | $-28.85 | $-9.80 | -69.9% | $1470.69 |
| 60s | 37 | 0.0% | $-33.68 | $-18.17 | -56.9% | $1434.69 |
| 5m | 36 | 2.8% | $-29.49 | $-13.91 | -74.5% | $1253.11 |
| 30m | 22 | 0.0% | $-26.14 | $-19.50 | -71.4% | $646.04 |

The sampled, calibration-screened structures lost money after costs. The 30m sample is only 22 event/day entries and is not a general profitability claim. Earlier provisional figures used less strict model-calibration checks; the table above is the released math. Explicit exclusions: `{"INSUFFICIENT_CONDITIONAL_PATH_EFFECTIVE_SAMPLE": 2, "MISSING_ACTUAL_BID_ASK_OR_STRIKE_BRACKET": 373, "MODEL_VALUE_OUTSIDE_ACTUAL_BOOK": 177, "NO_POSITIVE_EV_AFTER_COSTS": 113}`.

## Representation and runtime

The browser has one Markets table and one event/math pane with a PM/OPT/gap chart. Removed active analyzers, multi-wallet strategies, general tickets, evaluation workers and their browser APIs. Historical source archives and database records are retained. Administrative source replay, storage and acceptance remain CLI tools.

Terminal same-expiry verticals expose debit/max-payout Q_EXEC and binary-equivalent EV/Kelly; the actual vertical has a ramp, not a binary payoff. Later-expiry terminal structures use the same conditional payoff translation as touch structures, never p multiplied by a false digital payout. For touch events, continuous-GBM Brownian-bridge importance sampling gives conditional payoff means. Reweighting is p E_Q[X|H] + (1-p) E_Q[X|not H]. PM-only updates reuse conditional means; no giant simulation per PM tick. Effective paths and Monte Carlo error remain visible.

Calculation probability-1.3.0 explicitly admits Yahoo covering expiries up to seven days after cutoff as LOW-confidence IV proxies. The volatility law remains the existing flat-IV/zero-carry GBM. Crypto retains the 72-hour matching limit. Equity later-expiry, American-style, dividend, overnight, reference-source and unknown-delay limitations are displayed. Historical probability-1.2.1 frames are unchanged and the old prospective sidecar stays frozen. Older equity response buckets are contextual, not validation of the new mapping.

BASIS SIM is a new, isolated $100,000 funded USD account. Complete structures only; ask buys/bid sells; same conservative cost constants, one-second latency, current-event/quote/cash validation and an append-only ledger. No automatic entries, naked shorts, old-wallet reset or brokerage execution. Expiry cash payoff uses an as-of recorded price with an explicit proxy label; unavailable expiry prices leave settlement pending.

## Verification

Baseline suite: 83 passed. Removed tests protecting deleted products/algorithms. Added seven targeted math/structure regressions; retained recorder, versioned replay, immutable storage, stale packets, source adapters, acceptance and checkpoint coverage. Final suite: 58 passed.

Production activation: 2026-10-08 21:37:25 UTC; final reducer activation 22:13:25 UTC under the existing user-owned supervisor. Writer archive `c311e18edea48827ffed`, calculation `probability-1.3.0`, feature `dynamics-1.1.0`. Exactly one collector child was confirmed; source PM, Deribit BTC/ETH, Coinbase and seven Yahoo symbols were collecting. Catalog is PARTIAL (two failed catalog requests), not hidden as full coverage. No extended unattended duration gate is claimed by this reset.

Latest read-only bounded replay: 2 regions, 640 causal raw records, 456 exact observation matches, 0 mismatches, 0 unavailable reducers, 9.89s. The sample covers old and new archived reducers and crypto/equities; it is not a full 55-million-record replay. Segment integrity was `ok`; active quick_check took 0.049s and unchanged closed segments reused their sealing evidence. The frozen 180,779,655,168-byte v1 file was not rescanned.

Recorded prefix: 55,420,281 raw records, 85,785,810 observations/frames, 3,076,080 episode revisions. Historical analyzer count remains 5,159,511; last output was raw #55,194,195, before reset session #55,197,295. Raw/frame collection advanced after activation. Old wallet (2,719,526,912 bytes), prospective sidecar (145,657,856 bytes) and frozen v1 file all retained their exact post-graceful-stop size and mtime identities. No historical database deletion, rewrite, VACUUM, truncation or wallet reset occurred. Live BASIS SIM remains $100,000 with no production orders.

Actually exercised in Chrome: Markets/filter/sort, stock touch detail with actual call/put quotes/IV/volume/open interest, PM conditional payoff math, historical matched bucket, and 5m/30m/2h chart controls. Yahoo CLOSED shows an explicit reason and disabled paper buttons. Browser console had no errors. There are no old numbered tabs, ticket selector, wallet chart or run selector.

An isolated temporary recorder/DB on port 8766 exercised terminal BTC call-vertical selection -> PAPER BUY 1 -> PENDING -> FILLED -> displayed position -> CLOSE STRUCTURE -> PENDING -> FILLED. Entry debit $8.93, close credit $6.07, remaining fixture cash $99,997.13; the loss reflects spreads and modeled costs. The fixture was stopped and its browser tab closed. This is synthetic interaction evidence, not a live exchange fill. The targeted regression separately exercises stale-quote rejection and funded-account restart. No live stock fill was possible while NYSE was closed, and no live crypto order was manufactured when current eligible structures had no positive EV.

Local ignored evidence: `data/gap-response.json`, `data/markets-reset-2026-10-08.json` and preserved `data/versions/` reducer archives. Remaining limits: bounded historical coverage; sparse independent blocks in large-gap/equity strata; model probabilities versus executable prices; flat-IV/zero-carry conditional shapes; American/dividend/overnight equity proxies; unknown Yahoo delay; inverse-crypto USD cash proxy; no official settlement/exercise simulation; absent as-of expiry prices leave settlement pending. These are visible limitations, not profitable-trading or prospective-edge claims.
