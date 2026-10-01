# Operational acceptance — 2026-10-01

Status: **PARTIAL**. Measurement and recovery tooling is operational. Extended
unattended acceptance remains open. This record supplements, and does not replace,
the September 27 findings in [phase1-acceptance.md](phase1-acceptance.md).

## Run and provenance

The instrumented production campaign began **2026-10-01 11:40:27.130 UTC**.
Its start is persisted once and survives subsequent restarts. The final collector
bundle is **`2391f9029e07e3b8c7fc`**, calculation `probability-1.2.1`, feature
`dynamics-1.1.0`. Source archives remain under the production tape's `versions/`.
The code is on `feat/operational-acceptance`, based on the completed interaction
branch (`f8d2ded`). Research reducers, analyzer/strategy thresholds, sizing and
execution assumptions were not changed.

At **12:08:58 UTC**, the bounded report observed 27.81 minutes of timer-covered
campaign recording. The latest child had 85 seconds of uptime following a
controlled instrumentation restart. These are distinct measures.

| Gate | Instrumented campaign | Full historical tape |
| --- | --- | --- |
| 24 hours | **NOT YET ELAPSED** | **FAIL** |
| 72 hours | **NOT YET ELAPSED** | **FAIL** |
| 7 days | **NOT YET ELAPSED** | **NOT YET ELAPSED** |

Future gates must be evaluated from actual recording intervals; this document
does not claim an observed 24-hour, 72-hour or seven-day clean run.

## Objective criteria — operational-v1

- The timer expects five-second cadence. Separate sessions, receipt-clock
  reversals and timer intervals longer than 30 seconds split recording coverage.
  Missing intervals and restart downtime do not count toward elapsed gates.
- An elapsed gate **FAILS** for a replay mismatch, SQLite integrity failure,
  receipt-clock reversal or recording interruption longer than 60 seconds.
  A controlled restart is allowed within that limit. The limit was selected
  before running acceptance, not adjusted to make this dataset pass.
- An elapsed gate is **PARTIAL** when replay regions, reducer versions, integrity
  evidence or required recovery exercises are incomplete. SQLite integrity
  evidence must be no older than 24 hours and exposes its checked raw boundary.
- A gate is **PASS** only with sufficient recorded duration, complete evidence,
  current recording, complete marker projection and no failure above.
- Source ERROR/PARTIAL/STALE states alone do not fail recorder acceptance.
  Recording those failures honestly is part of successful instrumentation.
- Health accounting describes observed health-state coverage, not proof that
  every packet was captured. Recording gaps are excluded; old health claims are
  not carried through those gaps. Unavailable means ERROR, STALE or EMPTY;
  PARTIAL remains separately reported. Other states such as IDLE remain explicit.

The campaign assesses the new instrumentation prospectively. Full historical
gates and every detected interruption remain independently visible; starting the
campaign did not erase or excuse old failures.

## Tape and historical continuity

The first record remains dated **2026-09-26 18:49:33.466 UTC**. The 12:08 report
captured this committed prefix:

| Evidence | Value |
| --- | ---: |
| Raw records | 19,935,850 |
| Derived observations | 63,050,209 |
| Analyzer diagnostics | 1,985,151 |
| Episode revisions | 2,395,084 |
| Checkpoints | 893 |
| Main database | 171,110,883,328 bytes / 159.36 GiB |
| WAL at report | 4,345,298,112 bytes |
| Free disk | 385,768,247,296 bytes / 359.28 GiB |
| Known quarantined packets | **at least 7** |
| Persisted delayed-packet count | 46,019 at raw 19,935,967 |
| Persisted source-clock errors | 0 at that counter boundary |

Counts use dense append-only primary keys captured together. Counter snapshots
and resource samples identify their own raw boundary; they can be slightly newer
than the sampled replay prefix. Quarantine is a lower bound because older
checkpoints retained only a bounded error deque. A precise all-history quarantine
census is not claimed.

Historical timer gaps include the following large interruptions:

| Missing timer interval | Longest gap between committed raw receipts inside it | Cause |
| ---: | ---: | --- |
| 43,830.099 seconds | 43,830.066 seconds | **UNKNOWN** |
| 37,203.092 seconds | 37,187.258 seconds | **UNKNOWN** |
| 19,142.296 seconds | 19,141.570 seconds | **UNKNOWN** |
| 8,752.571 seconds | 8,752.528 seconds | **UNKNOWN** |

