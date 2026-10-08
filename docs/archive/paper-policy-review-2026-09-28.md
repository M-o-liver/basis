# Paper policy review — September 28, 2026

The initial review covered 61 closed positions in `diagnostic-directional-1.0.0`. OLIVER had no trades and retained $100,000. All figures below are local simulated USD. The audited prefix is saved in `data/changes/2026-09-28-cost-aware-policy/baseline-review.json`, with fill ledger IDs and complete per-close attribution.

| Wallet | Closes | Net P&L | Midpoint movement | Spread | Fees | Slippage |
|---|---:|---:|---:|---:|---:|---:|
| SW | 12 | -1,202.29 | 620.18 | 1,384.98 | 395.85 | 41.65 |
| COVARIANCE | 12 | -1,202.29 | 620.18 | 1,384.98 | 395.85 | 41.65 |
| EVENT_SYNC | 9 | -508.19 | 949.08 | 1,279.03 | 148.42 | 29.82 |
| LEAD_LAG | 7 | -420.60 | 775.93 | 966.81 | 205.56 | 24.16 |
| TOPOLOGY | 10 | -1,404.45 | 41.97 | 1,081.17 | 332.15 | 33.09 |
| ORDINAL_MMD | 11 | -1,822.91 | -170.97 | 1,219.37 | 396.26 | 36.31 |
| Total | 61 | -6,560.73 | 2,836.39 | 7,316.34 | 1,874.08 | 206.69 |

Every close reconciles to midpoint movement minus quoted half-spreads on both legs, fees and slippage. Positive midpoint movement is not a tradable return or proof of information advantage. Shared exposures and this small sample preclude treating these as independent observations.

SW and COVARIANCE shared all twelve entries. Their different anomaly detectors fed the same generic directional wrapper. Six positions lasted over two hours, contributing -$3,282.83; the equity tape contains a 37,189,608 ms (10.33 hour) recording gap. Four of those positions were copies of the same ETH exposure. The observed gap does not establish whether the cause was suspend, networking, or a stalled process. Offline stop losses cannot be promised or retrospectively filled.

Version 2 changes entry construction and oversight: a 5% ceiling on estimated round-trip drag, lowest-cost affordable nearby contract selection, fresh PM-side movement, neighboring-strike agreement, 0.5% entry budgets, thirty-minute cooldowns, and causal closed-trade loss feedback. It keeps the existing pessimistic execution costs. Stale queued orders expire; a clock gap triggers a recorded recovery hold. Profitability is unproven and must be evaluated prospectively in the new version.

New code does not erase losses: old positions are drained before rollover, then cash is carried into a separate versioned run. The table retains account returns and costs; the chart can select old runs. Original ledger records remain immutable. A consistent pre-change paper database and code archive are in the same `data/changes/2026-09-28-cost-aware-policy/` directory. Do not restore that database over subsequent live fills; it is a recovery/reference artifact, not a means of removing losses.

Ongoing review runs inside the paper service after each new fill, with persisted inputs and review version. It attributes costs, flags shared entries, and supplies a fixed risk feedback rule. It does not autonomously fit a winner to this sample, rewrite code, or claim a validated edge. Eight targeted checks cover accounting, future-data exclusion, version separation, capital/position preservation, delayed orders, cost changes during latency, contract selection and entry evidence. A copy of the actual ledger was also migrated without altering any balance or position.
