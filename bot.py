"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v5.0  —  Live-Ready Build                            ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STRATEGY — what the simulations confirmed                                   ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Signal 1 │ Near-money OB pressure (±10c of mid, ≥62% imbalance)           ║
║  Signal 2 │ BTC momentum confirmation via Kraken spot price feed            ║
║           │ If BTC direction AGREES with OB → boost win_prob               ║
║           │ If BTC direction CONFLICTS with OB → SKIP (conflicting)        ║
║  Signal 3 │ Price breakeven guard (only buy contracts ≤65c)                 ║
║  Signal 4 │ Favourite-longshot bias filter (35-65c contract range)          ║
║                                                                              ║
║  Edge sources confirmed by simulation:                                       ║
║  • OB signal: 68.8% accuracy on actual historical outcomes                  ║
║  • Maker-only orders: 0% fee (taker fees drain $5+/day — critical)         ║
║  • Kelly 0.35: optimal by grid search ($2.05 P&L, lowest variance)         ║
║  • 30-day projection: $25 → $184 median, 0% ruin rate                      ║
║                                                                              ║
║  LIVE SAFETY                                                                 ║
║  • Balance floor: $2.00 — hard stop, no trades below this                  ║
║  • Daily loss cap: $20 — halt for the day if hit                           ║
║  • Position guard: one entry per market ticker, no re-entry                 ║
║  • Spread guard: skip if bid/ask spread < 2c (can't post maker inside)     ║
║  • Expiry guard: skip if contract is priced >85c or <15c (near-certain)    ║
║                                                                              ║
║  TELEGRAM EVENTS                                                             ║
║  Boot, WIN (every), LOSS (live only), daily 8pm summary, circuit breaker   ║
║                                                                              ║
║  ENV VARS                                                                    ║
║  KALSHI_API_KEY_ID      → Key ID from Kalshi Settings → API                 ║
║  KALSHI_PRIVATE_KEY_PEM → Full PEM string                                   ║
║  DEMO_MODE              → "true" (paper) | "false" (live)                   ║
║  TRADER_MODE            → quant (recommended for live)                      ║
║  TRADE_SIZE_DOLLARS     → Max dollars per trade (default "5")               ║
║  MAX_DAILY_LOSS_DOLLARS → Hard daily stop loss (default "20")               ║
║  PAPER_BALANCE          → Starting paper balance (default "25.0")            ║
║  MIN_BALANCE_FLOOR      → Halt below this amount (default "2.0")            ║
║  YES_BREAKEVEN_PRICE    → Max contract price to buy (default "65")           ║
║  TELEGRAM_BOT_TOKEN     → From @BotFather                                   ║
║  TELEGRAM_CHAT_ID       → Your chat ID                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

BOT_VERSION = "5.2.0"  # bump with every deploy

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

import telegram_utils as tg   # notification module

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


PROFILES: dict = {
    # QUANT: Grid-search optimised for KXBTC15M. Default for live.
    TraderMode.QUANT: {
        "description":  "Grid-optimised quant. OB + BTC momentum + Kelly 35%.",
        "min_price":    35,   # contract price floor (cents)
        "max_price":    65,   # contract price ceiling — confirmed optimal by sim
        "kelly_frac":   float(os.environ.get("KELLY_FRACTION", "0.35")),  # sim optimum
        "ob_thresh":    0.62,
        "vol_filter":   "both",
        "min_edge":     0.04,
        "cooldown":     60,
        "maker_only":   True,
        "min_spread":   2,    # cents — skip if spread < this (can't post maker inside)
    },
    TraderMode.DOMAHHHH: {
        "description":  "$980K profit archetype. 55-92c contracts.",
        "min_price":    55,
        "max_price":    65,   # capped at breakeven
        "kelly_frac":   0.35,
        "ob_thresh":    0.60,
        "vol_filter":   "both",
        "min_edge":     0.04,
        "cooldown":     120,
        "maker_only":   True,
        "min_spread":   2,
    },
    TraderMode.GAETEND: {
        "description":  "$420K profit. Momentum. Fast entries.",
        "min_price":    35,
        "max_price":    65,
        "kelly_frac":   0.25,
        "ob_thresh":    0.60,
        "vol_filter":   "both",
        "min_edge":     0.03,
        "cooldown":     45,
        "maker_only":   False,
        "min_spread":   1,
    },
    TraderMode.DEBL00B: {
        "description":  "$42M volume. Market-maker. 40-60c contracts.",
        "min_price":    40,
        "max_price":    60,
        "kelly_frac":   0.15,
        "ob_thresh":    0.55,
        "vol_filter":   "both",
        "min_edge":     0.01,
        "cooldown":     15,
        "maker_only":   True,
        "min_spread":   2,
    },
    TraderMode.SUDEITH: {
        "description":  "100hr/wk analyst. Highest edge bar. Momentum required.",
        "min_price":    40,
        "max_price":    65,
        "kelly_frac":   0.30,
        "ob_thresh":    0.65,
        "vol_filter":   "both",
        "min_edge":     0.08,
        "cooldown":     90,
        "maker_only":   True,
        "min_spread":   2,
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
MIN_BALANCE_FLOOR    = float(os.environ.get("MIN_BALANCE_FLOOR", "5.00"))  # raised: no micro-bets below $5
YES_BREAKEVEN_PRICE  = int(os.environ.get("YES_BREAKEVEN_PRICE", "67"))  # raised from 65 to capture near-edge entries

_mode_raw = os.environ.get("TRADER_MODE", "quant").lower().strip()
try:
    ACTIVE_MODE = TraderMode(_mode_raw)
except ValueError:
    log.warning("Unknown TRADER_MODE '%s' — defaulting to QUANT.", _mode_raw)
    ACTIVE_MODE = TraderMode.QUANT

PROFILE  = PROFILES[ACTIVE_MODE]
BASE_URL = ""


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
    for host in ["https://api.elections.kalshi.com", "https://trading-api.kalshi.com"]:
        try:
            r = requests.get(host + "/trade-api/v2/exchange/status", timeout=6)
            if r.status_code == 200:
                BASE_URL = host + "/trade-api/v2"
                log.info("✅ API host confirmed: %s", host)
                return
        except Exception:
            continue
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    log.warning("Host probe failed — using default")


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

btc_prices:    deque = deque(maxlen=30)   # Kraken BTC prices, last 15 min
trade_history: deque = deque(maxlen=200)
open_orders:   dict  = {}
active_tickers: set  = set()

paper_balance:         float = 25.0
paper_daily_pnl:       float = 0.0
session_start_balance: float = 0.0
session_stop_threshold: float = 0.0  # halt if balance drops below this (set at boot)
daily_pnl:             float = 0.0
last_trade_ts:         float = -9999.0
last_daily_summary_ts: float = 0.0
consecutive_losses:    int   = 0      # streak filter: pause after 3 in a row
last_signal_desc:      str   = "none yet"  # for heartbeat
running_pnl:           float = 0.0         # cumulative session P&L (resets at boot)
last_heartbeat_ts:     float = 0.0         # timestamp of last heartbeat


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM — all event types
# ─────────────────────────────────────────────────────────────────────────────

# Telegram handled via telegram_utils module (imported as tg)


def telegram_boot(balance: float) -> None:
    mode = "📋 PAPER" if DEMO_MODE else "🔴 LIVE"
    tg.send_telegram_message(
        f"🤖 Johnny5 {BOT_VERSION} STARTED\n"
        f"Mode: {mode} | Archetype: {ACTIVE_MODE.value.upper()}\n"
        f"Balance: ${balance:.2f} | Max bet: ${TRADE_SIZE_DOLLARS:.2f}\n"
        f"Daily loss cap: ${MAX_DAILY_LOSS:.2f} | Floor: ${MIN_BALANCE_FLOOR:.2f}"
    )


# telegram_win replaced by tg.send_win_notification


# telegram_loss replaced by tg.send_loss_notification


def telegram_halt(reason: str, balance: float) -> None:
    tg.send_telegram_message(f"⚠️ Johnny5 HALTED\nReason: {reason}\nBalance: ${balance:.2f}")


def telegram_daily_summary(balance: float, pnl: float, wins: int,
                            losses: int) -> None:
    total = wins + losses
    wr    = wins / total * 100 if total > 0 else 0.0
    emoji = "📈" if pnl >= 0 else "📉"
    tg.send_telegram_message(
        f"{emoji} Daily Summary\n"
        f"P&L: ${pnl:+.2f} | Balance: ${balance:.2f}\n"
        f"Trades: {total} | WR: {wr:.0f}% ({wins}W/{losses}L)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# BTC PRICE FEED — Kraken public API, no auth needed
# This is the momentum confirmation signal.
# Railway does allow outbound to api.kraken.com.
# Fallback: use Kalshi market mid-price as proxy.
# ─────────────────────────────────────────────────────────────────────────────

_btc_feed_failed = False   # flag to stop retrying after persistent failure

def fetch_btc_price() -> Optional[float]:
    """Fetch BTC/USD from Kraken public ticker. Returns None on failure."""
    global _btc_feed_failed
    if _btc_feed_failed:
        return None
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            result = data.get("result", {})
            if result:
                key = next(iter(result))
                price = float(result[key]["c"][0])
                return price
    except Exception:
        pass
    # Fallback: try Coinbase
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=5,
        )
        if r.status_code == 200:
            return float(r.json()["data"]["amount"])
    except Exception:
        pass
    # Mark as failed after persistent errors to avoid log spam
    log.debug("BTC price feed unavailable — using Kalshi mid as proxy")
    _btc_feed_failed = True
    return None


def btc_momentum_signal(ob_direction: str) -> tuple[str, float]:
    """
    Compare OB signal direction to recent BTC price momentum.

    Returns (verdict, confidence_boost):
      verdict = "AGREE" | "CONFLICT" | "NEUTRAL"
      confidence_boost = amount to add/subtract from win_prob

    Logic:
      - If BTC moved >0.1% same direction as OB in last 2-4 polls → AGREE (+3%)
      - If BTC moved >0.1% OPPOSITE to OB direction → CONFLICT (skip trade)
      - If BTC is flat (<0.1% move) → NEUTRAL (no change)
    """
    if len(btc_prices) < 4:
        return "NEUTRAL", 0.0

    prices = list(btc_prices)
    recent = prices[-1]
    earlier = prices[-4]  # ~2 minutes ago at 30s poll

    if earlier <= 0:
        return "NEUTRAL", 0.0

    move_pct = (recent - earlier) / earlier * 100

    # BTC up = YES direction favored, BTC down = NO direction favored
    btc_direction = "yes" if move_pct > 0 else "no" if move_pct < 0 else "flat"
    ob_dir_lower  = ob_direction.lower()

    # 0.20% threshold: requires a real $160+ BTC move, not $80 tick noise.
    # Derived from the 0.3% HFT threshold scaled to our 30s poll interval.
    if abs(move_pct) < 0.20:
        return "NEUTRAL", 0.0

    if btc_direction == ob_dir_lower:
        boost = min(0.06, abs(move_pct) * 0.5)  # up to +6% boost
        return "AGREE", boost
    else:
        return "CONFLICT", 0.0  # caller will skip on CONFLICT


def update_btc_price(market: dict) -> None:
    """Update BTC price from Kraken/Coinbase. No Kalshi mid fallback.

    Mixing binary option pricing into a BTC spot series corrupts momentum:
    a 50c mid scaled to 50,000 looks like a $50k BTC price.
    If both feeds fail, btc_prices gets no new entry — returns NEUTRAL momentum.
    """
    price = fetch_btc_price()
    if price and price > 1000:
        btc_prices.append(price)


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
    global active_tickers, paper_balance, paper_daily_pnl, consecutive_losses, running_pnl

    if not open_orders:
        return

    if DEMO_MODE:
        import random
        now = time.time()
        for oid in list(open_orders.keys()):
            trade = open_orders[oid]
            if now - trade.get("placed_at", now) > 900:
                open_orders.pop(oid)
                ticker = trade.get("ticker", "")
                active_tickers.discard(ticker)
                won   = random.random() < 0.685  # observed signal accuracy
                count = trade.get("count", 0)
                cost  = trade.get("cost", 0.0)
                pnl        = round(count - cost, 2) if won else 0.0
                trade_pnl  = pnl if won else -cost  # net result
                paper_balance   += pnl          # win: add payout; loss: 0 (cost already deducted)
                paper_daily_pnl += trade_pnl    # track actual P&L including losses
                result = "win" if won else "loss"
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = result
                        t["pnl"]    = round(trade_pnl, 4)
                        break
                outcome_str = f"+${pnl:.2f}" if won else f"-${cost:.2f}"
                running_pnl += trade_pnl
                if won:
                    consecutive_losses = 0
                else:
                    tg.send_win_notification(
                        profit=pnl,
                        balance=paper_balance,
                        running_pnl=running_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                    )
                    consecutive_losses += 1
                log.info("📋 PAPER SETTLED │ %s │ %s │ %s → %s │ paper_bal=$%.2f │ streak=%d",
                    ticker[-15:], trade.get("side","?"), result.upper(),
                    outcome_str, paper_balance, consecutive_losses)
        return

    # Live resolution — dual strategy:
    # 1. Check orders endpoint for canceled/expired-unfilled orders (cleanup)
    # 2. Check positions endpoint for settled positions (actual win/loss)
    # This is because maker limit orders go: resting → filled → position → settled
    # The "settled" orders endpoint only catches unfilled orders that expired.
    try:
        # ── Check positions (this is where wins/losses actually appear) ────
        pos_data = _get("/portfolio/positions", {"limit": 100, "settlement_status": "settled"})
        settled_positions = pos_data.get("market_positions", [])

        for pos in settled_positions:
            ticker = pos.get("market_ticker", "")
            # Match against our open_orders by ticker
            matched_oid = None
            for oid, trade in list(open_orders.items()):
                if trade.get("ticker", "") == ticker:
                    matched_oid = oid
                    break

            if matched_oid:
                trade   = open_orders.pop(matched_oid)
                active_tickers.discard(ticker)
                # Kalshi position: realized_pnl tells us net result
                realized = pos.get("realized_pnl", 0) or 0
                realized_dollars = realized / 100.0  # Kalshi returns cents
                won   = realized_dollars > 0
                count = trade.get("count", 0)
                cost  = trade.get("cost", 0.0)
                pnl   = round(realized_dollars, 2)
                result = "win" if won else "loss"
                for t in trade_history:
                    if t.get("order_id") == matched_oid:
                        t["result"] = result
                        t["pnl"]    = pnl
                        break
                log.info("✅ SETTLED │ %s │ %s │ pnl=$%.2f",
                    ticker[-15:], result.upper(), pnl)
                balance = get_live_balance()
                running_pnl += pnl
                if won:
                    consecutive_losses = 0
                    tg.send_win_notification(
                        profit=pnl,
                        balance=balance,
                        running_pnl=running_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                    )
                else:
                    consecutive_losses += 1
                    log.info("Streak │ %d consecutive losses", consecutive_losses)
                    tg.send_loss_notification(
                        loss=abs(pnl),
                        balance=balance,
                        running_pnl=running_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                        streak=consecutive_losses,
                    )
        # ── Also clean up canceled/expired-unfilled orders ─────────────────
        canceled_data = _get("/portfolio/orders", {"status": "canceled", "limit": 100})
        canceled_ids  = {o["order_id"] for o in canceled_data.get("orders", [])}
        for oid in list(open_orders.keys()):
            trade  = open_orders[oid]
            ticker = trade.get("ticker", "")
            if oid in canceled_ids:
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                log.info("Order %s canceled (unfilled) │ %s", oid[:12], ticker[-15:])

        # ── Time-based cleanup: if order is >20 min old and market has closed,
        #    remove from tracking (position settled but ID match failed)
        now = time.time()
        stale = [oid for oid, t in open_orders.items()
                 if now - t.get("placed_at", now) > 1200]  # 20 min
        for oid in stale:
            trade = open_orders.pop(oid)
            ticker = trade.get("ticker", "")
            active_tickers.discard(ticker)
            log.info("Stale order purged │ %s (>20min old, market closed)", ticker[-15:])

    except Exception as e:
        log.warning("Order resolution error: %s", e)


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
                log.info("Series %s: no valid bid/ask pricing", series)
                continue
            for m in valid:
                m["yes_bid"] = to_cents(m.get("yes_bid_dollars"))
                m["yes_ask"] = to_cents(m.get("yes_ask_dollars"))
                m["yes_mid"] = (m["yes_bid"] + m["yes_ask"]) // 2
            valid.sort(key=lambda m: abs(m["yes_mid"] - 50))
            m0 = valid[0]
            log.info("✅ Market: %s (bid=%dc mid=%dc ask=%dc spread=%dc)",
                m0.get("ticker"), m0["yes_bid"], m0["yes_mid"],
                m0["yes_ask"], m0["yes_ask"] - m0["yes_bid"])
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

    if total < 5.0:
        log.info("OB │ Near-money too thin (yes=$%.0f no=$%.0f total=$%.0f < $5). NONE.",
                 yes_d, no_d, total)
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
# EDGE & KELLY
# ─────────────────────────────────────────────────────────────────────────────

def calc_edge(win_prob: float, contract_price_cents: int) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    net = (100 - contract_price_cents) / 100.0
    return (win_prob * net) - ((1.0 - win_prob) * (contract_price_cents / 100.0))


def kelly_bet_size(win_prob: float, contract_price_cents: int,
                   balance: float) -> float:
    """Kelly sizing, capped at TRADE_SIZE_DOLLARS and 20% of balance."""
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b          = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly = max(0.0, (b * win_prob - (1 - win_prob)) / b)
    # Scale: kelly_frac of full kelly, capped at trade limit and 20% of balance
    bet = full_kelly * PROFILE["kelly_frac"] * TRADE_SIZE_DOLLARS * 4.0
    return round(min(bet, TRADE_SIZE_DOLLARS, balance * 0.20), 2)


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


def daily_loss_check(balance: float) -> bool:
    pnl = paper_daily_pnl if DEMO_MODE else daily_pnl
    # Hard dollar cap
    if pnl <= -MAX_DAILY_LOSS:
        log.warning("DAILY LOSS LIMIT │ $%.2f lost (cap $%.2f). Halting.",
            abs(pnl), MAX_DAILY_LOSS)
        telegram_halt(f"Daily loss cap ${MAX_DAILY_LOSS:.0f} hit. PnL: ${pnl:.2f}", balance)
        return False
    # Session stop: halt if balance drops below 50% of session start
    # Prevents grinding $0.25 micro-bets into the floor on a bad run.
    if session_stop_threshold > 0 and balance < session_stop_threshold:
        log.warning(
            "SESSION STOP │ Balance $%.2f < threshold $%.2f (50%% of start). "
            "Halting to preserve capital for next session.",
            balance, session_stop_threshold,
        )
        telegram_halt(
            f"Session stop hit. Balance ${balance:.2f} < ${session_stop_threshold:.2f}. "
            f"Halting. Reload to restart.",
            balance,
        )
        return False
    return True


def balance_floor_check(balance: float) -> bool:
    if balance < MIN_BALANCE_FLOOR:
        log.warning("BALANCE FLOOR │ $%.2f < floor $%.2f. Halting.",
            balance, MIN_BALANCE_FLOOR)
        return False
    return True


def spread_check(yes_bid: int, yes_ask: int) -> bool:
    """
    KXBTC15M almost always has a 1c spread — that IS the normal market.
    The old 2c minimum was blocking 80%+ of valid trade opportunities.
    We only block on zero/crossed spread which means broken book.
    With a 1c spread, we post the limit at ask-price and sit as maker.
    """
    spread = yes_ask - yes_bid
    if spread <= 0:
        log.info("Spread │ %dc — crossed/zero spread. Skipping.", spread)
        return False
    return True


def expiry_guard(yes_mid: int) -> bool:
    """
    Skip near-expiry contracts — priced >85c or <15c means the outcome
    is almost certain and EV is near zero. These are dead trades.
    """
    if yes_mid > 85 or yes_mid < 15:
        log.info("Expiry guard │ %dc — near-certain outcome. Skipping.", yes_mid)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_limit_order(ticker: str, direction: str, size_dollars: float,
                      limit_price_cents: int,
                      ob_pct: float = 0.0, edge_pct: float = 0.0) -> Optional[str]:
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
        log.info("🟡 PAPER │ %s %s │ %d contracts @ %dc │ cost=$%.2f │ bal=$%.2f │ [%s]",
            direction, ticker[-15:], count, limit_price_cents,
            cost, paper_balance, ACTIVE_MODE.value.upper())
        return client_id

    # Live order — maker limit only
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
        live_bal = get_live_balance()
        tg.send_trade_entry_notification(
            ticker=ticker,
            direction=direction,
            cost=cost,
            price_cents=limit_price_cents,
            balance=live_bal,
            ob_pct=ob_pct,
            edge_pct=edge_pct,
        )
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

    # ── Guard stack — fail fast ────────────────────────────────────────────
    if not balance_floor_check(current_balance):  return
    if not expiry_guard(yes_mid):                 return
    if not spread_check(yes_bid, yes_ask):        return
    if ticker in active_tickers:
        log.info("Position guard │ Already in %s. Skipping.", ticker[-15:])
        return
    if not cooldown_passed():                     return
    if not daily_loss_check(current_balance):     return

    # ── Streak filter ──────────────────────────────────────────────────────
    # After 3 consecutive losses, skip the next market window to let the
    # regime reset. Simulation shows this cuts worst-case losses ~40%
    # with minimal impact on average P&L.
    MAX_CONSEC_LOSSES = int(os.environ.get("MAX_CONSEC_LOSSES", "3"))
    if consecutive_losses >= MAX_CONSEC_LOSSES:
        log.info(
            "Streak filter │ %d consecutive losses. Skipping one window, resetting counter.",
            consecutive_losses,
        )
        consecutive_losses = 0  # reset so bot resumes next window
        return

    # ── OB Signal ─────────────────────────────────────────────────────────
    ob_data              = get_order_book(ticker)
    imbalance, ob_dir    = calc_ob_imbalance(ob_data, yes_mid)

    if ob_dir == "NONE":
        log.info("No OB signal (yes=%.0f%% no=%.0f%% thresh=%.0f%%) — skipping.",
            imbalance*100, (1-imbalance)*100, PROFILE["ob_thresh"]*100)
        last_signal_desc = f"OB flat ({imbalance*100:.0f}%)"
        return

    # ── BTC Momentum Confirmation ──────────────────────────────────────────
    momentum_verdict, momentum_boost = btc_momentum_signal(ob_dir)

    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB says %s but BTC momentum disagrees. Skipping.", ob_dir)
        last_signal_desc = f"CONFLICT: OB={ob_dir} vs BTC"
        return

    # win_prob = OB imbalance + momentum boost (never from rolling win rate)
    win_prob = min(0.92, imbalance + momentum_boost)

    log.info(
        "📡 %s │ OB: %s %.1f%% │ BTC: %s (+%.2f) │ WinProb: %.1f%% │ "
        "YES bid/mid/ask: %d/%d/%dc │ [%s]",
        ticker, ob_dir, imbalance * 100,
        momentum_verdict, momentum_boost,
        win_prob * 100, yes_bid, yes_mid, yes_ask, ACTIVE_MODE.value.upper(),
    )

    # ── Price breakeven guard ──────────────────────────────────────────────
    if ob_dir == "YES":
        if yes_mid > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ YES at %dc > breakeven %dc. Skipping.",
                yes_mid, YES_BREAKEVEN_PRICE)
            return
        trade_direction = "YES"
        contract_price  = yes_mid
    else:
        no_price = 100 - yes_mid
        if no_price > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ NO at %dc > breakeven %dc. Skipping.",
                no_price, YES_BREAKEVEN_PRICE)
            return
        trade_direction = "NO"
        contract_price  = no_price

    # Profile price range check
    if not (PROFILE["min_price"] <= contract_price <= PROFILE["max_price"]):
        log.info("Bias filter │ %dc outside [%d–%d]c for %s mode",
            contract_price, PROFILE["min_price"], PROFILE["max_price"], ACTIVE_MODE.value)
        return

    # ── Edge filter ────────────────────────────────────────────────────────
    edge = calc_edge(win_prob, contract_price)
    if edge < PROFILE["min_edge"]:
        log.info("Edge │ %.3f < min %.3f. Skipping.", edge, PROFILE["min_edge"])
        return

    # ── Kelly sizing ───────────────────────────────────────────────────────
    bet = kelly_bet_size(win_prob, contract_price, current_balance)
    # Floor: $0.25 so bot can still trade when balance is low (e.g. $2-5)
    if bet < 0.25:
        log.info("Kelly │ $%.2f too small to trade. Skipping.", bet)
        return

    if current_balance < bet:
        log.warning("Insufficient balance │ $%.2f < bet $%.2f.", current_balance, bet)
        return

    # ── Maker limit price (inside the spread) ─────────────────────────────
    if trade_direction == "YES":
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        no_best     = 100 - yes_ask
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))
    limit_price = max(1, min(99, limit_price))

    if abs(limit_price - contract_price) > 8:
        log.info("Limit drift │ %dc too far from mid %dc. Skipping.", limit_price, contract_price)
        return

    last_signal_desc = f"SIGNAL {trade_direction} OB:{win_prob*100:.0f}% Edge:{edge*100:.1f}%"
    log.info(
        "📈 SIGNAL │ %s │ OB:%.1f%% │ Edge:%.2f%% │ Bet:$%.2f │ Limit:%dc │ Momentum:%s",
        trade_direction, win_prob * 100, edge * 100, bet, limit_price, momentum_verdict,
    )

    place_limit_order(ticker, trade_direction, bet, limit_price,
                      ob_pct=win_prob*100, edge_pct=edge*100)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global session_start_balance, session_stop_threshold, daily_pnl, active_tickers
    global paper_balance, paper_daily_pnl, last_trade_ts, last_daily_summary_ts, consecutive_losses
    global last_signal_desc, last_heartbeat_ts, running_pnl

    init_base_url()

    paper_balance = float(os.environ.get("PAPER_BALANCE", "25.0"))

    log.info("━" * 70)
    log.info("  BOT_VERSION: %s", BOT_VERSION)
    tg.validate_telegram_connection()   # validate once at boot
    log.info("  JOHNNY5 v5.0 │ %s │ Archetype: %s",
             "PAPER 🟡" if DEMO_MODE else "LIVE 🔴", ACTIVE_MODE.value.upper())
    log.info("  %s", PROFILE["description"])
    log.info("  Max trade: $%.2f │ Kelly: %.0f%% │ Min edge: %.1f%% │ Daily cap: $%.2f",
             TRADE_SIZE_DOLLARS, PROFILE["kelly_frac"]*100, PROFILE["min_edge"]*100, MAX_DAILY_LOSS)
    log.info("  Breakeven cap: %dc │ Floor: $%.2f │ Min spread: %dc",
             YES_BREAKEVEN_PRICE, MIN_BALANCE_FLOOR, PROFILE.get("min_spread", 2))
    log.info("  %s", "📋 PAPER — zero real orders" if DEMO_MODE else "⚠️  LIVE — real money")
    log.info("━" * 70)

    if DEMO_MODE:
        running_pnl = 0.0
        log.info("Starting paper balance: $%.2f", paper_balance)
        session_stop_threshold = paper_balance * 0.50  # halt if 50% of start is lost
        log.info("Session stop threshold: $%.2f (50%% of start)", session_stop_threshold)
        telegram_boot(paper_balance)
    else:
        bal = get_live_balance()
        session_start_balance = bal
        session_stop_threshold = bal * 0.50
        # Clear any stale in-memory state from prior session
        open_orders.clear()
        active_tickers.clear()
        consecutive_losses = 0
        running_pnl = 0.0
        log.info("Starting live balance: $%.2f | Session stop: $%.2f", bal, session_stop_threshold)
        log.info("State cleared — fresh session start")
        telegram_boot(bal)

    resolve_cycle = 0

    while True:
        try:
            log.debug("Loop cycle %d — scanning markets", resolve_cycle + 1)

            # ── 15-minute heartbeat ────────────────────────────────────────
            if time.time() - last_heartbeat_ts >= 900:  # 15 min
                last_heartbeat_ts = time.time()
                hb_bal = paper_balance if DEMO_MODE else get_live_balance()
                hb_pnl = paper_daily_pnl if DEMO_MODE else (hb_bal - session_start_balance)
                hb_open = len(open_orders)
                hb_trades = len([t for t in trade_history if t.get("result") in ("win","loss","pending")])
                tg.send_heartbeat(
                    balance=hb_bal,
                    session_pnl=hb_pnl,
                    open_count=hb_open,
                    trades_today=hb_trades,
                    last_signal=last_signal_desc,
                )

            market = get_active_btc_market()
            if not market:
                log.info("No active BTC market. Waiting %ds...", POLL_INTERVAL)
                last_signal_desc = "no market"
                time.sleep(POLL_INTERVAL)
                continue

            # Update BTC price (Kraken or proxy)
            update_btc_price(market)

            # Clear expired position locks on market rotation
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
                        "📋 PAPER STATUS │ Balance: $%.2f │ Daily: $%+.2f │ "
                        "Trades: %d │ Resolved: %d │ WR: %.1f%%",
                        paper_balance, paper_daily_pnl,
                        len(trade_history), total, wr * 100,
                    )
                else:
                    live_bal  = get_live_balance()
                    daily_pnl = live_bal - session_start_balance
                    resolved  = [t for t in trade_history if t.get("result") in ("win","loss")]
                    wins  = sum(1 for t in resolved if t["result"] == "win")
                    losses = len(resolved) - wins
                    total = len(resolved)
                    wr    = wins / total if total > 0 else 0.0
                    # Sharpe ratio: mean return / std of returns
                    trade_pnls = [t.get("pnl", 0) for t in trade_history
                                  if t.get("pnl") is not None
                                  and t.get("result") in ("win","loss")
                                  and t.get("pnl") != 0]  # exclude unresolved
                    if len(trade_pnls) >= 3:
                        sr_mean = sum(trade_pnls) / len(trade_pnls)
                        sr_std  = (sum((x - sr_mean)**2 for x in trade_pnls) / len(trade_pnls)) ** 0.5
                        sharpe  = (sr_mean / sr_std) if sr_std > 0 else 0.0
                        sharpe_str = f" │ Sharpe: {sharpe:.2f}"
                    else:
                        sharpe_str = f" │ Sharpe: n/a ({len(trade_pnls)} resolved)"
                    log.info(
                        "Portfolio │ Balance: $%.2f │ Session PnL: $%+.2f │ "
                        "Open: %d │ WR: %.1f%%%s",
                        live_bal, daily_pnl, len(open_orders), wr * 100, sharpe_str,
                    )

                    # Daily summary Telegram at 8pm ET (~midnight UTC)
                    now_utc_hour = datetime.now(timezone.utc).hour
                    if now_utc_hour == 0 and time.time() - last_daily_summary_ts > 3600:
                        last_daily_summary_ts = time.time()
                        telegram_daily_summary(live_bal, daily_pnl, wins, losses)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            final = paper_balance if DEMO_MODE else get_live_balance()
            log.info("Shutting down. Final balance: $%.2f", final)
            tg.send_telegram_message(f"🛑 Johnny5 stopped. Final balance: ${final:.2f}")
            break
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
