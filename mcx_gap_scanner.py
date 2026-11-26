#!/usr/bin/env python3
"""MCX option fair-value GAP scanner  --  BUY side only.  Upstox read-only data API.

For every MCX option underlying (CRUDEOIL, GOLD, SILVER, NATURALGAS, COPPER ...):
  1. picks the current monthly expiry (rolls to the next one when <= ROLL_DAYS days remain),
  2. builds the option chain itself (Upstox's option-chain endpoint does NOT cover MCX),
  3. estimates a fair price for every CE/PE with two independent "old school" models
        a) put-call parity   : C - P = e^(-rT) * (F - K)        (model-free, no volatility needed)
        b) Black-76 smile    : volatility interpolated from NEIGHBOURING strikes (own IV excluded)
  4. alerts when an option trades CHEAP versus fair value by more than the cost-aware threshold.
It never places orders.  See README.md for assumptions and limits.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import random
import sys
import time
from collections import defaultdict, namedtuple
from dataclasses import dataclass, fields
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
API = "https://api.upstox.com"


# ----------------------------------------------------------------------------- config
@dataclass
class Config:
    # expiry selection
    roll_days: int = 5              # <= this many calendar days left -> use the NEXT expiry
    include_weekly: bool = False    # monthly series only by default
    underlyings: str = ""           # comma list, e.g. "CRUDEOIL,GOLD"; empty = every MCX option underlying
    strikes_each_side: int = 8      # scan ATM +/- N strikes
    # signal thresholds (see README "How big should the gap be?")
    min_gap_pct: float = 10.0       # (fair - LTP) / LTP  must be at least this
    min_net_edge_pct: float = 4.0   # (fair - ask - half spread - charges) / ask must be at least this
    min_net_edge_rs_lot: float = 300.0  # ... and worth at least this many rupees per lot
    min_models: int = 2             # how many independent fair-value models must agree
    # liquidity / sanity filters
    max_spread_pct: float = 10.0    # bid-ask spread of the option you would BUY
    ref_spread_pct: float = 15.0    # looser limit for options used only as reference
    min_oi: float = 10
    min_volume: float = 1
    min_premium: float = 5.0
    min_delta: float = 0.10         # ignore lottery tickets
    max_delta: float = 0.95
    # model inputs (assumptions, not fetched facts)
    risk_free: float = 0.06
    expiry_time: str = "23:30"      # IST time used for time-to-expiry on expiry day
    # cost assumptions -- check against Upstox's charges page and edit
    brokerage_per_order: float = 20.0
    other_charges_pct: float = 0.15   # exchange + CTT + GST etc, % of premium, round trip
    # alert behaviour
    stop_loss_pct: float = 0.0      # 0 = no stop alert (you did not ask for one)
    cooldown_sec: int = 600
    signal_ttl_hours: int = 24
    poll_sec: int = 60
    session_start: str = "09:00"
    session_end: str = "23:55"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            raw = os.getenv(f.name.upper())
            if raw is None or raw == "":
                continue
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                val = raw.strip().lower() in ("1", "true", "yes", "y")
            elif isinstance(cur, int):
                val = int(float(raw))
            elif isinstance(cur, float):
                val = float(raw)
            else:
                val = raw.strip()
            setattr(cfg, f.name, val)
        return cfg

    def wanted(self, symbol: str) -> bool:
        if not self.underlyings:
            return True
        return symbol.upper() in {s.strip().upper() for s in self.underlyings.split(",")}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def num(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def now_ist() -> datetime:
    return datetime.now(IST)


# ----------------------------------------------------------------------------- Black-76 maths
def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76(F: float, K: float, T: float, vol: float, side: str, r: float) -> float:
    """European option on a futures price F (MCX options are options on futures)."""
    df = math.exp(-r * max(T, 0.0))
    if T <= 0 or vol <= 0:
        return df * max(F - K if side == "CE" else K - F, 0.0)
    sd = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if side == "CE":
        return df * (F * _n(d1) - K * _n(d2))
    return df * (K * _n(-d2) - F * _n(-d1))


def implied_vol(price: float, F: float, K: float, T: float, side: str, r: float):
    """Black-76 implied volatility by bisection; None if the price has no valid vol."""
    if min(price, F, K, T) <= 0:
        return None
    df = math.exp(-r * T)
    intrinsic = df * max(F - K if side == "CE" else K - F, 0.0)
    upper = df * (F if side == "CE" else K)
    if price <= intrinsic + 1e-9 or price >= upper:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, mid, side, r) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def greeks(F: float, K: float, T: float, vol: float, side: str, r: float) -> dict:
    """delta, gamma, vega (per 1 vol point), theta (per calendar day), all per 1 unit of underlying."""
    df = math.exp(-r * T)
    sd = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    price = black76(F, K, T, vol, side, r)
    delta = df * (_n(d1) if side == "CE" else _n(d1) - 1.0)
    gamma = df * _pdf(d1) / (F * sd)
    vega = df * F * _pdf(d1) * math.sqrt(T) / 100.0
    theta = (r * price - df * F * _pdf(d1) * vol / (2.0 * math.sqrt(T))) / 365.0
    return dict(delta=delta, gamma=gamma, vega=vega, theta=theta)


# ----------------------------------------------------------------------------- instruments
@dataclass(frozen=True)
class Opt:
    key: str
    symbol: str
    tsym: str
    strike: float
    side: str
    expiry: date
    lot: int
    tick: float
    ukey: str
    weekly: bool


@dataclass(frozen=True)
class Fut:
    key: str
    symbol: str
    expiry: date


@dataclass
class Group:
    symbol: str
    expiry: date
    fkey: str
    opts: list
    F: float = 0.0


def _expiry_date(ms) -> date:
    return datetime.fromtimestamp(num(ms) / 1000.0, IST).date()


def parse_master(rows):
    """Split the Upstox MCX instrument master into option and future lists."""
    opts, futs = [], []
    for r in rows:
        if r.get("segment") != "MCX_FO" or not r.get("expiry"):
            continue
        kind = r.get("instrument_type")
        sym = (r.get("underlying_symbol") or r.get("name") or "").strip().upper()
        exp = _expiry_date(r["expiry"])
        if kind in ("CE", "PE"):
            strike = num(r.get("strike_price"))
            if strike <= 0 or not r.get("instrument_key"):
                continue
            opts.append(Opt(r["instrument_key"], sym, r.get("trading_symbol", ""), strike, kind, exp,
                            int(num(r.get("lot_size"), 1)) or 1, num(r.get("tick_size"), 0.05) or 0.05,
                            r.get("underlying_key") or "", bool(r.get("weekly"))))
        elif kind == "FUT":
            futs.append(Fut(r["instrument_key"], sym, exp))
    return opts, futs


def pick_expiry(expiries, today: date, roll_days: int):
    """Nearest expiry with MORE than roll_days calendar days left (so 4-5 days left -> next expiry)."""
    for e in sorted(x for x in set(expiries) if x >= today):
        if (e - today).days > roll_days:
            return e
    return None


def select_groups(opts, futs, today: date, cfg: Config):
    by_sym = defaultdict(list)
    for o in opts:
        if cfg.wanted(o.symbol):
            by_sym[o.symbol].append(o)
    futs_by_sym = defaultdict(list)
    for f in futs:
        futs_by_sym[f.symbol].append(f)
    groups = []
    for sym, lst in sorted(by_sym.items()):
        pool = lst if cfg.include_weekly else [o for o in lst if not o.weekly]
        exp = pick_expiry([o.expiry for o in pool], today, cfg.roll_days)
        if exp is None:
            continue
        by_fut = defaultdict(list)
        for o in (x for x in pool if x.expiry == exp):
            fkey = o.ukey
            if not fkey:  # fallback: first future that expires on/after the option
                cands = sorted((f for f in futs_by_sym.get(sym, []) if f.expiry >= exp), key=lambda f: f.expiry)
                fkey = cands[0].key if cands else ""
            if fkey:
                by_fut[fkey].append(o)
        for fkey, lst2 in by_fut.items():
            groups.append(Group(sym, exp, fkey, lst2))
    return groups


# ----------------------------------------------------------------------------- quotes
@dataclass
class Q:
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    oi: float = 0.0
    vol: float = 0.0
    ltt: float = 0.0   # last trade time, epoch seconds (0 = unknown)


def parse_quotes(data: dict) -> dict:
    """Full-market-quote response -> {instrument_key: Q}. Response keys look like 'MCX_FO:SYMBOL', so we
    index on the instrument_token field inside each entry."""
    out = {}
    for k, v in (data or {}).items():
        key = v.get("instrument_token") or k.replace(":", "|", 1)
        depth = v.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0] or {}
        sell = (depth.get("sell") or [{}])[0] or {}
        ltt = num(v.get("last_trade_time")) / 1000.0
        out[key] = Q(num(v.get("last_price")), num(buy.get("price")), num(sell.get("price")),
                     num(buy.get("quantity")), num(sell.get("quantity")),
                     num(v.get("oi")), num(v.get("volume")), ltt)
    return out


class Feed:
    """Base class: subclasses supply instruments() and raw_quotes()."""

    def instruments(self, today: date):
        raise NotImplementedError

    def raw_quotes(self, keys: list) -> dict:
        raise NotImplementedError

    def quotes(self, keys, chunk: int = 100) -> dict:
        keys = sorted(set(k for k in keys if k))
        out = {}
        for i in range(0, len(keys), chunk):
            out.update(parse_quotes(self.raw_quotes(keys[i:i + chunk])))
            if i + chunk < len(keys):
                time.sleep(0.25)
        return out

    def upstox_greeks(self, keys) -> dict:
        return {}


class UpstoxFeed(Feed):
    def __init__(self, state_dir: Path):
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Set UPSTOX_ACCESS_TOKEN (Upstox Analytics Token is enough: it is read-only)")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.dir = state_dir

    def get(self, path: str, params: dict | None = None):
        for attempt in range(5):
            try:
                r = self.s.get(API + path, params=params, timeout=25)
            except requests.RequestException:
                if attempt == 4:
                    raise RuntimeError("Upstox network error") from None
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(30, max(2 ** attempt, num(r.headers.get("Retry-After"), 1))))
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"Upstox HTTP {r.status_code}: token invalid/expired or lacks data access")
            if r.status_code != 200:
                raise RuntimeError(f"Upstox HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            if body.get("status") != "success":
                raise RuntimeError("Upstox returned an unsuccessful response")
            return body.get("data") or {}
        raise RuntimeError("Upstox retry limit")

    def instruments(self, today: date):
        cache = self.dir / f"MCX_{today.isoformat()}.json"
        if cache.exists():
            rows = json.loads(cache.read_text())
        else:
            r = requests.get(MASTER_URL, timeout=90)
            r.raise_for_status()
            try:
                rows = json.loads(gzip.decompress(r.content))
            except OSError:
                rows = r.json()
            rows = [x for x in rows if x.get("segment") == "MCX_FO"]
            for old in self.dir.glob("MCX_*.json"):
                old.unlink(missing_ok=True)
            cache.write_text(json.dumps(rows))
        return parse_master(rows)

    def raw_quotes(self, keys):
        return self.get("/v2/market-quote/quotes", {"instrument_key": ",".join(keys)})

    def upstox_greeks(self, keys) -> dict:
        """Best-effort cross-check only. Never raises; returns {} if MCX greeks are unavailable."""
        out = {}
        try:
            for i in range(0, len(keys), 50):
                data = self.get("/v3/market-quote/option-greek", {"instrument_key": ",".join(keys[i:i + 50])})
                for k, v in (data or {}).items():
                    key = v.get("instrument_token") or (k if "|" in k else None)
                    if key and num(v.get("iv")) > 0:
                        out[key] = dict(iv=num(v.get("iv")), delta=num(v.get("delta")),
                                        theta=num(v.get("theta")), vega=num(v.get("vega")))
        except Exception:
            return out
        return out


# ----------------------------------------------------------------------------- fair-value engine
Row = namedtuple("Row", "opt q")


def _mid(q: Q) -> float:
    return (q.bid + q.ask) / 2.0


def _spread_pct(q: Q) -> float:
    return 100.0 * (q.ask - q.bid) / _mid(q) if _mid(q) > 0 else 999.0


def ref_ok(q: Q, cfg: Config) -> bool:
    """May this quote be used as a REFERENCE (for the smile or for parity)?"""
    return (q.bid > 0 and q.ask >= q.bid and _mid(q) >= cfg.min_premium
            and _spread_pct(q) <= cfg.ref_spread_pct and q.oi >= cfg.min_oi)


def peer_iv(K: float, pts: dict):
    """Leave-one-out IV: OI/spread-weighted interpolation between the 2 nearest strikes on each side.
    The option's own strike is never used, so its own price cannot vouch for itself."""
    below = sorted((k for k in pts if k < K), reverse=True)[:2]
    above = sorted(k for k in pts if k > K)[:2]
    if not below or not above:
        return None  # no extrapolation into the wings

    def centroid(ks):
        w = [pts[k][1] for k in ks]
        tot = sum(w)
        return (sum(wi * k for wi, k in zip(w, ks)) / tot, sum(wi * pts[k][0] for wi, k in zip(w, ks)) / tot)

    kl, vl = centroid(below)
    kh, vh = centroid(above)
    return vl + (vh - vl) * (K - kl) / (kh - kl)