The report also retains shorter interruptions and session boundaries. A missing
timer interval and an all-feed recording gap are different measurements: for
example, one 136.084-second timer interval contained a longest raw-receipt gap of
41.639 seconds. No exact cause is inferred from convergence, PID continuity or a
nearby restart alone.

The legacy supervisor log additionally records a child exit code 1 on September
29 at 15:11:09 UTC after a nonfinite-value serialization failure, followed by a
restart at 15:11:10; and a child exit code 0 on September 30 at 14:24:06 followed
by restart at 14:24:07. Those entries prove process events, not the cause of every
earlier recording interruption. The original first unattended stop is still
unconfirmed and has not been removed from acceptance history.

## Sampled replay

Normal verification is bounded to **25 seconds / 70,000 raw records**, with at
most 25,000 raw records of causal warm-up for a region. Deep verification expands
those bounds to **180 seconds / 400,000 records**, with 60,000 per region. The
fixed `chronology-v1` selection samples earliest/latest regions, ordinary middle
windows, session/restart/version boundaries, checkpoints, recorded source
transitions, inferred stale/recovery transitions and known quarantine neighborhoods.
Code-change crossings are split into separate regions.

Each region uses the exact content-addressed archived reducer. A checkpoint must
precede the region and match that reducer or a compatibility/migration rule in
that **historical** code archive. Raw input hashes are checked during warm-up and
comparison. Replay never loads a future checkpoint or silently substitutes the
current reducer. Missing versions and budget-skipped regions remain unverified.

The initial bounded run reproduced **6,939 observations**, 18/23 regions, with
zero mismatches. Its incomplete regions were reported, not treated as matches.
The first deeper run reproduced **19,389 observations** from **256,234 raw
records**, verifying **40/42 regions** in **147.68 seconds**, with **zero
mismatches and zero missing reducer versions**. It included BTC, ETH, AAPL,
AMZN, GOOGL, META, MSFT, NVDA, SPY and TSLA observations. One early boundary needs
more than the permitted causal warm-up; another window crossed a code version.
The latter prompted the version-splitting correction. A subsequent dated result
will distinguish these unverified cases from actual mismatches.

This is sampled historical evidence, not a claim that all 63 million observations
have been reproduced. Full historical verification remains an explicit operator
action. Early warm-up that exceeds these budgets remains unproven.

## Recovery exercises actually performed

| Path | Scope | Observed result |
| --- | --- | --- |
| Graceful supervisor replacement | Production | **PASS**, 4.98 seconds to recording; old child stopped; archived bundle restored; sampled committed prefix hashes unchanged |
| Child SIGKILL under supervisor | Production | **PASS**, 4.19 seconds; exit -9 persisted; new session; exactly one recovered child; sampled committed prefix unchanged; restore 2.61 seconds |
| Final graceful child restart | Production | **PASS**, 7.63 seconds; restore 1.70 seconds; committed tail hash unchanged |
| Duplicate collector | Production and isolated | **PASS**, clear ownership error before store/source startup; no second session; incumbent continued |
| Invalid newest checkpoint | Isolated | **PASS**, retained bad checkpoint, selected older checkpoint and replayed committed tail |
| All checkpoints unavailable | Isolated | **PASS**, raw-only restore reproduced the saved state and preserved observation count |
| Database write failure | Isolated | **PASS**, service exited nonzero, independent journal recorded BASIS_COLLECTOR_FAILURE, committed record remained intact, quick_check ok |
| Disk reserve reached | Isolated | **PASS**, DISK_SAFETY_STOP, nonzero exit, committed records preserved, no deletion; restart with ordinary reserve succeeded |

Failure injection used dedicated ignored databases under
`data/operational-acceptance-20261001/`. No production database was reset, replaced,
truncated or destructively migrated. Production restarts preserved committed
data. Sampled prefix hashes and append-only guards support that conclusion;
a fresh full hash audit of every production record is not claimed.

## Resource and API observations

At the 12:08 report, logical SQLite growth was approximately **1.44 GiB/hour**
over only **60.06 seconds**. CPU averaged **7.46% of one core** over that window.
The child RSS was approximately **591 MiB**; the highest observed process memory
high-water mark across instrumented children was about **855 MiB**. These short
windows do not prove week-long memory stability or a fixed growth forecast.

