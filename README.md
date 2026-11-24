# MCX S3 v3 — option buying + complete research recording

This package updates the existing MCX scanner. It does not change NSE files or place orders.

## Update your existing repository

1. Stop any active MCX scanner job.
2. Extract the ZIP. Replace these exact paths on your repository's default branch:
   - `mcx_s3_bot.py`
   - `mcx_recorder.py`
   - `tests/test_mcx.py` (inside the tests folder)
   - `README.md`
3. Commit them together. The corrected tests must be uploaded with the scanner.
4. Open Actions > MCX S3 scanner and recorder > Run workflow > mode `run`.

The existing workflow and all three secrets are unchanged. This ZIP also includes the workflow and requirements for a fresh installation. Do not delete `mcx-research-state` or any existing recordings. The ZIP is a replacement package for you to upload; the connected GitHub repository was only read while preparing it.

## Live-alert rules

| Rule | Requirement |
|---|---|
| Instruments | CRUDEOIL, NATURALGAS, GOLD, SILVER options; nearest active option expiry |
| Direction | Buy CE or PE; target above purchase reference |
| Strike | ATM and up to four actual listed strike steps on either side |
| Gap at LTP | At least 15% below model target |
| Gap at available ask | At least 15% below model target |
| Spread | `(ask-bid)/((ask+bid)/2)*100 <= 2%` |
| OI | >=500 in the broker's raw reported quantity units |
| Today's volume | >=100 in the broker's raw reported quantity units |
| Last-trade age | From 0 to 30 seconds inclusive, measured at evaluation |
| Book validity | Positive bid, ask and displayed quantities; bid <= ask |

ATM is calculated from the exact underlying futures price. Ties select the higher strike. The strike-step count uses the complete listed strike set for the same futures contract and option expiry, including strikes whose quotes are missing; removing a quote cannot move the strike indices. Both CE and PE may alert. There is no NSE-style gap-tier OI rule, 30% gap cap, delta limit, IV ceiling, 3-lot depth floor or five-day rollover in this MCX version.

Missing, nonfinite or negative required inputs do not pass. OI and volume are deliberately labelled **broker raw units**, not converted NSE-style lots. Master `lot_size` and `qty_multiplier` remain separately recorded so conversions can be verified before changing thresholds. The initial 500/100 thresholds are configurable code constants in `ALERT_FILTERS`, not claimed MCX performance findings.

15% is an alert threshold, not a fixed profit target. The gap formula is `(model target - price)/model target`; a model value of 100 permits LTP and ask no higher than 85. Target comes from Black-76 using the mapped future, strike, remaining expiry time, discount rate and same-side neighbouring-strike IV. Candidate own IV is excluded from its target calculation. The target method is unchanged in this release; the new liquidity checks restrict alerts only and do not filter IV peers or raw data collection.

Example message format:

```
BUY MCX OPTION | COMMODITY STRIKE CE/PE
Expiry: YYYY-MM-DD
Entry ₹85 | Target ₹100
Gap below model value at ask: 15.00%
Stop-loss: not set
HH:MM:SS IST | Option premium; not a futures trade
```

Entry is the ask reference at detection, not a claimed executed fill. The future is a pricing input, never an order. A stop-loss has not been invented. Target messages preserve the original target and show observed price separately. There are no order endpoints.

## All data continues recording

The filters do not shrink the recorder universe. All listed CE/PE strikes in the selected nearest expiry are fetched for each selected commodity, including those outside ATM ±4, low-volume options, wide books, stale/missing quotes and invalid-model rows.

Every option snapshot retains:

- Identity, strike, side, expiry, exact future key, lot size, quantity multiplier and raw tick size.
- Full raw option quote, all returned five-level bid/ask depth, OI, volume, total quantities, OHLC and any other broker quote fields.
- Best bid/ask and quantities, spread, volume/OI, LTP, trade timestamp, quote timestamp, receipt/evaluation times and age.
- Raw mapped-future quote and receipt timestamp, with independent future snapshot rows.
- Original broker Greek response and its receipt time when available. Missing broker Greeks stay null.
- Local Black-76 IV, signed delta, gamma, theta/day, vega/1 IV percentage point and rho/1 rate percentage point, when computable.
- Peer IV, peer keys, model target, LTP/ask gaps and model-status reason when the model cannot be calculated.
- **New:** `atm_strike`, signed `strike_steps_from_atm`, `alert_policy`, `alert_eligible` and the full `alert_rejection_reasons` array.
- Separate API-error rows with timestamps. Missing values are never invented as zero.

`mcx_recorder.py` is automatically used by `mcx_s3_bot.py`. You do not run two processes for alerts and recording.

## Where to find recorded files

In GitHub open the repository, change the branch selector from `main` to **`mcx-research-state`**, then open:

| Path | Contents |
|---|---|
| `snapshots/YYYY-MM-DD/*.jsonl.gz` | All option/future observations and errors |
| `reports/YYYY-MM-DD.json` | Daily watch records, outcomes and rejection counters |
| `reports/YYYY-MM-DD.txt` | Readable daily report |
| `state.json` | Current progress, watches, policies and Telegram queue |
| `master-YYYY-MM-DD.json.gz` | Instrument master used that day |

For example, recordings on 24 November 2026 are in `snapshots/2026-11-24/`. Download the complete folder, not just one five-minute chunk, for full-day comparisons. The snapshot files are compressed JSON Lines: decompress `.gz`, then each line is a JSON object.

