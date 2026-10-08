# BASIS prospective evaluation — 2026-10-01

**Status: INSUFFICIENT_PROSPECTIVE_SAMPLE. No informational edge is established.**

This pass adds measurement machinery on top of compact storage v2. It changes no probability model, analyzer algorithm, paper policy, cost or position size. The browser terminal is unchanged.

## Permanent registration

| Field | Frozen value |
| --- | --- |
| Campaign | `basis-edge-e42712f62c63`, version 1, PROSPECTIVE |
| Started | 2026-10-01 **21:09:05.802 UTC** |
| First eligible global raw ID | **21,952,685** |
| First eligible global frame ID | **67,942,860** |
| Git revision at registration | `ebb6e697db804235271fc75118e31c39f089468d` — storage-v2 / PR #3 |
| Producer archive at registration | `252261789b068fcea68d` |
| Reducer fingerprint | `1133d8d67863377be04f0c00edf36f1295577503d272d97599177ba3a2cd560e` |
| Calculation / feature | `probability-1.2.1` / `dynamics-1.1.0` |
| Analyzer versions | 1.1.0; sequential martingale remains disabled |
| Configuration hash | `f86f37f138746b93` |
| Storage | 2; global IDs continue the legacy prefix |

The exact protocol and initial as-of panel are in `data/basis.evaluation.sqlite3`, redundantly recorded as `evaluation_campaign` in the operational journal. UPDATE, DELETE and INSERT OR REPLACE of frozen records are rejected. `evaluate --start` returns the original campaign; unrelated code changes and restarts do not move it. Missing reducer archives or changed scientific/config definitions stop eligibility rather than substitute current code.

Registration preceded scoring. During implementation, post-boundary frames were causally reconstructed from their frozen inputs; this is **not** a claim that a live predictor made decisions at those earlier instants. Entries retain their actual creation time, opening IDs, semantic hash, source/model quality, features, comparable-strike nodes, underlying context, available analyzer outputs and native salient references. Equal-raw analyzer results generated after `Engine.observe()` are conservatively excluded.

## Frozen v1 protocol

- Entry: first eligible compact frame opening a gap of at least **4 pp**. Close at **1 pp**, or reverse/change semantics. A pre-boundary open gap is left-censored; a data outage does not manufacture repeated new episodes. Entry origins and outages remain inspectable.
- Primary horizon: **30 minutes**. Secondary fixed horizons: **30 seconds, 5 minutes, 2 hours**; expiry and resolution where available.
- Primary repricing outcome: `sign(PM_0 - OPT_0) * (OPT_h - OPT_0) * 100`. Gap convergence is separately `abs(GAP_0) - abs(GAP_h)`. Distance to opening Poly also records overshoot.
- Material-motion epsilon: **0.10 pp**. Movement attribution distinguishes target toward Poly, Poly toward target, both converging, common-direction motion, widening and stable/mixed cases. It asserts no cause.
- Horizon quote selection: last causally consumed frame **at or before** the deadline, maximum frame age **15 seconds**. No later nearest quote or interpolation is substituted. Quality failures are `MISSING`, with last-known provenance retained; clock/monotonic discontinuities cannot become future horizon data.
- Random controls: one deterministically scheduled opportunity per event/half-hour, seed **1729**. A treatment matches only an already available control from another event within two hours, in the same asset, type, direction, conventional-probability, expiry, session, IV and threshold-distance bins. Control outcomes never select the match. Comparisons apply the treatment's opening GAP sign to control target movement; coverage and reuse are reported.
- Simple baselines stratify this **episode family**, not all ticks: signed GAP; 4–8 / 8+ pp absolute GAP; REL ≥30%; fresh PM movement ≥0.25 pp in GAP direction; neighbor majority; PM travel ≥70%; matched random controls; no prediction. A smaller tail discrepancy is not silently promoted to a primary episode.
- Analyzer overlap: original scores, prior-64-score 90th-percentile evaluation flags after 32 valid baseline scores, same episode/time, and actual paper candidate overlap. Anomaly diagnostics do not supply price-direction votes.
- Incremental evaluation: causal expanding least squares comparing BASE and BASE+one analyzer or PM movement on the same complete cases. Labels must mature before the tested entry. BASE retains GAP, REL, prior spot movement, expiry, IV, direction and neighbor agreement. Training starts only after 50 complete observations and 20 events. No future spot enters forecasts.
- Retrospective spot conditioning separately includes subsequent spot movement and reports UP / DOWN / ±0.2% FLAT strata. This is association, not identification of causal leadership.
- Uncertainty: deterministic asset/UTC-day block bootstrap after **eight blocks**; no frame-based standard errors. Overall sufficiency also requires **50 scored primary episodes and 20 scored events**. These minima do not automatically establish an edge. Secondary comparisons remain descriptive and unadjusted for multiple testing.

Episode reopens and neighboring strikes can share the same underlying regime. Counts of entries, unique events, dependence blocks, raw prefix records and compact frames are all reported separately.

### Supplementary temporal feature registration

Temporal analyzers have structured diagnostics rather than scalar `raw_score`s. Their regression transforms received a separate immutable hypothesis, **`hypothesis-e4af07d68763`**, effective only from raw **22,081,092** and frame **68,072,230**, registered at **21:59:36.095 UTC**.

Lead/lag uses the unweighted mean of existing finite strengths across available scales. Event sync uses the mean of existing forward-minus-reverse conditional event frequencies for windows with positive triggers in both legs. Full diagnostics remain frozen. These are continuous predictors, with no new alarm cutoff or trading direction. Earlier entries do not receive these features retroactively. Arbitrary later hypotheses can be registered with future IDs; registration alone does not implement their scorer.

