"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v9.0.0  —  GROUND-UP REBUILD                         ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.0.0 — COMPLETE TRADING BRAIN REPLACEMENT                                ║
║  Trigger: catastrophic April 20 session, full halt.                         ║
║                                                                              ║
║  CORE REDESIGN:                                                              ║
║  - Bayesian probability model replaces flat OB ratio heuristic              ║
║  - Structural regime engine: trend + volatility + microstructure            ║
║  - Multi-factor edge score with independent veto layers                     ║
║  - Adaptive Kelly with volatility-scaled fraction                           ║
║  - Circuit breakers: vol spike, spread explosion, book collapse             ║
║  - Recovery protocol after red session                                      ║
║  - Time-of-day session quality scoring                                      ║
║  - 15-minute decision cycle aligned to market windows                      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

BOT_VERSION = "9.0.0"

import base64
import logging
import math
import os
import random
import signal
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Set, Tuple

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
# ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class Regime(Enum):
    TRENDING_UP   = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING       = "RANGING"
    HIGH_VOL      = "HIGH_VOL"
    UNKNOWN       = "UNKNOWN"


class SessionState(Enum):
    ACTIVE    = "ACTIVE"
    RECOVERY  = "RECOVERY"   # post-red session: halved sizing, strict filters
    HALTED    = "HALTED"     # permanent until restart


class TradeDirection(Enum):
    YES = "YES"
    NO  = "NO"


# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var missing: {key}")
    return val


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").lower().strip()
    if not raw:
        return default
    return raw in ("true", "1", "yes")


KALSHI_API_KEY_ID   = _require("KALSHI_API_KEY_ID")
_RAW_PEM            = _require("KALSHI_PRIVATE_KEY_PEM")
DEMO_MODE           = _env_bool("DEMO_MODE", True)
POLL_INTERVAL       = _env_int("POLL_INTERVAL_SECS", 30)

# ── Capital & sizing ──────────────────────────────────────────────────────────
TRADE_SIZE_CAP      = _env_float("TRADE_SIZE_DOLLARS", 5.0)
MAX_BET_FRACTION    = _env_float("MAX_BET_FRACTION", 0.08)       # 8% of balance
KELLY_FRACTION      = _env_float("KELLY_FRACTION", 0.30)         # 30% fractional Kelly
KELLY_RECOVERY_MULT = _env_float("KELLY_RECOVERY_MULT", 0.50)    # halve sizing in recovery

# ── Risk controls ─────────────────────────────────────────────────────────────
MIN_BALANCE_FLOOR     = _env_float("MIN_BALANCE_FLOOR", 5.0)
MAX_DAILY_LOSS        = _env_float("MAX_DAILY_LOSS_DOLLARS", 15.0)
SESSION_STOP_FRACTION = _env_float("SESSION_STOP_FRACTION", 0.40)  # halt at 40% drawdown
MAX_CONSEC_LOSSES     = _env_int("MAX_CONSEC_LOSSES", 2)
STREAK_PAUSE_SECS     = _env_int("STREAK_PAUSE_SECS", 1800)
STALE_ORDER_TIMEOUT   = _env_int("STALE_ORDER_TIMEOUT", 300)
MAX_CONCURRENT_POS    = _env_int("MAX_CONCURRENT_POS", 1)
MIN_SAMPLE_TRADES     = _env_int("MIN_SAMPLE_TRADES", 20)

# ── Regime detection ──────────────────────────────────────────────────────────
R2_TREND_THRESHOLD    = _env_float("R2_TREND_THRESHOLD", 0.70)
VOLATILITY_CAP_PCT    = _env_float("VOLATILITY_CAP_PCT", 0.18)   # max 0.18% mean abs return/bar
VOL_CIRCUIT_BREAKER   = _env_float("VOL_CIRCUIT_BREAKER", 0.40)  # single-bar spike kills session
TREND_LOOKBACK        = _env_int("TREND_LOOKBACK", 12)           # bars for regression
MIN_PRICES_FOR_REGIME = _env_int("MIN_PRICES_FOR_REGIME", 10)

# ── Signal thresholds ─────────────────────────────────────────────────────────
MIN_OB_DEPTH          = _env_float("MIN_OB_DEPTH_DOLLARS", 75.0)
OB_IMBALANCE_THRESH   = _env_float("OB_IMBALANCE_THRESH", 0.62)
MOMENTUM_THRESH_PCT   = _env_float("MOMENTUM_THRESH_PCT", 0.15)  # % move to call directional
MIN_EDGE_PCT          = _env_float("MIN_EDGE_PCT", 0.06)          # 6% minimum EV edge
MIN_CONFIDENCE        = _env_int("MIN_CONFIDENCE", 60)
MIN_WIN_PROB          = _env_float("MIN_WIN_PROB", 0.60)          # never trade below 60% est win prob
MIN_MINUTES_TO_EXPIRY = _env_float("MIN_MINUTES_TO_EXPIRY", 6.0)
YES_BREAKEVEN_PRICE   = _env_int("YES_BREAKEVEN_PRICE", 78)       # cents

# ── Time-of-day filter ────────────────────────────────────────────────────────
# Sessions scored 0-100. Only trade in UTC hours with score >= MIN_SESSION_SCORE
# High-quality: 13-22 UTC (US market hours). Low-quality: 0-5 UTC
SESSION_QUALITY: dict = {
    0: 20, 1: 10, 2: 10, 3: 10, 4: 15, 5: 30,
    6: 45, 7: 50, 8: 60, 9: 65, 10: 70, 11: 75,
    12: 80, 13: 90, 14: 95, 15: 95, 16: 95, 17: 90,
    18: 90, 19: 85, 20: 80, 21: 75, 22: 65, 23: 45,
}
MIN_SESSION_SCORE = _env_int("MIN_SESSION_SCORE", 60)

# ── Bayesian priors ───────────────────────────────────────────────────────────
# Base rate for OB signal accuracy in live data. Updated via performance guard.
OB_BASE_ACCURACY       = _env_float("OB_BASE_ACCURACY", 0.635)   # prior win rate given signal
MOMENTUM_ACCURACY_LIFT = _env_float("MOMENTUM_ACCURACY_LIFT", 0.045)  # additional accuracy when momentum agrees
NEUTRAL_ACCURACY_DRAG  = _env_float("NEUTRAL_ACCURACY_DRAG", 0.02)    # deduct when no momentum confirm

# ── Recovery protocol ─────────────────────────────────────────────────────────
RECOVERY_TRIGGER_PCT  = _env_float("RECOVERY_TRIGGER_PCT", 0.10)  # enter recovery if session loss > 10%
RECOVERY_EXIT_TRADES  = _env_int("RECOVERY_EXIT_TRADES", 5)       # min trades before exiting recovery
RECOVERY_WIN_RATE_MIN = _env_float("RECOVERY_WIN_RATE_MIN", 0.60) # must show >= 60% WR to exit recovery


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
    log.info("✅ RSA private key loaded.")
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


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

_http = requests.Session()
BASE_URL = ""


def _get(path: str, params: Optional[dict] = None) -> dict:
    r = _http.get(BASE_URL + path, params=params,
                  headers=_auth_headers("GET", path), timeout=12)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = _http.post(BASE_URL + path, json=body,
                   headers=_auth_headers("POST", path), timeout=12)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    r = _http.delete(BASE_URL + path,
                     headers=_auth_headers("DELETE", path), timeout=12)
    r.raise_for_status()
    return r.json()


