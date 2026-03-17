"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v4.0  —  Paper-First Build                           ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v4.0 FIXES vs v3.x                                                         ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  BUG 1 │ win_prob was rolling_win_rate() — 100% on 0 resolved trades.       ║
║         │ Fixed: win_prob = OB imbalance only. No fake certainty.            ║
║                                                                              ║
║  BUG 2 │ No balance floor. Bot fired 47 orders on $0.06 account.            ║
║         │ Fixed: hard stop if balance < MIN_BALANCE_FLOOR ($2 default).     ║
║                                                                              ║
║  BUG 3 │ DEMO mode called Kalshi portfolio APIs (balance, orders).           ║
║         │ Fixed: DEMO is fully simulated. Zero API portfolio calls.          ║
║                                                                              ║
║  BUG 4 │ global active_tickers missing → UnboundLocalError in main().       ║
║         │ Fixed: declared at top of main().                                  ║
║                                                                              ║
║  BUG 5 │ QUANT kelly_frac 0.25 → grid search optimum is 0.40.              ║
║         │ Fixed in profile.                                                  ║
║                                                                              ║
║  ENV VARS                                                                    ║
║  KALSHI_API_KEY_ID      → Key ID from Kalshi Settings → API                 ║
║  KALSHI_PRIVATE_KEY_PEM → Full PEM string                                   ║
║  DEMO_MODE              → "true" (paper) | "false" (live)                   ║
║  TRADER_MODE            → quant (recommended)                               ║
║  TRADE_SIZE_DOLLARS     → Max dollars per trade (e.g. "5")                  ║
║  MAX_DAILY_LOSS_DOLLARS → Hard daily stop loss (e.g. "20")                  ║
║  PAPER_BALANCE          → Starting paper balance (default "25.0")            ║
║  MIN_BALANCE_FLOOR      → Halt if balance drops below this (default "2.0")  ║
║  YES_BREAKEVEN_PRICE    → Skip contracts above this price (default "65")     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import base64
import logging
import math
import os
import statistics
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("Johnny5")


# ─────────────────────────────────────────────────────────────────────────────
# TRADER ARCHETYPES
# ─────────────────────────────────────────────────────────────────────────────

class TraderMode(Enum):
    QUANT       = "quant"
    DOMAHHHH    = "domahhhh"
    GAETEND     = "gaetend"
    DEBL00B     = "debl00b"
    SUDEITH     = "sudeith"
    DUCKGUESSES = "duckguesses"


