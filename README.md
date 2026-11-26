# MCX Option Gap Scanner (BUY side only, Upstox data API)

Finds MCX options that trade **cheap versus fair value**, so you can buy low and sell when price returns to fair.
Read-only: it never places orders.

## Run
```sh
pip install -r requirements.txt
python mcx_gap_scanner.py --demo --chain CRUDEOIL   # offline, no token: see the alert format first
cp .env.example .env                                  # paste your Upstox Analytics Token
python mcx_gap_scanner.py --test                      # checks token, MCX master, a quote, Telegram
python mcx_gap_scanner.py --once --chain CRUDEOIL     # one pass + full fair-value table for Crude
python mcx_gap_scanner.py                             # live loop every 60 s during MCX hours
python -m unittest discover -s tests -v
```
Alerts print on screen, go to Telegram if configured, and are appended to `state/signals.csv`.

## Important: Upstox has no option chain for MCX
Upstox's Put/Call Option Chain API states it is **not available for the MCX exchange**. So this tool builds the chain
itself from the public MCX instrument master (`MCX.json.gz`) plus Full Market Quotes (LTP, best bid/ask, OI, volume).
It auto-discovers every MCX option underlying (Crude, Gold, Silver, Natural Gas, Copper, Zinc ...), no hard-coded list.

## Expiry rule
Monthly series only. Uses the nearest expiry with **more than `ROLL_DAYS` (5) calendar days left**; with 5 or fewer days
it moves to the next expiry. Set `INCLUDE_WEEKLY=true` for weekly series.

## Fair value ("one value per option")
Two independent old-school models, averaged:
1. **Put-call parity**: `C - P = e^(-rT) (F - K)`. Fair CE comes from the PE at the same strike and vice versa. Needs no volatility.
2. **Black-76** (MCX options are on futures) with volatility interpolated from the **neighbouring strikes**, never the option's own price.

A signal needs **both** models to agree it is cheap (`MIN_MODELS=2`). Greeks (delta, theta, vega) come from our own Black-76;
OI and spread weight the smile and filter illiquid strikes; Upstox's option-greek IV is shown as a cross-check when it returns MCX data.

## How big should the gap be? (1-2% is too small)
I searched for a published standard and did **not** find a blog or forum that gives a "1-2%" rule. What the sources I found say:
mispricing must exceed transaction costs by a margin before trading (arXiv "Optimal Convergence Trading"), and professional
tools such as ORATS express edge as a percentage of the mid price. So the threshold here is derived from your real costs:

* **Gap vs LTP >= 10%** (`MIN_GAP_PCT`): `(fair - LTP) / LTP`. Your example: 500 vs 550 = 10%.
* **Net edge >= 4% after costs** (`MIN_NET_EDGE_PCT`): `fair - ask - half the bid/ask spread - brokerage/charges`, because you buy at the *ask*, not the LTP.
* **At least Rs 300 per lot** (`MIN_NET_EDGE_RS_LOT`), so tiny premiums do not trigger.
* Ignores options with spread above 10%, OI/volume too low, delta outside 0.10-0.95, or a **stale LTP** (LTP outside bid/ask; mid is used instead).

A 1-2% gap would usually be eaten by the spread alone. These numbers are starting defaults, not validated: watch a few
days with `--chain`, then tune. Charges are assumptions (Rs 20/order, 0.15% other); verify on Upstox's charges page.

## Exits (buy low, sell high)
At alert time fair value is frozen as the **target**. A later "SELL / TARGET REACHED" alert fires when the bid reaches it.
Optional `STOP_LOSS_PCT` (off by default, you did not ask for one). Unreached signals drop after 24 h or at expiry.

## Honest limits
* **Not tested against live Upstox.** I had no token. Logic is tested offline (12 tests, synthetic chain in Upstox's raw format). Field names in the MCX master (`underlying_key`, `underlying_symbol`, `weekly`, `expiry` in ms) follow Upstox's docs; run `--test` first.
* A "cheap vs model" option can be cheap for a reason (news, skew, thin market). It is a screening alert, not a guaranteed profit; the gap can also stay open or widen. Mind slippage and lot size.
* Fair value is an estimate; risk-free rate (6%) and expiry time (23:30 IST) are assumptions. Exchange holidays are not modelled; MCX evening-session hours vary (default 09:00-23:55).
* Analytics Token is valid one year and read-only; generate it in Upstox Developer Apps.

## Run it online on GitHub (free, no PC needed)
1. Create a **public** GitHub repo and upload the *contents* of this folder to the root. Keep the hidden `.github/workflows/mcx.yml`.
2. Upstox Developer Apps -> Analytics tab -> generate an **Analytics Token**.
3. Repo -> Settings -> Secrets and variables -> Actions -> New repository secret: `UPSTOX_ACCESS_TOKEN` (required), `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (optional, for phone alerts).
4. Settings -> Actions -> General -> Workflow permissions -> **Read and write**.
5. Actions tab -> "MCX option gap alerts" -> Run workflow -> **test**. Fix anything it reports, then run **run**.
After that it starts itself Mon-Fri in three jobs covering 09:00-23:55 IST. Open signals and `signals.csv` are kept on the `bot-state` branch.
Notes: the repo must be public for free minutes (a private repo's free quota is far below ~19 h/day); that also makes `signals.csv` public.
GitHub schedules can start minutes late or be skipped, and scheduled workflows are disabled after 60 days without repo activity, so this is for alerts, not tick-exact execution. To tune thresholds, add lines under `env:` in the workflow (e.g. `MIN_GAP_PCT: '10'`).
