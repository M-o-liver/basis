# BASIS storage profile — 2026-10-01

Branch: `feat/compact-segmented-tape`, based on operational acceptance PR #2 (`7071fc9`).

## Before: read-only production measurements

Production: `data/basis.sqlite3`. No rewrite, VACUUM, deletion, or schema change. Native SQLite `dbstat` aggregate accounting, one short-lived read transaction per B-tree; queries took 0.004–162 seconds, not one hour-long integrity transaction. The recorder was already stalled before profiling. Page accounting is a sequence of live snapshots; counts capture raw prefix **20,842,853**, observations **65,736,826**, episode revisions **2,667,614**, analyzers **2,053,611**, checkpoints **926**.

| Object | Pages (4096 bytes) | Allocated bytes | Payload bytes | Unused bytes |
|---|---:|---:|---:|---:|
| raw | 3,153,278 | 12,915,826,688 | 12,248,934,052 | 453,050,759 |
| raw_time | 117,760 | 482,344,960 | 358,328,350 | 60,074,935 |
| raw_kind | 133,252 | 545,800,192 | 419,295,976 | 62,376,637 |
| observations | 35,629,447 | 145,938,214,912 | 121,773,816,760 | 23,001,931,760 |
| obs_event | 515,154 | 2,110,070,784 | 1,689,650,411 | 217,028,051 |
| obs_raw | 336,654 | 1,378,934,784 | 1,009,279,001 | 168,405,461 |
| episodes | 668,615 | 2,738,647,040 | 2,430,462,501 | 277,460,952 |
| episode_key | 24,135 | 98,856,960 | 78,251,976 | 12,312,526 |
| analyzers | 713,535 | 2,922,639,360 | 2,336,662,463 | 559,713,828 |
| analyzer_key | 20,499 | 83,963,904 | 67,920,845 | 9,636,242 |
| checkpoints | 2,082,355 | 8,529,326,080 | 8,520,284,325 | 693,809 |
| checkpoint_version | 12 | 49,152 | 25,672 | 20,562 |

Total accounted B-trees: **177,744,674,816 bytes (165.538 GiB)**. Observations alone: **82.11%**. Their pages contain **23,001,931,760 unused bytes**; this is evidence, not permission to compact the old tape.

### Raw payloads over the captured chronology

Compressed payload sums and counts are exact across the captured raw prefix. Quantiles are a deterministic stratified sample (16 chronology bands, 1000 consecutive records each); rare-source quantiles have small samples and are explicitly counted. Health/session totals are not packet-capture guarantees.

| Source/kind | Rows | Compressed payload bytes | Mean bytes/row | Sample n | p50 | p95 | Recent payload bytes/hour |
|---|---:|---:|---:|---:|---:|---:|---:|
| basis/health | 74,032 | 5,698,029 | 77.0 | 59 | 64 | 229 | 116,250 |
| basis/session | 13 | 28,524 | 2194.2 | 1 | 367 | 367 | 0 |
| basis/timer | 56,328 | 1,403,308 | 24.9 | 44 | 25 | 25 | 17,640 |
| coinbase/history | 1,966 | 1,381,910 | 702.9 | 2 | 709 | 709 | 2,172 |
| coinbase/spot | 2,369,419 | 593,128,474 | 250.3 | 1877 | 250 | 254 | 9,878,442 |
| deribit/options | 37,761 | 1,394,969,846 | 36942.1 | 31 | 39836 | 41826 | 17,771,478 |
| polymarket_clob/pm | 18,249,544 | 6,326,609,272 | 346.7 | 13942 | 349 | 352 | 72,781,890 |
| polymarket_gamma/catalog | 1,428 | 739,432,406 | 517809.8 | 2 | 517063 | 517063 | 9,354,744 |
| yahoo/history | 26,181 | 18,457,902 | 705.0 | 21 | 823 | 858 | 75,156 |
| yahoo/options | 26,181 | 154,411,835 | 5897.9 | 21 | 5834 | 12763 | 501,462 |

### Derived fan-out and current cadence

The recent window is **14:45:59–14:55:59 UTC**, immediately before the stall. It excludes stopped time and the isolated later packet; it is not a manufactured uninterrupted acceptance run. Rates multiply the exact 600-second window by six. Payload rates exclude SQLite row/index/page overhead.