def evaluate(F: float, T: float, dte: int, rows: list, cfg: Config) -> list:
    """Fair value + gap for every option in one (underlying, expiry) chain. Returns a list of dicts."""
    r = cfg.risk_free
    df = math.exp(-r * T)
    by = {(x.opt.strike, x.opt.side): x for x in rows}
    strikes = sorted({x.opt.strike for x in rows})

    # volatility smile from the OTM option of each strike (ITM leg only if OTM is illiquid)
    pts = {}
    for K in strikes:
        otm = "CE" if K >= F else "PE"
        for side in (otm, "PE" if otm == "CE" else "CE"):
            x = by.get((K, side))
            if x and ref_ok(x.q, cfg):
                iv = implied_vol(_mid(x.q), F, K, T, side, r)
                if iv:
                    pts[K] = (iv, math.sqrt(max(x.q.oi, 1.0)) / max(_spread_pct(x.q), 0.5))
                    break

    out = []
    for x in rows:
        o, q = x.opt, x.q
        K, side = o.strike, o.side
        res = dict(symbol=o.symbol, tsym=o.tsym, key=o.key, side=side, strike=K, expiry=o.expiry.isoformat(),
                   dte=dte, F=F, lot=o.lot, tick=o.tick, ltp=q.ltp, bid=q.bid, ask=q.ask, oi=q.oi, vol=q.vol,
                   models={}, fair=None, gap=None, gap_pct=None, net_edge=None, net_edge_pct=None,
                   net_edge_lot=None, iv=None, delta=None, theta=None, theta_pct=None, vega=None,
                   spread_pct=None, confidence="", note="", status="")
        out.append(res)
        if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
            res["status"] = "NO_TWO_SIDED_QUOTE"
            continue
        mid = _mid(q)
        res["spread_pct"] = _spread_pct(q)
        if q.bid <= q.ltp <= q.ask:
            ref = q.ltp
        else:
            ref = mid
            res["note"] = "LTP outside bid/ask (stale) - using mid"
        res["ref"] = ref

        # ---- model 1: Black-76 with leave-one-out smile volatility
        models = {}
        iv_p = peer_iv(K, {k: v for k, v in pts.items() if k != K})
        if iv_p and iv_p > 0:
            models["smile"] = black76(F, K, T, iv_p, side, r)
        # ---- model 2: put-call parity from the opposite leg of the same strike
        oth = by.get((K, "PE" if side == "CE" else "CE"))
        if oth and ref_ok(oth.q, cfg):
            om = _mid(oth.q)
            p = om + df * (F - K) if side == "CE" else om + df * (K - F)
            models["parity"] = max(p, df * max(F - K if side == "CE" else K - F, 0.0))
        res["models"] = models
        if not models:
            res["status"] = "NO_MODEL"
            continue

        fair = sum(models.values()) / len(models)
        res["fair"] = fair
        res["gap"] = fair - ref
        res["gap_pct"] = 100.0 * (fair - ref) / ref
        charges = 2.0 * cfg.brokerage_per_order / o.lot + cfg.other_charges_pct / 100.0 * q.ask
        net = fair - q.ask - 0.5 * (q.ask - q.bid) - charges
        res["net_edge"], res["net_edge_pct"], res["net_edge_lot"] = net, 100.0 * net / q.ask, net * o.lot

        # ---- greeks (own Black-76; uses peer IV when available, else the option's own mid IV)
        iv_use = iv_p or implied_vol(mid, F, K, T, side, r)
        if iv_use:
            g = greeks(F, K, T, iv_use, side, r)
            res.update(iv=iv_use * 100.0, delta=g["delta"], theta=g["theta"], vega=g["vega"],
                       theta_pct=100.0 * abs(g["theta"]) / mid)

        # ---- decide (first failing rule is reported; useful for --chain view)
        confirm = all(100.0 * (m - ref) / ref >= cfg.min_gap_pct / 2.0 for m in models.values())
        checks = [
            ("MODELS<%d" % cfg.min_models, len(models) >= cfg.min_models),
            ("NOT_CHEAP", res["gap_pct"] >= cfg.min_gap_pct),
            ("MODELS_DISAGREE", confirm),
            ("PREMIUM_LOW", mid >= cfg.min_premium),
            ("SPREAD_WIDE", res["spread_pct"] <= cfg.max_spread_pct),
            ("OI_LOW", q.oi >= cfg.min_oi),
            ("VOL_LOW", q.vol >= cfg.min_volume),
            ("DELTA_BAND", res["delta"] is None or cfg.min_delta <= abs(res["delta"]) <= cfg.max_delta),
            ("EDGE_AFTER_COSTS<%.0f%%" % cfg.min_net_edge_pct, res["net_edge_pct"] >= cfg.min_net_edge_pct),
            ("EDGE_RS_LOT<%.0f" % cfg.min_net_edge_rs_lot, res["net_edge_lot"] >= cfg.min_net_edge_rs_lot),
        ]
        failed = [name for name, ok in checks if not ok]
        if failed:
            res["status"] = failed[0]
            continue
        res["status"] = "BUY"
        if len(models) == 2:
            disp = abs(models["smile"] - models["parity"]) / fair
            res["confidence"] = "HIGH" if disp <= 0.15 else "MEDIUM"
        else:
            res["confidence"] = "LOW (single model)"
    return out


