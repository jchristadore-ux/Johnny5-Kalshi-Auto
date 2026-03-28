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

BOT_VERSION = "6.0.0"  # bump with every deploy

import base64
import logging
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
    # QUANT: Regime-aware, high-selectivity build. Only trades with measurable statistical edge.
    # v6.0.0: Raised all thresholds. Requires TRENDING regime + AGREE momentum + deep book.
    TraderMode.QUANT: {
        "description":  "Regime-aware quant. Requires TRENDING + AGREE + deep OB. Kelly 35%.",
        "min_price":    35,   # contract price floor (cents)
        "max_price":    65,   # contract price ceiling
        "kelly_frac":   float(os.environ.get("KELLY_FRACTION", "0.35")),
        "ob_thresh":    0.70,  # raised from 0.62 — requires stronger institutional signal
        "vol_filter":   "both",
        "min_edge":     0.06,  # raised from 0.04 — higher bar for positive EV
        "cooldown":     120,   # raised from 60s — fewer, better trades
        "maker_only":   True,
        "min_spread":   2,    # cents
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

# ── v6.0.0: Quantitative safeguard parameters ────────────────────────────────
# Minimum composite confidence score (0-100) required before a trade fires.
# Score combines: OB strength, book depth, regime, momentum, time-to-expiry.
# At 65: requires TRENDING regime + strong OB + AGREE momentum to clear the bar.
MINIMUM_CONFIDENCE    = int(os.environ.get("MINIMUM_CONFIDENCE", "65"))

# Minimum total near-money order book depth ($) required to consider OB signal valid.
# At $50: requires real institutional participation, not 1-2 retail orders.
MIN_OB_DEPTH_DOLLARS  = float(os.environ.get("MIN_OB_DEPTH_DOLLARS", "50.0"))

# Minimum minutes remaining before market close to allow a new entry.
# At 3 min: no new entries in the last 3 minutes of a 15-min window.
MIN_MINUTES_TO_EXPIRY = float(os.environ.get("MIN_MINUTES_TO_EXPIRY", "3.0"))

# When True: BTC momentum must explicitly AGREE with OB direction.
# NEUTRAL (flat BTC) is no longer sufficient — we need directional confirmation.
REQUIRE_AGREE_MOMENTUM = os.environ.get("REQUIRE_AGREE_MOMENTUM", "true").lower() == "true"

# Maximum fraction of balance allowed per trade (hard cap on Kelly output).
MAX_BET_FRACTION      = float(os.environ.get("MAX_BET_FRACTION", "0.10"))

# Minimum number of live settled trades before performance guard activates.
MIN_SAMPLE_TRADES     = int(os.environ.get("MIN_SAMPLE_TRADES", "20"))

# UTC hours considered low-liquidity (thin books, unreliable signals).
# Default: 0-4 UTC = 7pm-midnight ET = after US close, before Asia opens.
_low_liq_raw = os.environ.get("LOW_LIQ_HOURS_UTC", "0,1,2,3,4")
LOW_LIQ_HOURS_UTC: set = {int(h.strip()) for h in _low_liq_raw.split(",") if h.strip()}


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
consecutive_losses:    int   = 0      # streak filter: pause after N in a row
last_signal_desc:      str   = "none yet"  # for heartbeat
running_pnl:           float = 0.0         # cumulative session P&L (resets at boot)
last_heartbeat_ts:     float = 0.0         # timestamp of last heartbeat

# v6.0.0 state
streak_pause_until:    float = 0.0    # timestamp: bot won't trade until this time
live_wins:             int   = 0      # settled wins this session (live + paper)
live_losses:           int   = 0      # settled losses this session (live + paper)


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

# FIX v5.2.1: restored timed backoff (v5.2.0 regressed this to a permanent-failure flag).
# A transient Kraken outage now backs off 5 min then retries, instead of dying for the session.
_btc_feed_backoff_until: float = 0.0

def fetch_btc_price() -> Optional[float]:
    """Fetch BTC/USD from Kraken public ticker, Coinbase as fallback.

    On persistent failure, backs off for 5 minutes before retrying.
    This prevents log spam while still recovering when the feed comes back.
    """
    global _btc_feed_backoff_until
    if time.time() < _btc_feed_backoff_until:
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
    # Back off for 5 minutes before retrying — avoids log spam, allows recovery
    log.debug("BTC price feed unavailable — backing off 5 min, using NEUTRAL momentum")
    _btc_feed_backoff_until = time.time() + 300
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
# REGIME DETECTION — v6.0.0
# Classifies current BTC price behaviour. We only trade in TRENDING markets.
# In RANGING or UNKNOWN regimes, OB imbalance signals have no predictive value.
# ─────────────────────────────────────────────────────────────────────────────

def compute_btc_regime() -> tuple[str, float]:
    """
    Classify the current BTC price regime using linear regression on the
    rolling price deque (last 10 samples = ~5 minutes at 30s poll).

    Returns: (regime, r_squared)

    Regimes:
      TRENDING  — strong directional move (R² > 0.65). OB signals are meaningful.
      RANGING   — oscillating, no direction (R² ≤ 0.65). OB signals are noise.
      HIGH_VOL  — mean absolute return > 0.15% per 30s (chaotic, avoid all trades).
      UNKNOWN   — insufficient data (< 8 samples). Conservative: treat as RANGING.

    Design rationale:
      The 68.8% OB signal accuracy was measured during trending conditions.
      In ranging markets the OB flips direction every few candles — the smart
      money positioning thesis breaks down entirely. The R² test is the
      simplest reliable way to separate these two regimes without look-ahead.
    """
    if len(btc_prices) < 8:
        return "UNKNOWN", 0.0

    prices = list(btc_prices)[-10:]
    n = len(prices)

    # Linear regression: fit y = a + b*x, compute R²
    xs     = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(prices) / n
    ss_xx  = sum((x - mean_x) ** 2 for x in xs)
    ss_xy  = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, prices))
    ss_yy  = sum((y - mean_y) ** 2 for y in prices)

    if ss_xx == 0 or ss_yy == 0:
        return "UNKNOWN", 0.0

    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)

    # Volatility: mean absolute return per 30-second bar
    returns = [abs((prices[i] - prices[i - 1]) / prices[i - 1])
               for i in range(1, n) if prices[i - 1] > 0]
    mean_abs_return = sum(returns) / len(returns) if returns else 0.0

    # HIGH_VOL: > 0.15% per 30s bar → ~18% annualized vol on 30s bars. Avoid.
    if mean_abs_return > 0.0015:
        log.info("Regime │ HIGH_VOL (mean_abs_ret=%.4f%%, R²=%.2f)",
                 mean_abs_return * 100, r_squared)
        return "HIGH_VOL", r_squared

    if r_squared > 0.65:
        direction = "UP" if ss_xy > 0 else "DOWN"
        log.info("Regime │ TRENDING %s (R²=%.2f, mean_ret=%.4f%%)",
                 direction, r_squared, mean_abs_return * 100)
        return "TRENDING", r_squared

    log.info("Regime │ RANGING (R²=%.2f, mean_abs_ret=%.4f%%)",
             r_squared, mean_abs_return * 100)
    return "RANGING", r_squared


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY — time to market expiry
# ─────────────────────────────────────────────────────────────────────────────