| Trigger | Raw inputs | Observations | Obs/input | Observation payload bytes/hour |
|---|---:|---:|---:|---:|
| basis/timer | 118 | 7,080 | 60.000 | 64,389,684 |
| coinbase/history | 4 | 76 | 19.000 | 549,666 |
| coinbase/spot | 6,567 | 125,344 | 19.087 | 922,840,116 |
| deribit/options | 79 | 1,500 | 18.987 | 10,818,888 |
| polymarket_clob/pm | 34,862 | 34,836 | 0.999 | 295,940,748 |
| polymarket_gamma/catalog | 3 | 180 | 60.000 | 1,633,740 |
| yahoo/history | 60 | 220 | 3.667 | 2,694,318 |
| yahoo/options | 60 | 220 | 3.667 | 2,701,686 |

Recent **169,456 observations / 41,909 inputs = 4.043×** overall; Coinbase spot alone fans out to about **19.1 events per tick**. Entire-history ratio is **3.154×**. Recent observation mean is **1280.1 compressed bytes/row**; stratified 17,000-row mean **1867.0**, p50 **1837**, p95 **2835**. Recent full-observation payload: **1,301,568,846 bytes/hour**.

| Event | Observations in 10 minutes | Compressed bytes |
|---|---:|---:|
| 3865960 | 253 | 294,665 |
| 3865962 | 3,818 | 3,625,652 |
| 3865999 | 3,430 | 3,253,668 |
| 3866000 | 3,317 | 3,132,100 |
| 4909048 | 3,741 | 6,093,687 |
| 4909051 | 3,746 | 6,270,523 |
| 4909053 | 4,015 | 7,231,011 |
| 4909055 | 4,661 | 8,780,543 |
| 4909057 | 1,547 | 2,636,993 |
| 4909059 | 4,034 | 7,675,318 |
| 4909061 | 3,880 | 7,212,147 |
| 4909063 | 3,771 | 5,914,212 |
| 4909068 | 3,747 | 5,738,142 |
| 4935716 | 467 | 940,512 |
| 4935773 | 376 | 769,614 |
| 4935774 | 1,076 | 2,206,267 |
| 4935775 | 522 | 1,069,601 |
| 4935790 | 461 | 938,435 |
| 4935794 | 284 | 577,818 |
| 4935799 | 328 | 667,382 |
| 4935801 | 343 | 698,701 |
| 4935811 | 141 | 290,486 |
| 4935812 | 329 | 676,584 |
| 4935813 | 561 | 1,152,314 |
| 4935814 | 464 | 951,997 |
| 4935815 | 314 | 644,281 |
| 4935822 | 441 | 896,692 |
| 4935823 | 767 | 1,560,264 |
| 4935824 | 141 | 288,233 |
| 4935828 | 554 | 1,129,533 |
| 4935829 | 391 | 795,432 |
| 4935836 | 345 | 708,687 |
| 4935838 | 593 | 1,217,749 |
| 4935841 | 702 | 1,444,642 |
| 4935842 | 271 | 557,464 |
| 4950486 | 6,192 | 11,273,302 |
| 4950489 | 5,927 | 10,905,870 |
| 5062271 | 6,271 | 9,222,377 |
| 5062280 | 3,992 | 5,908,438 |
| 5170730 | 3,682 | 3,698,842 |
| 5170735 | 5,255 | 5,200,845 |
| 5170736 | 7,446 | 7,382,327 |
| 5170746 | 3,790 | 3,767,028 |
| 5170763 | 3,373 | 3,341,852 |
| 5170764 | 3,698 | 3,657,689 |
| 5170770 | 3,432 | 3,390,275 |
| 5170771 | 5,070 | 5,014,719 |
| 5170772 | 3,647 | 3,599,032 |
| 5170773 | 3,769 | 3,725,870 |
| 5170780 | 4,161 | 4,108,047 |
| 5170781 | 4,544 | 4,515,386 |
| 5170787 | 4,344 | 4,305,309 |
| 5170788 | 3,595 | 3,565,268 |
| 5170793 | 3,617 | 3,602,316 |
| 5170794 | 5,202 | 5,166,351 |
| 701495 | 3,827 | 3,691,693 |
| 701496 | 3,872 | 3,647,769 |
| 701501 | 3,555 | 3,640,559 |
| 701545 | 3,279 | 3,057,195 |
| 701548 | 3,289 | 3,065,968 |
| 701549 | 3,536 | 3,352,916 |
| 701552 | 3,260 | 3,079,549 |

