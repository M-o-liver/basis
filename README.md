# BASIS

A local, monochrome research terminal comparing explicit Polymarket propositions with probabilities reconstructed from conventional markets. **Polymarket is the reference signal; CHEAP/RICH describes conventional exposure.** Includes isolated paper wallets. Never sends real orders.

```
GAP pp = 100 × (PM − OPT)
REL    = abs(PM − OPT) / OPT       undefined when OPT = 0
GAP > 0: conventional exposure CHEAP relative to PM
GAP < 0: conventional exposure RICH relative to PM
```

REL is disagreement, not Kelly sizing. Options transforms are risk-neutral/model estimates, not established physical probabilities. Large REL does not establish an edge.

## Run

The repository contains the application, tests and documentation. Research tapes,
paper-wallet databases, logs, source-version archives and the local virtual
environment stay outside Git. A fresh clone starts a new dataset and new paper
wallets; the included policy review documents simulated results, not real trades.

Python 3.11+ with numpy, websockets, and the optional equity adapter yfinance:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./basis serve
```

Open **http://127.0.0.1:8765/** or run `./basis watch` in another terminal. The server collects without a browser or TUI open. One process may own a tape; an advisory lock prevents duplicate collectors. `server.py` remains a compatibility launcher. The server binds to loopback. Public source calls require no credentials.

```sh
./basis status
./basis doctor
./basis replay ETH-2300
./basis replay <event_id> --from 2026-09-26T19:00:00Z --to 2026-09-26T19:05:00Z
./basis replay <gap_event_id>
./basis replay --verify --to 2026-09-26T19:00:00Z
./basis replay <event_id> --json --limit 10000
./basis raw <raw_id>
./basis episodes
./basis algos
./basis link mapping.json
./basis --db data/experiment.sqlite3 serve --port 8766 --config config.json
```

An asset/strike selector must be unique; otherwise use its event ID. Global `--db` and `--url` options precede the command. Verification captures a stable prefix and exits nonzero for mismatches, unavailable code versions, or an empty selection. Full verification is expensive on large tapes; ordinary replay reads saved observations directly.

For a background run in an ordinary user shell:

```sh
mkdir -p data
nohup ./basis supervise >>data/collector.log 2>&1 &
echo $! >data/collector.pid
```

Stop that process with SIGTERM; shutdown commits a checkpoint. The userland supervisor restarts an exited collector with bounded backoff and logs child exit status. It cannot restart itself after a machine reboot or diagnose every hung process. No system service or machine configuration is installed. Check `./basis doctor` after restart. Extended unattended validation remains an acceptance gate.

## Controls

Web views: **1 Monitor, 2 Markets, 3 Tape, 4 Gaps, 5 Algos, 6 Curves, 7 Health**. `/` or Ctrl-K filters; j/k or arrows select; Enter opens details; Escape returns; `[`/`]` page; R refreshes discovery; L edits a mapping; P pauses display; `?` shows help. Pausing never pauses collection.

The primary table keeps PM, OPT, GAP, REL, SIDE, TREND, BASIS, and STATE. Absolute gap is the default sort. `>`/`>>` mean opening/opening fast; `<`/`<<` mean closing/closing fast. Details expose numeric horizons, acceleration, travel, model inputs, raw source links, and resolution wording. The display cutoff only highlights rows; episode thresholds live in collection configuration.

Native `./basis watch` has monitor, episodes, algos, and health on keys 1–4. Enter opens JSON; j/k and Page Up/Down scroll; Escape returns; q exits. A wide terminal is useful for the full monitor line.

## Architecture

```
public adapters → immutable raw journal → deterministic reducer
                                         ├─ normalized observations
                                         ├─ rolling features / gap episodes
                                         ├─ comparable-strike curves
                                         └─ versioned analyzers
                                               ↓
                               SQLite / replay / CLI / thin web terminal