def minutes_to_expiry(market: dict) -> float:
    """Return minutes remaining until market closes. Returns 999 if unknown."""
    close_time_str = market.get("close_time")
    if not close_time_str:
        return 999.0
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        now_dt   = datetime.now(timezone.utc)
        delta    = (close_dt - now_dt).total_seconds() / 60.0
        return max(0.0, delta)
    except Exception:
        return 999.0


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE SCORING — v6.0.0
# A trade only fires when this score reaches MINIMUM_CONFIDENCE (default 65).
# The score forces all four favourable conditions to align simultaneously.
# No single factor can carry the trade alone.
# ─────────────────────────────────────────────────────────────────────────────

def compute_confidence_score(
    ob_quality: dict,
    regime: str,
    r_squared: float,
    momentum_verdict: str,
    momentum_boost: float,
    mins_remaining: float,
) -> float:
    """
    Composite trade confidence score (0–100).

    Component breakdown (max points):
      OB imbalance strength  30 pts   — linear from ob_thresh to 1.0
      OB near-money depth    20 pts   — $50=10pts, $200=20pts (capped)
      Market regime          25 pts   — TRENDING=25, UNKNOWN=5, RANGING=0, HIGH_VOL=-10
        + trend strength      5 pts   — bonus for high R² in TRENDING
      BTC momentum           15 pts   — only counts if AGREE; scales with boost magnitude
      Time remaining         10 pts   — full credit at ≥10 min, zero at MIN_MINUTES_TO_EXPIRY

    Minimum to trade: MINIMUM_CONFIDENCE (default 65).

    To reach 65 with default settings you need approximately:
      TRENDING (25) + strong OB ≥70% (≥10) + depth ≥$100 (≥10) + AGREE momentum (≥5)
        + 8+ min remaining (≥8) = 58+. Marginal cases fail; clear setups pass.
    """
    imbalance = ob_quality.get("imbalance", 0.5)
    depth     = ob_quality.get("near_money_depth", 0.0)
    thresh    = PROFILE["ob_thresh"]

    # OB imbalance: 0-30 pts (linear scale above threshold)
    imb_pts = max(0.0, (imbalance - thresh) / (1.0 - thresh)) * 30.0

    # OB depth: 0-20 pts ($50 = 10pts, $200 = 20pts)
    depth_pts = min(20.0, max(0.0, depth / 10.0))

    # Regime: -10 to 30 pts
    regime_base = {"TRENDING": 25.0, "UNKNOWN": 5.0, "RANGING": 0.0, "HIGH_VOL": -10.0}
    regime_pts  = regime_base.get(regime, 0.0)
    if regime == "TRENDING":
        regime_pts += min(5.0, r_squared * 5.0)  # up to +5 for strong R²

    # Momentum: 0-15 pts (only AGREE contributes)
    momentum_pts = 0.0
    if momentum_verdict == "AGREE":
        momentum_pts = min(15.0, momentum_boost * 250.0)

    # Time: 0-10 pts
    time_pts = min(10.0, max(0.0,
        (mins_remaining - MIN_MINUTES_TO_EXPIRY) / max(1.0, 10.0 - MIN_MINUTES_TO_EXPIRY) * 10.0
    ))

    total = imb_pts + depth_pts + regime_pts + momentum_pts + time_pts

    log.info(
        "Confidence │ imb=%.1f depth=%.1f regime=%.1f momentum=%.1f time=%.1f "
        "→ SCORE=%.0f/100 (min=%d)",
        imb_pts, depth_pts, regime_pts, momentum_pts, time_pts, total, MINIMUM_CONFIDENCE,
    )

    return max(0.0, min(100.0, total))


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICAL PERFORMANCE GUARD — v6.0.0
# Wilson score confidence interval: validates that live win rate is
# statistically above breakeven before allowing continued trading.
# ─────────────────────────────────────────────────────────────────────────────