Recent episode revisions: **9,192 rows/hour**, **7,945,020 UTF-8 bytes/hour**. Analyzer outputs: **25,200 rows/hour**, **25,695,546 bytes/hour**. Neither dominates the monolith. Checkpoints account for **8,529,326,080 bytes**; the latest checkpoint contains **324,436 rolling rows**, compressed to **7,105,421 bytes**. Rewriting similar windows every five minutes is another avoidable growth stream.

### Compression benchmark

300 earliest sampled observations: zlib1 **393,468 bytes / 0.0061s**, zlib3 **391,281 / 0.0065s**, zlib6 **380,532 / 0.0075s**, zlib9 **376,587 / 0.0094s**. zlib6 saves only **3.3%** on these full rows. Compression alone cannot meet the target. The raw sample (300 mixed packets, including large snapshots) is not an equal-weight source benchmark; no new codec dependency is warranted.

### Operational defect found while measuring

The legacy service stopped timer recording at raw **20,842,508**, 14:55:59.018 UTC. One later delayed PM receipt was committed at 15:29:35.749. An owned API thread was blocked in SQLite `readDbPage`, while the collector and service waited on their shared connection locks. The exact originating HTTP request is not proven by the native stack. This stall began before profiling. The old optional event predicate could force a complete 145.9 GB observation-table scan for an empty selected scope. The fix uses a covering-index ID selection and separate bounded read-only API connections; it does not change the research calculation. Historical gaps and recovery evidence remain in the acceptance journal.

## Storage v2 design

- Preserve every logical source payload and its original receipt/source timestamps, sequence, session and SHA-256. Lossless schema/identifier dictionaries, bounded anchor/delta or preset-dictionary encoding; no reduced source cadence.
- One compact typed frame per event per second at the first causal update, plus semantic-version changes. Existing analyzer grid is 15 seconds; replay still uses every native source tick and the unchanged reducer, not sampled frames.
- Immutable event semantics, model-evaluation metadata, PM depth and equity context references. Full features remain exact at each recorded frame boundary.
- Exact-time native PM/OPT/spot salient jumps and source-state transitions, with causal IDs; ordinary recalculations are not full JSON rows.
- Lifecycle journal records terminal changes immediately; redundant episode updates are intentionally sampled, with material peak/state changes retained. Full episode evolution remains reconstructible from raw.
- Compress analyzer outputs without changing algorithms or parameters. Content-address calendar-minute rolling-history checkpoint chunks so unchanged history is shared.
- Global monotonically increasing raw/frame/revision identities across legacy and 256 MiB/UTC-day v2 segments. A manifest controls closed immutable segments; current operator DB alias, paper and operational sidecars remain stable.
- Closed-segment quick_check and file hash are recorded once after safe normal checkpoint/close. Routine acceptance checks only the small active segment and validates unchanged closed-file metadata; frozen legacy integrity is dated evidence, not an excuse for a daily live rescan.

## Same-stream live validation — PASS

2026-10-01 **16:44:42.074–17:14:47.733 UTC**, **1,805.659 seconds / 30.094 minutes**. The shadow tailed already committed production sources; no duplicate feeds or legacy writes. Exact archived producer: **`7be83cd5f24448f6c0b7`**. Unchanged reducer fingerprint: **`1133d8d67863377be04f0c00edf36f1295577503d272d97599177ba3a2cd560e`**.

| Measurement | v1 | v2 |
| --- | --- | --- |
| New database bytes | 784,838,656 | 60,747,776 |
| Bytes/hour | 1,564,758,251 | 121,114,809 |
| Human units/hour | 1.457 GiB | 115.504 MiB |
| Logical source records | 121,771 | 121,771 |
| Live derived recalculations | 436,777 | 436,777; unchanged |
| Persisted research rows | 436,777 | 41,298 compact frames |
| Analyzer outputs compared | 11,760 | 11,760 |

