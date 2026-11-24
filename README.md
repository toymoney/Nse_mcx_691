# MCX S3 v4 — option buying + complete research recording

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

## Live-alert rules — v4

- Buy CE or PE only, ATM ±4 actual listed strike steps.
- Both LTP and available ask must be 2% to 5% below the calculated target, inclusive.
- Candidate and BOTH immediately bracketing usable same-side IV neighbours: OI >=500, volume >=100 (broker raw units), spread <=2%, trade age 0–30 seconds, positive bid/ask quantities, uncrossed book.
- A failed neighbour blocks the target; the model does not skip to a farther liquid strike.
- Target IV is interpolated from neighbouring bid–ask midpoint IV, not neighbouring LTP IV. Own IV/Greeks remain recorded from own LTP.
- Targets remain calculated Black-76 values, fixed at alert entry. No delta, IV ceiling or directional rule was added.
- Gap means `(target-price)/target*100`; it is not profit divided by entry. Target 100 permits price 95 through 98.
- All raw snapshots, broker Greeks, own Greeks, OI, volume, book and rejection reasons remain recorded. `peer_input_checks` adds each peer midpoint, midpoint IV and failed checks.

Replace mcx_s3_bot.py, tests/test_mcx.py and README.md together. Included recorder, requirements and workflow support a complete replacement installation. Existing recordings and mcx-research-state must be retained. This ZIP is prepared for upload; it is not deployed to GitHub.

After replacement, stop the old active MCX run and start the MCX workflow in run mode. A running job keeps its previously checked-out code until restarted. Secrets and the recording locations are unchanged: mcx-research-state/snapshots/YYYY-MM-DD and reports/. Older watches remain recorded separately; pending old-policy entry/target messages are suppressed.

Validation: 30 tests passed, including inclusive 2%/5% limits, midpoint pricing, both-peer liquidity/freshness rejection, full recording, deduplication and later-trade-only target checks. These are implementation checks, not a replay result for the revised strategy.