# ----------------------------------------------------------------------------- alerts
def fmt_signal(r: dict) -> str:
    tick = r["tick"]
    fair_r = round(r["fair"] / tick) * tick if tick else r["fair"]
    m = " | ".join(f"{k} {v:,.2f}" for k, v in r["models"].items())
    lines = [
        f"BUY  {r['symbol']} {r['strike']:g} {r['side']}   exp {r['expiry']} ({r['dte']}d left)   [{r['confidence']}]",
        f"  LTP {r['ltp']:,.2f}  bid {r['bid']:,.2f} / ask {r['ask']:,.2f}   (spread {r['spread_pct']:.1f}%)",
        f"  FAIR {fair_r:,.2f}   <- {m}",
        f"  GAP  {r['gap']:+,.2f} per unit ({r['gap_pct']:+.1f}% vs LTP)   |   after spread+costs at ask: "
        f"{r['net_edge']:+,.2f} ({r['net_edge_pct']:+.1f}%) = Rs {r['net_edge_lot']:+,.0f} per lot (lot {r['lot']})",
        f"  Fut {r['F']:,.2f}  IV {r['iv'] or 0:.1f}%  delta {r['delta'] or 0:+.2f}  theta/day {r['theta'] or 0:+.2f} "
        f"({r['theta_pct'] or 0:.1f}% of premium)  OI {r['oi']:,.0f}  vol {r['vol']:,.0f}",
        f"  PLAN buy near ask {r['ask']:,.2f}  -> sell when bid reaches ~{fair_r:,.2f} (fair value frozen at alert time)",
    ]
    if r.get("note"):
        lines.append(f"  note: {r['note']}")
    if r.get("upstox_iv"):
        lines.append(f"  Upstox IV cross-check: {r['upstox_iv']:.1f}%")
    return "\n".join(lines)