**Reduction: 12.920×.** The <150 MiB/hour and ≥10× minimum passed. The preferred 20–75 MiB/hour target did **not** pass. Native timing was retained. Frame count fell **10.576×**, and each retained frame is smaller and shares repeated data.

All **121,771** complete logical source records/hashes matched, including receipt/source/monotonic timestamps, provider fields, session and global identity. All **41,298** frames and **11,760** analyzer outputs matched corresponding v1 records. Independent replay matched PM books, spot, option surfaces, latest observations/gaps, rolling dynamics, native salient history, active episodes and the complete **1,007-key** analyzer cache: **zero mismatches**. No episode revisions occurred in this live window; episode behavior was exercised by synthetic representation tests, not claimed as a live lifecycle exercise.

Two verifier defects were caught before cutover: `load_checkpoint` aliases mutable state, so the first verifier reused an advanced baseline; the next final comparison used a UI-capped 1,000-output query against a 1,007-key cache. Verification was repeated from freshly read immutable initial/final checkpoints, checking every recorded frame/analyzer causally and reconstructing the final cache from its seed plus all revisions. Original ERROR/FAIL reports remain under ignored `data/storage-validation-20261001/`; accepted evidence names and explains them. No failed evidence was deleted or counted as a match.

### Actual v2 page accounting

Final shadow database **60,850,176 bytes**, including its initial **102,400-byte** schema. Actual dbstat page allocation:

| Object | Pages | Bytes |
| --- | --- | --- |
| records | 5,989 | 24,530,944 |
| blobs | 2,655 | 10,874,880 |
| frames | 2,415 | 9,891,840 |
| analyzers | 1,711 | 7,008,256 |
| record_time | 718 | 2,940,928 |
| record_stream | 528 | 2,162,688 |
| frame_event | 265 | 1,085,440 |
| frame_raw | 220 | 901,120 |
| sqlite_autoindex_blobs_1 | 177 | 724,992 |
| analyzer_key | 120 | 491,520 |
| checkpoints | 21 | 86,016 |
| salient | 12 | 49,152 |
| event_versions | 9 | 36,864 |
| salient_scope | 5 | 20,480 |
| sqlite_schema | 3 | 12,288 |
| streams | 1 | 4,096 |
| sqlite_autoindex_streams_1 | 1 | 4,096 |
| sqlite_autoindex_event_versions_1 | 1 | 4,096 |
| seal | 1 | 4,096 |
| event_identity | 1 | 4,096 |
| episodes | 1 | 4,096 |
| episode_key | 1 | 4,096 |
| checkpoint_version | 1 | 4,096 |

**61 immutable semantic versions**, **16,091 shared blobs**, **566 salient records**, **6 checkpoints**. Blobs include source dictionaries, model/context/depth references and checkpoint chunks; their bytes cannot be attributed exclusively to one stream. Raw records/indexes now account for about **49%**. Analyzer outputs remain complete; only encoding changed.

### Source encoding payoff

Same live window. Compressed bytes exclude SQLite rows/indexes and shared blobs; this table does not double-count those references. Exact logical payloads survive decoding. This preserves canonical adapter data, not original provider wire bytes.

| Source/kind | Rows | v1 bytes | v2 bytes | v2 mean / p50 / p95 bytes |
| --- | --- | --- | --- | --- |
| basis/health | 401 | 48,895 | 17,194 | 42.9 / 10 / 222 |
| basis/timer | 127 | 3,147 | 1,926 | 15.2 / 10 / 23 |
| coinbase/history | 10 | 901 | 716 | 71.6 / 72 / 72 |
| coinbase/spot | 17,532 | 4,400,639 | 939,864 | 53.6 / 48 / 139 |
| deribit/options | 120 | 4,499,789 | 2,791,690 | 23264.1 / 22340 / 32694 |
| polymarket_clob/pm | 103,290 | 35,600,627 | 10,881,922 | 105.4 / 105 / 170 |
| polymarket_gamma/catalog | 1 | 521,740 | 349,084 | 349084.0 / 349084 / 349084 |
| yahoo/history | 145 | 30,348 | 9,504 | 65.5 / 40 / 135 |
| yahoo/options | 145 | 202,272 | 22,034 | 152.0 / 62 / 382 |

