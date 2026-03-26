"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v5.3.0  —  Multi-Market + Adaptive Signals           ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v5.3.0 UPGRADES                                                            ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  1. Multi-market scanner: scores ALL open markets, trades best edge        ║
║  2. Stale order cancellation: cancel unfilled makers after 5 min           ║
║  3. Adaptive OB threshold: thin books need stronger signal                 ║
║  4. Time-of-day filter: skip low-liquidity UTC hours                       ║
║  5. OB trend detection: only trade if pressure is building                 ║
║  6. Smarter paper mode: outcomes from real BTC movement, not coin flip     ║
║  7. Win rate confidence: Wilson score intervals on live stats              ║
║  8. Concurrent position limit: cap simultaneous open positions             ║
║                                                                              ║
║  STRATEGY (unchanged)                                                        ║
║  Signal 1 │ Near-money OB pressure (±10c of mid, adaptive threshold)       ║
║  Signal 2 │ BTC momentum confirmation via Kraken spot price feed            ║
║  Signal 3 │ Price breakeven guard (contracts ≤ YES_BREAKEVEN_PRICE)         ║
║  Signal 4 │ Favourite-longshot bias filter (35-65c range)                   ║
║                                                                              ║
║  ENV VARS (new in v5.3.0 marked with *)                                     ║
║  KALSHI_API_KEY_ID      → Key ID from Kalshi Settings → API                 ║
║  KALSHI_PRIVATE_KEY_PEM → Full PEM string                                   ║
║  DEMO_MODE              → "true" (paper) | "false" (live)                   ║
║  TRADER_MODE            → quant (recommended for live)                      ║
║  TRADE_SIZE_DOLLARS     → Max dollars per trade (default "5")               ║
║  MAX_DAILY_LOSS_DOLLARS → Hard daily stop loss (default "20")               ║
║  PAPER_BALANCE          → Starting paper balance (default "25.0")            ║
║  MIN_BALANCE_FLOOR      → Halt below this amount (default "5.0")            ║
║  YES_BREAKEVEN_PRICE    → Max contract price to buy (default "67")           ║
║  TELEGRAM_BOT_TOKEN     → From @BotFather                                   ║
║  TELEGRAM_CHAT_ID       → Your chat ID                                       ║
║  *STALE_ORDER_TIMEOUT   → Seconds before canceling unfilled order (300)     ║
║  *MAX_CONCURRENT_POS    → Max simultaneous open positions (2)               ║
║  *LOW_LIQ_START_UTC     → Low-liquidity window start hour UTC (4)           ║
║  *LOW_LIQ_END_UTC       → Low-liquidity window end hour UTC (8)             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

BOT_VERSION = "5.3.0"