class Notifier:
    def __init__(self, state_dir: Path, quiet: bool = False):
        self.tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.csv = state_dir / "signals.csv"
        self.quiet = quiet
        self.sent = []

    def send(self, text: str):
        self.sent.append(text)
        if not self.quiet:
            print("\n" + "=" * 78 + "\n" + now_ist().strftime("%Y-%m-%d %H:%M:%S IST") + "\n" + text, flush=True)
        if self.tok and self.chat:
            try:
                requests.post(f"https://api.telegram.org/bot{self.tok}/sendMessage",
                              json={"chat_id": self.chat, "text": text}, timeout=15)
            except requests.RequestException as e:
                print(f"[telegram failed: {e.__class__.__name__}]", file=sys.stderr)

    def log(self, r: dict, ts: datetime):
        new = not self.csv.exists()
        with self.csv.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "symbol", "tsym", "side", "strike", "expiry", "ltp", "bid", "ask", "fair",
                            "gap", "gap_pct", "net_edge_pct", "net_edge_lot", "models", "confidence"])
            w.writerow([ts.isoformat(timespec="seconds"), r["symbol"], r["tsym"], r["side"], r["strike"],
                        r["expiry"], r["ltp"], r["bid"], r["ask"], round(r["fair"], 3), round(r["gap"], 3),
                        round(r["gap_pct"], 2), round(r["net_edge_pct"], 2), round(r["net_edge_lot"], 0),
                        "+".join(r["models"]), r["confidence"]])