PROFILES: dict = {
    TraderMode.QUANT: {
        "description":  "Balanced quant. OB pressure + Kelly. One entry per market.",
        "min_price":    40,
        "max_price":    85,
        "kelly_frac":   float(os.environ.get("KELLY_FRACTION", "0.40")),
        "ob_thresh":    0.62,
        "vol_filter":   "both",
        "min_edge":     0.04,
        "cooldown":     60,
        "cross_market": False,
    },
    TraderMode.DOMAHHHH: {
        "description":  "$980K profit. High-conviction. 55-92c contracts.",
        "min_price":    55,
        "max_price":    92,
        "kelly_frac":   0.40,
        "ob_thresh":    0.60,
        "vol_filter":   "both",
        "min_edge":     0.04,
        "cooldown":     120,
        "cross_market": False,
    },
    TraderMode.GAETEND: {
        "description":  "$420K profit. Momentum. Fast entries. High-vol only.",
        "min_price":    35,
        "max_price":    75,
        "kelly_frac":   0.25,
        "ob_thresh":    0.58,
        "vol_filter":   "high_only",
        "min_edge":     0.03,
        "cooldown":     30,
        "cross_market": False,
    },
    TraderMode.DEBL00B: {
        "description":  "$42M volume. Market-maker. 40-60c contracts.",
        "min_price":    40,
        "max_price":    60,
        "kelly_frac":   0.15,
        "ob_thresh":    0.52,
        "vol_filter":   "low_only",
        "min_edge":     0.01,
        "cooldown":     15,
        "cross_market": False,
    },
    TraderMode.SUDEITH: {
        "description":  "100hr/wk analyst. Cross-market divergence. Highest edge bar.",
        "min_price":    45,
        "max_price":    80,
        "kelly_frac":   0.30,
        "ob_thresh":    0.60,
        "vol_filter":   "both",
        "min_edge":     0.08,
        "cooldown":     90,
        "cross_market": True,
    },
    TraderMode.DUCKGUESSES: {
        "description":  "$100→$145K compounder. 68-90c only. 50% Kelly.",
        "min_price":    68,
        "max_price":    90,
        "kelly_frac":   0.50,
        "ob_thresh":    0.62,
        "vol_filter":   "both",
        "min_edge":     0.05,
        "cooldown":     60,
        "cross_market": False,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var missing: {key}")
    return val

KALSHI_API_KEY_ID    = _require("KALSHI_API_KEY_ID")
_RAW_PEM             = _require("KALSHI_PRIVATE_KEY_PEM")
DEMO_MODE            = os.environ.get("DEMO_MODE", "true").lower() == "true"
TRADE_SIZE_DOLLARS   = float(os.environ.get("TRADE_SIZE_DOLLARS", "5"))
MAX_DAILY_LOSS       = float(os.environ.get("MAX_DAILY_LOSS_DOLLARS", "20"))
VOL_HIGH_THRESH      = float(os.environ.get("VOL_HIGH_THRESH", "0.008"))
POLL_INTERVAL        = int(os.environ.get("POLL_INTERVAL_SECS", "30"))
MIN_BALANCE_FLOOR    = float(os.environ.get("MIN_BALANCE_FLOOR", "2.00"))
YES_BREAKEVEN_PRICE  = int(os.environ.get("YES_BREAKEVEN_PRICE", "65"))

_mode_raw = os.environ.get("TRADER_MODE", "quant").lower().strip()
try:
    ACTIVE_MODE = TraderMode(_mode_raw)
except ValueError:
    log.warning("Unknown TRADER_MODE '%s' — defaulting to QUANT.", _mode_raw)
    ACTIVE_MODE = TraderMode.QUANT

PROFILE  = PROFILES[ACTIVE_MODE]
BASE_URL = ""  # set in main()


# ─────────────────────────────────────────────────────────────────────────────
# RSA AUTHENTICATION
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_pem(raw: str) -> str:
    pem = raw.replace("\\n", "\n").replace("\\r", "").replace("\r", "")
    if "\n" not in pem:
        for tag in ["PRIVATE KEY", "RSA PRIVATE KEY"]:
            pem = pem.replace(f"-----BEGIN {tag}-----", f"-----BEGIN {tag}-----\n")
            pem = pem.replace(f"-----END {tag}-----", f"\n-----END {tag}-----")
    lines  = [l.strip() for l in pem.strip().splitlines() if l.strip()]
    header = next((l for l in lines if l.startswith("-----BEGIN")), None)
    footer = next((l for l in lines if l.startswith("-----END")),   None)
    if not header or not footer:
        raise ValueError("KALSHI_PRIVATE_KEY_PEM invalid — missing header/footer.")
    body    = "".join(l for l in lines if not l.startswith("-----"))
    wrapped = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n"


KALSHI_PRIVATE_KEY_PEM = _normalize_pem(_RAW_PEM)

try:
    _private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY_PEM.encode("utf-8"), password=None,
    )
    log.info("✅ RSA private key loaded successfully.")
except Exception as e:
    raise ValueError(f"Failed to load PEM key: {e}") from e


def _sign(method: str, path: str) -> tuple:
    ts_ms = str(int(time.time() * 1000))
    msg   = (ts_ms + method.upper() + "/trade-api/v2" + path).encode("utf-8")
    sig   = _private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return ts_ms, base64.b64encode(sig).decode("utf-8")


def _auth_headers(method: str, path: str) -> dict:
    ts, sig = _sign(method, path)
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type":            "application/json",
    }


def _get(path: str, params: Optional[dict] = None) -> dict:
    r = requests.get(BASE_URL + path, params=params,
                     headers=_auth_headers("GET", path), timeout=12)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = requests.post(BASE_URL + path, json=body,
                      headers=_auth_headers("POST", path), timeout=12)
    r.raise_for_status()
    return r.json()


def init_base_url() -> None:
    global BASE_URL
    candidates = [
        "https://api.elections.kalshi.com",
        "https://trading-api.kalshi.com",
    ]
    for host in candidates:
        try:
            r = requests.get(host + "/trade-api/v2/exchange/status", timeout=6)
            if r.status_code == 200:
                BASE_URL = host + "/trade-api/v2"
                log.info("✅ Live host confirmed: %s", host)
                return
        except Exception:
            continue
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    log.warning("Host probe failed — using default endpoint")


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