```

`collector.py` owns asynchronous feeds and bounded HTTP workers; `semantics.py` mapping; `pricing.py` probability conversion; `engine.py` receipt-ordered reduction; `features.py` dynamics/episodes/curves; `algos.py` diagnostics. The browser formats results and does no pricing.

`data/basis.sqlite3` uses WAL, full synchronous commits, transactions linking raw and derived records, and UPDATE/DELETE rejection triggers:

- `raw`: receipt wall/monotonic times, source time when supplied, source/kind/subject, compressed original parsed payload, SHA-256, session.
- `observations`: event semantics, PM/OPT/spot timestamps, selected option inputs, gap/REL, confidence, quality flags, rolling features, versions, raw references.
- `episodes`: immutable lifecycle revisions, thresholds, peak, duration, travel, close reason. Outage closure is `DATA_GAP`, never convergence.
- `analyzers`: time, scope, input window, parameters, versions, score, explanatory features, diagnostics, disabled/insufficient-data reasons.
- `checkpoints`: hashed compressed restart state, bound to the exact source revision. Raw history remains authoritative.

Each session archives Python source in `data/versions/<content-hash>/`. Verification uses the archived reducer and recorded parameters, rather than silently applying today's code to old results. Keep this directory with the database. Ordinary replay does not execute archives; `--verify` loads only archives matching their content hash. These locally generated archives are trusted project code, not an import format for unknown code.

Receipt order determines information availability. Delayed/future packets remain raw but cannot rewind live state. As-of grids use only already-available observations. Outages break contiguous analysis windows. Restart reads a matching checkpoint plus subsequent raw records, or rebuilds from raw after a code change. Large-tape startup can take time. Replay does not rewrite history.

New observations are compressed. Rolling memory keeps one compact causal anchor per second, for at most two hours. All source updates remain on disk. The collector stops explicitly at its free-space reserve. There is no automatic deletion. Monitor disk growth and archive intentionally. For online backups use SQLite's backup API; copying only the main file while WAL is active is insufficient.

## Sources and mapping limits

- Polymarket Gamma: discovery, resolution wording, token IDs, marked indicative quotes. CLOB WebSocket: books and best bid/ask. Crossed books stay invalid; stale/absent books may use separately marked Gamma indications.
- Deribit: BTC/ETH option summaries, bid/ask/mark, IV, forward, expiries. Coin premiums are normalized with the reported underlying price; original values remain raw.
- Coinbase: independent BTC/ETH spot and daily reference-path candles. Touch windows require complete UTC coverage.
- Yahoo/yfinance: discovered stock touch and terminal events, calls and puts, source-timestamped spot, regular-session highs/lows, earnings dates and session returns. Source delay and American-option/zero-dividend/overnight-jump model limits remain explicit; this is not an exchange-quality equity feed. US session coverage and expiry closes use the XNYS calendar, including holidays, early closes and DST.

Discovery is bounded: default 60 monitored events, at most 20 per asset, plus operator mappings. Unsupported propositions remain in Markets with a reason. Partial discovery and failed requests are recorded. This is not exhaustive Polymarket coverage.

Automatic mappings are **UNVERIFIED**. Type, threshold, direction, cutoff, reference venue, inclusivity, path window and original rules are inspectable. L appends a revision; MISMATCH blocks calculation. Generic raw/event records support future classes, but only supported price propositions produce OPT estimates today.

Exact-expiry terminal events use finite call-spread slopes where valid brackets exist. Other supported price events use a disclosed local-IV, zero-carry model. Touch estimates require path history; an observed barrier crossing produces VERIFY_HIT for resolution review. Venue differences, expiry interpolation, sparse/asymmetric strikes, wide books, unknown timestamps and tail sensitivity remain visible. These are model proxies, not claims of identical settlement economics.

## Diagnostics

Gap changes use 1, 5, 15, 30, 60, 300 and 900 seconds when supported. Finite differences expose velocity and acceleration. PM TRAVEL and OPT TRAVEL describe movement, not causal leadership. Native jumps preserve subsecond receipt order separately from the analysis grid.

Curve groups require matching asset, proposition, direction, cutoff, path window, settlement source and threshold inclusivity. Outputs include nodes, monotonicity violations, gap extrema and neighboring clusters. Touch curves are not terminal CDFs.

| Family | Analyzer | Output and limits |
| --- | --- | --- |
| Temporal | `lead_lag_multiscale` | Lagged increments, source-cadence checks, moving-block uncertainty and spot residuals; exploratory association, not causality. |
| Temporal | `event_sync` | Native receipt-time coincidences and adaptive lag windows; equal-time events excluded; follow-up censoring explicit. |
| Distribution | `sliced_wasserstein` | Baseline-standardized empirical projection distances with contributing weights. |
| Distribution | `ordinal_mmd` | Delta-kernel MMD² over ordinal gap motifs, explicit ties; dependent motifs get no fictitious p-value. |
| Geometry | `covariance_manifold` | Regularized SPD covariance distance and changed correlations. |
| Geometry | `topology` | H0 components across correlation-distance scales; needs three comparable strikes; no higher-homology claim. |
| Evidence | `sequential_martingale` | **Disabled:** no conditional null or calibrated component e-processes. Correlated alarms are never multiplied. |

Distribution defaults need two 64-sample windows at a 15-second grid, about 32 minutes of fresh continuous history. Longer lags use more history. Sub-cadence scales remain insufficient. Every analyzer explains missing requirements. Outputs are research diagnostics, not independent votes or trade orders.

`basislab/config.py` defines validated thresholds, collection rates, age limits, capacity and analysis windows. `serve --config file.json` overrides fields; parameter hashes are persisted.

## Validation and phase gate

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
node --check app.js
./basis doctor
./basis replay --verify --to <explicit-UTC-time>
```