The full quick_check holds a read snapshot and allows the WAL to grow while
normal recording continues. Main-file growth therefore temporarily pauses.
Resource instrumentation now records SQLite logical page bytes alongside main
file bytes, WAL allocation and free disk. Routine acceptance avoids this full
scan and reports dated integrity evidence. Checkpoint and restore durations,
WAL/RSS high-water measurements and CPU/growth windows are persisted.

Three requests per endpoint were measured against the real large production
database during the integrity scan:

| API/context | Median | Slow observed | Response/context |
| --- | ---: | ---: | --- |
| `/api/state` | 6.9 ms | 7.6 ms | 225 KB, 60 rows |
| `/api/catalog` | 29.8 ms | 30.3 ms | 4.76 MB, 2,425 markets |
| `/api/replay?limit=100&tail=1` | 10.3 ms | 10.8 ms | 304 KB, 100 observations |
| `/api/episodes` | 670.1 ms | 895.9 ms | 444 KB, 500 latest episode rows |
| `/api/algos` | 661.3 ms | 726.2 ms | 958 KB, 1,000 rows |
| `/api/wallets` | 2.5 ms | 3.4 ms | 48 KB, 8 wallets |
| `/api/equity?hours=24` | 15.6 ms | 69.1 ms | 430 KB, retained equity series |
| `/api/history?wallet=OLIVER&limit=100` | 12.8 ms | 1,339.1 ms | 859 bytes, explicit empty OLIVER activity |

Before the scan, episode/analyzer medians were 250/296 ms with cold/contended
responses of 2.87/2.51 seconds. No clearly pathological endpoint was established,
so research queries and caching behavior were not broadly changed.

Historical source accounting at the bounded prefix includes approximately
81.17 hours of catalog PARTIAL and 20.54 minutes of catalog ERROR; Deribit BTC
had 81.16 hours OK, 22.54 minutes ERROR and 14.36 seconds STALE, with its longest
observed unavailable covered interval 892 seconds. Yahoo separately reported OK,
PARTIAL and IDLE coverage, and per-symbol histories remain inspectable. Source
outages and proxy limitations are not reclassified as collector failures.

## Integrity, tests and remaining evidence

The production **full SQLite quick_check is running** at this dated update.
An `ok` result is not yet claimed. Its actual result, duration and checked raw
boundary will be appended to the acceptance journal on completion. Isolated
failure/recovery databases returned `ok`.

Five new regression tests cover stable causal/version-split sampling and missing
reducers, all gate states, controlled versus unknown continuity/source accounting,
invalid-checkpoint fallback, and lock rejection before source/store startup.
The full suite passes **68 tests** (the existing 63 plus five), in under one
second locally. Existing replay, transaction failure, malformed-input, checkpoint
and interaction tests remain intact.

Remaining **PARTIAL** evidence: full production integrity completion, early
historical warm-up beyond replay budgets, precise all-history quarantine totals,
every-packet capture, recovery from an indefinitely hung process or machine reboot,
and sustained resource behavior. The 24h/72h/7d instrumented gates are explicitly
**NOT YET ELAPSED**.

Use `./basis acceptance --json` for the current authoritative measurements.
The append-only `<tape>.acceptance.sqlite3` contains marker hashes/raw IDs, dated
acceptance runs, resource samples, checkpoint/restore timings and recovery
evidence. Raw tape, old checkpoints, old failures and paper history remain intact.

## Validation update — 12:29 UTC

The corrected sampler verified **41/42 regions**, making **20,274 observation
comparisons** after replaying **247,637 raw records**, in **153.76 seconds**.
There were **zero mismatches and zero unavailable reducer versions**. Only the
early boundary at raw **911,111**, using archived reducer `d3202d9d4aa6c32f91c7`,
remains unverified because causal warm-up exceeds the deep budget. Regions can
overlap; comparison totals are not a claim of unique full-tape coverage.
The code-crossing case is now split and verified. The result is persisted at
12:23:22 UTC against raw prefix **19,968,656**.

An additional isolated supervisor exercise held collection at **DISK_SAFETY_STOP**
using a controlled free-space fixture with the ordinary 256 MiB reserve. After
the fixture reported adequate space, the supervisor resumed collection in
**1.61 seconds**, created one new child and committed new timer records. This
exercises actual pause/resume recording, rather than only reopening a service.
Production disk space and production data were not altered for that exercise.

The full production integrity scan is still running at this update. Its result
is not inferred from the successful sampled replay.
