# BASIS

A dense, monochrome market table for one question: when Polymarket probability P differs from option-implied probability Q, which side subsequently moves, and what actual option structure expresses the difference after costs?

**Polymarket is the reference signal.** Conventional-market exposure is cheap when P > Q and rich when P < Q. Options probabilities are risk-neutral/model estimates, not necessarily physical beliefs.

The browser contains only **Markets** and one selected-event math pane. It shows actual calls/puts, GAP, relative gap, log-odds/odds ratio, concrete defined-risk legs, costs, conditional PM-based EV, historical event/day sample counts and one small PM/OPT/gap chart. No analyzer screens, general tickets, automatic policies or wallet competition.

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./basis serve
```

Open http://127.0.0.1:8765. Filter with `/`, select with j/k or click, close details with Esc, refresh with R. Default order is currently executable positive modeled EV. Yahoo remains delayed/unknown-delay research data; closed stock markets block paper entry.

BASIS SIM starts once with $100,000 in `data/basis.sim.sqlite3`. The operator buys complete displayed structures, at actual ask/bid plus explicit fees/slippage/impact and one-second latency. No real orders. Historical wallets and the prospective sidecar are preserved, excluded from the live product.

## The math

GAP pp = 100(P-Q). REL = abs(P-Q)/Q. LOG ODDS GAP = logit(P)-logit(Q). ODDS× = exp(LOG ODDS GAP). Exact endpoints use half the assumed .001 quote resolution; interior tail values remain raw.

Same-expiry terminal events use call/put debit verticals bracketing the threshold. Q_EXEC is debit / maximum payout. Binary-equivalent EV is P(exposure) × max payout − debit; this is an approximation to the vertical ramp, and binary-equivalent Kelly is not actual multi-leg Kelly sizing. Negative gaps select opposite event exposure.

Touch and later-expiry events use actual option payoffs under the existing flat-IV/zero-carry GBM, with continuous barriers and importance sampling. PM changes the event mixing weight: P* = P Q(.|H) + (1-P) Q(.|not H). Conditional means are cached; EV(P) is a linear substitution. Q-only price, PM increment, numerical uncertainty and calibration against the quote band are visible. Positive model residual alone is not a PM edge.

US equities use multiplier 100. Deribit inverse premiums are converted from their native coin units at observed spot, with multiplier 1; the paper account is a USD cash-payoff proxy. [Deribit contract/settlement conventions](https://support.deribit.com/hc/en-us/articles/29734325712413-Settlement) differ from brokerage-quality USD fills. Yahoo American options, zero dividends, overnight paths and later expiry are explicit proxies. probability-1.3.0 extends the equity IV-tenor mapping to seven days after cutoff; crypto remains at 72 hours.

## Historical research

```sh
./basis gaps
./basis gaps --json --per-day 2 --output data/gap-response.json
./basis gaps --from 2026-10-01T00:00:00Z --to 2026-10-08T00:00:00Z
./basis math <event_id>
```

`gaps` reads the tape, including small continuous discrepancies, using bounded deterministic chronology windows. The JSON includes seven horizons, error-correction regressions, spot attribution, class/regime strata, native jump studies, gap/odds bins, censored half-lives and quote-based historical trade math. N means event/day blocks, not frames. It writes a derived exploratory report, never rewrites the source tape. The [initial numerical results and limitations](docs/gap-response-2026-10-08.md) show no established global OPT-following edge and negative sampled trade returns after costs. Increase `--per-day` deliberately for wider coverage; the default is not a full dataset replay.

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
