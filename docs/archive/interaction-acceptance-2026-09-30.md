# Ticket and view reliability acceptance

Browser checks ran in Chrome on September 29–30, 2026, against an isolated paper database on port 8766 with public live feeds. Production remained on port 8765. Test orders and run resets affected only the acceptance database. No browser-testing packages were installed.

## Defects and changes

| Defect found | Result |
| --- | --- |
| Submit closed the interaction at “queued” | Ticket polls a ledger-backed order-status API through actual latency; final fill/rejection/cancellation stays visible. |
| Unknown explicit event could fall back to another underlying; unrelated views retained hidden event context | Backend rejects unknown/blank IDs and asset mismatch; ticket requires a selected current event and owns a frozen context. |
| Deribit reducer retained calls, hiding actual puts from discovery | Surfaces retain both rights; discovery gives each a nearby-strike quota. Research still uses the existing calls. |
| Yahoo's 60-second collection interval exceeded the generic 45-second paper quote limit | Yahoo uses recent receipt over two collection cycles, preserves source timestamps, labels estimated execution, and checks the current regular session. Crypto remains strict. |
| Candidate quote failures disappeared into empty lists | Discovery returns bounded, counted exclusion reasons. Closed stocks explain why execution is unavailable. |
| Blank tables and selected-scope emptiness looked broken | Every view has explicit loading/data/empty/error state; selected scope exposes global count and Show all without silently changing scope. Selected filtering happens before backend limits. |
| Secondary request errors claimed service disconnection; charts blocked ordinary polling | Service and view request states are separate. Catalog, research views, theses and charts run independently of base polling. |
| Wallet/run selection did not consistently reach positions/history; empty manual history was obscured by marks | Wallet/run selectors use validated run ownership; archives retain marks and ledger. Browser ledger omits periodic marks. Wallet details expose cash, positions and exact costs. |
| Extreme-tail relative gap overflow stopped collection and broke JSON state | Unrepresentable REL becomes unavailable with an explicit quality flag; valid absolute gaps remain available. |
| Transaction cleanup masked database failure; shutdown attempted a checkpoint of uncommitted state | Preserve SQLite's original error, block further ingestion/execution after a write failure, and skip invalid checkpoints. Restart uses committed tape. |

## Actual browser paths

| Path | Observed result |
| --- | --- |
| Monitor, Markets, Tape, Gaps, Algos, Curves, Health, Wallets, Positions, Ledger | All ten views operated. Data or explicit empty reason rendered; no blank table was used as a state. |
| Tape/Gaps/Algos on selected unmapped event 4904730 | Explicit selected-scope emptiness, global record counts and Show all. Switching to all showed analyzer records; returning to selected restored the same context. |
| Paper ticket from empty OLIVER Ledger | Explicit instruction to select a market event; no stale trade context reused. |
| BTC event 4841940 → actual call → preview → submit → pending → filled | Bought one BTC-30SEP26-84000-C. Actual fill $183.76, fees $0.83, total debit $184.60; remaining cash and position shown in the modal. |
| Same BTC context → actual put → preview → submit → filled → Ledger | Bought one BTC-30SEP26-84000-P from real Deribit put data. Actual fill $835.33, total debit $836.82. ORDER and FILL appeared in OLIVER's ledger. |
| TSLA event 3866261 while regular session OPEN → SPOT:TSLA → preview → filled | One-share estimated Yahoo fill $355.25; total debit $355.60. Receipt/source timestamps, proxy assumptions and 120-second receipt limit were visible. |
| Filled ticket → View position / View ledger | Correct OLIVER run selected; actual holdings and orders rendered. |
| Wallet selector OLIVER → SW | Position table changed to an explicit “No SW positions” state. |
| Isolated OLIVER reset to run 2 → select historical run 1 | Current run showed no activity; run 1 retained all three positions, six order/fill entries and historical chart. Archive equity $99,825.96 and costs $146.66 matched backend figures. |
| Stock ticket with a controlled CLOSED fixture | No instruments; summarized MARKET_CLOSED reason, Preview disabled. The live market had opened by the resumed browser check, so this closed UI path was injected. Calendar-based closed execution was also tested in Python. |
| Submitted crypto call with controlled stale-quote failure at execution time | Ledger recorded REJECT; modal stayed open and displayed “quote became stale during latency,” with View ledger available. |
| Injected failures in /api/equity, /api/history, /api/algos, /api/replay | Each displayed its exact request error. Wallet table survived chart failure; base service remained connected and monitor rendered data. |
| Five-second /api/equity delay → Monitor | Monitor returned in about one second and observation counts advanced while the chart request remained in flight. |

Failure injection used a temporary local harness outside the repository. It did not change production execution rules. The acceptance tape was moved from memory-backed `/tmp` into ignored `data/interaction-acceptance-20260929/`, together with its reducer archive; production data was not moved or reset.

## Regression coverage

Eight tests in `tests/test_interactions.py` cover explicit context rejection, actual put discovery with unchanged call calculations, source-aware Yahoo/crypto freshness, closed/missing-source diagnostics, queryable latency/fill/reject/cancel states and archived run ownership, filtering before limits, finite tail state, and a real SQLite page-limit failure preserving its cause and blocking execution/checkpointing. The existing migration fingerprint assertion was updated.

Validation: 63 Python tests passed; `node --check app.js` and `git diff --check` passed. Existing research tests remain in the suite.

## Limits

Yahoo remains delayed/proxy/estimated; its option-book timestamp is unknown. Closed-session UI and final stale rejection were controlled fixtures, not a claim of real market failure. Cancellation was exercised in the backend regression, not manually in the browser. Options retain existing USD cash-equivalent settlement limitations. No algorithm, market, strategy, fill-cost or risk-policy expansion was made.