def init_base_url() -> None:
    global BASE_URL
    for host in ["https://api.elections.kalshi.com", "https://trading-api.kalshi.com"]:
        try:
            r = _http.get(host + "/trade-api/v2/exchange/status", timeout=6)
            if r.status_code == 200:
                BASE_URL = host + "/trade-api/v2"
                log.info("✅ API host: %s", host)
                return
        except Exception:
            continue
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    log.warning("Host probe failed — using default.")


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────────────────────────────────────

# Price history
btc_prices:  deque = deque(maxlen=60)   # 30-min window at 30s intervals
btc_returns: deque = deque(maxlen=59)   # pct returns between consecutive prices

# Trade state
open_orders:    dict = {}
active_tickers: set  = set()
trade_history:  deque = deque(maxlen=500)
session_traded_tickers: Set[str] = set()
_processed_settlement_ids: Set[str] = set()

# Session accounting
paper_balance:          float = 25.0
paper_daily_pnl:        float = 0.0
session_start_balance:  float = 0.0
session_stop_threshold: float = 0.0
live_wins:              int   = 0
live_losses:            int   = 0
consecutive_losses:     int   = 0
streak_pause_until:     float = 0.0
running_pnl:            float = 0.0
daily_pnl:              float = 0.0
last_trade_ts:          float = -9999.0
last_heartbeat_ts:      float = 0.0
last_daily_summary_ts:  float = 0.0
last_signal_desc:       str   = "none yet"

# Session management
session_state:     SessionState = SessionState.ACTIVE
recovery_trades:   int          = 0
_session_start_ts: str          = ""
_session_halted:   bool         = False
_shutdown_requested: bool       = False
_last_known_balance: float      = 0.0

# OB history for trend detection
_prev_ob: dict = {}

# Circuit breaker state
_vol_circuit_open:   bool  = False
_vol_circuit_until:  float = 0.0

# Performance tracking (Bayesian update)
_live_prior: float = OB_BASE_ACCURACY


# ─────────────────────────────────────────────────────────────────────────────
# SIGTERM
# ─────────────────────────────────────────────────────────────────────────────

def _sigterm_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.info("SIGTERM — graceful shutdown.")


signal.signal(signal.SIGTERM, _sigterm_handler)


# ─────────────────────────────────────────────────────────────────────────────
# BTC PRICE FEED
# ─────────────────────────────────────────────────────────────────────────────

_btc_backoff_until: float = 0.0


def fetch_btc_price() -> Optional[float]:
    global _btc_backoff_until
    if time.time() < _btc_backoff_until:
        return None
    # Primary: Kraken
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=5)
        if r.status_code == 200:
            result = r.json().get("result", {})
            if result:
                key = next(iter(result))
                price = float(result[key]["c"][0])
                if price > 1000:
                    return price
    except Exception:
        pass
    # Fallback: Coinbase
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        if r.status_code == 200:
            price = float(r.json()["data"]["amount"])
            if price > 1000:
                return price
    except Exception:
        pass
    _btc_backoff_until = time.time() + 300
    log.debug("BTC feed failed — backing off 5 min")
    return None


def ingest_btc_price() -> None:
    """Fetch BTC price, append to deque, compute return."""
    price = fetch_btc_price()
    if price is None:
        return
    if btc_prices:
        prev = btc_prices[-1]
        if prev > 0:
            ret = (price - prev) / prev * 100.0
            btc_returns.append(ret)
    btc_prices.append(price)


# ─────────────────────────────────────────────────────────────────────────────
# REGIME ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _linear_regression(ys: list) -> Tuple[float, float, float]:
    """Return slope, intercept, r_squared for a list of y values (x = 0,1,...n-1)."""
    n = len(ys)
    if n < 3:
        return 0.0, ys[0] if ys else 0.0, 0.0
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    ss_xx = sum((x - mx) ** 2 for x in xs)
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ss_yy = sum((y - my) ** 2 for y in ys)
    if ss_xx == 0 or ss_yy == 0:
        return 0.0, my, 0.0
    slope = ss_xy / ss_xx
    intercept = my - slope * mx
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)
    return slope, intercept, r_squared


def compute_regime() -> Tuple[Regime, float, float]:
    """
    Returns (regime, r_squared, realized_vol_pct).

    Regime classification hierarchy:
      1. HIGH_VOL: mean absolute return per bar > VOLATILITY_CAP_PCT
      2. TRENDING_UP / TRENDING_DOWN: R² > R2_TREND_THRESHOLD on last TREND_LOOKBACK bars
      3. RANGING: sufficient data but no trend
      4. UNKNOWN: insufficient data
    """
    if len(btc_prices) < MIN_PRICES_FOR_REGIME:
        return Regime.UNKNOWN, 0.0, 0.0

    prices = list(btc_prices)[-TREND_LOOKBACK:]
    returns = list(btc_returns)[-(TREND_LOOKBACK - 1):]

    # Realized volatility: mean absolute return
    if returns:
        realized_vol = sum(abs(r) for r in returns) / len(returns)
    else:
        realized_vol = 0.0

    # Circuit breaker: single-bar spike
    if returns and max(abs(r) for r in returns[-3:]) > VOL_CIRCUIT_BREAKER:
        log.warning("VOL CIRCUIT │ single-bar spike %.3f%% — regime=HIGH_VOL", max(abs(r) for r in returns[-3:]))
        return Regime.HIGH_VOL, 0.0, realized_vol

    if realized_vol > VOLATILITY_CAP_PCT:
        log.info("Regime │ HIGH_VOL (vol=%.4f%% > cap=%.4f%%)", realized_vol, VOLATILITY_CAP_PCT)
        return Regime.HIGH_VOL, 0.0, realized_vol

    slope, _, r_squared = _linear_regression(prices)

    if r_squared >= R2_TREND_THRESHOLD:
        regime = Regime.TRENDING_UP if slope > 0 else Regime.TRENDING_DOWN
        log.info("Regime │ %s (R²=%.3f, slope=%.2f)", regime.value, r_squared, slope)
        return regime, r_squared, realized_vol

    log.info("Regime │ RANGING (R²=%.3f)", r_squared)
    return Regime.RANGING, r_squared, realized_vol


# ─────────────────────────────────────────────────────────────────────────────
# VOLATILITY CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────────────────

def check_vol_circuit() -> bool:
    """Returns True if circuit is OPEN (trading blocked). Updates state."""
    global _vol_circuit_open, _vol_circuit_until

    if _vol_circuit_open:
        if time.time() > _vol_circuit_until:
            _vol_circuit_open = False
            log.info("Vol circuit breaker CLOSED — resuming.")
        else:
            mins_left = (_vol_circuit_until - time.time()) / 60.0
            log.info("Vol circuit OPEN — %.1f min remaining.", mins_left)
            return True

    if len(btc_returns) < 3:
        return False

    recent = list(btc_returns)[-6:]
    max_move = max(abs(r) for r in recent)
    if max_move > VOL_CIRCUIT_BREAKER:
        _vol_circuit_open  = True
        _vol_circuit_until = time.time() + 1800  # 30-min cooling period
        log.warning("Vol circuit OPENED — max_move=%.3f%% > cap=%.3f%%", max_move, VOL_CIRCUIT_BREAKER)
        tg.send_telegram_message(
            f"⚡ VOL CIRCUIT BREAKER OPENED\n"
            f"Max recent move: {max_move:.3f}%\nTrading paused 30 min."
        )
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# MOMENTUM SIGNAL
# ─────────────────────────────────────────────────────────────────────────────