Tests cover conversions, sign/REL, finite values, semantics, timestamps, provenance, stale/crossed/delayed sources, malformed packets, path history, immutable records, hashes, causal replay, disk reopen/checkpoints, memory bounds, travel, lifecycle, curves and synthetic analyzer behavior.

**Manual paper execution is available as an experiment, at the operator's request.** Research acceptance remains open; versioned experimental analyzer policies are enabled. See [the acceptance record](docs/phase1-acceptance.md). Outcome ingestion/calibration, exact settlement feeds and more sophisticated probability surfaces remain research work; convergence is not a substitute.

## Paper wallets

Press **8 Wallets**, **9 Positions**, **0 Ledger**, or **T Paper ticket**. OLIVER and seven reserved analyzer wallets each start at $100,000. Only OLIVER accepts manual orders. Analyzer wallets run separate versioned experimental policies; analyzers themselves do not place orders.

```sh
./basis wallets
./basis instruments ETH
./basis buy SPOT:ETH 1 --reason 'Manual directional experiment' --preview
./basis buy SPOT:ETH 1 --reason 'Manual directional experiment'
./basis positions
./basis close SPOT:ETH --reason 'End experiment'
./basis history
./basis wallet reset OLIVER
./basis wallet reset all
./basis history --run-id 1
./basis settle <expired-option> <spot-raw-id> --reason 'Document this settlement proxy'
./basis paper-replay orders.json --output data/replay-experiment.paper.sqlite3
```

Paper data lives separately in `data/basis.paper.sqlite3`. Runs and ledger entries are immutable. Reset creates a new numbered run and cancels pending orders; earlier positions and performance remain archived. Every fill records decision/submit/fill time, instrument, multiplier, quote reference, raw signal reference, bid/ask, fees, slippage, cash and position before/after.

Execution uses ask for buys and bid for sells, then configured fees, adverse slippage, estimated size impact and at least one second of latency. Quotes are checked again at fill time. Defaults are 10 bps fees, 10 bps slippage, 5 additional impact bps per $10,000, plus $0.65 per option contract. Missing spot spreads are estimated at 20 bps per side; options require both bid and ask. These are explicit conservative assumptions, not venue fee schedules or depth measurements.

Supported expressions are unlevered long spot and fully paid long calls and puts; sales cannot exceed holdings. Listed equity calls use a 100-share multiplier, Deribit calls one underlying unit with premiums normalized to USD. **Options use USD cash-equivalent accounting, not physical exercise, assignment, or coin-collateral accounting.** Expired positions require explicit settlement against a recorded matching spot observation within 60 seconds of expiry. The ledger labels that choice as a proxy. Stale/expired marks block added risk. There are no real trades, short options or free leverage.

Risk defaults: 10% of equity per new trade, 25% per position, 100% gross exposure, 10 positions, 10,000 units/contracts, 10% daily loss and 25% drawdown. Risk is rechecked at execution. Bankrupt runs cannot place orders. Liquidation marks use bid; stale marks are visibly flagged.

Replay accepts a JSON array with `timestamp` (explicit UTC), `instrument`, `side`, `quantity`, and `reason`. It uses the same execution engine and creates a separate database/report. Decisions precede packets with identical millisecond timestamps; unavailable future quotes cannot be used. This replays an explicit order plan, not an implemented automatic trading policy.