def wilson_lower_bound(wins: int, total: int, z: float = 1.645) -> float:
    """
    Wilson score confidence interval lower bound (90% CI, z=1.645).

    Returns the worst-case estimated win rate at 90% confidence.
    If this lower bound is below 50%, the live edge has not been demonstrated.

    Returns 0.0 when sample size < 10 (insufficient data).
    """
    if total < 10:
        return 0.0
    p      = wins / total
    denom  = 1.0 + z ** 2 / total
    center = (p + z ** 2 / (2.0 * total)) / denom
    spread = (z * (p * (1.0 - p) / total + z ** 2 / (4.0 * total ** 2)) ** 0.5) / denom
    return max(0.0, center - spread)


def performance_guard() -> bool:
    """
    Halt trading if the live Wilson CI lower bound falls below 50%.
    Activates only after MIN_SAMPLE_TRADES settled trades.

    This guards against trading through a degraded-edge environment.
    If we can't demonstrate statistically that we're above a coin flip,
    we have no business risking capital.

    Returns True (allow trade) if:
      - Sample too small (< MIN_SAMPLE_TRADES) — give benefit of the doubt
      - Wilson lower bound ≥ 50%

    Returns False (block trade) if Wilson lower bound < 50%.
    """
    total = live_wins + live_losses
    if total < MIN_SAMPLE_TRADES:
        return True  # insufficient sample — keep trading with benefit of doubt

    wlb = wilson_lower_bound(live_wins, total)
    if wlb < 0.50:
        log.warning(
            "PERFORMANCE GUARD │ Wilson CI lower bound %.1f%% < 50%% "
            "(wins=%d / total=%d). Win rate unproven. Halting new entries.",
            wlb * 100, live_wins, total,
        )
        return False

    log.debug("Performance guard │ WLB=%.1f%% OK (wins=%d/total=%d)",
              wlb * 100, live_wins, total)
    return True


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
    global live_wins, live_losses, streak_pause_until

    if not open_orders:
        return

    STREAK_THRESHOLD = int(os.environ.get("MAX_CONSEC_LOSSES", "2"))
    STREAK_PAUSE_SEC = int(os.environ.get("STREAK_PAUSE_SECS", "1800"))  # 30 minutes

    if DEMO_MODE:
        import random
        now = time.time()
        for oid in list(open_orders.keys()):
            trade = open_orders[oid]
            if now - trade.get("placed_at", now) > 900:
                open_orders.pop(oid)
                ticker = trade.get("ticker", "")
                active_tickers.discard(ticker)
                won   = random.random() < 0.685  # paper sim uses historical signal accuracy
                count = trade.get("count", 0)
                cost  = trade.get("cost", 0.0)

                # v6.0.0 P&L fix:
                # At entry, paper_balance was decremented by `cost` (correct).
                # On WIN: add full payout (`count` dollars) — cost already deducted at entry.
                #   Net: -cost + count = profit. e.g. 2 contracts @ 50¢: -1.00 + 2.00 = +1.00
                # On LOSS: no balance change (cost was already deducted at entry, payout=0).
                # v5 bug: added `pnl = count - cost` instead of `count` — showing breakeven on wins.
                if won:
                    paper_balance   += count           # full payout
                    trade_pnl        = round(count - cost, 2)  # actual profit
                    paper_daily_pnl += trade_pnl
                else:
                    trade_pnl        = round(-cost, 2)  # full loss of stake
                    paper_daily_pnl += trade_pnl
                    # paper_balance already reduced at entry; no further change

                result = "win" if won else "loss"
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = result
                        t["pnl"]    = round(trade_pnl, 4)
                        break
                outcome_str = f"+${trade_pnl:.2f}" if won else f"-${cost:.2f}"
                running_pnl += trade_pnl

                if won:
                    consecutive_losses = 0
                    live_wins += 1
                    tg.send_win_notification(
                        profit=trade_pnl,
                        balance=paper_balance,
                        daily_pnl=paper_daily_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                    )
                else:
                    consecutive_losses += 1
                    live_losses += 1
                    if consecutive_losses >= STREAK_THRESHOLD:
                        streak_pause_until = time.time() + STREAK_PAUSE_SEC
                        log.warning(
                            "Streak pause activated │ %d consecutive losses — "
                            "pausing new entries for %.0f min.",
                            consecutive_losses, STREAK_PAUSE_SEC / 60,
                        )
                    tg.send_loss_notification(
                        loss=abs(trade_pnl),
                        balance=paper_balance,
                        daily_pnl=paper_daily_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                        streak=consecutive_losses,
                    )
                log.info(
                    "📋 PAPER SETTLED │ %s │ %s │ %s → %s │ "
                    "paper_bal=$%.2f │ streak=%d │ WR=%d/%d",
                    ticker[-15:], trade.get("side", "?"), result.upper(),
                    outcome_str, paper_balance, consecutive_losses,
                    live_wins, live_wins + live_losses,
                )
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
                balance = get_live_balance()
                running_pnl   += pnl
                live_daily_pnl = balance - session_start_balance
                wlb = wilson_lower_bound(live_wins, live_wins + live_losses)
                log.info(
                    "✅ SETTLED │ %s │ %s │ pnl=$%.2f │ WR=%d/%d │ WilsonLB=%.1f%%",
                    ticker[-15:], result.upper(), pnl,
                    live_wins, live_wins + live_losses, wlb * 100,
                )
                if won:
                    consecutive_losses = 0
                    live_wins += 1
                    tg.send_win_notification(
                        profit=pnl,
                        balance=balance,
                        daily_pnl=live_daily_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                    )
                else:
                    consecutive_losses += 1
                    live_losses += 1
                    if consecutive_losses >= STREAK_THRESHOLD:
                        streak_pause_until = time.time() + STREAK_PAUSE_SEC
                        log.warning(
                            "Streak pause activated │ %d consecutive losses — "
                            "pausing new entries for %.0f min.",
                            consecutive_losses, STREAK_PAUSE_SEC / 60,
                        )
                    else:
                        log.info("Streak │ %d consecutive losses", consecutive_losses)
                    tg.send_loss_notification(
                        loss=abs(pnl),
                        balance=balance,
                        daily_pnl=live_daily_pnl,
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


def calc_ob_quality(ob_data: dict, yes_mid: int) -> dict:
    """
    Enhanced near-money order book analysis (v6.0.0).

    Examines depth within ±10¢ of mid price. Returns a quality dict:
      imbalance         — dominant-side ratio (0.5–1.0)
      direction         — "YES" | "NO" | "NONE"
      near_money_depth  — total $ depth in the near-money zone
      level_count_yes   — number of distinct YES price levels
      level_count_no    — number of distinct NO price levels

    Key changes from v5 calc_ob_imbalance:
      - Minimum depth raised from $5 to MIN_OB_DEPTH_DOLLARS ($50 default).
        A $5 threshold allowed a single retail order to generate a signal.
        $50 requires real, multi-party participation.
      - OB threshold raised to 0.70 in QUANT profile (was 0.62).
      - Returns quality dict so downstream layers can inspect depth, level count.
    """
    ob_fp      = ob_data.get("orderbook_fp", {})
    yes_levels = ob_fp.get("yes_dollars", [])
    no_levels  = ob_fp.get("no_dollars",  [])
    near       = 10
    y_lo, y_hi = (yes_mid - near) / 100.0, (yes_mid + near) / 100.0
    n_mid       = (100 - yes_mid) / 100.0
    n_lo, n_hi  = n_mid - near / 100.0, n_mid + near / 100.0

    def near_depth_info(levels, lo, hi):
        total_depth = 0.0
        level_count = 0
        for e in levels:
            try:
                price = float(e[0])
                size  = float(e[1])
                if lo <= price <= hi and size > 0:
                    total_depth += size
                    level_count += 1
            except Exception:
                pass
        return total_depth, level_count

    yes_d, yes_lc = near_depth_info(yes_levels, y_lo, y_hi)
    no_d,  no_lc  = near_depth_info(no_levels,  n_lo, n_hi)
    total  = yes_d + no_d
    thresh = PROFILE["ob_thresh"]

    log.info(
        "OB │ Near-money: YES=$%.0f(%d lvls) NO=$%.0f(%d lvls) "
        "total=$%.0f min=$%.0f thresh=%.0f%%",
        yes_d, yes_lc, no_d, no_lc, total, MIN_OB_DEPTH_DOLLARS, thresh * 100,
    )

    if total < MIN_OB_DEPTH_DOLLARS:
        log.info(
            "OB │ Depth $%.0f < minimum $%.0f — insufficient liquidity. NONE.",
            total, MIN_OB_DEPTH_DOLLARS,
        )
        return {"imbalance": 0.5, "direction": "NONE",
                "near_money_depth": total,
                "level_count_yes": yes_lc, "level_count_no": no_lc}

    yr = yes_d / total
    nr = no_d  / total

    if yr >= thresh:
        direction = "YES"
        imbalance = yr
    elif nr >= thresh:
        direction = "NO"
        imbalance = nr
    else:
        direction = "NONE"
        imbalance = max(yr, nr)

    return {"imbalance": imbalance, "direction": direction,
            "near_money_depth": total,
            "level_count_yes": yes_lc, "level_count_no": no_lc}


# Keep legacy name as alias so existing tests don't break
def calc_ob_imbalance(ob_data: dict, yes_mid: int) -> tuple:
    """Legacy wrapper around calc_ob_quality — returns (imbalance, direction)."""
    q = calc_ob_quality(ob_data, yes_mid)
    return q["imbalance"], q["direction"]


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
    """
    Fractional Kelly bet sizing (v6.0.0 — corrected formula).

    Kelly formula: f* = (b*p - q) / b
      where b = payout odds, p = win_prob, q = 1 - win_prob

    Fractional Kelly bet = f* × kelly_frac × balance

    Caps (take the minimum of all three):
      1. TRADE_SIZE_DOLLARS — hard dollar cap per trade
      2. MAX_BET_FRACTION × balance — max fraction of bankroll (default 10%)
      3. kelly_frac × full_kelly × balance — the fractional Kelly itself

    v5 bug: used `TRADE_SIZE_DOLLARS * 4.0` as proxy for bankroll (incorrect).
    This caused bets to NOT shrink as balance decayed — accelerating drawdowns.
    The corrected formula uses actual `balance`, so sizing self-adjusts.
    """
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b          = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly = max(0.0, (b * win_prob - (1.0 - win_prob)) / b)
    kelly_bet  = full_kelly * PROFILE["kelly_frac"] * balance
    return round(min(kelly_bet, TRADE_SIZE_DOLLARS, balance * MAX_BET_FRACTION), 2)


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
        tg.send_trade_entry_notification(
            ticker=ticker,
            direction=direction,
            cost=cost,
            price_cents=limit_price_cents,
            balance=paper_balance,
            ob_pct=ob_pct,
            edge_pct=edge_pct,
        )
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
    """
    v6.0.0 — Layered decision engine.

    A trade is only placed when ALL of the following are true simultaneously:
      Layer 1 — Hard guards:     balance floor, spread, position guard, cooldown, daily loss
      Layer 2 — Streak pause:    30-min cooldown after 2 consecutive losses
      Layer 3 — Performance:     Wilson CI lower bound ≥ 50% (after 20 trades)
      Layer 4 — Time filter:     ≥ MIN_MINUTES_TO_EXPIRY remaining, not low-liq hour
      Layer 5 — Regime:          BTC regime is TRENDING (not RANGING, HIGH_VOL, or UNKNOWN)
      Layer 6 — OB quality:      imbalance ≥ 70%, near-money depth ≥ $50
      Layer 7 — BTC momentum:    must explicitly AGREE (NEUTRAL no longer sufficient)
      Layer 8 — Confidence:      composite score ≥ MINIMUM_CONFIDENCE (default 65)
      Layer 9 — Edge & sizing:   EV > min_edge, Kelly bet ≥ $0.25

    Each layer logs why it passed or failed. Every trade that fires logs a full
    EDGE JUSTIFICATION record explaining exactly why it was taken.
    """
    global consecutive_losses, last_signal_desc, streak_pause_until

    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)

    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        return

    yes_mid = (yes_bid + yes_ask) // 2

    # ── Layer 1: Hard guards (fail fast) ──────────────────────────────────
    if not balance_floor_check(current_balance):
        return
    if not expiry_guard(yes_mid):
        return
    if not spread_check(yes_bid, yes_ask):
        return
    if ticker in active_tickers:
        log.info("Position guard │ Already in %s. Skipping.", ticker[-15:])
        return
    if not cooldown_passed():
        return
    if not daily_loss_check(current_balance):
        return

    # ── Layer 2: Streak pause ──────────────────────────────────────────────
    # After MAX_CONSEC_LOSSES (default 2) consecutive losses, the bot pauses
    # for STREAK_PAUSE_SECS (default 1800 = 30 minutes). This is a hard wait,
    # not a one-window skip. It forces regime to shift before re-entry.
    STREAK_THRESHOLD = int(os.environ.get("MAX_CONSEC_LOSSES", "2"))
    STREAK_PAUSE_SEC = int(os.environ.get("STREAK_PAUSE_SECS", "1800"))

    if consecutive_losses >= STREAK_THRESHOLD:
        now = time.time()
        if now < streak_pause_until:
            log.info(
                "Streak pause │ %d consecutive losses. Resuming in %.0f min.",
                consecutive_losses, (streak_pause_until - now) / 60,
            )
            last_signal_desc = f"streak pause ({consecutive_losses} losses)"
            return
        # Pause has expired — reset streak and resume
        log.info("Streak pause expired — resetting consecutive_losses, resuming.")
        consecutive_losses = 0

    # ── Layer 3: Statistical performance guard ─────────────────────────────
    if not performance_guard():
        last_signal_desc = "performance guard active (Wilson LB < 50%)"
        return

    # ── Layer 4: Time filters ──────────────────────────────────────────────
    # 4a: Low-liquidity UTC hours (thin books, unreliable signals)
    utc_hour = datetime.now(timezone.utc).hour
    if utc_hour in LOW_LIQ_HOURS_UTC:
        log.info(
            "Low-liq filter │ UTC hour %d in low-liq set %s. Skipping.",
            utc_hour, sorted(LOW_LIQ_HOURS_UTC),
        )
        last_signal_desc = f"low-liq hour UTC:{utc_hour}"
        return

    # 4b: Too close to market expiry (last MIN_MINUTES_TO_EXPIRY minutes)
    mins_remaining = minutes_to_expiry(market)
    if mins_remaining < MIN_MINUTES_TO_EXPIRY:
        log.info(
            "Expiry imminent │ %.1f min remaining < %.1f min minimum. Skipping.",
            mins_remaining, MIN_MINUTES_TO_EXPIRY,
        )
        last_signal_desc = f"expiry imminent ({mins_remaining:.1f}min)"
        return

    # ── Layer 5: Regime detection ──────────────────────────────────────────
    # Only trade when BTC is in a confirmed TRENDING regime.
    # In RANGING or UNKNOWN regimes, OB imbalance has no directional edge.
    # In HIGH_VOL, unpredictable swings invalidate all short-term signals.
    regime, r_squared = compute_btc_regime()
    if regime != "TRENDING":
        log.info(
            "Regime filter │ %s (R²=%.2f) — only TRENDING allowed. Skipping.",
            regime, r_squared,
        )
        last_signal_desc = f"regime={regime} (R²={r_squared:.2f})"
        return

    # ── Layer 6: OB signal quality ─────────────────────────────────────────
    ob_data    = get_order_book(ticker)
    ob_quality = calc_ob_quality(ob_data, yes_mid)
    ob_dir     = ob_quality["direction"]

    if ob_dir == "NONE":
        log.info(
            "OB │ No signal — imbalance=%.0f%% depth=$%.0f (min=$%.0f thresh=%.0f%%).",
            ob_quality["imbalance"] * 100,
            ob_quality["near_money_depth"],
            MIN_OB_DEPTH_DOLLARS,
            PROFILE["ob_thresh"] * 100,
        )
        last_signal_desc = (
            f"OB flat ({ob_quality['imbalance']*100:.0f}%"
            f" depth=${ob_quality['near_money_depth']:.0f})"
        )
        return

    # ── Layer 7: BTC momentum — AGREE required ─────────────────────────────
    # v6.0.0 change: NEUTRAL is no longer acceptable.
    # In v5, flat BTC (NEUTRAL) allowed OB to fire alone.
    # Now we require the spot market to explicitly confirm the direction.
    # This alone cuts trade frequency by ~40-60% in choppy/flat conditions.
    momentum_verdict, momentum_boost = btc_momentum_signal(ob_dir)

    if REQUIRE_AGREE_MOMENTUM and momentum_verdict != "AGREE":
        log.info(
            "Momentum filter │ Required AGREE, got %s (OB=%s). Skipping.",
            momentum_verdict, ob_dir,
        )
        last_signal_desc = f"momentum={momentum_verdict} (need AGREE, OB={ob_dir})"
        return

    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB=%s vs BTC. Skipping.", ob_dir)
        last_signal_desc = f"CONFLICT: OB={ob_dir} vs BTC"
        return

    # ── Layer 8: Confidence score ──────────────────────────────────────────
    # Composite score combining OB strength, depth, regime quality, momentum,
    # and time remaining. All four factors must align to clear the bar.
    confidence = compute_confidence_score(
        ob_quality      = ob_quality,
        regime          = regime,
        r_squared       = r_squared,
        momentum_verdict = momentum_verdict,
        momentum_boost  = momentum_boost,
        mins_remaining  = mins_remaining,
    )

    if confidence < MINIMUM_CONFIDENCE:
        log.info(
            "Confidence │ Score %.0f < minimum %d — no trade. "
            "[regime=%s R²=%.2f OB=%.0f%% depth=$%.0f momentum=%s]",
            confidence, MINIMUM_CONFIDENCE,
            regime, r_squared,
            ob_quality["imbalance"] * 100, ob_quality["near_money_depth"],
            momentum_verdict,
        )
        last_signal_desc = f"confidence {confidence:.0f}/{MINIMUM_CONFIDENCE}"
        return

    # win_prob = OB imbalance + momentum boost (capped at 92%)
    win_prob = min(0.92, ob_quality["imbalance"] + momentum_boost)

    log.info(
        "📡 %s │ Regime:%s(R²=%.2f) │ OB:%s %.1f%% depth=$%.0f │ "
        "BTC:%s(+%.2f%%) │ WinProb:%.1f%% │ Conf:%.0f/100 │ "
        "%.1fmin remain │ bid/mid/ask:%d/%d/%dc │ [%s]",
        ticker, regime, r_squared, ob_dir,
        ob_quality["imbalance"] * 100, ob_quality["near_money_depth"],
        momentum_verdict, momentum_boost * 100,
        win_prob * 100, confidence, mins_remaining,
        yes_bid, yes_mid, yes_ask, ACTIVE_MODE.value.upper(),
    )

    # ── Layer 9a: Price breakeven guard ───────────────────────────────────
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

    if not (PROFILE["min_price"] <= contract_price <= PROFILE["max_price"]):
        log.info("Bias filter │ %dc outside [%d–%d]c. Skipping.",
                 contract_price, PROFILE["min_price"], PROFILE["max_price"])
        return

    # ── Layer 9b: Edge filter ──────────────────────────────────────────────
    edge = calc_edge(win_prob, contract_price)
    if edge < PROFILE["min_edge"]:
        log.info("Edge │ %.3f < min %.3f. Skipping.", edge, PROFILE["min_edge"])
        return

    # ── Layer 9c: Kelly sizing ─────────────────────────────────────────────
    bet = kelly_bet_size(win_prob, contract_price, current_balance)
    if bet < 0.25:
        log.info("Kelly │ $%.2f too small. Skipping.", bet)
        return

    if current_balance < bet:
        log.warning("Insufficient balance │ $%.2f < bet $%.2f.", current_balance, bet)
        return

    # ── Maker limit price (1¢ inside spread) ──────────────────────────────
    if trade_direction == "YES":
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        no_best     = 100 - yes_ask
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))
    limit_price = max(1, min(99, limit_price))

    if abs(limit_price - contract_price) > 8:
        log.info("Limit drift │ %dc too far from mid %dc. Skipping.",
                 limit_price, contract_price)
        return

    # ── EDGE JUSTIFICATION — logged for every trade that fires ────────────
    # This is the audit trail. Every trade must be explainable by data.
    wlb_str = (
        f"WilsonLB={wilson_lower_bound(live_wins, live_wins + live_losses)*100:.1f}%"
        if (live_wins + live_losses) >= 10 else "WilsonLB=n/a(<10 trades)"
    )
    edge_justification = (
        f"EDGE JUSTIFICATION │ {trade_direction} {ticker[-15:]} @ {contract_price}¢ │ "
        f"Regime={regime}(R²={r_squared:.2f}) │ "
        f"OB={ob_quality['imbalance']*100:.0f}% depth=${ob_quality['near_money_depth']:.0f} │ "
        f"Momentum={momentum_verdict}(+{momentum_boost*100:.1f}%) │ "
        f"WinProb={win_prob*100:.1f}% Edge={edge*100:.2f}% │ "
        f"Confidence={confidence:.0f}/100 │ Bet=${bet:.2f} │ "
        f"{mins_remaining:.1f}min remain │ {wlb_str}"
    )
    log.info("📋 %s", edge_justification)

    last_signal_desc = (
        f"SIGNAL {trade_direction} conf={confidence:.0f} "
        f"OB={ob_quality['imbalance']*100:.0f}% "
        f"edge={edge*100:.1f}% regime={regime}"
    )
    log.info(
        "📈 SIGNAL │ %s │ OB:%.1f%% │ Edge:%.2f%% │ Bet:$%.2f │ "
        "Limit:%dc │ Conf:%.0f/100 │ Momentum:%s │ Regime:%s",
        trade_direction, win_prob * 100, edge * 100, bet,
        limit_price, confidence, momentum_verdict, regime,
    )

    place_limit_order(ticker, trade_direction, bet, limit_price,
                      ob_pct=win_prob * 100, edge_pct=edge * 100)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global session_start_balance, session_stop_threshold, daily_pnl, active_tickers
    global paper_balance, paper_daily_pnl, last_trade_ts, last_daily_summary_ts, consecutive_losses
    global last_signal_desc, last_heartbeat_ts, running_pnl
    global live_wins, live_losses, streak_pause_until

    init_base_url()

    paper_balance = float(os.environ.get("PAPER_BALANCE", "25.0"))

    log.info("━" * 70)
    log.info("  BOT_VERSION: %s", BOT_VERSION)
    tg.validate_telegram_connection()   # validate once at boot
    log.info("  JOHNNY5 v6.0 │ %s │ Archetype: %s",
             "PAPER 🟡" if DEMO_MODE else "LIVE 🔴", ACTIVE_MODE.value.upper())
    log.info("  %s", PROFILE["description"])
    log.info("  Max trade: $%.2f │ Kelly: %.0f%% │ Min edge: %.1f%% │ Daily cap: $%.2f",
             TRADE_SIZE_DOLLARS, PROFILE["kelly_frac"] * 100,
             PROFILE["min_edge"] * 100, MAX_DAILY_LOSS)
    log.info("  Breakeven cap: %dc │ Floor: $%.2f │ OB thresh: %.0f%% │ Min depth: $%.0f",
             YES_BREAKEVEN_PRICE, MIN_BALANCE_FLOOR,
             PROFILE["ob_thresh"] * 100, MIN_OB_DEPTH_DOLLARS)
    log.info("  Confidence min: %d │ Require AGREE: %s │ Min expiry: %.1fmin",
             MINIMUM_CONFIDENCE, REQUIRE_AGREE_MOMENTUM, MIN_MINUTES_TO_EXPIRY)
    log.info("  Max bet fraction: %.0f%% │ Streak threshold: %s │ Pause: %ss",
             MAX_BET_FRACTION * 100,
             os.environ.get("MAX_CONSEC_LOSSES", "2"),
             os.environ.get("STREAK_PAUSE_SECS", "1800"))
    log.info("  Low-liq UTC hours: %s", sorted(LOW_LIQ_HOURS_UTC))
    log.info("  %s", "📋 PAPER — zero real orders" if DEMO_MODE else "⚠️  LIVE — real money")
    log.info("━" * 70)

    # Reset session state
    live_wins          = 0
    live_losses        = 0
    streak_pause_until = 0.0

    if DEMO_MODE:
        running_pnl = 0.0
        log.info("Starting paper balance: $%.2f", paper_balance)
        session_stop_threshold = paper_balance * 0.50
        log.info("Session stop threshold: $%.2f (50%% of start)", session_stop_threshold)
        telegram_boot(paper_balance)
    else:
        bal = get_live_balance()
        session_start_balance  = bal
        session_stop_threshold = bal * 0.50
        open_orders.clear()
        active_tickers.clear()
        consecutive_losses = 0
        running_pnl        = 0.0
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
                hb_bal    = paper_balance if DEMO_MODE else get_live_balance()
                hb_pnl    = paper_daily_pnl if DEMO_MODE else (hb_bal - session_start_balance)
                hb_open   = len(open_orders)
                hb_trades = len([t for t in trade_history if t.get("result") in ("win", "loss", "pending")])
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
