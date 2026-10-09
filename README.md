# BASIS

A dense, monochrome market table for one question: when Polymarket probability P differs from option-implied probability Q, which side subsequently moves, and what actual option structure expresses the difference after costs?

**Polymarket is the reference signal.** Conventional-market exposure is cheap when P > Q and rich when P < Q. Options probabilities are risk-neutral/model estimates, not necessarily physical beliefs.

The browser contains **Markets**, one selected-event math pane, a compact **Starred** section and the **paperMoney experiment journal**. It shows actual calls/puts, gap formation, log-odds/odds ratio, exact defined-risk structures, conditional payoffs, PM information value versus execution drag and historical event/day sample counts.

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./basis serve
```

Open http://127.0.0.1:8765. Filter with `/`, select with j/k or click, close details with Esc, refresh with R and star with S or ☆. Default ordering is prior PM-created gap strength, then information/drag; raw gap and other sorts remain available. Yahoo remains delayed/unknown-delay research data.

A star freezes the displayed signal and exact contracts in `data/basis.stars.sqlite3`. Entry waits for the first complete valid options receipt after STAR+5s, or next XNYS open+10s for a closed stock market. It records actual receipt delay and STAR versus ENTRY gaps. Exact contracts cannot be replaced. The quote deadline is frozen: ten minutes after target for crypto, thirty minutes for delayed stock feeds, capped by expiry. Earlier stars keep their original deadline. Quotes unavailable by that deadline produce MISSED with a reason.

A missing executable structure blocks starring with its actual reason. Zero/missing Yahoo bid/ask excludes the associated placeholder IV from new research calculations; the original provider IV is retained. A MISSED signal opened no tracked entry. Use Retry/current signal to inspect a fresh structure, or × / Remove from list to dismiss it. Removal stops active tracking and appends an immutable removal record; its original and final mark remain queryable.

Each star is an independent observation, with no account balance or capital limits. Entry crosses ask/bid with explicit existing costs; liquidation crosses bid/ask with exit costs. Select a star for its executable RETURN % / 1× P&L graph and STOP to retain its final mark. Closed equities retain a labeled last mark. Expiry uses a causal recorded-spot payoff proxy only when available, otherwise SETTLEMENT MISSING. Historical SIM, wallet and prospective databases remain preserved and outside the live product.

## The math

GAP pp = 100(P-Q). REL = abs(P-Q)/Q. LOG ODDS GAP = logit(P)-logit(Q). ODDS× = exp(LOG ODDS GAP). Exact endpoints use half the assumed .001 quote resolution; interior tail values remain raw.

Terminal and touch events use actual long call/put or vertical payoffs. H is evaluated at event cutoff and X at option expiry, including when expiry is later. Expected PM payoff = P E_Q[X|H] + (1−P) E_Q[X|not H]. A vertical's ramp is evaluated directly; debit / maximum payout remains descriptive, not its EV model.

Conditional paths use the existing flat-IV/zero-carry GBM, continuous barriers and importance sampling. Conditional means are cached; PM-only updates are a linear substitution. Event mass uses the displayed recorded Q while conditional shape remains the model's. PM information value = (P−Q)(E_Q[X|H]−E_Q[X|not H]). Nearby strikes/widths are ranked by positive net information/EV, then information/drag. Q-only price must fit the quote band within numerical uncertainty. Entry drag, estimated exit drag and expiry EV remain separate; a favorable model-pricing residual alone is not a PM edge.

US equities use multiplier 100. Deribit inverse premiums are converted from their native coin units at observed spot, with multiplier 1; tracked results are a USD payoff proxy. Native exchange settlement differs from this representation. Yahoo American options, zero dividends, overnight paths and later expiry remain explicit proxies. The underlying probability-1.3.0 reducer is unchanged.

## Historical research

Selected equity details expose the captured resolution wording and `quote-audit-1.0`: OTM bid/mid/ask IV inversion plus a regular-session touch sensitivity check. It never rewrites the recorded Q or silently replaces the probability reducer. Nights evolve prices but cannot resolve a regular-session barrier. Its numerical error is not model or trading confidence.

The **Audit defined-risk payoffs** button captures an on-demand `payoff-audit-1.0` report. It couples regular-session touch outcomes with later option expiry payoffs, checks each leg against its quoted book, and tests the entire PM bid/ask interval. Fixed OTM bid/mid/ask, session variance and carry scenarios expose fragile EV. Analytic vanilla prices anchor the baseline; a conditional PM mass tilt is an unvalidated research assumption. A sensitivity pass still requires venue, latency and forward validation. Reports retain their capture time until explicitly updated; starred structures keep their original mathematics.

For offline reproduction use `./basis payoff-audit captured-detail.json --max-loss 500 --output data/payoff-audit.json`. An optional `--book captured-gui-book.json` supplies normalized paperMoney GUI quotes for the exact captured contracts. The book contains `asset`, `expiry` (UTC milliseconds), `spot`, `received_ms`, `source_ms` (null when unknown), `quote_status` and `contracts` with `instrument`, `strike`, `option_type`, `bid`, `ask`. Underlying/expiry/contract mismatches and pre-signal receipts are rejected. Neither receipt timestamps nor this model establish synchronous exchange quotes. This CLI is an engineering/research tool; execution remains in the paperMoney GUI.

The [RSI protocol](docs/rsi-protocol.md) separates actual GUI paperMoney fills from stars and exploratory tests. `./basis journal` verifies and reads the append-only local journal in `data/basis.experiment.sqlite3`. `./basis journal --record evidence.json` appends one envelope (`kind`, `data`, `observed_ms`), and `--full --output data/rsi-summary.json` exports a derived summary with every original record. Account baselines survive restart; partial closes use FIFO; unknown costs stay null; later confirmed fees append linked evidence. The journal cannot place orders. Account/fill artifacts remain Git-ignored.

```sh
./basis gaps
./basis gaps --json --per-day 2 --output data/gap-shape.json
./basis gaps --from 2026-10-01T00:00:00Z --to 2026-10-08T00:00:00Z
./basis math <event_id>
```

`gaps` reads all usable gap magnitudes in bounded deterministic chronology windows. The JSON includes eight horizons through 2h, nonlinear gap/log-odds response curves, block uncertainty, prior-only formation, prediction-time versus ex-post spot controls, native impulse studies and right-censored closure/doubling hazards. Historical exact structures are evaluated at fixed 30m/2h/session-close/next-open/recorded-expiry horizons with ordinary entry latency. N means event/day blocks, not frames.

The [new numerical report](docs/gap-shape-2026-10-08.md) finds stronger descriptive response at larger gaps, uncertain incremental fresh-PM information after controls, and negative overall executable structure returns. Sparse favorable strata remain exploratory. `data/gap-shape.json` supplies comparable history to market details; `--per-day` deliberately increases coverage. Derived reports never rewrite the source tape. The [earlier report](docs/gap-response-2026-10-08.md) remains dated evidence.

## Recorder / recovery

```sh
nohup ./basis supervise >>data/collector.log 2>&1 &
echo $! >data/collector.pid
./basis status
./basis acceptance --json
./basis replay <event_id> --from ... --to ...
./basis raw <global_raw_id>
./basis link mapping.json
.venv/bin/python -m unittest discover -s tests -q
```

The userland supervisor handles collector exit, not host reboot. SIGTERM checkpoints and stops safely. Single-writer advisory locks, source timestamps, immutable records, source-aware freshness, quarantine, disk reserve and bounded versioned replay remain. Exact archived reducers preserve historical probability definitions. A named, exact-revision checkpoint transition activates this reset; old tape is never reinterpreted silently.

Storage v2 keeps one-second compact frames, exact source timing/salient events, content references and bounded option-surface delta anchors in immutable closed SQLite segments plus a small active WAL. The frozen legacy monolith stays read-only. Keep the alias `.storage.json`, tape manifest and segments, `versions/`, acceptance journal and existing sidecars together. Never delete WAL files, reset history or run a huge live-monolith integrity scan as routine housekeeping.

[Storage profile](docs/storage-profile-2026-10-01.md) · [Operational evidence](docs/operational-acceptance-2026-10-01.md) · [Acceptance chronology](docs/phase1-acceptance.md). Older interaction/policy/evaluation findings are dated historical records in `docs/archive/`; they do not describe the current product.

Raw/paper databases, logs, runtime archives and generated research reports remain ignored by Git. A source clone contains no production tape or credentials.