9,597 full anchors, 461 recursive field deltas, 111,713 preset-dictionary deltas. Maximum dependency depth **15**; full anchors at five minutes or sooner. Yahoo's unchanged large structures share content. Its chain availability in this window limits what its tiny payload counts prove.

Most savings come from removed derived fan-out, compact/shared frames, lossless raw encoding and shared checkpoint chunks. Meaningful episode transitions replace redundant updates; this quiet window does not quantify that benefit. Stronger compression contributes but is not the primary fix.

### Reconstruction and resources

- Same archived reducer on original v1 sources: **121,771 records / 21.742s = 5,601 records/s**.
- V2 replay **including every frame/analyzer comparison**: **121,771 / 43.381s = 2,807 records/s**. This is a stricter workload, not a codec-only throughput benchmark; five-minute replay is measured in seconds.
- Segment-reader open, 20 samples: median **0.505ms**, slow **0.768ms**.
- 100 cold Deribit surface decodes: median **45.65ms**, slow **146.85ms**, max sampled depth **12**. Sequential replay reuses its bounded vector cache.
- Shadow CPU including equivalence work: **7.39% of one core**; steady RSS **441,352,192 bytes**, validation peak **577,273,856 bytes**. Original producer over **1,783.492s**: **6.60%**, RSS **552,026,112 bytes**, peak **702,005,248 bytes**. Shadow CPU is additional validation work, not replacement-collector CPU.
- Shadow WAL high-water **7,815,672 bytes**; old allocated WAL **10,427,748,872 bytes**. No WAL was manually deleted.

## Production cutover — COMPLETE

The old supervisor stopped normally at **17:42:38 UTC**; final checkpoint succeeded in **0.959s**, and normal SQLite close checkpointed its WAL. Explicit cutover published `data/basis.sqlite3.storage.json` and `data/tape/manifest.json`. V2 began at **17:44:04 UTC**. No legacy schema/data migration, destructive compaction or wallet reset.

Legacy is frozen at **180,779,655,168 bytes (168.364 GiB)**, with exact size/mtime retained. Its prefix contains **21,314,772 raw**, **67,381,505 observations**, **2,095,611 analyzer outputs**, **2,668,837 episode revisions**, **947 checkpoints**. First v2 raw ID **21,314,773**.

All eight wallet/run IDs, including **OLIVER run 1 / ID 1**, remained unchanged. Paper/acceptance sidecars, archived runs, campaign start and reducer archives retain their paths. Exactly one supervisor/collector was observed. Models, calculation/feature versions, config, algorithm thresholds and paper policies/costs were unchanged.

A real large-tape query defect surfaced: selected-event episode lookup still filtered unindexed legacy `event_id`, reading full revisions until its five-second budget failed. The reader now bounds the existing episode-key index by event prefix and checks exact event identity on grouped latest rows. No index was added to the frozen DB. A graceful v2 child restart applied it; restore **2.535s**, no unavailable checkpoints. Collector archive **`32465d739603d9219ef2`**, same reducer/config.

### Production growth and resources

The first v2 checkpoint imported about **8 MiB** of legacy rolling/context state once. Initial short averages included this seed and a deliberate shutdown checkpoint and exceeded 150 MiB/hour; they are not presented as steady-state growth.

After that initial checkpoint, **859.987s / 14.333 minutes** recorded **36,139,008 bytes**, or **151,281,855 bytes/hour = 144.274 MiB/hour**. This includes subsequent checkpoint/restart writes and is **10.34×** below the simultaneous v1 baseline. It is busier/more frame-dense than the shadow; rates depend on source activity. RSS **666,865,664 bytes**, peak **866,013,184 bytes**.

First replacement child: **8.25% of one core over 423.625s**, versus original 6.60%; no CPU/memory improvement is claimed. Initial v2 checkpoints **1.990s / 1.700s**. Longer resource behavior remains unproven.

### Real API/browser evidence