## Closed markets, settlement and calibration

Equity regimes use the existing XNYS calendar, including holidays, DST and half-days. Deferred entries freeze the last actually OPEN target estimate and Poly at close, rather than allow an aging closed-market model value to masquerade as repricing. They retain latent GAP and PM change since close. Closed-regime controls use the same frozen-target semantics.

Next-open, +1m, +5m and +30m outcomes require a post-open target receipt and reference timestamp. The first available Yahoo proxy within five minutes is separately recorded; it is **not an exchange auction print**. A missing target baseline remains an explicit ineligible deferred candidate. It is not discarded as ordinary stale data.

Official resolution collection uses [Polymarket's resolution-state API](https://docs.polymarket.com/api-reference/markets/get-resolution-state), with Gamma condition IDs and exact provider response snapshots persisted through the existing single research writer. Binary payout, settlement wording/version, dispute/review fields, source timestamps and receipt provenance are retained. Unknown, pending, fractional/void and conflicting results are not converted to NO. A provider last-update time is labeled as such when the exact settlement time is unavailable. Observed reference-path crossings are separate `INDEPENDENT_PATH_HIT` evidence and never overwrite official results.

Calibration uses the **first eligible forecast per unique proposition**, merges sparse adjacent probability bins, retains Brier contributions and excludes wording/version conflicts or forecasts after known settlement. Options probabilities remain risk-neutral/model proxies. Equity residual strata cover event type, direction, threshold distance, expiry and IV; empirical bands require 30 resolved events in a stratum. Lack of data is `CALIBRATION INSUFFICIENT`.

Repricing results separately expose assets and CRYPTO / EQUITY_PROXY classes. Curve comparisons use common semantic nodes, surface movement toward the opening counterpart, crossings, monotonicity and curvature; one opening per family/half-hour is counted in the descriptive curve summary. Strikes are not treated as independent discoveries.

## Observed production evidence

At **22:25:58 UTC**, after **76.9 minutes** from the immutable registration:

| Measurement | Actual evidence |
| --- | --- |
| Compact frames consumed | **197,529** |
| Source records in consumed global prefix | **226,281** |
| Frozen first forecasts | **22 unique events** |
| Scheduled control moments | **55** |
| New eligible gap episodes / scored primary outcomes | **0 / 0** |
| Deferred equity candidates | **18**, all `MISSING_LAST_OPEN_TARGET_BASELINE` |
| Officially resolved prospective forecast events | **0** |
| Scientific version conflicts | **0** |
| Evaluation sidecar main-file size | **1,785,856 bytes**, including historical smoke campaigns |

Valid crypto gaps were below the frozen 4 pp trigger at registration. There were no eligible pre-boundary open gaps. Large unavailable conventional estimates, including CUTOFF rows, are not fabricated into episodes. **The threshold has not been lowered to obtain results.** The 18 deferred candidates retain the inability to establish a usable target-close baseline; no live next-open response has yet been observed.

The original frozen database remains **180,779,655,168 bytes**, with original mtime_ns **1790876555742511937**. It was not rewritten, vacuumed or truncated. Paper run IDs and wallets were preserved. Exactly one supervised collector was verified after each activation. Two graceful child restarts were performed; measured restore times were **8.415s** and **8.191s**. The same campaign and supplementary hypothesis survived both.

An isolated sidecar preview read **80,953 production compact frames in 18.94s**, without reducer/config conflicts, before enabling production evaluation writes. A real historical smoke request exposed a time-only legacy scan exceeding the five-second budget. The evaluation reader now locates the first frame using indexed primary-key probes of the nondecreasing receipt-clamped frame clock. It successfully evaluated **7,747 recent v2 frames** and **18,681 frames across the v1/v2 cutover**, with no version conflicts. Their campaign mode is irreversibly **EXPLORATORY / HISTORICAL**; right-censored horizon data stays `MISSING`.

Six targeted regressions cover immutable registration/REPLACE, frozen openings and future analyzer exclusion, causal horizon/clock/range selection, deferred holidays/DST and post-open receipt, unique-event/official calibration, and deterministic analyzer overlap. The full **83-test** suite passes. The official API was read against a known historical resolved condition; **no new prospective official resolution or live next-open fill is claimed**.

## Operation

```sh
./basis evaluate
./basis evaluate --json
./basis evaluate --prospective --episode '<evaluation-entry-id>'
./basis evaluate --historical --from 2026-10-01T21:05:00Z --to 2026-10-01T21:08:00Z --json
./basis evaluate --register-hypothesis hypothesis.json
```

Reports read the evaluation sidecar; they do not replay millions of raw records. The independent service worker advances its transactionally committed cursor continuously and resumes after restart. `/api/state` exposes evaluator RUNNING/ERROR independently of collector health; `/api/evaluation` exposes the report. Evaluation failure does not claim a recorder disconnection. Historical requests require an explicit range of at most six hours, process at most 50,000 frames per invocation, and can be resumed with the identical command. No UI redesign or P&L optimization accompanies this pass.

Until new eligible episodes, resolved propositions and dependence blocks accumulate, the appropriate conclusion is **insufficient evidence**, not “edge” or “no edge.” Current equity coverage additionally prevents a meaningful deferred-response/calibration result. Source outages, mapping/model limitations, controls without matches and missing horizons remain visible in the accumulating record.
