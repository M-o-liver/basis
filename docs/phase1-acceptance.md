# Phase 1 acceptance record

Storage update on 2026-10-01: [measured storage profile, exact replay and preserving v2 cutover](storage-profile-2026-10-01.md). This retains old failures and does not pass future duration gates.

Operational update on 2026-10-01: [dated operational acceptance evidence](operational-acceptance-2026-10-01.md)
adds bounded chronological verification, persisted continuity/source accounting,
recovery exercises and explicit 24h/72h/7d gates. The earlier findings below remain
unchanged. Historical unexplained recording gaps still fail elapsed historical
gates; the newly instrumented campaign has not reached its duration gates.

Update on 2026-09-28: extended research acceptance is still open. At the operator's
subsequent request, experimental automated paper policies are now enabled, with
versioned underlying theses, cost filters and loss feedback. Stock touch/terminal
acquisition is implemented, and 55 automated tests pass. Sequential martingale
evidence remains disabled pending calibration. The dated evidence below records
the earlier checkpoint; statements about disabled automation or absent stock
targets describe that earlier state. See README and the September 28 paper-policy
review for the current implementation and limitations.

Status on 2026-09-27: **research acceptance remains open.** The operator subsequently requested building the manual paper layer now. Manual experimental execution is available; automated analyzer policies remain disabled. This does not retroactively declare Phase 1 accepted.

The first live run recorded 911,110 source records, 3,964,119 observations, 52,460 episode revisions and 163,800 analyzer outputs from 2026-09-26 18:49:33.466 UTC to 2026-09-27 01:39:31.755 UTC: about 6 hours 49 minutes. The process was no longer listening when inspected on September 27. Its application log contained no shutdown error; the reason is unconfirmed. This does **not** pass unattended stability acceptance.

An initial captured prefix reproduced 36,582 observations from 7,889 raw records with zero mismatches and no missing code version. This does not mean all 3.96 million observations have been verified.

The replacement adds compressed observations, bounded compact rolling history, native subsecond timing, hashed restart checkpoints, source-age checks, and quarantined-packet replay. Existing raw tape and historical outputs remain intact.

| Gate | Evidence / remaining work |
| --- | --- |
| Acquisition | Public PM, Deribit and Coinbase feeds produced the recorded run. Catalog was explicitly PARTIAL with two failed requests. Yahoo had no discovered equity target. |
| Semantics | Rules, assumptions, versions and venue differences inspectable; automatic mappings UNVERIFIED. Exact settlement equivalence not claimed. |
| Provenance | Observations link raw sources and retain selected option inputs, timestamps and model/config/code versions. |
| Persistence | SQLite WAL, synchronous transactions, hashes and immutable records. About 15 GB from the original uncompressed-observation run remains preserved. |
| Replay | Stored-row replay and archived-code verification implemented; initial live prefix passed. |
| Episodes | Open/close thresholds, peak/travel/duration and reversal/outage/expiry reasons tested. |
| Dynamics | Seven horizons, velocity/acceleration and descriptive travel tested. |
| Curves | Semantic grouping, monotonicity, extrema and clusters tested. |
| Analyzers | Seven registered with versioned output; insufficient-data reasons explicit; sequential evidence disabled pending statistical calibration. |
| Analyzer persistence | Append-only diagnostics include windows, parameters, versions and explanations. |
| Tests | 39 passing automated tests at this checkpoint, including synthetic shifts, motifs, clusters and directional lag. |
| Lookahead | Prefix replay, as-of windows and delayed-packet tests pass. Receipt time governs availability; latency limits interpretation. |
| TUI / health | Monochrome web and native curses interfaces; final interaction/restart checks pending. |
| Unattended operation | **Not passed.** Validate the revised collector through an extended run and deliberate restart; inspect memory, disk growth, stale intervals, quarantine and replay. |

The revised collector passed 41 research tests. A raw-integrity scan verified 911,246 payload hashes and SQLite quick_check returned ok. Graceful checkpoint restart took 1.5 seconds; a forced collector exit recovered under the userland supervisor in 6 seconds with no quarantined records. Extended unattended acceptance remains open. Manual paper functionality is documented in README; automatic strategy policies remain disabled.