Alternatively, open a completed Actions run and download the **mcx-data-RUN_ID** artifact under Artifacts. It includes state, recordings, master and reports. Artifact retention is seven days; the separate data branch is retained without automatic pruning. Raw all-strike archives accumulate over time and may become large. Credentials are not written to research files; data/message text on a public branch is public.

Every snapshot is retained before alert selection. There is no need for a live alert in order to analyse a rejected opportunity later. CSV export includes the rejection-reason array as a JSON string.

## Restart and compatibility

State format remains compatible. New alert policy is `MCX_OPTIONS_BUY_GAP15_LIQUID_ATM4_V3`. On upgrade, unsent entry/hit messages from previous policies are marked suppressed so old small-gap or illiquid alerts do not drain into Telegram. They are not falsely marked delivered or deleted. Prior watches remain stored and continue target observation silently; report JSON identifies legacy watches separately.

Deduplication is once per contract per day within the new policy. A contract may qualify under this new policy even if an old policy previously created a watch today. Current-policy queued alerts survive subsequent restarts. Existing targets are not rewritten.

## Outcomes and observation scope

This is a REST snapshot recorder, not a complete tick archive. By default, each sweep is followed by a 60-second pause; API, computation, Telegram and checkpoints add to elapsed time. Quote batches are not simultaneous; receipt/evaluation times are preserved.

A watch target is fixed at creation. A hit requires an actual later same-day trade timestamp strictly after entry; a repeated stale pre-entry LTP cannot create a hit. `NO_TOUCH_OBSERVED` means no crossing in collected observations, not complete exchange coverage. Reports describe target observations, not stop-loss/cost/fill results.

Daily counters include repeated per-snapshot rejection reasons (not unique contracts). A row can fail several filters. The report's actual rejection reasons let us see whether spread, OI, volume, age or strike scope is responsible for low alert counts.

The recorder's offline `analyse` command tests every positive-gap model snapshot against later collected prices within that day, independent of alert eligibility. It labels `TARGET_OBSERVED`, `NO_TOUCH_OBSERVED` or `NO_LATER_DATA`. Those rows are research opportunities, not deduplicated live alert counts.

## Schedule and modes

Current default non-agri session is 09:00–23:55 IST during the published 2 November 2026–12 March 2027 window; outside it the close defaults to 23:30. `MCX_CLOSE=HH:MM` overrides changed/special timings. There is no 3:20 PM NSE cutoff. Reporting starts two minutes after configured close. Weekends are excluded; no full exchange holiday/special-session calendar is bundled.

Model expiry uses the master-listed date at configured close. If contract-specific expiry-day timings differ, set verified per-symbol `EXPIRY_TIMES_JSON` overrides. The master often contains a date marker rather than a tradable-session expiry timestamp. The model-rate default is 6% annually (`RISK_FREE_RATE`).

The workflow starts at 08:50, 13:40, 18:30 and 23:20 IST on weekdays. Jobs run up to 295 minutes, queue to protect the data branch, checkpoint about every five minutes and attempt a final save/artifact upload on exit. Scheduling and job handoffs can leave gaps; unobserved data is not fabricated.

| Mode | Telegram | Recording |
|---|---|---|
| `run` | Live filtered watches and target/report updates | All snapshots |
| `record` | Disabled | All snapshots, with filtered watch tracking |
| `test` | Startup/connection status, no new watches | One sweep |
| `once` | Filtered watches/updates | One sweep |

For a fresh repository, add `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` as secrets and enable Settings > Actions > General > Read and write workflow permissions. Include `.github/workflows/mcx_s3.yml`, using Add file > Create new file if your upload interface omits hidden folders. Use a valid Upstox token with MCX data access. The Put/Call Option Chain API is not used for MCX: quotes are assembled by instrument key. Broker Greeks are requested in 50-key batches; if unavailable, the error is saved and local calculations continue.

## Local use and research export

Python 3.11+:

```
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python mcx_s3_bot.py --mode run
```

Set secrets in your own environment. Local state defaults to `mcx-state/`; Git checkpointing is off unless `GIT_STATE=true`. The separate module's `python mcx_recorder.py record` runs the collector silently without Telegram secrets. Do not run it alongside the scanner in the same state directory.

After downloading recorded files, use the actual extracted snapshot path:

```
python mcx_recorder.py export --source mcx-state/snapshots/2026-11-24 --output mcx_snapshots.csv
python mcx_recorder.py analyse --source mcx-state/snapshots/2026-11-24 --output mcx_outcomes.csv
```

## Verification

28 offline tests passed. They cover pricing/IV inversion, own-IV exclusion, signed Greeks, exact futures mapping, missing values, complete-list ATM selection and ties, all liquidity boundaries, missing/nonfinite/negative inputs, the 15% ask/LTP checks, recording rejected rows, CSV export, restart/deduplication, post-entry target observation and a mocked collector sweep. Authenticated MCX data/Telegram and GitHub execution were not run using your credentials for this release.

References:
- https://upstox.com/developer/api-documentation/instruments/
- https://upstox.com/developer/api-documentation/get-full-market-quote/
- https://upstox.com/developer/api-documentation/option-greek/
- https://www.mcxindia.com/faq/market-operations
- https://upstox.com/announcements/revised-timings/