def compute_momentum(ob_direction: str) -> Tuple[str, float]:
    """
    Returns (verdict, accuracy_adjustment).
    verdict: AGREE | CONFLICT | NEUTRAL
    accuracy_adjustment: added to base prior for Bayesian estimate
    """
    if len(btc_prices) < 5:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    prices = list(btc_prices)
    # Use 4-bar lookback (~2 minutes at 30s intervals)
    recent  = prices[-1]
    earlier = prices[-4]
    if earlier <= 0:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    move_pct = (recent - earlier) / earlier * 100.0
    btc_dir  = "YES" if move_pct > 0 else ("NO" if move_pct < 0 else "FLAT")
    ob_dir   = ob_direction.upper()

    if abs(move_pct) < MOMENTUM_THRESH_PCT:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    if btc_dir == ob_dir:
        # Scale accuracy lift with move magnitude (capped at 2x base lift)
        magnitude_scale = min(2.0, abs(move_pct) / MOMENTUM_THRESH_PCT)
        return "AGREE", MOMENTUM_ACCURACY_LIFT * magnitude_scale

    return "CONFLICT", 0.0


# ─────────────────────────────────────────────────────────────────────────────
# ORDER BOOK ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ob_levels(levels: list, lo: float, hi: float) -> Tuple[float, int]:
    """Sum dollar depth within price range [lo, hi]. Returns (depth, level_count)."""
    depth = 0.0
    count = 0
    for entry in levels:
        try:
            price = float(entry[0])
            size  = float(entry[1])
            if lo <= price <= hi and size > 0:
                depth += size
                count += 1
        except Exception:
            pass
    return depth, count


def analyze_order_book(ob_data: dict, yes_mid: int) -> Optional[dict]:
    """
    Returns None if book fails quality checks (thin, ghost, balanced).
    Otherwise returns quality dict with signal direction and statistics.
    """
    ob_fp      = ob_data.get("orderbook_fp", {})
    yes_levels = ob_fp.get("yes_dollars", [])
    no_levels  = ob_fp.get("no_dollars",  [])

    near = 10  # ±10 cents of mid
    y_lo = (yes_mid - near) / 100.0
    y_hi = (yes_mid + near) / 100.0
    n_mid = (100 - yes_mid) / 100.0
    n_lo  = n_mid - near / 100.0
    n_hi  = n_mid + near / 100.0

    yes_depth, yes_lc = _parse_ob_levels(yes_levels, y_lo, y_hi)
    no_depth,  no_lc  = _parse_ob_levels(no_levels,  n_lo, n_hi)
    total = yes_depth + no_depth

    # Depth floor
    if total < MIN_OB_DEPTH:
        log.info("OB │ depth $%.0f < min $%.0f — no signal", total, MIN_OB_DEPTH)
        return None

    # Ghost OB: one side has zero levels — no counterparty to fill against
    if yes_lc == 0 or no_lc == 0:
        log.info("OB │ ghost book (YES:%d levels, NO:%d levels) — skipping", yes_lc, no_lc)
        return None

    yr = yes_depth / total
    nr = no_depth  / total

    # Adaptive threshold: deeper books relax imbalance requirement
    if total >= 5000:
        eff_thresh = max(0.58, OB_IMBALANCE_THRESH - 0.04)
    elif total >= 500:
        eff_thresh = max(0.58, OB_IMBALANCE_THRESH - 0.02)
    elif total < 20:
        eff_thresh = min(0.80, OB_IMBALANCE_THRESH + 0.08)
    else:
        eff_thresh = OB_IMBALANCE_THRESH

    if yr >= eff_thresh:
        direction = "YES"
        imbalance = yr
    elif nr >= eff_thresh:
        direction = "NO"
        imbalance = nr
    else:
        log.info("OB │ no dominant side (YES:%.1f%% NO:%.1f%% thresh:%.1f%%)",
                 yr * 100, nr * 100, eff_thresh * 100)
        return None

    log.info("OB │ %s %.1f%% │ depth=$%.0f │ thresh=%.1f%%",
             direction, imbalance * 100, total, eff_thresh * 100)

    return {
        "direction":    direction,
        "imbalance":    imbalance,
        "total_depth":  total,
        "yes_depth":    yes_depth,
        "no_depth":     no_depth,
        "yes_lc":       yes_lc,
        "no_lc":        no_lc,
        "eff_thresh":   eff_thresh,
    }


def check_ob_trend(ticker: str, direction: str, imbalance: float) -> bool:
    """
    True = pressure building or stable. False = fading (block trade).
    Fading defined as same direction but >10% drop in imbalance ratio.
    """
    now  = time.time()
    prev = _prev_ob.get(ticker)
    _prev_ob[ticker] = (direction, imbalance, now)

    if prev is None:
        return True

    prev_dir, prev_imb, prev_ts = prev
    if now - prev_ts > 600:  # stale — reset
        return True

    if direction == prev_dir and imbalance < prev_imb - 0.10:
        log.info("OB trend │ fading %.1f%%→%.1f%% — blocking", prev_imb * 100, imbalance * 100)
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# BAYESIAN PROBABILITY MODEL
# ─────────────────────────────────────────────────────────────────────────────

def bayesian_win_prob(
    ob: dict,
    momentum_verdict: str,
    momentum_adj: float,
    regime: Regime,
    r_squared: float,
    realized_vol: float,
) -> float:
    """
    Estimate true win probability using Bayesian update from priors.

    Prior: OB_BASE_ACCURACY (empirical accuracy of this signal class)
    Likelihood updates:
      + momentum confirms → +lift
      - neutral momentum  → -drag
      + high R² trend     → +bonus
      + deep book         → +small bonus
      - conflict          → return 0 (already blocked upstream)

    Constrains final estimate to [0.50, 0.92] — never overconfident.
    """
    prior = _live_prior  # updated by performance guard over time

    # Regime adjustment
    if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        r2_bonus = (r_squared - R2_TREND_THRESHOLD) * 0.10
        regime_adj = 0.02 + r2_bonus
    else:
        regime_adj = 0.0

    # Depth adjustment: log-scale benefit for deeper books
    depth_adj = 0.0
    if ob["total_depth"] > 500:
        depth_adj = min(0.02, math.log10(ob["total_depth"] / 500) * 0.02)

    # Volatility penalty: higher vol → lower predictability
    vol_penalty = min(0.04, realized_vol / VOLATILITY_CAP_PCT * 0.04)

    win_prob = prior + momentum_adj + regime_adj + depth_adj - vol_penalty
    win_prob = max(0.50, min(0.92, win_prob))

    log.info("WinProb │ prior=%.3f mom=%.3f regime=%.3f depth=%.3f vol=-%.3f → P=%.3f",
             prior, momentum_adj, regime_adj, depth_adj, vol_penalty, win_prob)
    return win_prob


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def compute_confidence(
    ob: dict,
    regime: Regime,
    r_squared: float,
    momentum_verdict: str,
    win_prob: float,
    mins_remaining: float,
    session_score: int,
) -> float:
    """
    Returns score 0-100. Minimum to trade = MIN_CONFIDENCE.

    Components:
      OB imbalance strength      0-25
      OB depth quality           0-15
      Regime quality             0-20
      Momentum alignment         0-15
      Win probability            0-15
      Time remaining             0-10
    """
    # OB imbalance: linear scale from threshold to 1.0
    thresh = ob["eff_thresh"]
    imb_pts = max(0.0, (ob["imbalance"] - thresh) / (1.0 - thresh)) * 25.0

    # OB depth: log scale, capped at 15
    depth_pts = min(15.0, math.log10(max(1, ob["total_depth"] / MIN_OB_DEPTH)) * 10.0)

    # Regime
    regime_map = {
        Regime.TRENDING_UP:   20.0,
        Regime.TRENDING_DOWN: 20.0,
        Regime.RANGING:       0.0,
        Regime.HIGH_VOL:      -20.0,
        Regime.UNKNOWN:       0.0,
    }
    regime_pts = regime_map.get(regime, 0.0)
    if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        regime_pts += min(5.0, (r_squared - R2_TREND_THRESHOLD) * 15.0)

    # Momentum
    momentum_map = {"AGREE": 15.0, "NEUTRAL": 2.0, "CONFLICT": -20.0}
    momentum_pts = momentum_map.get(momentum_verdict, 0.0)

    # Win probability contribution
    prob_pts = max(0.0, (win_prob - 0.50) / 0.42 * 15.0)  # 0 at 50%, 15 at 92%

    # Time remaining
    time_pts = min(10.0, max(0.0,
        (mins_remaining - MIN_MINUTES_TO_EXPIRY) /
        max(0.1, 10.0 - MIN_MINUTES_TO_EXPIRY) * 10.0
    ))

    total = imb_pts + depth_pts + regime_pts + momentum_pts + prob_pts + time_pts
    total = max(0.0, min(100.0, total))

    log.info("Conf │ imb=%.1f depth=%.1f regime=%.1f mom=%.1f prob=%.1f time=%.1f → %.0f",
             imb_pts, depth_pts, regime_pts, momentum_pts, prob_pts, time_pts, total)
    return total


