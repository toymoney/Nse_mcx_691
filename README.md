# MCX S3 research scanner + complete snapshot recorder

Separate MCX project. Keep the existing NSE scanner running in its own repository.

## What is included

- `mcx_s3_bot.py`: discovers contracts, fetches full quotes and broker Greeks where available, calculates Black-76 IV/Greeks and peer-IV model values, sends Telegram model watches and target-observed updates.
- `mcx_recorder.py`: separate recording module and standalone record/export/analyse commands. Saves every selected option snapshot, including no-model and no-alert records. It is automatically used by the scanner: do not run a second recorder concurrently against the same state directory.
- `.github/workflows/mcx_s3.yml`: session schedules, tests, persistent MCX data branch and downloadable Actions data artifacts.
- `tests/test_mcx.py`, `requirements.txt`, `.gitignore` and this guide.

There are no strategy thresholds for gap size, OI, volume, volume/OI, bid/ask spread, depth, delta, IV or trade age. No ATM-only restriction and no five-day rollover. There are no order/trade execution endpoints.

Scope is all listed CE/PE strikes of the nearest active option expiry for CRUDEOIL, NATURALGAS, GOLD and SILVER. These are separate contract families; mini/micro variants are not silently substituted. Actual futures mapping uses the option's exact `underlying_key` from the master. We skip unmapped/expired contracts instead of guessing. Nearest expiry is a data-collection scope, not a tested signal filter. Set MCX_SYMBOLS to choose different master symbols; an empty value selects all MCX option symbols (verify their session/expiry specifications first).

A new BUY-side model watch is made whenever the computed fixed model target exceeds LTP, with no minimum gap. One watch per contract per day prevents repeated Telegram alerts. Snapshot recording continues every sweep for every selected strike regardless of watches or outcomes. There is no active-commodity or CE/PE suppression. DOWN/zero-gap snapshots are saved for research but do not produce buy watches.

## First upload: new GitHub repository

