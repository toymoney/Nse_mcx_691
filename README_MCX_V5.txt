MCX ATM-FIRST FRESH REFRESH V5 — 25 November 2026

INSTALL
1. Extract this ZIP.
2. Replace mcx_s3_bot.py at the root of toymoney/Nse_mcx_691 on the main branch with the included complete file.
3. Optional: add tests/test_fresh_fetch.py alongside your existing tests.
4. Keep mcx_recorder.py, requirements.txt, workflow, secrets and the research-state branch/data as they are.
5. A currently running Actions job keeps its old checkout: cancel that job if needed, then run the existing workflow again. Later jobs use the replacement file. No new settings or secrets are required.

WHAT CHANGED
- Discover ATM from each exact underlying futures contract.
- Fetch ATM +/-4 candidates plus one listed neighbouring strike beyond each edge first (normally ATM +/-5 for both CE and PE). The outer strikes support pricing, not extra alerts.
- Evaluate this small group immediately without waiting for all strikes or broker Greeks.
- Before staging an alert, fetch the candidate, both selected bracketing neighbours and exact futures together in a single quote request.
- Immediately before Telegram delivery, repeat that four-instrument refresh, recompute midpoint IV/interpolated model value and enforce all filters again. A disappeared gap, missing input, stale input or ATM move outside +/-4 blocks delivery.
- Once delivered, retain the entry model target unchanged for later touch checking. If a queued entry is rejected by the send-time check, it is not counted as an alerted trade and can be reconsidered later without creating duplicate watches.
- Continue recording all other option strikes after the priority alert path. Fetch supplementary broker Greeks last, in separate kind=broker_greeks records keyed by instrument and timestamp; raw option quotes and local model Greeks remain in option records. Consumers needing broker Greeks can join those separate records by key/time; they are not delayed into each option row.

COUNTS
The daily Telegram report, reports/YYYY-MM-DD.json, state.json and sweep console distinguish:
  diagnostic_valid_pricing_gap_too_small
  diagnostic_blocked_stale_inputs
Additional counters include valid_pricing_gap_too_large, eligible_before_refresh, other_input_or_liquidity_block, refresh_missing_inputs, final_refresh_passed and final_refresh_rejected.
Diagnostic counts are cumulative candidate evaluation/check counts, NOT unique signals. Repeated sweeps and failed final rechecks may count the same contract again. In a given classification, stale inputs take precedence over gap diagnostics. Background out-of-scope strikes do not inflate these candidate diagnostic counts.
Each option snapshot records fetch_phase, fetch_version, input_freshness_reasons, alert_rejection_reasons and peer_input_checks.

UNCHANGED RULES
- Black-76 model; peer IV derived from bid/ask midpoint and interpolated by actual strike distances.
- Existing 2-5% gap at BOTH LTP and ask, including both boundaries.
- IMPORTANT: the existing live bot defines gap as 100*(model-price)/model. This denominator is preserved; it has not been changed to return-on-entry.
- Candidate AND neighbouring liquidity rules: OI raw >=500; volume raw >=100; spread <=2%; last trade <=30s; positive bid/ask quantities; valid uncrossed positive book.
- ATM +/-4 computed from the FULL listed strike ladder, including during final refresh.
- Existing once-per-contract-per-day policy and state identity retained. Targets remain fixed after confirmed entry; only later actual trade timestamps can register a touch. No order endpoints added.

FRESHNESS DETAILS
The new pre-alert freshness gate also requires candidate/peer quote timestamps and receipt timestamps, plus underlying futures trade/quote/receipt timestamps, to be known and 0-30 seconds old. A fresh HTTP response containing old trades does not pass. Missing input is not replaced with a cached value. One REST request reduces timing mismatch; it is not a promise of an atomic exchange snapshot.
The priority group uses listed neighbouring strikes. If it cannot produce two valid bracketing midpoint IV inputs, no model or alert is fabricated. Background partial-batch model fields are research diagnostics; only the priority path plus the four-instrument refresh can create an alert.
Old queued entry messages without refresh metadata are suppressed after this upgrade, rather than sent without verification. Already sent entries and historical recordings are preserved.

VALIDATION
39 offline tests passed, including single-request four-instrument refresh, full-ladder ATM movement, stale candidate/peer/futures rejection, small-gap classification, missing-refresh handling, send-time gap rejection, successful delivery, retry without duplicate watches, fixed targets and strict post-entry trade checking. The test suite also verifies priority delivery occurs before far-strike fetching and broker Greeks.
Pricing, IV interpolation, discovery and existing liquidity functions were compared with the fetched main-branch source and are unchanged.
Live broker/Telegram operation has not been tested here; delivery tests use mocks and send no messages. This change improves freshness and explains rejected candidates; it does not guarantee alerts or target hits.