# ----------------------------------------------------------------------------- state + exit tracking
def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"open": {}, "last_alert": {}}


def save_state(path: Path, st: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def check_exits(st: dict, quotes: dict, cfg: Config, now: datetime, notifier: Notifier) -> None:
    for key, sig in list(st["open"].items()):
        age_h = (now - datetime.fromisoformat(sig["time"])).total_seconds() / 3600
        if date.fromisoformat(sig["expiry"]) < now.date() or age_h > cfg.signal_ttl_hours:
            del st["open"][key]
            continue
        q = quotes.get(key)
        if not q or q.bid <= 0:
            continue
        label = f"{sig['symbol']} {sig['strike']:g} {sig['side']} ({sig['expiry']})"
        if q.bid >= sig["target"]:
            pnl = (q.bid - sig["entry_ask"]) * sig["lot"]
            notifier.send(f"SELL / TARGET REACHED  {label}\n  bid {q.bid:,.2f} >= frozen fair {sig['target']:,.2f}"
                          f"   (entry ask was {sig['entry_ask']:,.2f}; ~Rs {pnl:+,.0f} per lot before charges)")
            del st["open"][key]
        elif cfg.stop_loss_pct > 0 and q.bid <= sig["entry_ask"] * (1 - cfg.stop_loss_pct / 100):
            pnl = (q.bid - sig["entry_ask"]) * sig["lot"]
            notifier.send(f"STOP LEVEL HIT  {label}\n  bid {q.bid:,.2f} vs entry ask {sig['entry_ask']:,.2f} "
                          f"(-{cfg.stop_loss_pct:.0f}%)   ~Rs {pnl:+,.0f} per lot")
            del st["open"][key]


# ----------------------------------------------------------------------------- one scan cycle
def expiry_T(expiry: date, now: datetime, cfg: Config) -> float:
    hh, mm = (int(x) for x in cfg.expiry_time.split(":"))
    end = datetime.combine(expiry, dtime(hh, mm), IST)
    return max((end - now).total_seconds(), 0.0) / (365.0 * 86400.0)


def scan_once(feed: Feed, cfg: Config, st: dict, notifier: Notifier, now: datetime, want_chain: str = ""):
    """One full pass over all MCX option underlyings. Returns (signals, all_results)."""
    opts, futs = feed.instruments(now.date())
    groups = select_groups(opts, futs, now.date(), cfg)
    if not groups:
        print("No MCX option groups found for the selected expiry rules.", file=sys.stderr)
        return [], []
    fq = feed.quotes([g.fkey for g in groups])
    live = []
    for g in groups:
        q = fq.get(g.fkey)
        if not q or q.ltp <= 0:
            print(f"[skip] {g.symbol} {g.expiry}: no futures price for {g.fkey}", file=sys.stderr)
            continue
        g.F = q.ltp
        strikes = sorted({o.strike for o in g.opts})
        i = strikes.index(min(strikes, key=lambda k: abs(k - g.F)))
        keep = set(strikes[max(0, i - cfg.strikes_each_side): i + cfg.strikes_each_side + 1])
        g.opts = [o for o in g.opts if o.strike in keep]
        live.append(g)
    want = [o.key for g in live for o in g.opts] + list(st["open"].keys())
    oq = feed.quotes(want)
    check_exits(st, oq, cfg, now, notifier)

    results, signals = [], []
    for g in live:
        rows = [Row(o, oq[o.key]) for o in g.opts if o.key in oq]
        if len(rows) < 4:
            continue
        res = evaluate(g.F, expiry_T(g.expiry, now, cfg), (g.expiry - now.date()).days, rows, cfg)
        results += res
        signals += [x for x in res if x["status"] == "BUY"]
    if signals:
        gk = feed.upstox_greeks([s["key"] for s in signals])
        for s in signals:
            if s["key"] in gk:
                s["upstox_iv"] = gk[s["key"]]["iv"]
    for s in sorted(signals, key=lambda x: -x["net_edge_lot"]):
        if s["key"] in st["open"]:
            continue
        last = st["last_alert"].get(s["key"])
        if last and (now - datetime.fromisoformat(last)).total_seconds() < cfg.cooldown_sec:
            continue
        notifier.send(fmt_signal(s))
        notifier.log(s, now)
        st["last_alert"][s["key"]] = now.isoformat()
        st["open"][s["key"]] = dict(symbol=s["symbol"], strike=s["strike"], side=s["side"], expiry=s["expiry"],
                                    entry_ask=s["ask"], target=s["fair"], lot=s["lot"], time=now.isoformat())
    if want_chain:
        print_chain(results, want_chain)
    return signals, results


def print_chain(results: list, symbol: str) -> None:
    rows = [r for r in results if r["symbol"] == symbol.upper()]
    if not rows:
        print(f"(no chain data for {symbol})")
        return
    print(f"\n{symbol.upper()}  expiry {rows[0]['expiry']}  futures {rows[0]['F']:,.2f}")
    print(f"{'strike':>9} {'side':>4} {'LTP':>9} {'bid':>9} {'ask':>9} {'fair':>9} {'gap%':>7} {'net%':>7} {'models':>12}  status")
    for r in sorted(rows, key=lambda x: (x["strike"], x["side"])):
        f = lambda v, p=2: "-" if v is None else f"{v:,.{p}f}"
        print(f"{r['strike']:>9g} {r['side']:>4} {f(r['ltp']):>9} {f(r['bid']):>9} {f(r['ask']):>9} {f(r['fair']):>9} "
              f"{f(r['gap_pct'], 1):>7} {f(r['net_edge_pct'], 1):>7} {'+'.join(r['models']) or '-':>12}  {r['status']}")


def in_session(now: datetime, cfg: Config) -> bool:
    if now.weekday() >= 5:
        return False
    s, e = (dtime(*map(int, x.split(":"))) for x in (cfg.session_start, cfg.session_end))
    return s <= now.time() <= e


# ----------------------------------------------------------------------------- offline demo feed
class DemoFeed(Feed):
    """Synthetic MCX chain in the SAME raw format as Upstox, so the real parsing code is exercised.
    Prices are generated from Black-76 with a smile + noise; a few mispricings are injected."""

    UNIVERSE = {  # symbol: (futures, strike step, lot, tick, base vol, [days to each expiry])
        "CRUDEOIL": (7500.0, 50.0, 100, 0.1, 0.34, [22, 52]),
        "GOLD": (125000.0, 1000.0, 100, 1.0, 0.16, [28, 88]),
        "NATURALGAS": (300.0, 5.0, 1250, 0.05, 0.62, [3, 33]),   # 3 days left -> must roll to the 33d expiry
    }

    def __init__(self, today: date | None = None, seed: int = 7, inject: bool = True):
        self.today = today or now_ist().date()
        self.rng = random.Random(seed)
        self.rows, self.book = [], {}
        n = 100000
        for sym, (F, step, lot, tick, v0, days) in self.UNIVERSE.items():
            for di, d in enumerate(days):
                exp = self.today + timedelta(days=d)
                fkey = f"MCX_FO|{sym}FUT{di}"
                self.rows.append(dict(segment="MCX_FO", instrument_type="FUT", instrument_key=fkey, underlying_symbol=sym,
                                      expiry=self._ms(exp + timedelta(days=5)), lot_size=lot, tick_size=tick))
                self.book[fkey] = dict(symbol=sym, last_price=F, depth={"buy": [{"price": F, "quantity": 1}],
                                                                       "sell": [{"price": F, "quantity": 1}]}, oi=1e5, volume=1e5)
                T = d / 365.0
                for j in range(-14, 15):
                    K = F + j * step
                    if K <= 0:
                        continue
                    iv = v0 * (1 + 0.8 * math.log(K / F) ** 2 * 40 - 0.5 * math.log(K / F))
                    for side in ("CE", "PE"):
                        n += 1
                        key = f"MCX_FO|{n}"
                        self.rows.append(dict(segment="MCX_FO", instrument_type=side, instrument_key=key, underlying_symbol=sym,
                                              underlying_key=fkey, expiry=self._ms(exp), strike_price=K, lot_size=lot, tick_size=tick,
                                              weekly=False, trading_symbol=f"{sym} {K:g} {side} {exp:%d %b %y}".upper()))
                        p = max(black76(F, K, T, iv, side, 0.06), tick * 2)
                        sp = min(0.02 + 0.004 * abs(j), 0.09)
                        ltp = p * (1 + self.rng.gauss(0, 0.008))
                        self.book[key] = self._q(sym, key, tick, ltp, p * (1 - sp / 2), p * (1 + sp / 2))
        if inject:
            self._inject()

    @staticmethod
    def _ms(d: date) -> int:
        return int(datetime.combine(d, dtime(23, 59, 59), IST).timestamp() * 1000)

    def _q(self, sym, key, tick, ltp, bid, ask):
        rd = lambda x: max(round(x / tick) * tick, tick)
        return dict(symbol=sym, last_price=rd(ltp), oi=self.rng.randint(300, 20000), volume=self.rng.randint(30, 3000),
                    last_trade_time=str(int(time.time() * 1000)),
                    depth={"buy": [{"price": rd(bid), "quantity": 100}], "sell": [{"price": rd(ask), "quantity": 100}]})

    def _find(self, sym, di, strike, side):
        exp = self._ms(self.today + timedelta(days=self.UNIVERSE[sym][5][di]))
        for r in self.rows:
            if (r.get("underlying_symbol") == sym and r["instrument_type"] == side and r.get("strike_price") == strike
                    and r["expiry"] == exp):
                return r["instrument_key"]

    def _inject(self):
        # 1) the user's example: Crude 7500 PE trades ~12% below its true value  -> must alert
        b = self.book[self._find("CRUDEOIL", 0, 7500.0, "PE")]
        for k in ("last_price",):
            b[k] *= 0.88
        for s in ("buy", "sell"):
            b["depth"][s][0]["price"] *= 0.88
        # 2) stale LTP trap: only LTP is low, bid/ask are fair                    -> must NOT alert
        self.book[self._find("CRUDEOIL", 0, 7300.0, "CE")]["last_price"] *= 0.8
        # 3) cheap-looking but 30% wide market                                    -> must NOT alert
        b = self.book[self._find("CRUDEOIL", 0, 7700.0, "CE")]
        b["last_price"] *= 0.85
        b["depth"]["buy"][0]["price"] *= 0.80
        b["depth"]["sell"][0]["price"] *= 1.10
        # 4) cheap option in the 3-day expiry that we must skip (rolled)          -> must NOT alert
        b = self.book[self._find("NATURALGAS", 0, 300.0, "CE")]
        b["last_price"] *= 0.8
        for s in ("buy", "sell"):
            b["depth"][s][0]["price"] *= 0.8

    def instruments(self, today):
        return parse_master(self.rows)

    def raw_quotes(self, keys):
        out = {}
        for k in keys:
            if k in self.book:
                v = dict(self.book[k])
                v["instrument_token"] = k
                out[f"MCX_FO:{v['symbol']}{k[-5:]}"] = v
        return out


# ----------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="MCX option fair-value gap scanner (BUY side only, Upstox data)")
    ap.add_argument("--demo", action="store_true", help="offline run on synthetic data (no token needed)")
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--test", action="store_true", help="check Upstox token, instrument master, one quote, Telegram")
    ap.add_argument("--chain", default="", metavar="SYMBOL", help="print the full fair-value table for e.g. CRUDEOIL")
    ap.add_argument("--ignore-hours", action="store_true", help="scan even outside MCX session hours")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    state_dir = Path(os.getenv("STATE_DIR", str(ROOT / "state")))
    state_dir.mkdir(parents=True, exist_ok=True)
    feed = DemoFeed() if args.demo else UpstoxFeed(state_dir)
    notifier = Notifier(state_dir)
    state_path = state_dir / ("state_demo.json" if args.demo else "state.json")
    if args.demo:
        state_path.unlink(missing_ok=True)

    if args.test:
        opts, futs = feed.instruments(now_ist().date())
        groups = select_groups(opts, futs, now_ist().date(), cfg)
        print(f"Instrument master OK: {len(opts)} MCX option contracts, {len(futs)} futures, "
              f"{len(groups)} underlying/expiry groups selected:")
        for g in groups:
            print(f"  {g.symbol:<14} expiry {g.expiry}  ({(g.expiry - now_ist().date()).days}d)  {len(g.opts)} contracts")
        if groups:
            q = feed.quotes([groups[0].fkey]).get(groups[0].fkey)
            print(f"Quote check {groups[0].fkey}: {q}")
        notifier.send("MCX gap scanner connectivity test OK")
        return 0

    st = load_state(state_path)
    print(f"MCX gap scanner started ({'DEMO' if args.demo else 'LIVE data'}). roll_days={cfg.roll_days} "
          f"min_gap={cfg.min_gap_pct}% min_net_edge={cfg.min_net_edge_pct}% (after spread+costs)", flush=True)
    while True:
        now = now_ist()
        if args.demo or args.ignore_hours or in_session(now, cfg):
            try:
                signals, results = scan_once(feed, cfg, st, notifier, now, args.chain)
                save_state(state_path, st)
                print(f"[{now:%H:%M:%S}] scanned {len(results)} options, {len(signals)} qualifying, "
                      f"{len(st['open'])} open signals", flush=True)
            except Exception as e:  # keep the loop alive on transient API errors
                print(f"[{now:%H:%M:%S}] scan failed: {e}", file=sys.stderr, flush=True)
        else:
            print(f"[{now:%H:%M}] outside MCX session; waiting", flush=True)
        if args.once or args.demo:
            return 0
        time.sleep(cfg.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