import base64
import logging
import math
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import telegram_utils as tg

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
    TraderMode.QUANT: {
        "description":  "Grid-optimised quant. OB + BTC momentum + Kelly 35%.",
        "min_price":    35,
        "max_price":    65,
        "kelly_frac":   float(os.environ.get("KELLY_FRACTION", "0.35")),
        "ob_thresh":    0.62,
        "vol_filter":   "both",
        "min_edge":     0.04,
        "cooldown":     60,
        "maker_only":   True,
        "min_spread":   2,
    },
    TraderMode.DOMAHHHH: {
        "description":  "$980K profit archetype. 55-92c contracts.",
        "min_price":    55,
        "max_price":    65,
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
MIN_BALANCE_FLOOR    = float(os.environ.get("MIN_BALANCE_FLOOR", "5.00"))
YES_BREAKEVEN_PRICE  = int(os.environ.get("YES_BREAKEVEN_PRICE", "67"))

# v5.3.0 new env vars
STALE_ORDER_TIMEOUT  = int(os.environ.get("STALE_ORDER_TIMEOUT", "300"))      # 5 min
MAX_CONCURRENT_POS   = int(os.environ.get("MAX_CONCURRENT_POS", "2"))         # max open positions
LOW_LIQ_START_UTC    = int(os.environ.get("LOW_LIQ_START_UTC", "4"))          # skip start hour
LOW_LIQ_END_UTC      = int(os.environ.get("LOW_LIQ_END_UTC", "8"))           # skip end hour

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


def _delete(path: str) -> dict:
    """DELETE for order cancellation."""
    r = requests.delete(BASE_URL + path,
                        headers=_auth_headers("DELETE", path), timeout=12)
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

btc_prices:    deque = deque(maxlen=30)
trade_history: deque = deque(maxlen=200)
open_orders:   dict  = {}
active_tickers: set  = set()

paper_balance:         float = 25.0
paper_daily_pnl:       float = 0.0
session_start_balance: float = 0.0
session_stop_threshold: float = 0.0
daily_pnl:             float = 0.0
last_trade_ts:         float = -9999.0
last_daily_summary_ts: float = 0.0
consecutive_losses:    int   = 0
last_signal_desc:      str   = "none yet"
running_pnl:           float = 0.0
last_heartbeat_ts:     float = 0.0

# v5.3.0: OB trend tracking — stores previous OB snapshot per ticker
_prev_ob: dict = {}   # ticker -> (imbalance, direction, timestamp)


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM — all event types
# ─────────────────────────────────────────────────────────────────────────────

def telegram_boot(balance: float) -> None:
    mode = "📋 PAPER" if DEMO_MODE else "🔴 LIVE"
    tg.send_telegram_message(
        f"🤖 Johnny5 {BOT_VERSION} STARTED\n"
        f"Mode: {mode} | Archetype: {ACTIVE_MODE.value.upper()}\n"
        f"Balance: ${balance:.2f} | Max bet: ${TRADE_SIZE_DOLLARS:.2f}\n"
        f"Daily loss cap: ${MAX_DAILY_LOSS:.2f} | Floor: ${MIN_BALANCE_FLOOR:.2f}\n"
        f"Stale cancel: {STALE_ORDER_TIMEOUT}s | Max positions: {MAX_CONCURRENT_POS}\n"
        f"Low-liq skip: {LOW_LIQ_START_UTC}-{LOW_LIQ_END_UTC} UTC"
    )


def telegram_halt(reason: str, balance: float) -> None:
    tg.send_telegram_message(f"⚠️ Johnny5 HALTED\nReason: {reason}\nBalance: ${balance:.2f}")


def telegram_daily_summary(balance: float, pnl: float, wins: int,
                            losses: int) -> None:
    total = wins + losses
    wr    = wins / total * 100 if total > 0 else 0.0
    emoji = "📈" if pnl >= 0 else "📉"
    # v5.3.0: add Wilson confidence interval
    wr_pct, wr_lo, wr_hi = wilson_confidence(wins, total)
    conf_str = f" (95% CI: {wr_lo:.0f}%-{wr_hi:.0f}%)" if total >= 5 else ""
    tg.send_telegram_message(
        f"{emoji} Daily Summary\n"
        f"P&L: ${pnl:+.2f} | Balance: ${balance:.2f}\n"
        f"Trades: {total} | WR: {wr:.0f}%{conf_str} ({wins}W/{losses}L)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# WIN RATE CONFIDENCE (v5.3.0)
# ─────────────────────────────────────────────────────────────────────────────

def wilson_confidence(wins: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score confidence interval for win rate.

    Returns (point_estimate_pct, lower_bound_pct, upper_bound_pct).
    z=1.96 → 95% confidence. Requires total >= 1.
    """
    if total == 0:
        return 0.0, 0.0, 0.0
    p = wins / total
    denom = 1 + z ** 2 / total
    center = (p + z ** 2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * total)) / total) / denom
    return (
        round(p * 100, 1),
        round(max(0.0, center - spread) * 100, 1),
        round(min(1.0, center + spread) * 100, 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# BTC PRICE FEED
# ─────────────────────────────────────────────────────────────────────────────

_btc_feed_backoff_until: float = 0.0

def fetch_btc_price() -> Optional[float]:
    global _btc_feed_backoff_until
    if time.time() < _btc_feed_backoff_until:
        return None
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            result = data.get("result", {})
            if result:
                key = next(iter(result))
                return float(result[key]["c"][0])
    except Exception:
        pass
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5,
        )
        if r.status_code == 200:
            return float(r.json()["data"]["amount"])
    except Exception:
        pass
    log.debug("BTC price feed unavailable — backing off 5 min")
    _btc_feed_backoff_until = time.time() + 300
    return None


def btc_momentum_signal(ob_direction: str) -> tuple[str, float]:
    if len(btc_prices) < 4:
        return "NEUTRAL", 0.0
    prices = list(btc_prices)
    recent  = prices[-1]
    earlier = prices[-4]
    if earlier <= 0:
        return "NEUTRAL", 0.0
    move_pct = (recent - earlier) / earlier * 100
    btc_direction = "yes" if move_pct > 0 else "no" if move_pct < 0 else "flat"
    ob_dir_lower  = ob_direction.lower()
    if abs(move_pct) < 0.20:
        return "NEUTRAL", 0.0
    if btc_direction == ob_dir_lower:
        boost = min(0.06, abs(move_pct) * 0.5)
        return "AGREE", boost
    else:
        return "CONFLICT", 0.0


def update_btc_price(market: dict) -> None:
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


# ─────────────────────────────────────────────────────────────────────────────
# STALE ORDER CANCELLATION (v5.3.0)
# ─────────────────────────────────────────────────────────────────────────────

def cancel_stale_orders() -> None:
    """Cancel unfilled maker orders older than STALE_ORDER_TIMEOUT to free capital."""
    global paper_balance, paper_daily_pnl

    if not open_orders:
        return

    now = time.time()
    stale_ids = [oid for oid, t in open_orders.items()
                 if now - t.get("placed_at", now) > STALE_ORDER_TIMEOUT]

    for oid in stale_ids:
        trade  = open_orders[oid]
        ticker = trade.get("ticker", "")
        cost   = trade.get("cost", 0.0)

        if DEMO_MODE:
            paper_balance   += cost
            paper_daily_pnl += cost   # undo the cost deduction
            open_orders.pop(oid)
            active_tickers.discard(ticker)
            for t in trade_history:
                if t.get("order_id") == oid:
                    t["result"] = "canceled"
                    t["pnl"]    = 0.0
                    break
            log.info("📋 PAPER CANCEL │ %s │ $%.2f refunded │ stale >%ds │ bal=$%.2f",
                     ticker[-15:], cost, STALE_ORDER_TIMEOUT, paper_balance)
        else:
            try:
                _delete(f"/portfolio/orders/{oid}")
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = "canceled"
                        t["pnl"]    = 0.0
                        break
                log.info("🔄 ORDER CANCELED │ %s │ stale >%ds │ ID:%s",
                         ticker[-15:], STALE_ORDER_TIMEOUT, oid[:12])
            except Exception as e:
                log.warning("Cancel failed │ %s │ %s", oid[:12], e)


# ─────────────────────────────────────────────────────────────────────────────
# ORDER RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def resolve_open_orders() -> None:
    global active_tickers, paper_balance, paper_daily_pnl, consecutive_losses, running_pnl

    if not open_orders:
        return

    if DEMO_MODE:
        now = time.time()
        for oid in list(open_orders.keys()):
            trade = open_orders[oid]
            if now - trade.get("placed_at", now) > 900:
                open_orders.pop(oid)
                ticker = trade.get("ticker", "")
                active_tickers.discard(ticker)
                count = trade.get("count", 0)
                cost  = trade.get("cost", 0.0)

                # v5.3.0: smarter paper mode — use BTC price movement
                entry_btc = trade.get("entry_btc_price")
                current_btc = fetch_btc_price()
                side = trade.get("side", "").upper()

                if entry_btc and current_btc and entry_btc > 1000 and current_btc > 1000:
                    btc_moved_up = current_btc > entry_btc
                    won = (side == "YES" and btc_moved_up) or (side == "NO" and not btc_moved_up)
                    sim_method = "btc"
                else:
                    import random
                    won = random.random() < 0.685
                    sim_method = "rng"

                pnl        = round(count - cost, 2) if won else 0.0
                trade_pnl  = pnl if won else -cost
                paper_balance   += pnl
                paper_daily_pnl += trade_pnl
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
                    tg.send_win_notification(
                        profit=pnl, balance=paper_balance,
                        running_pnl=running_pnl, ticker=ticker,
                        direction=trade.get("side", "?"),
                    )
                else:
                    consecutive_losses += 1
                    tg.send_loss_notification(
                        loss=abs(trade_pnl), balance=paper_balance,
                        running_pnl=running_pnl, ticker=ticker,
                        direction=trade.get("side", "?"),
                        streak=consecutive_losses,
                    )
                log.info("📋 PAPER SETTLED │ %s │ %s │ %s → %s │ sim=%s │ bal=$%.2f │ streak=%d",
                    ticker[-15:], trade.get("side","?"), result.upper(),
                    outcome_str, sim_method, paper_balance, consecutive_losses)
        return

    # ── Live resolution ────────────────────────────────────────────────────
    try:
        pos_data = _get("/portfolio/positions", {"limit": 100, "settlement_status": "settled"})
        settled_positions = pos_data.get("market_positions", [])

        for pos in settled_positions:
            ticker = pos.get("market_ticker", "")
            matched_oid = None
            for oid, trade in list(open_orders.items()):
                if trade.get("ticker", "") == ticker:
                    matched_oid = oid
                    break

            if matched_oid:
                trade   = open_orders.pop(matched_oid)
                active_tickers.discard(ticker)
                realized = pos.get("realized_pnl", 0) or 0
                realized_dollars = realized / 100.0
                won    = realized_dollars > 0
                pnl    = round(realized_dollars, 2)
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
                        profit=pnl, balance=balance,
                        running_pnl=running_pnl, ticker=ticker,
                        direction=trade.get("side", "?"),
                    )
                else:
                    consecutive_losses += 1
                    log.info("Streak │ %d consecutive losses", consecutive_losses)
                    tg.send_loss_notification(
                        loss=abs(pnl), balance=balance,
                        running_pnl=running_pnl, ticker=ticker,
                        direction=trade.get("side", "?"),
                        streak=consecutive_losses,
                    )

        canceled_data = _get("/portfolio/orders", {"status": "canceled", "limit": 100})
        canceled_ids  = {o["order_id"] for o in canceled_data.get("orders", [])}
        for oid in list(open_orders.keys()):
            trade  = open_orders[oid]
            ticker = trade.get("ticker", "")
            if oid in canceled_ids:
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                log.info("Order %s canceled (unfilled) │ %s", oid[:12], ticker[-15:])

        now = time.time()
        stale = [oid for oid, t in open_orders.items()
                 if now - t.get("placed_at", now) > 1200]
        for oid in stale:
            trade = open_orders.pop(oid)
            ticker = trade.get("ticker", "")
            active_tickers.discard(ticker)
            log.info("Stale order purged │ %s (>20min old)", ticker[-15:])

    except Exception as e:
        log.warning("Order resolution error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# MARKET DISCOVERY — v5.3.0: MULTI-MARKET SCANNER
# ─────────────────────────────────────────────────────────────────────────────

BTC_SERIES = ["KXBTC15M", "KXBTCD", "KXBTC"]

def _to_cents(val) -> int:
    try:
        return int(round(float(val) * 100))
    except Exception:
        return 0


def get_active_btc_markets() -> list[dict]:
    """Return ALL valid open BTC markets, sorted by proximity to 50c mid.

    v5.3.0: scans all series, returns full list so the decision engine
    can evaluate each market and pick the best signal.
    """
    all_valid = []
    for series in BTC_SERIES:
        try:
            data    = _get("/markets", {"series_ticker": series, "status": "open", "limit": 20})
            markets = data.get("markets", [])
            if not markets:
                continue
            valid = [m for m in markets
                     if _to_cents(m.get("yes_bid_dollars")) > 0
                     and _to_cents(m.get("yes_ask_dollars")) > 0
                     and _to_cents(m.get("yes_bid_dollars")) < _to_cents(m.get("yes_ask_dollars"))]
            for m in valid:
                m["yes_bid"] = _to_cents(m.get("yes_bid_dollars"))
                m["yes_ask"] = _to_cents(m.get("yes_ask_dollars"))
                m["yes_mid"] = (m["yes_bid"] + m["yes_ask"]) // 2
            all_valid.extend(valid)
        except Exception as e:
            log.warning("Market discovery failed for %s: %s", series, e)

    if not all_valid:
        return []

    all_valid.sort(key=lambda m: abs(m["yes_mid"] - 50))
    log.info("📡 Multi-market scan │ %d valid markets", len(all_valid))
    for m in all_valid[:5]:
        log.info("  → %s │ mid=%dc │ spread=%dc",
                 m.get("ticker", "?"), m["yes_mid"], m["yes_ask"] - m["yes_bid"])
    return all_valid


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1: NEAR-MONEY ORDER BOOK PRESSURE + ADAPTIVE THRESHOLD (v5.3.0)
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


def adaptive_ob_threshold(total_depth: float) -> float:
    """v5.3.0: Scale OB confidence requirement based on book depth.

    Thin books ($5-15): higher threshold — need stronger signal to trust
    Medium books ($15-50): use profile default
    Thick books ($50+): slightly lower threshold — deep book is more reliable
    """
    base = PROFILE["ob_thresh"]
    if total_depth < 15:
        return max(base, 0.70)
    elif total_depth >= 50:
        return min(base, 0.58)
    else:
        return base


def calc_ob_imbalance(ob_data: dict, yes_mid: int) -> tuple:
    """Near-money depth only — within 10c of current mid.
    v5.3.0: uses adaptive threshold based on book depth.
    """
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
        return 0.5, "NONE", total

    yr     = yes_d / total
    nr     = no_d  / total
    thresh = adaptive_ob_threshold(total)
    log.info("OB │ Near-money: yes=$%.0f no=$%.0f yes_ratio=%.1f%% thresh=%.0f%% (depth=$%.0f)",
        yes_d, no_d, yr * 100, thresh * 100, total)

    if yr >= thresh: return yr, "YES", total
    if nr >= thresh: return nr, "NO",  total
    return max(yr, nr), "NONE", total


# ─────────────────────────────────────────────────────────────────────────────
# OB TREND DETECTION (v5.3.0)
# ─────────────────────────────────────────────────────────────────────────────

def ob_trend_check(ticker: str, imbalance: float, direction: str) -> bool:
    """v5.3.0: Only trade if OB pressure is building or stable, not fading.

    Compares current OB snapshot to the previous one for this ticker.
    Skips if: direction flipped, or imbalance dropped >5%.
    """
    global _prev_ob
    prev = _prev_ob.get(ticker)
    _prev_ob[ticker] = (imbalance, direction, time.time())

    if prev is None:
        return True  # first observation, allow

    prev_imb, prev_dir, prev_ts = prev

    # Stale previous data (>10 min old) — treat as fresh
    if time.time() - prev_ts > 600:
        return True

    # Direction flip = unstable pressure
    if prev_dir != direction and prev_dir != "NONE":
        log.info("OB trend │ Direction flipped %s→%s. Pressure unstable. Skipping.",
                 prev_dir, direction)
        return False

    # Imbalance fading >5%
    if imbalance < prev_imb - 0.05:
        log.info("OB trend │ Pressure fading %.1f%%→%.1f%%. Skipping.",
                 prev_imb * 100, imbalance * 100)
        return False

    log.info("OB trend │ Pressure stable/building %.1f%%→%.1f%%. OK.",
             prev_imb * 100, imbalance * 100)
    return True


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
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b          = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly = max(0.0, (b * win_prob - (1 - win_prob)) / b)
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
    if pnl <= -MAX_DAILY_LOSS:
        log.warning("DAILY LOSS LIMIT │ $%.2f lost (cap $%.2f). Halting.",
            abs(pnl), MAX_DAILY_LOSS)
        telegram_halt(f"Daily loss cap ${MAX_DAILY_LOSS:.0f} hit. PnL: ${pnl:.2f}", balance)
        return False
    if session_stop_threshold > 0 and balance < session_stop_threshold:
        log.warning("SESSION STOP │ Balance $%.2f < threshold $%.2f.",
            balance, session_stop_threshold)
        telegram_halt(
            f"Session stop hit. Balance ${balance:.2f} < ${session_stop_threshold:.2f}.",
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
    spread = yes_ask - yes_bid
    if spread <= 0:
        log.info("Spread │ %dc — crossed/zero spread. Skipping.", spread)
        return False
    return True


def expiry_guard(yes_mid: int) -> bool:
    if yes_mid > 85 or yes_mid < 15:
        log.info("Expiry guard │ %dc — near-certain outcome. Skipping.", yes_mid)
        return False
    return True


def liquidity_hours_check() -> bool:
    """v5.3.0: Skip low-liquidity hours where OB noise is highest.

    Default: skip 4-8 UTC (midnight-4am ET). Configurable via env.
    """
    hour = datetime.now(timezone.utc).hour
    if LOW_LIQ_START_UTC <= hour < LOW_LIQ_END_UTC:
        log.info("Liquidity filter │ Hour %d UTC — low liquidity window [%d-%d]. Skipping.",
                 hour, LOW_LIQ_START_UTC, LOW_LIQ_END_UTC)
        return False
    return True


def concurrent_position_check() -> bool:
    """v5.3.0: Cap simultaneous open positions."""
    if len(open_orders) >= MAX_CONCURRENT_POS:
        log.info("Concurrent limit │ %d positions open (max %d). Skipping.",
                 len(open_orders), MAX_CONCURRENT_POS)
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
            "entry_btc_price": btc_prices[-1] if btc_prices else None,  # v5.3.0
        }
        trade_history.append(record)
        open_orders[client_id] = record
        log.info("🟡 PAPER │ %s %s │ %d @ %dc │ $%.2f │ bal=$%.2f │ [%s]",
            direction, ticker[-15:], count, limit_price_cents,
            cost, paper_balance, ACTIVE_MODE.value.upper())
        return client_id

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
            "entry_btc_price": btc_prices[-1] if btc_prices else None,  # v5.3.0
        }
        trade_history.append(record)
        open_orders[order_id] = record
        active_tickers.add(ticker)
        log.info("✅ ORDER │ %s %s │ %d @ %dc │ $%.2f │ ID:%s │ [%s]",
            direction, ticker[-15:], count, limit_price_cents,
            size_dollars, order_id[:12], ACTIVE_MODE.value.upper())
        live_bal = get_live_balance()
        tg.send_trade_entry_notification(
            ticker=ticker, direction=direction, cost=cost,
            price_cents=limit_price_cents, balance=live_bal,
            ob_pct=ob_pct, edge_pct=edge_pct,
        )
        return order_id
    except requests.HTTPError as e:
        log.error("Order failed │ HTTP %s │ %s", e.response.status_code, e.response.text[:200])
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DECISION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_decision(market: dict, current_balance: float) -> None:
    global consecutive_losses, last_signal_desc
    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)

    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        return

    yes_mid = (yes_bid + yes_ask) // 2

    # ── Guard stack ────────────────────────────────────────────────────────
    if not balance_floor_check(current_balance):  return
    if not expiry_guard(yes_mid):                 return
    if not spread_check(yes_bid, yes_ask):        return
    if not liquidity_hours_check():               return   # v5.3.0
    if not concurrent_position_check():           return   # v5.3.0
    if ticker in active_tickers:
        log.info("Position guard │ Already in %s. Skipping.", ticker[-15:])
        return
    if not cooldown_passed():                     return
    if not daily_loss_check(current_balance):     return

    # ── Streak filter ──────────────────────────────────────────────────────
    MAX_CONSEC_LOSSES = int(os.environ.get("MAX_CONSEC_LOSSES", "3"))
    if consecutive_losses >= MAX_CONSEC_LOSSES:
        log.info("Streak filter │ %d consecutive losses. Skipping one window.",
                 consecutive_losses)
        consecutive_losses = 0
        return

    # ── OB Signal (v5.3.0: returns total_depth for logging) ───────────────
    ob_data                       = get_order_book(ticker)
    imbalance, ob_dir, ob_depth   = calc_ob_imbalance(ob_data, yes_mid)

    if ob_dir == "NONE":
        log.info("No OB signal (imb=%.0f%% depth=$%.0f) — skipping.",
            imbalance * 100, ob_depth)
        last_signal_desc = f"OB flat ({imbalance*100:.0f}%)"
        return

    # ── OB Trend Detection (v5.3.0) ───────────────────────────────────────
    if not ob_trend_check(ticker, imbalance, ob_dir):
        last_signal_desc = f"OB fading {ob_dir} ({imbalance*100:.0f}%)"
        return

    # ── BTC Momentum Confirmation ──────────────────────────────────────────
    momentum_verdict, momentum_boost = btc_momentum_signal(ob_dir)

    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB says %s but BTC disagrees. Skipping.", ob_dir)
        last_signal_desc = f"CONFLICT: OB={ob_dir} vs BTC"
        return

    win_prob = min(0.92, imbalance + momentum_boost)

    log.info(
        "📡 %s │ OB: %s %.1f%% (depth=$%.0f) │ BTC: %s (+%.2f) │ WinProb: %.1f%% │ "
        "bid/mid/ask: %d/%d/%dc │ [%s]",
        ticker, ob_dir, imbalance * 100, ob_depth,
        momentum_verdict, momentum_boost,
        win_prob * 100, yes_bid, yes_mid, yes_ask, ACTIVE_MODE.value.upper(),
    )

    # ── Price breakeven guard ──────────────────────────────────────────────
    if ob_dir == "YES":
        if yes_mid > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ YES at %dc > breakeven %dc.", yes_mid, YES_BREAKEVEN_PRICE)
            return
        trade_direction = "YES"
        contract_price  = yes_mid
    else:
        no_price = 100 - yes_mid
        if no_price > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ NO at %dc > breakeven %dc.", no_price, YES_BREAKEVEN_PRICE)
            return
        trade_direction = "NO"
        contract_price  = no_price

    if not (PROFILE["min_price"] <= contract_price <= PROFILE["max_price"]):
        log.info("Bias filter │ %dc outside [%d–%d]c for %s",
            contract_price, PROFILE["min_price"], PROFILE["max_price"], ACTIVE_MODE.value)
        return

    # ── Edge filter ────────────────────────────────────────────────────────
    edge = calc_edge(win_prob, contract_price)
    if edge < PROFILE["min_edge"]:
        log.info("Edge │ %.3f < min %.3f.", edge, PROFILE["min_edge"])
        return

    # ── Kelly sizing ───────────────────────────────────────────────────────
    bet = kelly_bet_size(win_prob, contract_price, current_balance)
    if bet < 0.25:
        log.info("Kelly │ $%.2f too small.", bet)
        return
    if current_balance < bet:
        log.warning("Insufficient balance │ $%.2f < bet $%.2f.", current_balance, bet)
        return

    # ── Maker limit price ─────────────────────────────────────────────────
    if trade_direction == "YES":
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        no_best     = 100 - yes_ask
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))
    limit_price = max(1, min(99, limit_price))

    if abs(limit_price - contract_price) > 8:
        log.info("Limit drift │ %dc too far from mid %dc.", limit_price, contract_price)
        return

    last_signal_desc = (f"SIGNAL {trade_direction} OB:{win_prob*100:.0f}% "
                        f"Edge:{edge*100:.1f}% Depth:${ob_depth:.0f}")
    log.info(
        "📈 SIGNAL │ %s │ OB:%.1f%% │ Edge:%.2f%% │ Bet:$%.2f │ Limit:%dc │ %s │ Depth:$%.0f",
        trade_direction, win_prob * 100, edge * 100, bet, limit_price,
        momentum_verdict, ob_depth,
    )

    place_limit_order(ticker, trade_direction, bet, limit_price,
                      ob_pct=win_prob * 100, edge_pct=edge * 100)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global session_start_balance, session_stop_threshold, daily_pnl, active_tickers
    global paper_balance, paper_daily_pnl, last_trade_ts, last_daily_summary_ts
    global consecutive_losses, last_signal_desc, last_heartbeat_ts, running_pnl

    init_base_url()

    paper_balance = float(os.environ.get("PAPER_BALANCE", "25.0"))

    log.info("━" * 70)
    log.info("  BOT_VERSION: %s", BOT_VERSION)
    tg.validate_telegram_connection()
    log.info("  JOHNNY5 %s │ %s │ Archetype: %s", BOT_VERSION,
             "PAPER 🟡" if DEMO_MODE else "LIVE 🔴", ACTIVE_MODE.value.upper())
    log.info("  %s", PROFILE["description"])
    log.info("  Max trade: $%.2f │ Kelly: %.0f%% │ Min edge: %.1f%% │ Daily cap: $%.2f",
             TRADE_SIZE_DOLLARS, PROFILE["kelly_frac"]*100, PROFILE["min_edge"]*100, MAX_DAILY_LOSS)
    log.info("  Breakeven: %dc │ Floor: $%.2f │ Stale cancel: %ds │ Max positions: %d",
             YES_BREAKEVEN_PRICE, MIN_BALANCE_FLOOR, STALE_ORDER_TIMEOUT, MAX_CONCURRENT_POS)
    log.info("  Low-liquidity skip: %d-%d UTC │ OB trend detection: ON",
             LOW_LIQ_START_UTC, LOW_LIQ_END_UTC)
    log.info("  %s", "📋 PAPER (BTC-movement sim)" if DEMO_MODE else "⚠️  LIVE — real money")
    log.info("━" * 70)

    if DEMO_MODE:
        running_pnl = 0.0
        session_stop_threshold = paper_balance * 0.50
        log.info("Paper balance: $%.2f | Session stop: $%.2f", paper_balance, session_stop_threshold)
        telegram_boot(paper_balance)
    else:
        bal = get_live_balance()
        session_start_balance = bal
        session_stop_threshold = bal * 0.50
        open_orders.clear()
        active_tickers.clear()
        consecutive_losses = 0
        running_pnl = 0.0
        log.info("Live balance: $%.2f | Session stop: $%.2f", bal, session_stop_threshold)
        telegram_boot(bal)

    resolve_cycle = 0

    while True:
        try:
            # ── 15-minute heartbeat ────────────────────────────────────────
            if time.time() - last_heartbeat_ts >= 900:
                last_heartbeat_ts = time.time()
                hb_bal = paper_balance if DEMO_MODE else get_live_balance()
                hb_pnl = paper_daily_pnl if DEMO_MODE else (hb_bal - session_start_balance)
                hb_open = len(open_orders)
                resolved = [t for t in trade_history if t.get("result") in ("win", "loss")]
                hb_wins  = sum(1 for t in resolved if t["result"] == "win")
                hb_total = len(resolved)
                # v5.3.0: add confidence interval to heartbeat
                wr_pct, wr_lo, wr_hi = wilson_confidence(hb_wins, hb_total)
                conf_str = f" WR:{wr_pct:.0f}% [{wr_lo:.0f}-{wr_hi:.0f}%]" if hb_total >= 5 else ""
                tg.send_heartbeat(
                    balance=hb_bal, session_pnl=hb_pnl,
                    open_count=hb_open, trades_today=hb_total,
                    last_signal=last_signal_desc + conf_str,
                )

            # ── v5.3.0: Cancel stale unfilled orders every cycle ───────────
            cancel_stale_orders()

            # ── v5.3.0: Multi-market scan ──────────────────────────────────
            markets = get_active_btc_markets()
            if not markets:
                log.info("No active BTC market. Waiting %ds...", POLL_INTERVAL)
                last_signal_desc = "no market"
                time.sleep(POLL_INTERVAL)
                continue

            update_btc_price(markets[0])

            # Clear expired position locks — only for tickers NOT in current markets
            current_tickers = {m.get("ticker", "") for m in markets}
            expired = active_tickers - current_tickers
            if expired:
                log.info("Clearing expired position locks: %s", expired)
                active_tickers -= expired

            current_balance = paper_balance if DEMO_MODE else get_live_balance()

            # v5.3.0: try each market until one trades (or all skip)
            for market in markets:
                run_decision(market, current_balance)
                # Refresh balance after a trade may have fired
                if DEMO_MODE:
                    current_balance = paper_balance
                # Cooldown will block further trades this cycle anyway

            resolve_cycle += 1
            if resolve_cycle % 10 == 0:
                resolve_open_orders()

                resolved = [t for t in trade_history if t.get("result") in ("win", "loss")]
                wins   = sum(1 for t in resolved if t["result"] == "win")
                losses = len(resolved) - wins
                total  = len(resolved)
                wr     = wins / total if total > 0 else 0.0
                wr_pct, wr_lo, wr_hi = wilson_confidence(wins, total)

                if DEMO_MODE:
                    conf_str = f" │ 95%CI: [{wr_lo:.0f}-{wr_hi:.0f}%]" if total >= 5 else ""
                    log.info(
                        "📋 PAPER STATUS │ Bal:$%.2f │ PnL:$%+.2f │ "
                        "Trades:%d │ WR:%.1f%%%s │ Open:%d",
                        paper_balance, paper_daily_pnl,
                        total, wr * 100, conf_str, len(open_orders),
                    )
                else:
                    live_bal  = get_live_balance()
                    daily_pnl = live_bal - session_start_balance
                    trade_pnls = [t.get("pnl", 0) for t in trade_history
                                  if t.get("pnl") is not None
                                  and t.get("result") in ("win", "loss")
                                  and t.get("pnl") != 0]
                    if len(trade_pnls) >= 3:
                        sr_mean = sum(trade_pnls) / len(trade_pnls)
                        sr_std  = (sum((x - sr_mean)**2 for x in trade_pnls) / len(trade_pnls)) ** 0.5
                        sharpe  = (sr_mean / sr_std) if sr_std > 0 else 0.0
                        sharpe_str = f" │ Sharpe:{sharpe:.2f}"
                    else:
                        sharpe_str = ""
                    conf_str = f" │ 95%CI:[{wr_lo:.0f}-{wr_hi:.0f}%]" if total >= 5 else ""
                    log.info(
                        "Portfolio │ Bal:$%.2f │ PnL:$%+.2f │ Open:%d │ "
                        "WR:%.1f%%%s%s",
                        live_bal, daily_pnl, len(open_orders),
                        wr * 100, conf_str, sharpe_str,
                    )

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