# ─────────────────────────────────────────────────────────────────────────────
# EDGE & SIZING
# ─────────────────────────────────────────────────────────────────────────────

def calc_edge(win_prob: float, contract_price_cents: int) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    net   = (100 - contract_price_cents) / 100.0
    stake = contract_price_cents / 100.0
    return (win_prob * net) - ((1.0 - win_prob) * stake)


def kelly_bet(win_prob: float, contract_price_cents: int, balance: float) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly = max(0.0, (b * win_prob - (1.0 - win_prob)) / b)

    # Adjust fraction for session state
    kf = KELLY_FRACTION
    if session_state == SessionState.RECOVERY:
        kf *= KELLY_RECOVERY_MULT

    bet = full_kelly * kf * balance
    return round(min(bet, TRADE_SIZE_CAP, balance * MAX_BET_FRACTION), 2)


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICAL PERFORMANCE GUARD
# ─────────────────────────────────────────────────────────────────────────────

def wilson_lower_bound(wins: int, total: int, z: float = 1.645) -> float:
    if total < 10:
        return 0.0
    p      = wins / total
    denom  = 1.0 + z ** 2 / total
    center = (p + z ** 2 / (2.0 * total)) / denom
    spread = (z * (p * (1.0 - p) / total + z ** 2 / (4.0 * total ** 2)) ** 0.5) / denom
    return max(0.0, center - spread)


def wilson_confidence(wins: int, total: int, z: float = 1.96) -> Tuple[float, float, float]:
    if total == 0:
        return 0.0, 0.0, 0.0
    p      = wins / total
    denom  = 1.0 + z ** 2 / total
    center = (p + z ** 2 / (2.0 * total)) / denom
    spread = (z * (p * (1.0 - p) / total + z ** 2 / (4.0 * total ** 2)) ** 0.5) / denom
    return round(p * 100, 1), round(max(0, center - spread) * 100, 1), round(min(1, center + spread) * 100, 1)


def update_live_prior() -> None:
    """Bayesian update of live prior from recent settled trades."""
    global _live_prior
    total = live_wins + live_losses
    if total < 10:
        return
    # Blend empirical win rate with starting prior (shrinkage)
    empirical = live_wins / total
    weight    = min(1.0, total / 50.0)  # full weight at 50+ trades
    _live_prior = OB_BASE_ACCURACY * (1.0 - weight) + empirical * weight
    log.debug("Prior updated → %.3f (empirical=%.3f, weight=%.2f)", _live_prior, empirical, weight)


def performance_guard() -> bool:
    total = live_wins + live_losses
    if total < MIN_SAMPLE_TRADES:
        return True
    wlb = wilson_lower_bound(live_wins, total)
    if wlb < 0.50:
        log.warning("PERF GUARD │ Wilson LB %.1f%% < 50%%", wlb * 100)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# SESSION QUALITY
# ─────────────────────────────────────────────────────────────────────────────

def get_session_score() -> int:
    utc_hour = datetime.now(timezone.utc).hour
    return SESSION_QUALITY.get(utc_hour, 50)


# ─────────────────────────────────────────────────────────────────────────────
# RECOVERY PROTOCOL
# ─────────────────────────────────────────────────────────────────────────────

def update_session_state(current_balance: float) -> None:
    """
    Evaluate whether to enter, maintain, or exit recovery mode.
    Recovery: entered when session loss > RECOVERY_TRIGGER_PCT.
    Exit: after RECOVERY_EXIT_TRADES with WR >= RECOVERY_WIN_RATE_MIN.
    """
    global session_state, recovery_trades

    if session_state == SessionState.HALTED:
        return

    session_loss_pct = (session_start_balance - current_balance) / max(1.0, session_start_balance)

    if session_state == SessionState.ACTIVE:
        if session_loss_pct > RECOVERY_TRIGGER_PCT:
            session_state = SessionState.RECOVERY
            recovery_trades = 0
            log.warning("SESSION RECOVERY │ Loss %.1f%% > trigger %.1f%%",
                        session_loss_pct * 100, RECOVERY_TRIGGER_PCT * 100)
            tg.send_telegram_message(
                f"⚠️ SESSION RECOVERY MODE\n"
                f"Loss: {session_loss_pct*100:.1f}%\n"
                f"Sizing halved. Strict filters only."
            )

    elif session_state == SessionState.RECOVERY:
        total_rec = live_wins + live_losses
        if total_rec >= RECOVERY_EXIT_TRADES:
            wr = live_wins / total_rec if total_rec > 0 else 0.0
            if wr >= RECOVERY_WIN_RATE_MIN:
                session_state = SessionState.ACTIVE
                log.info("SESSION RECOVERY EXITED │ WR=%.1f%% over %d trades", wr * 100, total_rec)
                tg.send_telegram_message(f"✅ Recovery mode exited — WR {wr*100:.0f}%")


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO / BALANCE
# ─────────────────────────────────────────────────────────────────────────────

def get_live_balance(allow_cached_zero: bool = True) -> float:
    global _last_known_balance
    try:
        data = _get("/portfolio/balance")
        bal_d = data.get("balance_dollars")
        if bal_d is not None:
            try:
                bal = float(bal_d)
            except Exception:
                bal = (data.get("balance", 0) or 0) / 100.0
        else:
            bal = (data.get("balance", 0) or 0) / 100.0
        _last_known_balance = bal
        return bal
    except Exception as e:
        if not allow_cached_zero and _last_known_balance <= 0.0:
            log.error("Balance fetch failed with no cached value: %s", e)
            raise
        log.warning("Balance fetch failed: %s — cached $%.2f", e, _last_known_balance)
        return _last_known_balance


def _extract_realized_dollars(rec: dict) -> Optional[float]:
    for k in ("realized_pnl_dollars", "settlement_pnl_dollars", "pnl_dollars"):
        v = rec.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                continue
    for k in ("realized_pnl_cents", "realized_pnl", "settlement_pnl", "pnl"):
        v = rec.get(k)
        if v is not None:
            try:
                return float(v) / 100.0
            except Exception:
                continue
    return None