1. Create a separate repository, for example `MCX-S3-Research`.
2. Extract the ZIP and upload the contents at repository root. Do not upload just the ZIP. Include both Python modules, tests and requirements.
3. Create the workflow path `.github/workflows/mcx_s3.yml` using **Add file > Create new file** if your upload interface omits hidden folders. Copy the supplied YAML. This is a new workflow; do not replace your NSE workflow with it.
4. Add the same three repository secrets: `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. The broker account/token must have usable MCX market-data access. Renew access tokens through the normal Upstox login process when required.
5. Under **Settings > Actions > General > Workflow permissions**, enable **Read and write permissions**.
6. Open **Actions > MCX S3 scanner and recorder > Run workflow > mode: test**. It fetches one complete sweep and saves snapshots, sends startup/test status, and creates no new watches. Zero returned MCX option quotes makes the test fail explicitly. Inspect `last_quote_count`, `last_missing_quote_count`, `last_broker_greeks_count` and model-status counts in the data branch's state file. Partial coverage is reported rather than fabricated.
7. Run **mode: run** for Telegram watches plus recording. **mode: record** records and tracks watches silently without Telegram. `once` performs one sweep with alerts.
8. Scheduled jobs subsequently run automatically on weekdays while the workflow stays enabled on the default branch.

No extra Telegram IDs, webhook setup or manual price inputs are required. Do not share tokens in chat, code or screenshots. Connection checks here did not use your authenticated quotes or Telegram credentials; use the supplied test mode before relying on live coverage.

## Where the research data is saved

Switch the GitHub branch selector to **mcx-research-state**:

```
state.json
master-YYYY-MM-DD.json.gz
snapshots/YYYY-MM-DD/HHMM-unique-id.jsonl.gz
reports/YYYY-MM-DD.json
reports/YYYY-MM-DD.txt
```

The workflow checkpoints closed compressed snapshot chunks and state about every five minutes and on normal exit. Each restart writes new uniquely named chunks; it never truncates a prior chunk. State/report data is separate from the NSE state branch. Downloadable **mcx-data-RUN_ID** artifacts appear at the bottom of each Actions run after it finishes, with seven-day artifact retention. Git data is retained without automatic pruning. A large all-strike raw-data archive will grow; archive/manage historical data deliberately as the research period grows. Public repositories make these market snapshots and message texts public. Credentials are not included in snapshots.

Each chunk is gzip-compressed JSON Lines: one independent JSON object per line. Preserve the whole day's folder for later analysis. Do not edit state or snapshots while the scanner job is running. A forced runner termination can lose the most recent uncheckpointed records; the exit step attempts to save them.

## Recorded data

Every selected option produces a record on every sweep, even if the quote is missing or IV cannot be solved. Each record includes:

- Instrument key, commodity, strike, CE/PE, expiry timestamp used, exact underlying futures key, lot size, quantity multiplier and raw tick size.
- LTP, OI, cumulative volume, volume/OI, best bid and ask, their displayed quantities, spread in price and percent, and full top-five depth and all other broker quote fields under `raw_quote`.
- Broker quote update timestamp, last-trade timestamp, quote receipt timestamp, evaluation timestamp, trade age and data-quality flags.
- Full underlying futures quote plus its receipt timestamp. Futures also get independent snapshot records.
- Unmodified broker Greek response and its separate receipt timestamp, if supplied; calculated Black-76 IV, signed delta, gamma, theta/day, vega/1 percentage-point IV, rho/1 percentage-point rate.
- Peer IV, peer instrument keys, model target, signed LTP gap and ask-based gap, or a specific reason the model could not be calculated.
- API batch errors and timestamps as separate error records.

Missing values remain null, never substituted with zero or yesterday's value. MCX API quantity units are preserved as **broker raw**; master lot_size/qty_multiplier are included separately. Do not automatically divide all MCX OI/depth numbers by lot_size as if these were NSE equity quantities. Quantity-unit conversion must be checked against the live MCX response before designing lot-based filters.

Broker Greeks and local calculated Greeks are different fields. The Greeks endpoint is requested in batches of at most 50. If it rejects MCX or becomes unavailable, the error is recorded and local calculations continue; broker fields remain missing. It retries broker Greek capability on the next job. Local IV is solved from each contract's own LTP; own IV is used for local Greeks only. For the candidate's target, own IV is excluded: the nearest usable same-expiry, same-side strikes on both sides supply interpolated peer IV. OI/spread do not weight that interpolation. Edge strikes or insufficient peer data are retained with a model-status reason, not an invented target.

Mathematically impossible prices, nonpositive inputs and missing/different-day trade inputs cannot produce a model value. These are technical validity requirements, not strategy thresholds. Old same-day LTPs, wide spreads and shallow books are recorded and can produce flagged research watches: there is intentionally no strategy freshness/depth/spread gate.

## Observation frequency and outcomes

This is a REST **snapshot** recorder, not an every-tick market-feed archive. The pause after each sweep is 60 seconds by default, plus API fetch, computation, Telegram and checkpoint time. Quotes are batched, not simultaneous. All quote receipt times are retained. A large universe or delayed API extends the interval; sweep timing appears in logs and state.

Targets are fixed when a watch is made. Only a later same-day last trade with a timestamp strictly AFTER entry can confirm a target. Repeated stale quotes cannot manufacture hits. The report uses `TARGET_OBSERVED` or `NO_TOUCH_OBSERVED`; it does not assert full tick/candle coverage. No stop-loss, fees or fill simulation is implemented. Offline analysis labels records with no later data as `NO_LATER_DATA`.

Telegram sends at most five queued messages per loop and prioritises startup/test/report and target updates. Without thresholds, many model watches can be created; all are persisted, while Telegram delivery may lag. Each contract alerts once daily; a crash during a successful but unacknowledged Telegram request can cause a duplicate on retry.

## MCX session and persistence

For these four non-agri commodity families, the default scheduled session is 09:00–23:55 IST from 2 November 2026 through 12 March 2027. Outside that published window the default close is 23:30; `MCX_CLOSE=HH:MM` overrides it for a changed/special schedule. There is no 3:20 PM NSE cutoff. New MCX watches may be created up to the configured commodity close; reporting starts two minutes after close. Weekends are excluded. No complete exchange holiday/special-session calendar is bundled; a weekday holiday will retain previous-day quote flags and cannot model those previous-day inputs.

The master often supplies a midnight date marker rather than an authoritative tradable expiry time. Model expiry uses that listed date at the configured MCX close. `EXPIRY_TIMES_JSON` permits per-symbol expiry-time overrides, for example `{"YOUR_SYMBOL":"HH:MM"}` using actual verified contract specifications. The selected close is an explicit configurable modelling assumption, not a claim that all commodity options have identical expiry-day sessions.

Four weekday GitHub starts: 08:50, 13:40, 18:30 and 23:20 IST. Each job runs up to 295 minutes; later jobs queue behind an active job sharing the MCX state branch. GitHub scheduling may be delayed; data gaps are not backfilled as if they were observed. A local continuously running process avoids job handoff gaps but needs a computer left on.

## Local commands

Install Python 3.11+ and `python -m pip install -r requirements.txt`; set the three secrets as environment variables in your own system.

```
python -m unittest discover -s tests -v
python mcx_s3_bot.py --mode test
python mcx_s3_bot.py --mode run
```

The separate recorder can run silently:

```
python mcx_recorder.py record
```

It uses the same collector/model and does not need Telegram secrets. Do not run `record` alongside the scanner with the same MCX_STATE_DIR.

Download the `mcx-research-state` branch/data artifact and export one day:

```
python mcx_recorder.py export --source mcx-state/snapshots/2026-11-24 --output mcx_snapshots.csv
python mcx_recorder.py analyse --source mcx-state/snapshots/2026-11-24 --output mcx_outcomes.csv
```

Use the actual extracted snapshot path. Export writes a flat option table; full raw depth/futures/Greek responses remain in JSONL. `analyse` evaluates every positive-model-gap snapshot against later observed trade prices within the same day. These are snapshot opportunities, not unique live alerts; reports/state hold the deduplicated alert cohort. It does not apply the later NSE filters. These files support testing those thresholds later without losing the unfiltered comparison data.

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| MCX_SYMBOLS | CRUDEOIL,NATURALGAS,GOLD,SILVER | Exact option family names |
| MCX_STATE_DIR | mcx-state locally | Separate persistent directory |
| SNAPSHOT_INTERVAL_SECONDS | 60 | Pause after each sweep |
| MAX_RUN_MINUTES | 300 locally; 295 workflow | Maximum process run time |
| RISK_FREE_RATE | 0.06 | Annual model discount rate |
| MCX_CLOSE | published-window default | HH:MM session override |
| EXPIRY_TIMES_JSON | {} | Verified per-symbol expiry time override |
| GIT_STATE | false locally | Workflow enables git checkpoint/push |

## Verification

Offline tests cover futures-option mapping, all-strike discovery, expiry selection, Black-76 parity/IV inversion and signed Greeks, absent data, previous-day input handling, own-IV exclusion, Greek request batch limits, recorder restarts, CSV export, post-entry outcomes, deduplication, report labels and a full mocked sweep with broker-Greek fallback. The public MCX master was downloaded successfully; on the development check 1,518 selected options mapped to four exact futures across the four families, with no guessed mappings. Counts and listings change.

Authenticated MCX quotes/Greeks, Telegram and your GitHub scheduling have not been exercised with your credentials here.

Official references:
- https://upstox.com/developer/api-documentation/instruments/
- https://upstox.com/developer/api-documentation/get-full-market-quote/
- https://upstox.com/developer/api-documentation/option-greek/
- https://upstox.com/developer/api-documentation/get-pc-option-chain/ (MCX option-chain endpoint unavailable; this code does not call it)
- https://www.mcxindia.com/faq/market-operations (Black-76 for options on futures)
- https://upstox.com/announcements/revised-timings/