References: [Polymarket realtime data](https://docs.polymarket.com/market-data/realtime-data), [Deribit summaries](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency), [Coinbase WebSocket](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-endpoints), [kernel two-sample testing](https://www.jmlr.org/papers/v13/gretton12a.html).


## Automatic paper policies and wallet curves

The analyzer wallets use versioned directional policies. Version 2 requires its own fresh diagnostic, at least 0.75 pp of disagreement, probabilities between 2% and 98%, fresh PM movement in the gap direction, and two neighboring strikes agreeing. PM must account for at least 60% of the leg movement over 30–60 seconds, and the gap must exceed the PM book width. These are descriptive filters, not causal or profitability claims. They buy a near-ATM call for bullish disagreement or a put for bearish disagreement, keeping Polymarket as the reference. This remains a directional proxy, not exact event replication.

Entries use at most 0.5% of current equity, two positions, and a thirty-minute cooldown. Within the supported nearby contracts the policy selects the lowest modeled round-trip cost that fits the cash budget. Quoted spread, both commissions and both slippage estimates must total no more than 5% of entry cost. The execution engine checks that cost cap again after latency. Execution costs were not reduced to improve reported P&L.

SW, covariance, topology and ordinal MMD have separate declared score thresholds. Lead-lag requires a PM-leading scale; event synchronization requires at least three qualifying PM triggers and stronger PM-to-OPT coincidence than the reverse. MARTINGALE is armed but stays in cash while the sequential analyzer is disabled; correlated alarms never substitute for a calibrated e-process. Inspect wallet details for exact parameters and its current waiting/holding/blocked reason.

Exits: gap below 0.2 pp, directional reversal, 20% premium loss, 30% premium gain, one hour held, or approaching expiry. Orders expire after thirty seconds; an execution-clock gap over ninety seconds holds new entries for two minutes while fresh quotes are acquired. The program cannot execute while the machine or feeds are offline and never backdates imaginary exits. Every order records its diagnostic, input observation, version, costs and reason.

Automatic outcome feedback uses only already-closed trades in the current policy run. Three consecutive losses, or a negative total over the last five closes, blocks entries for one hour from the last close; subsequent probes use half size until that loss condition clears. Exits continue during the entry brake. This is a declared risk rule, not an optimizer trained on future outcomes. The parameters and feedback version are recorded with each policy.

Policy upgrades first drain any prior-version positions using available executable quotes, then atomically start a separate run carrying the remaining cash. Account returns, costs and fill counts retain the old results. No automatic upgrade refills a wallet. Explicit `wallet reset` still starts a fresh $100,000 experiment while retaining its history.

**8 Wallets** includes a monochrome equity chart, USD/return toggle, wallet filter and time range. Select a wallet to choose historical runs. Chart returns are per run; the table shows account returns including carried losses. Hover or focus the chart and use left/right arrows for recorded values. Equity is marked at bid every 30 seconds; stale marks are flagged. Curves never join different runs.

`./basis review` and `/api/review` reconcile actual closed trades as midpoint movement minus bid/ask spread, fees and slippage. This is attribution, not executable midpoint profit. Wallet details show the same breakdown, and the report flags shared trades and long recording gaps. Reviews are append-only, versioned and refreshed automatically after new fills; they expose when multiple analyzers are expressing the same trade. The initial diagnosis and change record are in `docs/paper-policy-review-2026-09-28.md`.

Use the Wallets pause/resume button or `./basis automation pause` / `./basis automation resume`. Pausing cancels pending algo orders and suspends new automated entries/exits; held positions remain visible and marked. Research collection and OLIVER's manual wallet continue independently.


## Underlying thesis and stocks

Monitor's asset selector switches between All assets, Crypto and Stocks. Select an event and open Trade thesis for its proposed direction, rejection reasons, 5m/15m spot moves, current IV, trailing realized volatility, threshold distance and time to expiry. Stock records also retain company name, exchange, session state, earnings calendar and daily returns. The same structured output is available at `/api/theses` and `./basis thesis EVENT_ID`.

An analyzer only nominates a candidate. A separate versioned thesis rejects missing context, countertrend expressions, expensive long volatility, imminent cutoffs and unmodeled equity catalyst risk. The instrument check records one-hour up/flat/down spot scenarios, repriced with constant IV and full execution costs; a favorable one-sigma move must at least cover cost and decay. These are model scenarios, not probabilities or expected returns. It does not claim to be a news-reading or fundamental-research AI. Every resulting order retains its thesis and original observation; periodic context theses are persisted separately.

The stock adapter previously excluded touch events, which left every selected stock unfetched. The parser also failed to recognize `(LOW)` as downside. Both paths are fixed. Regular-session history validates every expected session and flags corporate actions or previously crossed barriers; weekends and exchange holidays are not missing ticks. Calls and puts retain their own actual quotes. Market-closed values are marked CLOSED and are not executable. A timestamped stock price does not establish a timestamp for the option book; Yahoo delay remains unknown and any such fills remain estimated.

The one-time checkpoint migration is bound to exact old/new reducer digests. It preserves unchanged crypto state, journals stock reclassification from its original raw catalog, and discards old stock rolling/surface state before recollection. It never rewrites historical observations. Any other reducer change still requires replay.

Adapter references: [yfinance option-chain implementation](https://github.com/ranaroussi/yfinance/blob/main/yfinance/ticker.py) and [exchange_calendars session API](https://github.com/gerrymanoim/exchange_calendars/blob/master/README.md).