def _extract_ticker(rec: dict) -> str:
    for k in ("market_ticker", "ticker", "event_ticker"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


def _fetch_settled_records(since_ts: str) -> list:
    """Settlements-primary, positions-fallback."""
    try:
        data = _get("/portfolio/settlements", {"limit": 100})
        recs = data.get("settlements") or data.get("market_settlements") or []
        if recs:
            return recs
    except Exception as e:
        log.debug("Settlements endpoint failed: %s", e)
    try:
        data = _get("/portfolio/positions", {
            "limit": 100,
            "settlement_status": "settled",
            "created_since": since_ts,
        })
        return data.get("market_positions", [])
    except Exception as e:
        log.warning("Both settlement endpoints failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def resolve_open_orders() -> None:
    global active_tickers, paper_balance, paper_daily_pnl, consecutive_losses
    global running_pnl, live_wins, live_losses, streak_pause_until

    if not open_orders:
        return

    if DEMO_MODE:
        now = time.time()
        for oid in list(open_orders.keys()):
            trade = open_orders[oid]
            if now - trade.get("placed_at", now) < 900:
                continue
            open_orders.pop(oid)
            ticker = trade.get("ticker", "")
            active_tickers.discard(ticker)
            count = trade.get("count", 0)
            cost  = trade.get("cost", 0.0)
            side  = trade.get("side", "YES").upper()
            entry_btc = trade.get("btc_entry_price", 0)
            cur_btc   = fetch_btc_price()

            if entry_btc > 0 and cur_btc and cur_btc > 1000:
                btc_up = cur_btc > entry_btc
                won = btc_up if side == "YES" else not btc_up
                sim = "btc"
            else:
                won = random.random() < _live_prior
                sim = "rng"

            if won:
                paper_balance   += count
                trade_pnl        = round(count - cost, 2)
                paper_daily_pnl += trade_pnl
            else:
                trade_pnl        = round(-cost, 2)
                paper_daily_pnl += trade_pnl

            running_pnl += trade_pnl
            result = "win" if won else "loss"
            for t in trade_history:
                if t.get("order_id") == oid:
                    t["result"] = result
                    t["pnl"]    = round(trade_pnl, 4)
                    break

            if won:
                consecutive_losses = 0
                live_wins += 1
                tg.send_win_notification(
                    profit=trade_pnl, balance=paper_balance, daily_pnl=paper_daily_pnl,
                    ticker=ticker, direction=trade.get("side", "?"),
                )
            else:
                consecutive_losses += 1
                live_losses += 1
                if consecutive_losses >= MAX_CONSEC_LOSSES:
                    streak_pause_until = time.time() + STREAK_PAUSE_SECS
                tg.send_loss_notification(
                    loss=abs(trade_pnl), balance=paper_balance, daily_pnl=paper_daily_pnl,
                    ticker=ticker, direction=trade.get("side", "?"), streak=consecutive_losses,
                )

            log.info("📋 PAPER SETTLED │ %s │ %s │ %s │ sim=%s │ bal=$%.2f",
                     ticker[-15:], side, result.upper(), sim, paper_balance)

        update_live_prior()
        return

    # ── Live ──────────────────────────────────────────────────────────────────
    try:
        since_ts = _session_start_ts or (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        records = _fetch_settled_records(since_ts)
        log.info("RESOLVE │ %d settled, %d open, %d processed",
                 len(records), len(open_orders), len(_processed_settlement_ids))

        ticker_to_oid: dict = {}
        for oid, trade in open_orders.items():
            tk = trade.get("ticker", "")
            if tk:
                ticker_to_oid[tk] = oid
                ticker_to_oid[tk.upper()] = oid

        for rec in records:
            rec_ticker  = _extract_ticker(rec)
            rec_created = rec.get("created_time") or rec.get("settled_time") or rec.get("timestamp", "")
            rec_id      = f"{rec_ticker}:{rec_created}"

            if rec_id in _processed_settlement_ids:
                continue

            matched_oid = ticker_to_oid.get(rec_ticker) or ticker_to_oid.get(rec_ticker.upper())
            if not matched_oid:
                for oid, trade in list(open_orders.items()):
                    if trade.get("ticker", "").upper() == rec_ticker.upper():
                        matched_oid = oid
                        break

            _processed_settlement_ids.add(rec_id)
            if not matched_oid:
                continue

            trade = open_orders.pop(matched_oid)
            active_tickers.discard(rec_ticker)
            active_tickers.discard(trade.get("ticker", ""))

            pnl_d = _extract_realized_dollars(rec)
            if pnl_d is None:
                log.warning("RESOLVE │ %s — no pnl field. Keys: %s", rec_ticker[-15:], list(rec.keys()))
                continue

            if pnl_d == 0.0:
                log.info("NO-FILL │ %s", rec_ticker[-15:])
                for t in trade_history:
                    if t.get("order_id") == matched_oid:
                        t["result"] = "unfilled"
                        t["pnl"] = 0.0
                        break
                continue

            won    = pnl_d > 0
            pnl    = round(pnl_d, 2)
            result = "win" if won else "loss"
            for t in trade_history:
                if t.get("order_id") == matched_oid:
                    t["result"] = result
                    t["pnl"]    = pnl
                    break

            balance       = get_live_balance()
            running_pnl  += pnl
            live_daily_pnl = balance - session_start_balance

            if won:
                consecutive_losses = 0
                live_wins += 1
            else:
                consecutive_losses += 1
                live_losses += 1
                if consecutive_losses >= MAX_CONSEC_LOSSES:
                    streak_pause_until = time.time() + STREAK_PAUSE_SECS

            wlb = wilson_lower_bound(live_wins, live_wins + live_losses)
            log.info("✅ SETTLED │ %s │ %s │ $%.2f │ WR=%d/%d │ LB=%.1f%%",
                     rec_ticker[-15:], result.upper(), pnl,
                     live_wins, live_wins + live_losses, wlb * 100)

            if won:
                tg.send_win_notification(
                    profit=pnl, balance=balance, daily_pnl=live_daily_pnl,
                    ticker=rec_ticker, direction=trade.get("side", "?"),
                )
            else:
                tg.send_loss_notification(
                    loss=abs(pnl), balance=balance, daily_pnl=live_daily_pnl,
                    ticker=rec_ticker, direction=trade.get("side", "?"),
                    streak=consecutive_losses,
                )

        update_live_prior()

        # Canceled order cleanup
        try:
            canceled = _get("/portfolio/orders", {"status": "canceled", "limit": 100})
            canceled_ids = {o["order_id"] for o in canceled.get("orders", [])}
            for oid in list(open_orders.keys()):
                if oid in canceled_ids:
                    trade = open_orders.pop(oid)
                    active_tickers.discard(trade.get("ticker", ""))
                    log.info("Order canceled │ %s", oid[:12])
        except Exception:
            pass

        # Age-based purge: >20 min
        now = time.time()
        stale = [oid for oid, t in open_orders.items() if now - t.get("placed_at", now) > 1200]
        for oid in stale:
            trade = open_orders.pop(oid)
            active_tickers.discard(trade.get("ticker", ""))
            log.info("Stale purged │ %s", trade.get("ticker", "?")[-15:])

    except Exception as e:
        log.warning("Resolution error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# STALE ORDER CANCELLATION
# ─────────────────────────────────────────────────────────────────────────────

def cancel_stale_orders() -> None:
    global paper_balance
    now = time.time()
    for oid in list(open_orders.keys()):
        trade = open_orders[oid]
        if now - trade.get("placed_at", now) < STALE_ORDER_TIMEOUT:
            continue
        ticker = trade.get("ticker", "")
        cost   = trade.get("cost", 0.0)
        if DEMO_MODE:
            open_orders.pop(oid)
            active_tickers.discard(ticker)
            paper_balance += cost  # refund only — no daily_pnl touch
            for t in trade_history:
                if t.get("order_id") == oid:
                    t["result"] = "canceled"
                    t["pnl"]    = 0.0
                    break
            log.info("Stale cancel (paper) │ %s │ $%.2f refunded", ticker[-15:], cost)
        else:
            try:
                _delete(f"/portfolio/orders/{oid}")
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                log.info("Stale cancel (live) │ %s │ %s", ticker[-15:], oid[:12])
            except Exception as e:
                log.warning("Stale cancel failed %s: %s", oid[:12], e)


# ─────────────────────────────────────────────────────────────────────────────
# MARKET DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

BTC_SERIES = ["KXBTC15M", "KXBTCD", "KXBTC"]


def _to_cents(val) -> int:
    try:
        return int(round(float(val) * 100))
    except Exception:
        return 0


def get_active_market() -> Optional[dict]:
    for series in BTC_SERIES:
        try:
            data    = _get("/markets", {"series_ticker": series, "status": "open", "limit": 20})
            markets = data.get("markets", [])
            if not markets:
                continue
            valid = []
            for m in markets:
                bid = _to_cents(m.get("yes_bid_dollars"))
                ask = _to_cents(m.get("yes_ask_dollars"))
                if bid > 0 and ask > 0 and bid < ask:
                    m["yes_bid"] = bid
                    m["yes_ask"] = ask
                    m["yes_mid"] = (bid + ask) // 2
                    valid.append(m)
            if not valid:
                continue
            valid.sort(key=lambda m: abs(m["yes_mid"] - 50))
            m0 = valid[0]
            log.info("Market │ %s bid=%dc mid=%dc ask=%dc",
                     m0.get("ticker"), m0["yes_bid"], m0["yes_mid"], m0["yes_ask"])
            return m0
        except Exception as e:
            log.warning("Market discovery %s: %s", series, e)
    return None


def get_order_book(ticker: str) -> dict:
    return _get(f"/markets/{ticker}/orderbook")


def minutes_to_expiry(market: dict) -> float:
    ct = market.get("close_time")
    if not ct:
        return 999.0
    try:
        close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        delta    = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
        return max(0.0, delta)
    except Exception:
        return 999.0


# ─────────────────────────────────────────────────────────────────────────────
# GUARD STACK
# ─────────────────────────────────────────────────────────────────────────────

def balance_floor_check(balance: float) -> bool:
    if balance < MIN_BALANCE_FLOOR:
        log.warning("BALANCE FLOOR │ $%.2f < $%.2f", balance, MIN_BALANCE_FLOOR)
        return False
    return True


def daily_loss_check(balance: float) -> bool:
    global _session_halted
    if _session_halted:
        return False
    pnl = paper_daily_pnl if DEMO_MODE else daily_pnl
    if pnl <= -MAX_DAILY_LOSS:
        _session_halted = True
        log.warning("DAILY LOSS │ $%.2f — halted.", abs(pnl))
        telegram_halt(f"Daily loss cap ${abs(pnl):.2f}", balance)
        return False
    if session_stop_threshold > 0 and balance < session_stop_threshold:
        _session_halted = True
        log.warning("SESSION STOP │ $%.2f < $%.2f — halted.", balance, session_stop_threshold)
        telegram_halt(f"Session stop at ${balance:.2f}", balance)
        return False
    return True


def spread_check(bid: int, ask: int) -> bool:
    if ask - bid <= 0:
        log.info("Spread │ zero/crossed")
        return False
    return True


def expiry_guard(mid: int) -> bool:
    if mid > 85 or mid < 15:
        log.info("Expiry │ %dc near-certain", mid)
        return False
    return True


def cooldown_check() -> bool:
    cd = 60  # minimum 60s between any trades
    elapsed = time.time() - last_trade_ts
    if elapsed < cd:
        log.info("Cooldown │ %.0fs remaining", cd - elapsed)
        return False
    return True


def session_quality_check() -> bool:
    score = get_session_score()
    utc_h = datetime.now(timezone.utc).hour
    if score < MIN_SESSION_SCORE:
        log.info("Session quality │ UTC%d score=%d < min=%d", utc_h, score, MIN_SESSION_SCORE)
        return False
    return True


def streak_check() -> bool:
    global consecutive_losses
    if consecutive_losses >= MAX_CONSEC_LOSSES:
        if time.time() < streak_pause_until:
            log.info("Streak pause │ %d consec losses", consecutive_losses)
            return False
        consecutive_losses = 0
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_order(ticker: str, direction: str, bet_dollars: float,
                limit_cents: int, win_prob: float, edge: float) -> Optional[str]:
    global last_trade_ts, paper_balance

    if limit_cents <= 0:
        return None
    count = int((bet_dollars * 100) / limit_cents)
    if count < 1:
        log.info("Order │ 0 contracts at $%.2f @ %dc", bet_dollars, limit_cents)
        return None
    cost      = (limit_cents * count) / 100.0
    client_id = f"j5-{uuid.uuid4().hex[:10]}"
    btc_entry = list(btc_prices)[-1] if btc_prices else 0

    if DEMO_MODE:
        paper_balance -= cost
        last_trade_ts  = time.time()
        active_tickers.add(ticker)
        session_traded_tickers.add(ticker)
        rec = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker, "side": direction,
            "price": limit_cents, "count": count, "cost": cost,
            "order_id": client_id, "result": "pending",
            "placed_at": time.time(), "btc_entry_price": btc_entry,
        }
        trade_history.append(rec)
        open_orders[client_id] = rec
        log.info("🟡 PAPER │ %s %s │ %d @ %dc │ $%.2f │ bal=$%.2f",
                 direction, ticker[-15:], count, limit_cents, cost, paper_balance)
        tg.send_trade_entry_notification(
            ticker=ticker, direction=direction, cost=cost,
            price_cents=limit_cents, balance=paper_balance,
            ob_pct=win_prob * 100, edge_pct=edge * 100,
        )
        return client_id

    # ── Live order (current Kalshi API: dollars fields) ───────────────────────
    body: dict = {
        "ticker": ticker,
        "client_order_id": client_id,
        "type": "limit",
        "action": "buy",
        "side": direction.lower(),
        "count": count,
    }
    if direction.upper() == "YES":
        body["yes_price_dollars"] = f"{limit_cents / 100:.2f}"
    else:
        body["no_price_dollars"] = f"{limit_cents / 100:.2f}"

    try:
        resp     = _post("/portfolio/orders", body)
        order_id = resp.get("order", {}).get("order_id", client_id)
        last_trade_ts = time.time()
        rec = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker, "side": direction,
            "price": limit_cents, "count": count, "cost": cost,
            "order_id": order_id, "result": "pending",
            "placed_at": time.time(), "btc_entry_price": btc_entry,
        }
        trade_history.append(rec)
        open_orders[order_id] = rec
        active_tickers.add(ticker)
        session_traded_tickers.add(ticker)
        log.info("✅ ORDER │ %s %s │ %d @ %dc │ $%.2f │ %s",
                 direction, ticker[-15:], count, limit_cents, bet_dollars, order_id[:12])
        live_bal = get_live_balance()
        tg.send_trade_entry_notification(
            ticker=ticker, direction=direction, cost=cost,
            price_cents=limit_cents, balance=live_bal,
            ob_pct=win_prob * 100, edge_pct=edge * 100,
        )
        return order_id
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "???"
        body_t = e.response.text[:300] if e.response is not None else str(e)
        log.error("Order failed │ HTTP %s │ %s", status, body_t)
        return None
    except Exception as e:
        log.error("Order failed │ %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM WRAPPERS
# ─────────────────────────────────────────────────────────────────────────────

def telegram_boot(balance: float) -> None:
    mode = "📋 PAPER" if DEMO_MODE else "🔴 LIVE"
    sess = session_state.value
    tg.send_telegram_message(
        f"🤖 Johnny5 {BOT_VERSION} STARTED\n"
        f"{mode} │ State: {sess}\n"
        f"Balance: ${balance:.2f}\n"
        f"DailyLoss≤${MAX_DAILY_LOSS:.0f} | Floor=${MIN_BALANCE_FLOOR:.0f}\n"
        f"MinConf={MIN_CONFIDENCE} | MinWinP={MIN_WIN_PROB:.0f}% | R²≥{R2_TREND_THRESHOLD}\n"
        f"OBDepth≥${MIN_OB_DEPTH:.0f} | OBImb≥{OB_IMBALANCE_THRESH*100:.0f}%\n"
        f"SessionScore≥{MIN_SESSION_SCORE} | Kelly={KELLY_FRACTION}"
    )


def telegram_halt(reason: str, balance: float) -> None:
    tg.send_telegram_message(
        f"⛔ HALTED (PERMANENT)\nReason: {reason}\nBalance: ${balance:.2f}"
    )


def telegram_daily_summary(balance: float, pnl: float, wins: int, losses: int) -> None:
    total = wins + losses
    wr    = wins / total * 100 if total > 0 else 0.0
    emoji = "📈" if pnl >= 0 else "📉"
    ci_str = ""
    if total >= 10:
        wlb = wilson_lower_bound(wins, total)
        ci_str = f" LB={wlb*100:.0f}%"
    tg.send_telegram_message(
        f"{emoji} Daily Summary\n"
        f"P&L: ${pnl:+.2f} │ Balance: ${balance:.2f}\n"
        f"WR: {wr:.0f}%{ci_str} ({wins}W/{losses}L)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DECISION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_decision(market: dict, balance: float) -> None:
    global last_signal_desc

    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        return
    yes_mid = (yes_bid + yes_ask) // 2

    # ── Layer 1: Hard guards ─────────────────────────────────────────────────
    if not balance_floor_check(balance):
        return
    if not expiry_guard(yes_mid):
        return
    if not spread_check(yes_bid, yes_ask):
        return
    if ticker in active_tickers:
        log.info("Position guard │ %s", ticker[-15:])
        return
    if ticker in session_traded_tickers:
        log.info("Session guard │ already traded %s", ticker[-15:])
        last_signal_desc = f"session re-entry ({ticker[-10:]})"
        return
    if not cooldown_check():
        return
    if not daily_loss_check(balance):
        return

    # ── Layer 2: Streak / streak pause ──────────────────────────────────────
    if not streak_check():
        last_signal_desc = f"streak pause ({consecutive_losses}L)"
        return

    # ── Layer 3: Statistical performance guard ───────────────────────────────
    if not performance_guard():
        last_signal_desc = "perf guard (Wilson LB < 50%)"
        return

    # ── Layer 4: Session quality ─────────────────────────────────────────────
    if not session_quality_check():
        last_signal_desc = f"session quality UTC{datetime.now(timezone.utc).hour}"
        return

    # ── Layer 5: Concurrent position ─────────────────────────────────────────
    if len(open_orders) >= MAX_CONCURRENT_POS:
        log.info("Concurrent │ %d open", len(open_orders))
        return

    # ── Layer 6: Time to expiry ──────────────────────────────────────────────
    mins = minutes_to_expiry(market)
    if mins < MIN_MINUTES_TO_EXPIRY:
        log.info("Expiry imminent │ %.1f min", mins)
        last_signal_desc = "expiry imminent"
        return

    # ── Layer 7: Vol circuit breaker ─────────────────────────────────────────
    if check_vol_circuit():
        last_signal_desc = "vol circuit open"
        return

    # ── Layer 8: Regime ──────────────────────────────────────────────────────
    regime, r_squared, realized_vol = compute_regime()
    if regime in (Regime.UNKNOWN, Regime.RANGING, Regime.HIGH_VOL):
        log.info("Regime │ %s — no trade", regime.value)
        last_signal_desc = f"regime={regime.value}"
        return

    # ── Layer 9: Order book analysis ─────────────────────────────────────────
    try:
        ob_raw = get_order_book(ticker)
    except Exception as e:
        log.warning("OB fetch failed: %s", e)
        return

    ob = analyze_order_book(ob_raw, yes_mid)
    if ob is None:
        last_signal_desc = "OB no signal"
        return

    # OB direction must agree with regime direction
    ob_dir = ob["direction"]
    if regime == Regime.TRENDING_UP and ob_dir != "YES":
        log.info("Direction mismatch │ regime=UP but OB=%s", ob_dir)
        last_signal_desc = f"regime/OB mismatch (UP vs {ob_dir})"
        return
    if regime == Regime.TRENDING_DOWN and ob_dir != "NO":
        log.info("Direction mismatch │ regime=DOWN but OB=%s", ob_dir)
        last_signal_desc = f"regime/OB mismatch (DOWN vs {ob_dir})"
        return

    # OB trend: fading signal = no trade
    if not check_ob_trend(ticker, ob_dir, ob["imbalance"]):
        last_signal_desc = "OB fading"
        return

    # ── Layer 10: Momentum ───────────────────────────────────────────────────
    momentum_verdict, momentum_adj = compute_momentum(ob_dir)
    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB=%s", ob_dir)
        last_signal_desc = f"CONFLICT OB={ob_dir}"
        return

    # In recovery mode, require AGREE momentum — no NEUTRAL trades
    if session_state == SessionState.RECOVERY and momentum_verdict != "AGREE":
        log.info("Recovery mode │ requires AGREE momentum, got %s", momentum_verdict)
        last_signal_desc = "recovery: AGREE required"
        return

    # ── Layer 11: Probability model ──────────────────────────────────────────
    win_prob = bayesian_win_prob(ob, momentum_verdict, momentum_adj, regime, r_squared, realized_vol)

    if win_prob < MIN_WIN_PROB:
        log.info("WinProb │ %.3f < min %.3f", win_prob, MIN_WIN_PROB)
        last_signal_desc = f"win_prob {win_prob:.2f} < {MIN_WIN_PROB:.2f}"
        return

    # ── Layer 12: Confidence score ───────────────────────────────────────────
    session_score = get_session_score()
    conf = compute_confidence(ob, regime, r_squared, momentum_verdict,
                               win_prob, mins, session_score)
    if conf < MIN_CONFIDENCE:
        log.info("Confidence │ %.0f < %d", conf, MIN_CONFIDENCE)
        last_signal_desc = f"conf {conf:.0f} < {MIN_CONFIDENCE}"
        return

    # ── Layer 13: Contract price / direction ─────────────────────────────────
    if ob_dir == "YES":
        if yes_mid > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ YES %dc > breakeven", yes_mid)
            return
        direction = "YES"
        contract_price = yes_mid
    else:
        no_price = 100 - yes_mid
        if no_price > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ NO %dc > breakeven", no_price)
            return
        direction = "NO"
        contract_price = no_price

    if not (25 <= contract_price <= 75):
        log.info("Bias filter │ %dc outside 25-75", contract_price)
        return

    # ── Layer 14: Edge ───────────────────────────────────────────────────────
    edge = calc_edge(win_prob, contract_price)
    if edge < MIN_EDGE_PCT:
        log.info("Edge │ %.3f < min %.3f", edge, MIN_EDGE_PCT)
        last_signal_desc = f"edge {edge:.3f} < {MIN_EDGE_PCT:.3f}"
        return

    # ── Layer 15: Kelly sizing ───────────────────────────────────────────────
    bet = kelly_bet(win_prob, contract_price, balance)
    if bet < 0.25:
        log.info("Kelly │ $%.2f too small", bet)
        return
    if balance < bet:
        log.warning("Insufficient balance")
        return

    # ── Limit price (maker) ──────────────────────────────────────────────────
    if direction == "YES":
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        no_best     = 100 - yes_ask
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))
    limit_price = max(1, min(99, limit_price))

    if abs(limit_price - contract_price) > 8:
        log.info("Limit drift │ %dc too far", limit_price)
        return

    wlb_str = ""
    total = live_wins + live_losses
    if total >= 10:
        wlb = wilson_lower_bound(live_wins, total)
        wlb_str = f" WLB={wlb*100:.1f}%"

    log.info(
        "📋 EDGE JUSTIFICATION │ %s %s @ %dc │ regime=%s(R²=%.2f) │ "
        "OB=%.1f%% $%.0f │ BTC=%s │ WinP=%.1f%% Edge=%.1f%% Conf=%.0f │ "
        "Bet=$%.2f │ %.1fmin%s",
        direction, ticker[-15:], contract_price,
        regime.value, r_squared,
        ob["imbalance"] * 100, ob["total_depth"],
        momentum_verdict, win_prob * 100, edge * 100, conf,
        bet, mins, wlb_str
    )

    last_signal_desc = f"SIGNAL {direction} conf={conf:.0f} p={win_prob:.2f}"
    place_order(ticker, direction, bet, limit_price, win_prob, edge)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global session_start_balance, session_stop_threshold, daily_pnl
    global paper_balance, paper_daily_pnl, last_trade_ts, last_daily_summary_ts
    global consecutive_losses, last_signal_desc, last_heartbeat_ts, running_pnl
    global live_wins, live_losses, streak_pause_until
    global _last_known_balance, _shutdown_requested, _session_start_ts
    global _session_halted, session_traded_tickers, session_state, recovery_trades

    init_base_url()

    paper_balance = float(os.environ.get("PAPER_BALANCE", "25.0"))
    _session_start_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session_halted   = False
    session_state     = SessionState.ACTIVE
    recovery_trades   = 0
    session_traded_tickers = set()

    log.info("━" * 70)
    log.info("  JOHNNY5 %s │ %s", BOT_VERSION, "PAPER 🟡" if DEMO_MODE else "LIVE 🔴")
    log.info("  Start: %s", _session_start_ts)
    log.info("  Regime R²≥%.2f | VolCap=%.3f%% | Circuit=%.2f%%",
             R2_TREND_THRESHOLD, VOLATILITY_CAP_PCT, VOL_CIRCUIT_BREAKER)
    log.info("  OB depth≥$%.0f imb≥%.0f%% | WinP≥%.0f%% Edge≥%.0f%%",
             MIN_OB_DEPTH, OB_IMBALANCE_THRESH * 100, MIN_WIN_PROB * 100, MIN_EDGE_PCT * 100)
    log.info("  Kelly=%.2f cap=%.0f%% | Session score≥%d",
             KELLY_FRACTION, MAX_BET_FRACTION * 100, MIN_SESSION_SCORE)
    log.info("━" * 70)

    tg.validate_telegram_connection()

    live_wins = 0
    live_losses = 0
    streak_pause_until = 0.0

    if DEMO_MODE:
        running_pnl            = 0.0
        session_start_balance  = paper_balance
        session_stop_threshold = paper_balance * SESSION_STOP_FRACTION
        telegram_boot(paper_balance)
    else:
        try:
            bal = get_live_balance(allow_cached_zero=False)
        except Exception as e:
            log.error("Cannot fetch starting balance — aborting: %s", e)
            tg.send_telegram_message(f"🛑 Johnny5 {BOT_VERSION} boot failed: balance error")
            return
        if bal <= 0.0:
            log.error("Starting balance $0 — aborting")
            tg.send_telegram_message(f"🛑 Johnny5 {BOT_VERSION} boot failed: balance=$0")
            return
        _last_known_balance    = bal
        session_start_balance  = bal
        session_stop_threshold = bal * SESSION_STOP_FRACTION
        open_orders.clear()
        active_tickers.clear()
        consecutive_losses = 0
        running_pnl        = 0.0
        telegram_boot(bal)

    resolve_cycle = 0

    while not _shutdown_requested:
        try:
            if _session_halted:
                log.info("Permanently halted — sleeping 1hr.")
                time.sleep(3600)
                continue

            # ── Heartbeat ────────────────────────────────────────────────────
            if time.time() - last_heartbeat_ts >= 900:
                last_heartbeat_ts = time.time()
                hb_bal  = paper_balance if DEMO_MODE else get_live_balance()
                hb_pnl  = paper_daily_pnl if DEMO_MODE else (hb_bal - session_start_balance)
                hb_open = len(open_orders)
                hb_tr   = len([t for t in trade_history
                                if t.get("result") in ("win", "loss", "pending")])
                tg.send_heartbeat(
                    balance=hb_bal, session_pnl=hb_pnl, open_count=hb_open,
                    trades_today=hb_tr, last_signal=last_signal_desc,
                )

            # ── BTC price ─────────────────────────────────────────────────────
            ingest_btc_price()

            # ── Market ───────────────────────────────────────────────────────
            market = get_active_market()
            if not market:
                log.info("No active market — waiting %ds", POLL_INTERVAL)
                last_signal_desc = "no market"
                time.sleep(POLL_INTERVAL)
                continue

            # ── Expire stale position locks ───────────────────────────────────
            current_ticker = market.get("ticker", "")
            tickers_with_orders = {t.get("ticker", "") for t in open_orders.values()}
            expired = {t for t in active_tickers
                       if t != current_ticker and t not in tickers_with_orders}
            if expired:
                active_tickers -= expired
                log.info("Expired locks: %s", expired)

            # ── Balance ───────────────────────────────────────────────────────
            current_balance = paper_balance if DEMO_MODE else get_live_balance()

            # ── Session state machine ─────────────────────────────────────────
            update_session_state(current_balance)

            # ── Decision ─────────────────────────────────────────────────────
            run_decision(market, current_balance)

            # ── Resolution ───────────────────────────────────────────────────
            resolve_cycle += 1
            if resolve_cycle % 3 == 0:
                resolve_open_orders()
                cancel_stale_orders()

                if DEMO_MODE:
                    resolved = [t for t in trade_history if t.get("result") in ("win", "loss")]
                    wins  = sum(1 for t in resolved if t["result"] == "win")
                    total = len(resolved)
                    wr    = wins / total if total > 0 else 0.0
                    log.info("📋 PAPER │ $%.2f │ PnL=$%+.2f │ WR=%.1f%% │ Prior=%.3f │ State=%s",
                             paper_balance, paper_daily_pnl, wr * 100,
                             _live_prior, session_state.value)
                else:
                    live_bal   = get_live_balance()
                    daily_pnl  = live_bal - session_start_balance
                    wlb        = wilson_lower_bound(live_wins, live_wins + live_losses)
                    log.info("Portfolio │ $%.2f │ PnL=$%+.2f │ WR=%d/%d LB=%.1f%% │ Prior=%.3f │ State=%s",
                             live_bal, daily_pnl, live_wins, live_wins + live_losses,
                             wlb * 100, _live_prior, session_state.value)

                    if datetime.now(timezone.utc).hour == 0 and \
                       time.time() - last_daily_summary_ts > 3600:
                        last_daily_summary_ts = time.time()
                        telegram_daily_summary(live_bal, daily_pnl, live_wins, live_losses)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Unexpected: %s", e, exc_info=True)
            time.sleep(POLL_INTERVAL)

    final = paper_balance if DEMO_MODE else get_live_balance()
    log.info("Shutdown. Final balance: $%.2f", final)
    tg.send_telegram_message(f"🛑 Johnny5 {BOT_VERSION} stopped. Final: ${final:.2f}")


if __name__ == "__main__":
    main()