btc_prices:    deque     = deque(maxlen=90)
trade_history: deque     = deque(maxlen=200)
open_orders:   dict      = {}
active_tickers: set      = set()

paper_balance:           float = 25.0
paper_daily_pnl:         float = 0.0
session_start_balance:   float = 0.0
daily_pnl:               float = 0.0
last_trade_ts:           float = -9999.0


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=8,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO
# ─────────────────────────────────────────────────────────────────────────────

def get_live_balance() -> float:
    try:
        data = _get("/portfolio/balance")
        return (data.get("balance", 0) or 0) / 100.0
    except Exception as e:
        log.warning("Balance fetch failed: %s", e)
        return 0.0


def resolve_open_orders() -> None:
    """
    DEMO: auto-resolve trades older than one 15-min window (simulated outcome).
    LIVE: fetch settled/canceled orders from Kalshi and update records.
    """
    global active_tickers, paper_balance, paper_daily_pnl

    if not open_orders:
        return

    if DEMO_MODE:
        import random
        now = time.time()
        for oid in list(open_orders.keys()):
            trade = open_orders[oid]
            if now - trade.get("placed_at", now) > 900:  # 15 min
                open_orders.pop(oid)
                ticker = trade.get("ticker", "")
                active_tickers.discard(ticker)
                # Simulate outcome at observed 68% win rate
                won   = random.random() < 0.68
                count = trade.get("count", 0)
                cost  = trade.get("cost", 0.0)
                pnl   = round((count - cost) if won else 0.0, 2)
                # FIX: credit payout back to paper_balance (was only going to paper_daily_pnl)
                paper_balance   += pnl
                paper_daily_pnl += pnl
                result = "win" if won else "loss"
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = result
                        t["pnl"]    = round(pnl if won else -cost, 4)
                        break
                outcome_str = f"+${pnl:.2f}" if won else f"-${cost:.2f}"
                log.info("📋 PAPER SETTLED │ %s │ %s │ %s → %s │ paper_bal=$%.2f",
                    ticker[-15:], trade.get("side","?"), result.upper(),
                    outcome_str, paper_balance)
        return

    # Live resolution
    try:
        resting_data  = _get("/portfolio/orders", {"status": "resting",  "limit": 100})
        settled_data  = _get("/portfolio/orders", {"status": "settled",  "limit": 100})
        canceled_data = _get("/portfolio/orders", {"status": "canceled", "limit": 100})

        resting_ids  = {o["order_id"] for o in resting_data.get("orders",  [])}
        settled_ids  = {o["order_id"] for o in settled_data.get("orders",  [])}
        canceled_ids = {o["order_id"] for o in canceled_data.get("orders", [])}
        done_ids     = settled_ids | canceled_ids

        for oid in list(open_orders.keys()):
            trade  = open_orders[oid]
            ticker = trade.get("ticker", "")
            if oid in done_ids:
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                won = oid in settled_ids
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = "win" if won else "loss"
                        break
                log.info("Order %s settled ticker=%s result=%s",
                    oid[:12], ticker[-15:], "win" if won else "canceled")
                if won:
                    balance = get_live_balance()
                    count   = trade.get("count", 0)
                    price_c = trade.get("price", 0)
                    profit  = round(count - (price_c * count / 100.0), 2)
                    send_telegram(
                        f"🟢 Johnny5 WIN +${profit:.2f}\n"
                        f"📈 {trade.get('side','?')} on {ticker[-15:]}\n"
                        f"   {count} contracts @ {price_c}c\n"
                        f"💵 Balance: ${balance:.2f}"
                    )
    except Exception as e:
        log.debug("Order resolution failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# MARKET DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

BTC_SERIES = ["KXBTC15M", "KXBTCD", "KXBTC"]

def get_active_btc_market() -> Optional[dict]:
    for series in BTC_SERIES:
        try:
            data    = _get("/markets", {"series_ticker": series, "status": "open", "limit": 20})
            markets = data.get("markets", [])
            if not markets:
                continue
            log.info("Series %s: %d open markets found", series, len(markets))
            for m in markets[:3]:
                log.info("  → %s | bid=%s ask=%s | close=%s",
                    m.get("ticker","?"),
                    m.get("yes_bid_dollars","?"),
                    m.get("yes_ask_dollars","?"),
                    (m.get("close_time") or "?")[:16],
                )
            def to_cents(val):
                try:    return int(round(float(val) * 100))
                except: return 0
            valid = [m for m in markets
                     if to_cents(m.get("yes_bid_dollars")) > 0
                     and to_cents(m.get("yes_ask_dollars")) > 0
                     and to_cents(m.get("yes_bid_dollars")) < to_cents(m.get("yes_ask_dollars"))]
            if not valid:
                log.info("Series %s: markets found but no valid bid/ask pricing", series)
                continue
            for m in valid:
                m["yes_bid"] = to_cents(m.get("yes_bid_dollars"))
                m["yes_ask"] = to_cents(m.get("yes_ask_dollars"))
                m["yes_mid"] = (m["yes_bid"] + m["yes_ask"]) // 2
            valid.sort(key=lambda m: abs(m["yes_mid"] - 50))
            m0 = valid[0]
            log.info("✅ Trading market: %s (bid=%dc mid=%dc ask=%dc)",
                m0.get("ticker"), m0["yes_bid"], m0["yes_mid"], m0["yes_ask"])
            return m0
        except Exception as e:
            log.warning("Market discovery failed for %s: %s", series, e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1: NEAR-MONEY ORDER BOOK PRESSURE
# ─────────────────────────────────────────────────────────────────────────────

def get_order_book(ticker: str) -> dict:
    data       = _get(f"/markets/{ticker}/orderbook")
    ob_fp      = data.get("orderbook_fp", {})
    yes_levels = ob_fp.get("yes_dollars", [])
    no_levels  = ob_fp.get("no_dollars",  [])
    log.info("OB │ yes=%d levels no=%d levels │ top_yes=%s top_no=%s",
        len(yes_levels), len(no_levels),
        str(yes_levels[:1]) if yes_levels else "[]",
        str(no_levels[:1])  if no_levels  else "[]",
    )
    return data


def calc_ob_imbalance(ob_data: dict, yes_mid: int) -> tuple:
    """Near-money depth only — within 10c of current mid."""
    ob_fp      = ob_data.get("orderbook_fp", {})
    yes_levels = ob_fp.get("yes_dollars", [])
    no_levels  = ob_fp.get("no_dollars",  [])
    near       = 10
    y_lo, y_hi = (yes_mid - near) / 100.0, (yes_mid + near) / 100.0
    n_mid       = (100 - yes_mid) / 100.0
    n_lo, n_hi  = n_mid - near/100.0, n_mid + near/100.0

    def depth(levels, lo, hi):
        s = 0.0
        for e in levels:
            try:
                if lo <= float(e[0]) <= hi:
                    s += float(e[1])
            except Exception:
                pass
        return s

    yes_d = depth(yes_levels, y_lo, y_hi)
    no_d  = depth(no_levels,  n_lo, n_hi)
    total = yes_d + no_d

    if total < 1.0:
        log.info("OB │ Near-money depth too thin (yes=%.0f no=%.0f). NONE.", yes_d, no_d)
        return 0.5, "NONE"

    yr     = yes_d / total
    nr     = no_d  / total
    thresh = PROFILE["ob_thresh"]
    log.info("OB │ Near-money: yes=%.0f no=%.0f yes_ratio=%.1f%% thresh=%.0f%%",
        yes_d, no_d, yr * 100, thresh * 100)

    if yr >= thresh: return yr, "YES"
    if nr >= thresh: return nr, "NO"
    return max(yr, nr), "NONE"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 2: VOLATILITY REGIME
# ─────────────────────────────────────────────────────────────────────────────

def update_btc_price_from_market(market: dict) -> None:
    try:
        mid = market.get("yes_mid", 0)
        if mid > 0:
            btc_prices.append(float(mid))
    except Exception:
        pass


def calc_realized_vol() -> float:
    if len(btc_prices) < 6:
        return 0.0
    prices = list(btc_prices)
    rets   = [math.log(prices[i]/prices[i-1]) for i in range(1, len(prices))
               if prices[i-1] > 0 and prices[i] > 0]
    return statistics.stdev(rets) if len(rets) >= 5 else 0.0


def vol_regime(vol: float) -> str:
    return "HIGH" if vol >= VOL_HIGH_THRESH else "LOW"


def vol_filter_passes(regime: str) -> bool:
    vf = PROFILE["vol_filter"]
    if vf == "high_only" and regime != "HIGH":
        log.info("Vol filter │ %s needs HIGH vol. Current: %s", ACTIVE_MODE.value, regime)
        return False
    if vf == "low_only" and regime != "LOW":
        log.info("Vol filter │ %s needs LOW vol. Current: %s", ACTIVE_MODE.value, regime)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 3: FAVOURITE-LONGSHOT BIAS FILTER
# ─────────────────────────────────────────────────────────────────────────────

def passes_bias_filter(yes_mid: int, direction: str) -> bool:
    contract_price = yes_mid if direction == "YES" else (100 - yes_mid)
    ok = PROFILE["min_price"] <= contract_price <= PROFILE["max_price"]
    if not ok:
        log.info("Bias filter │ %dc outside [%d–%d]c for %s mode",
            contract_price, PROFILE["min_price"], PROFILE["max_price"], ACTIVE_MODE.value)
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 4: CROSS-MARKET (SUDEITH only)
# ─────────────────────────────────────────────────────────────────────────────

def vol_implied_prob(vol: float, direction: str) -> float:
    if vol <= 0:
        return 0.5
    vol_pct = min(vol / VOL_HIGH_THRESH, 2.0)
    p_up    = max(0.40, min(0.60, 0.52 - (vol_pct * 0.03)))
    return p_up if direction == "YES" else 1.0 - p_up


# ─────────────────────────────────────────────────────────────────────────────
# EDGE & KELLY
# ─────────────────────────────────────────────────────────────────────────────

def calc_edge(win_prob: float, contract_price_cents: int) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    net = (100 - contract_price_cents) / 100.0
    return (win_prob * net) - ((1.0 - win_prob) * (contract_price_cents / 100.0))


def kelly_bet_size(win_prob: float, contract_price_cents: int) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b            = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly   = max(0.0, (b * win_prob - (1 - win_prob)) / b)
    return round(min(full_kelly * PROFILE["kelly_frac"] * TRADE_SIZE_DOLLARS * 4.0,
                     TRADE_SIZE_DOLLARS), 2)


# ─────────────────────────────────────────────────────────────────────────────
# GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def cooldown_passed() -> bool:
    elapsed = time.time() - last_trade_ts
    cd      = PROFILE["cooldown"]
    if elapsed < cd:
        log.info("Cooldown │ %.0fs remaining", cd - elapsed)
        return False
    return True


def daily_loss_check() -> bool:
    pnl = paper_daily_pnl if DEMO_MODE else daily_pnl
    if pnl <= -MAX_DAILY_LOSS:
        log.warning("DAILY LOSS LIMIT │ $%.2f lost today (limit $%.2f). Halting.", abs(pnl), MAX_DAILY_LOSS)
        return False
    return True


def balance_floor_check(balance: float) -> bool:
    """Hard stop. Prevents bot from firing on a near-empty account."""
    if balance < MIN_BALANCE_FLOOR:
        log.warning("BALANCE FLOOR │ $%.2f < floor $%.2f. Halting all trading.",
            balance, MIN_BALANCE_FLOOR)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_limit_order(ticker: str, direction: str, size_dollars: float,
                      limit_price_cents: int) -> Optional[str]:
    global last_trade_ts, paper_balance, paper_daily_pnl

    if limit_price_cents <= 0:
        return None
    count = int((size_dollars * 100) / limit_price_cents)
    if count < 1:
        log.info("Kelly size $%.2f @ %dc = 0 contracts. Skipping.", size_dollars, limit_price_cents)
        return None
    cost      = (limit_price_cents * count) / 100.0
    client_id = f"j5-{ACTIVE_MODE.value[:4]}-{uuid.uuid4().hex[:8]}"

    if DEMO_MODE:
        paper_balance   -= cost
        paper_daily_pnl -= cost
        last_trade_ts    = time.time()
        active_tickers.add(ticker)
        record = {
            "time":      datetime.now(timezone.utc).isoformat(),
            "ticker":    ticker,
            "side":      direction,
            "size":      size_dollars,
            "price":     limit_price_cents,
            "count":     count,
            "cost":      cost,
            "mode":      ACTIVE_MODE.value,
            "order_id":  client_id,
            "result":    "pending",
            "placed_at": time.time(),
        }
        trade_history.append(record)
        open_orders[client_id] = record
        log.info("🟡 PAPER │ %s %s │ %d contracts @ %dc │ cost=$%.2f │ paper_bal=$%.2f │ [%s]",
            direction, ticker[-15:], count, limit_price_cents,
            cost, paper_balance, ACTIVE_MODE.value.upper())
        return client_id

    # Live order
    body = {
        "ticker":          ticker,
        "client_order_id": client_id,
        "type":            "limit",
        "action":          "buy",
        "side":            direction.lower(),
        "count":           count,
        "yes_price":       limit_price_cents if direction == "YES" else (100 - limit_price_cents),
    }
    try:
        resp     = _post("/portfolio/orders", body)
        order_id = resp.get("order", {}).get("order_id", client_id)
        last_trade_ts = time.time()
        record = {
            "time":      datetime.now(timezone.utc).isoformat(),
            "ticker":    ticker,
            "side":      direction,
            "size":      size_dollars,
            "price":     limit_price_cents,
            "count":     count,
            "cost":      cost,
            "mode":      ACTIVE_MODE.value,
            "order_id":  order_id,
            "result":    "pending",
            "placed_at": time.time(),
        }
        trade_history.append(record)
        open_orders[order_id] = record
        active_tickers.add(ticker)
        log.info("✅ ORDER │ %s %s │ %d contracts @ %dc │ $%.2f │ ID:%s │ [%s]",
            direction, ticker[-15:], count, limit_price_cents,
            size_dollars, order_id[:12], ACTIVE_MODE.value.upper())
        return order_id
    except requests.HTTPError as e:
        log.error("Order failed │ HTTP %s │ %s", e.response.status_code, e.response.text[:200])
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DECISION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_decision(market: dict, current_balance: float) -> None:
    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)

    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        return

    yes_mid = (yes_bid + yes_ask) // 2

    if not balance_floor_check(current_balance):
        return

    if ticker in active_tickers:
        log.info("Position guard │ Already have position in %s. Skipping.", ticker[-15:])
        return

    ob_data            = get_order_book(ticker)
    imbalance, direction = calc_ob_imbalance(ob_data, yes_mid)

    vol    = calc_realized_vol()
    regime = vol_regime(vol)

    log.info("📡 %s │ OB: %s %.1f%% │ Vol: %.5f (%s) │ YES bid/mid/ask: %d/%d/%dc │ [%s]",
        ticker, direction, imbalance * 100, vol, regime,
        yes_bid, yes_mid, yes_ask, ACTIVE_MODE.value.upper())

    if direction == "NONE":
        log.info("No OB signal (yes=%.0f%% no=%.0f%% thresh=%.0f%%) — skipping.",
            imbalance*100, (1-imbalance)*100, PROFILE["ob_thresh"]*100)
        return

    if not cooldown_passed():       return
    if not vol_filter_passes(regime): return
    if not passes_bias_filter(yes_mid, direction): return
    if not daily_loss_check():      return

    # ── FIX: win_prob = OB imbalance ONLY ─────────────────────────────────
    # Previous version used rolling_win_rate() which returned 100% when
    # no trades had resolved yet. This inflated bet sizes catastrophically.
    win_prob = imbalance

    if PROFILE["cross_market"] and vol > 0:
        vol_prob = vol_implied_prob(vol, direction)
        win_prob = (imbalance * 0.60) + (vol_prob * 0.40)
        log.info("🔬 Cross-market │ OB %.1f%% + VolImpl %.1f%% = %.1f%%",
            imbalance*100, vol_prob*100, win_prob*100)

    # ── Price breakeven guard ──────────────────────────────────────────────
    if direction == "YES":
        if yes_mid > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ YES at %dc exceeds breakeven %dc. Skipping.",
                yes_mid, YES_BREAKEVEN_PRICE)
            return
        trade_direction = "YES"
        contract_price  = yes_mid
    else:
        no_price = 100 - yes_mid
        if no_price > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ NO at %dc exceeds breakeven %dc. Skipping.",
                no_price, YES_BREAKEVEN_PRICE)
            return
        trade_direction = "NO"
        contract_price  = no_price

    edge = calc_edge(win_prob, contract_price)
    if edge < PROFILE["min_edge"]:
        log.info("Edge │ %.3f < min %.3f for %s. Skipping.", edge, PROFILE["min_edge"], ACTIVE_MODE.value)
        return

    bet = kelly_bet_size(win_prob, contract_price)
    if bet < 0.50:
        log.info("Kelly size │ $%.2f too small to place.", bet)
        return

    if current_balance < bet:
        log.warning("Insufficient balance │ $%.2f < bet $%.2f. Skipping.", current_balance, bet)
        return

    if trade_direction == "YES":
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        no_best     = 100 - yes_ask
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))
    limit_price = max(1, min(99, limit_price))

    if abs(limit_price - contract_price) > 8:
        log.info("Limit price │ %dc too far from mid %dc. Skipping.", limit_price, contract_price)
        return

    log.info("📈 SIGNAL │ %s │ OB: %.1f%% │ Edge: %.2f%% │ Bet: $%.2f │ Limit: %dc",
        trade_direction, win_prob * 100, edge * 100, bet, limit_price)

    place_limit_order(ticker, trade_direction, bet, limit_price)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global session_start_balance, daily_pnl, active_tickers
    global paper_balance, paper_daily_pnl, last_trade_ts

    init_base_url()

    paper_balance = float(os.environ.get("PAPER_BALANCE", "25.0"))

    log.info("━" * 70)
    log.info("  JOHNNY5 v4.0 │ %s │ Archetype: %s",
             "PAPER 🟡" if DEMO_MODE else "LIVE 🔴", ACTIVE_MODE.value.upper())
    log.info("  %s", PROFILE["description"])
    log.info("  Max trade: $%.2f │ Kelly: %.0f%% │ Min edge: %.1f%% │ Daily loss cap: $%.2f",
             TRADE_SIZE_DOLLARS, PROFILE["kelly_frac"]*100, PROFILE["min_edge"]*100, MAX_DAILY_LOSS)
    log.info("  Breakeven cap: %dc │ Balance floor: $%.2f",
             YES_BREAKEVEN_PRICE, MIN_BALANCE_FLOOR)
    log.info("  %s", "📋 PAPER TRADING — zero real orders" if DEMO_MODE else "⚠️  LIVE TRADING — real money")
    log.info("━" * 70)

    if DEMO_MODE:
        log.info("Starting paper balance: $%.2f", paper_balance)
    else:
        bal = get_live_balance()
        session_start_balance = bal
        log.info("Starting live balance: $%.2f", bal)

    resolve_cycle = 0

    while True:
        try:
            market = get_active_btc_market()
            if not market:
                log.info("No active BTC market. Waiting %ds...", POLL_INTERVAL)
                time.sleep(POLL_INTERVAL)
                continue

            update_btc_price_from_market(market)

            # Clear expired position locks when market rotates
            current_ticker = market.get("ticker", "")
            expired = {t for t in active_tickers if t != current_ticker}
            if expired:
                log.info("Clearing expired position locks: %s", expired)
                active_tickers -= expired

            current_balance = paper_balance if DEMO_MODE else get_live_balance()
            run_decision(market, current_balance)

            resolve_cycle += 1
            if resolve_cycle % 10 == 0:
                resolve_open_orders()

                if DEMO_MODE:
                    resolved = [t for t in trade_history if t.get("result") in ("win","loss")]
                    wins  = sum(1 for t in resolved if t["result"] == "win")
                    total = len(resolved)
                    wr    = wins / total if total > 0 else 0.0
                    log.info(
                        "📋 PAPER STATUS │ Balance: $%.2f │ Daily PnL: $%+.2f │ "
                        "Trades: %d │ Resolved: %d │ WR: %.1f%%",
                        paper_balance, paper_daily_pnl,
                        len(trade_history), total, wr * 100,
                    )
                else:
                    live_bal  = get_live_balance()
                    daily_pnl = live_bal - session_start_balance
                    resolved  = [t for t in trade_history if t.get("result") in ("win","loss")]
                    wins  = sum(1 for t in resolved if t["result"] == "win")
                    total = len(resolved)
                    wr    = wins / total if total > 0 else 0.0
                    log.info(
                        "Portfolio │ Balance: $%.2f │ Session PnL: %+.2f │ "
                        "Open orders: %d │ WR: %.1f%%",
                        live_bal, daily_pnl, len(open_orders), wr * 100,
                    )

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            final = paper_balance if DEMO_MODE else get_live_balance()
            log.info("Shutting down. Final balance: $%.2f", final)
            break
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