Three requests/endpoint; scoped results and sizes:

| Endpoint/context | Median ms | Slow ms | Rows/series | Bytes |
| --- | --- | --- | --- | --- |
| /api/state | 6.2 | 11.3 | 60 | 195485 |
| /api/catalog | 34.2 | 36.8 | 2503 | 4936270 |
| /api/replay?event_id=4950486&tail=1&limit=50 | 11.1 | 11.5 | 50 | 207551 |
| /api/episodes?event_id=4950486 | 132.3 | 136.1 | 0 | 130 |
| /api/algos?event_id=4950486 | 135.2 | 142.3 | 7 | 7555 |
| /api/wallets | 2.1 | 2.4 | 8 | 47936 |
| /api/equity?wallet=OLIVER | 16.2 | 47.3 | 8 | 412324 |
| /api/history?wallet=OLIVER | 12.9 | 1044.4 | 0 | 859 |

The equity request above returned default all-wallet series; the OLIVER history response correctly contained no orders. Selected episodes originally failed three times at five seconds; after the index fix median **132.3ms** with explicit empty reason/global count.

One selected-event query returned **1,038 legacy observations + 38 v2 frames** across cutover. Raw API hashes/provenance succeeded on both sides. Real Chrome rendered Monitor/new Tape data and selected Gaps EMPTY + **3,300 global episodes**, with its all-events control. No manual orders were submitted in this storage pass.

Explicit cutover/restart sampling checked **770 observations / three verified regions**, zero mismatches/missing versions. A fourth 64-tick latest window had no frame and was explicitly unverified. Routine sampling now preserves latest-region priority and expands v2 suffixes to 1,024 causal records without exceeding the captured prefix.

Latest routine acceptance: **9/33 regions**, **3,022 observations**, **24,998 raw records**, **zero mismatches or missing reducers**, including **604 latest v2 frames**. Budget-skipped regions are not matches. Report **28.869s**, active quick_check **0.203s**, frozen legacy identity unchanged; its previous dated structural result remains separate.

## Remaining limits / acceptance status

**Storage migration PASS; extended unattended operation PARTIAL.** Campaign 24h/72h/7d remain **NOT YET ELAPSED**. Historical 24h/72h remain **FAIL** for old gaps; 7d **NOT YET ELAPSED**. Pre-profile stall/recovery, failed validator reports and controlled cutover/restart intervals remain recorded.

Six new high-value tests; full **74-test suite passes**. Coverage: exact anchors/deltas; semantic references/bounded amplification/causal inputs; shared checkpoints/full research replay; rotation/global IDs/busy-reader deferral/cached integrity; mixed history/acceptance/selected episode lookup; rollback dictionary safety. Existing research, interaction, paper and operational tests retained.

Automatic production size/day rotation has not yet elapsed; rotation was exercised in isolated databases. Frozen legacy full quick_check remains dated at raw **19,851,074**, not a full-tail audit. No daily monolith rescan. Closed-file integrity/hash is recorded at seal and reused only while size/mtime remain unchanged; this does not promise to detect malicious preservation of metadata. Source/provider changes can raise lossless raw costs. The preferred ≤75 MiB/hour target, multi-day rates, indefinite-hang/machine-reboot recovery and future duration gates remain unproven. No automatic legacy deletion or cold compaction.

### Final production observation, 18:15 UTC

Actual v2 runtime observed **31.794 minutes**. V2 segment bytes **86,310,912**; legacy size/mtime still exactly unchanged, integrity **ok**, one supervisor/collector, all wallet run IDs preserved.

The longer post-seed window measured **25.916 minutes**, **62,054,400 bytes**, **143,664,490 bytes/hour = 137.009 MiB/hour**, a **10.89×** reduction against measured v1. The startup-inclusive **31.460-minute** average was **154.368 MiB/hour**, still slightly above 150 because it includes the one-time imported state. This distinction is explicit; the steady minimum passed, not every short startup interval.

Current child over **24.191 minutes**: **13.86% of one core**, RSS **668,217,344 bytes**, peak **866,013,184 bytes**, WAL **4,873,992 bytes**. Browser/API load differs from the earlier producer window; these are observations, not a controlled CPU speedup claim.
