"""
JOHNNY5-KALSHI-AUTO  v9.3.1  —  Production Build
"No disassemble."

v9.3.1 — HALT LOGIC: never halt/pause after a single loss
  Reported behaviour: the bot stops trading after ONE losing trade.

  Two code-level paths could produce that, both fixed here without loosening
  the real daily/session stops:

  1. STREAK PAUSE armed by a single loss.
     streak_check() / the loss-settlement handlers arm a 30-min pause when
     consecutive_losses >= MAX_CONSEC_LOSSES. A "consecutive-loss" pause is by
     definition >= 2 losses; if MAX_CONSEC_LOSSES is set to 1 in the Railway
     env, every isolated loss arms the pause and the bot looks halted.
     FIX: MAX_CONSEC_LOSSES is now floored at 2 in code, so one loss can never
     arm the streak pause regardless of the env value.

  2. DAILY-LOSS CAP tighter than one full-stake trade.
     daily_loss_check() used loss_cap = min(MAX_DAILY_LOSS, 6%*start_balance).
     On a small account the 6% clause can fall to/below a single position's
     cost (e.g. $1.50 on a $25 start), so a loss could trip the PERMANENT daily
     halt. A daily stop tighter than one trade is incoherent.
     FIX: loss_cap is floored just above the largest single stake we can place
     (TRADE_SIZE_DOLLARS capped by MAX_BET_FRACTION*start_balance), so a single
     loss can never breach the daily cap. The fixed MAX_DAILY_LOSS dollar value
     still governs as the binding cap; nothing is loosened above it.

  No other logic, thresholds, sizing, or endpoints changed. Audit categories
  1-6 (kwargs, globals, paper arithmetic, balance-fetch, heartbeat PnL,
  resolution endpoint) were re-verified clean against v9.3.0.

v9.3.0 — DOCTRINE RESTORE: stop the NEUTRAL-momentum bleed

DIAGNOSIS (2026-06-20->22 LIVE session, v9.2.0, ~2.7 days):
  6 trades fired, ALL on BTC=NEUTRAL. Three doctrine guards had drifted open
  (all from the v9.0.6 "throughput" push, retained through v9.2.0). Together
  they manufactured a fake ~25% edge on a coin flip and bet the per-trade cap.

FIX (restore, do not engineer around — zero-trade calm sessions are CORRECT):
  1. momentum_gate_ok(): doctrine Layer 7. REQUIRE_AGREE_MOMENTUM (default
     true) rejects NEUTRAL and CONFLICT in EVERY session state.
  2. NEUTRAL_ACCURACY_DRAG default 0.0 -> 0.02 (honest win_prob if gate off).
  3. compute_confidence(): NEUTRAL 8.0 -> 2.0 (Layer 8: momentum only scores
     when AGREE).
  4. Restore drifted thresholds to doctrine: OB_IMBALANCE_THRESH 0.64->0.70,
     R2_TREND_THRESHOLD 0.62->0.65, MIN_CONFIDENCE 60->65,
     YES_BREAKEVEN_PRICE 78->67.

The recovery deadlock that justified removing the AGREE gate is ALREADY solved
by update_session_state()'s balance-heal exit and RECOVERY_MAX_SECS wall-clock
backstop, so re-blocking NEUTRAL cannot relock recovery.

RAILWAY ENV VAR CHANGES REQUIRED (an env override beats these defaults):
  - REQUIRE_AGREE_MOMENTUM : set true (or leave unset)
  - NEUTRAL_ACCURACY_DRAG  : set 0.02 (or delete)
  - OB_IMBALANCE_THRESH    : set 0.70 (or delete)
  - R2_TREND_THRESHOLD     : set 0.65 (or delete)
  - MIN_CONFIDENCE         : set 65   (or delete)
  - YES_BREAKEVEN_PRICE    : set 67   (or delete)

v9.1.0 — RECOVERY DEADLOCK (real fix) + RISK TIGHTENING
  1. RECOVERY no longer FORCES momentum==AGREE as a recovery-only extra gate
     (Layer-7 AGREE now applies uniformly via momentum_gate_ok).
  2. RECOVERY_MAX_SECS hard timeout in update_session_state().
  3. MAX_BET_FRACTION 0.08 -> 0.04.
  4. MAX_DAILY_LOSS_PCT (6%) — daily stop halts on the tighter of the fixed
     dollar cap and a fraction of the session-start balance.

v9.0.8 — PERF-GUARD DEADLOCK FIX (boot-time settlement gate)
v9.0.7 — SETTLEMENT SCHEMA CORRECTED:
  pnl = (revenue/100) - yes_total_cost_dollars - no_total_cost_dollars - fee_cost
"""

from __future__ import annotations

BOT_VERSION = "9.3.1"

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
from ladder import StakeLadder

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("Johnny5")


# -----------------------------------------------------------------------------
# ENUMERATIONS
# -----------------------------------------------------------------------------

class Regime(Enum):
    TRENDING_UP   = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING       = "RANGING"
    HIGH_VOL      = "HIGH_VOL"
    UNKNOWN       = "UNKNOWN"


class SessionState(Enum):
    ACTIVE    = "ACTIVE"
    RECOVERY  = "RECOVERY"
    HALTED    = "HALTED"


# -----------------------------------------------------------------------------
# ENVIRONMENT CONFIGURATION
# -----------------------------------------------------------------------------

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

# -- Capital & sizing ---------------------------------------------------------
TRADE_SIZE_CAP      = _env_float("TRADE_SIZE_DOLLARS", 5.0)
MAX_BET_FRACTION    = _env_float("MAX_BET_FRACTION", 0.04)
KELLY_FRACTION      = _env_float("KELLY_FRACTION", 0.30)
KELLY_RECOVERY_MULT = _env_float("KELLY_RECOVERY_MULT", 0.50)

LADDER_ENABLED = _env_bool("LADDER_ENABLED", False)

# -- Risk controls ------------------------------------------------------------
MIN_BALANCE_FLOOR     = _env_float("MIN_BALANCE_FLOOR", 5.0)
MAX_DAILY_LOSS        = _env_float("MAX_DAILY_LOSS_DOLLARS", 15.0)
MAX_DAILY_LOSS_PCT    = _env_float("MAX_DAILY_LOSS_PCT", 0.06)
SESSION_STOP_FRACTION = _env_float("SESSION_STOP_FRACTION", 0.40)
# v9.3.1: floored at 2. A "consecutive-loss" pause is meaningless below 2; a
# value of 1 would arm the 30-min streak pause after a single isolated loss.
MAX_CONSEC_LOSSES     = max(2, _env_int("MAX_CONSEC_LOSSES", 2))
STREAK_PAUSE_SECS     = _env_int("STREAK_PAUSE_SECS", 1800)
STALE_ORDER_TIMEOUT   = _env_int("STALE_ORDER_TIMEOUT", 300)
MAX_CONCURRENT_POS    = _env_int("MAX_CONCURRENT_POS", 1)
MIN_SAMPLE_TRADES     = _env_int("MIN_SAMPLE_TRADES", 20)

# -- Regime detection ---------------------------------------------------------
R2_TREND_THRESHOLD    = _env_float("R2_TREND_THRESHOLD", 0.65)
VOLATILITY_CAP_PCT    = _env_float("VOLATILITY_CAP_PCT", 0.18)
VOL_CIRCUIT_BREAKER   = _env_float("VOL_CIRCUIT_BREAKER", 0.40)
TREND_LOOKBACK        = _env_int("TREND_LOOKBACK", 12)
MIN_PRICES_FOR_REGIME = _env_int("MIN_PRICES_FOR_REGIME", 10)

# -- Signal thresholds --------------------------------------------------------
MIN_OB_DEPTH          = _env_float("MIN_OB_DEPTH_DOLLARS", 75.0)
OB_IMBALANCE_THRESH   = _env_float("OB_IMBALANCE_THRESH", 0.70)
MOMENTUM_THRESH_PCT   = _env_float("MOMENTUM_THRESH_PCT", 0.15)
MIN_EDGE_PCT          = _env_float("MIN_EDGE_PCT", 0.06)
MIN_CONFIDENCE        = _env_int("MIN_CONFIDENCE", 65)
MIN_WIN_PROB          = _env_float("MIN_WIN_PROB", 0.60)
MIN_MINUTES_TO_EXPIRY = _env_float("MIN_MINUTES_TO_EXPIRY", 6.0)
YES_BREAKEVEN_PRICE   = _env_int("YES_BREAKEVEN_PRICE", 67)

# v9.3.0: DOCTRINE LAYER 7 — BTC momentum must explicitly AGREE with the OB
# direction. NEUTRAL (flat BTC) and CONFLICT are both rejections. Default ON.
REQUIRE_AGREE_MOMENTUM = _env_bool("REQUIRE_AGREE_MOMENTUM", True)

# -- Time-of-day session quality ----------------------------------------------
SESSION_QUALITY: dict = {
    0: 20, 1: 10, 2: 10, 3: 10, 4: 15, 5: 30,
    6: 45, 7: 50, 8: 60, 9: 65, 10: 70, 11: 75,
    12: 80, 13: 90, 14: 95, 15: 95, 16: 95, 17: 90,
    18: 90, 19: 85, 20: 80, 21: 75, 22: 65, 23: 45,
}
MIN_SESSION_SCORE = _env_int("MIN_SESSION_SCORE", 60)

# -- Bayesian priors ----------------------------------------------------------
OB_BASE_ACCURACY       = _env_float("OB_BASE_ACCURACY", 0.635)
MOMENTUM_ACCURACY_LIFT = _env_float("MOMENTUM_ACCURACY_LIFT", 0.045)
NEUTRAL_ACCURACY_DRAG  = _env_float("NEUTRAL_ACCURACY_DRAG", 0.02)

# -- Recovery protocol --------------------------------------------------------
RECOVERY_TRIGGER_PCT  = _env_float("RECOVERY_TRIGGER_PCT", 0.10)
RECOVERY_EXIT_TRADES  = _env_int("RECOVERY_EXIT_TRADES", 5)
RECOVERY_WIN_RATE_MIN = _env_float("RECOVERY_WIN_RATE_MIN", 0.60)
RECOVERY_MAX_SECS     = _env_int("RECOVERY_MAX_SECS", 3600)


# -----------------------------------------------------------------------------
# RSA AUTHENTICATION
# -----------------------------------------------------------------------------

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
    log.info("RSA private key loaded.")
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


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

_http    = requests.Session()
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
                log.info("API host: %s", host)
                return
        except Exception:
            continue
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    log.warning("Host probe failed — using default.")


# -----------------------------------------------------------------------------
# GLOBAL STATE
#
# RULE: open_orders, active_tickers, trade_history, session_traded_tickers,
# _processed_settlement_ids, btc_prices, btc_returns, _prev_ob are module-level
# mutable containers. They are mutated IN-PLACE everywhere. They must NEVER
# appear in any function's `global` declaration.
# -----------------------------------------------------------------------------

btc_prices:  deque = deque(maxlen=60)
btc_returns: deque = deque(maxlen=59)

open_orders:               dict     = {}
active_tickers:            set      = set()
trade_history:             deque    = deque(maxlen=500)
session_traded_tickers:    Set[str] = set()
_processed_settlement_ids: Set[str] = set()

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

session_state:         SessionState = SessionState.ACTIVE
recovery_trades:       int          = 0
recovery_entry_wins:   int          = 0
recovery_entry_losses: int          = 0
recovery_entered_ts:   float        = 0.0
_session_start_ts:     str          = ""
_session_day:          str          = ""
_session_halted:       bool         = False
_shutdown_requested:   bool         = False
_last_known_balance:   float        = 0.0

_prev_ob: dict = {}

_vol_circuit_open:  bool  = False
_vol_circuit_until: float = 0.0

_live_prior: float = OB_BASE_ACCURACY

stake_ladder: Optional[StakeLadder] = StakeLadder() if LADDER_ENABLED else None


# -----------------------------------------------------------------------------
# SIGTERM
# -----------------------------------------------------------------------------

def _sigterm_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.info("SIGTERM — graceful shutdown.")


signal.signal(signal.SIGTERM, _sigterm_handler)


# -----------------------------------------------------------------------------
# BTC PRICE FEED
# -----------------------------------------------------------------------------

_btc_backoff_until: float = 0.0


def fetch_btc_price() -> Optional[float]:
    global _btc_backoff_until
    if time.time() < _btc_backoff_until:
        return None
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=5)
        if r.status_code == 200:
            result = r.json().get("result", {})
            if result:
                key   = next(iter(result))
                price = float(result[key]["c"][0])
                if price > 1000:
                    return price
    except Exception:
        pass
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
    price = fetch_btc_price()
    if price is None:
        return
    if btc_prices:
        prev = btc_prices[-1]
        if prev > 0:
            btc_returns.append((price - prev) / prev * 100.0)
    btc_prices.append(price)


# -----------------------------------------------------------------------------
# REGIME ENGINE
# -----------------------------------------------------------------------------

def _linear_regression(ys: list) -> Tuple[float, float, float]:
    n = len(ys)
    if n < 3:
        return 0.0, ys[0] if ys else 0.0, 0.0
    xs    = list(range(n))
    mx    = (n - 1) / 2.0
    my    = sum(ys) / n
    ss_xx = sum((x - mx) ** 2 for x in xs)
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ss_yy = sum((y - my) ** 2 for y in ys)
    if ss_xx == 0 or ss_yy == 0:
        return 0.0, my, 0.0
    slope     = ss_xy / ss_xx
    intercept = my - slope * mx
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)
    return slope, intercept, r_squared


def compute_regime() -> Tuple[Regime, float, float]:
    if len(btc_prices) < MIN_PRICES_FOR_REGIME:
        return Regime.UNKNOWN, 0.0, 0.0

    prices  = list(btc_prices)[-TREND_LOOKBACK:]
    returns = list(btc_returns)[-(TREND_LOOKBACK - 1):]

    realized_vol = sum(abs(r) for r in returns) / len(returns) if returns else 0.0

    if returns and max(abs(r) for r in returns[-3:]) > VOL_CIRCUIT_BREAKER:
        log.warning("VOL CIRCUIT | spike %.3f%%", max(abs(r) for r in returns[-3:]))
        return Regime.HIGH_VOL, 0.0, realized_vol

    if realized_vol > VOLATILITY_CAP_PCT:
        log.info("Regime | HIGH_VOL (vol=%.4f%%)", realized_vol)
        return Regime.HIGH_VOL, 0.0, realized_vol

    slope, _, r_squared = _linear_regression(prices)

    if r_squared >= R2_TREND_THRESHOLD:
        regime = Regime.TRENDING_UP if slope > 0 else Regime.TRENDING_DOWN
        log.info("Regime | %s (R2=%.3f)", regime.value, r_squared)
        return regime, r_squared, realized_vol

    log.info("Regime | RANGING (R2=%.3f)", r_squared)
    return Regime.RANGING, r_squared, realized_vol


# -----------------------------------------------------------------------------
# VOLATILITY CIRCUIT BREAKER
# -----------------------------------------------------------------------------

def check_vol_circuit() -> bool:
    global _vol_circuit_open, _vol_circuit_until

    if _vol_circuit_open:
        if time.time() > _vol_circuit_until:
            _vol_circuit_open = False
            log.info("Vol circuit CLOSED — resuming.")
        else:
            log.info("Vol circuit OPEN — %.1f min remaining.",
                     (_vol_circuit_until - time.time()) / 60.0)
            return True

    if len(btc_returns) < 3:
        return False

    recent   = list(btc_returns)[-6:]
    max_move = max(abs(r) for r in recent)
    if max_move > VOL_CIRCUIT_BREAKER:
        _vol_circuit_open  = True
        _vol_circuit_until = time.time() + 1800
        log.warning("Vol circuit OPENED — %.3f%%", max_move)
        tg.send_telegram_message(
            f"VOL CIRCUIT BREAKER OPENED\n"
            f"Max move: {max_move:.3f}% — trading paused 30 min."
        )
        return True

    return False


# -----------------------------------------------------------------------------
# MOMENTUM SIGNAL
# -----------------------------------------------------------------------------

def compute_momentum(ob_direction: str) -> Tuple[str, float]:
    if len(btc_prices) < 5:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    prices  = list(btc_prices)
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
        magnitude_scale = min(2.0, abs(move_pct) / MOMENTUM_THRESH_PCT)
        return "AGREE", MOMENTUM_ACCURACY_LIFT * magnitude_scale

    return "CONFLICT", 0.0


def momentum_gate_ok(momentum_verdict: str) -> bool:
    """DOCTRINE LAYER 7 — the AGREE-required momentum gate.

    A trade requires BTC spot momentum to EXPLICITLY AGREE with the order-book
    direction. When REQUIRE_AGREE_MOMENTUM is true (default), both CONFLICT and
    NEUTRAL are rejections. Applies in EVERY session state, RECOVERY included.
    """
    if not REQUIRE_AGREE_MOMENTUM:
        return True
    return momentum_verdict == "AGREE"


# -----------------------------------------------------------------------------
# ORDER BOOK ANALYSIS
# -----------------------------------------------------------------------------

def _parse_ob_levels(levels: list, lo: float, hi: float) -> Tuple[float, int]:
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
    ob_fp      = ob_data.get("orderbook_fp", {})
    yes_levels = ob_fp.get("yes_dollars", [])
    no_levels  = ob_fp.get("no_dollars",  [])

    near  = 10
    y_lo  = (yes_mid - near) / 100.0
    y_hi  = (yes_mid + near) / 100.0
    n_mid = (100 - yes_mid) / 100.0
    n_lo  = n_mid - near / 100.0
    n_hi  = n_mid + near / 100.0

    yes_depth, yes_lc = _parse_ob_levels(yes_levels, y_lo, y_hi)
    no_depth,  no_lc  = _parse_ob_levels(no_levels,  n_lo, n_hi)
    total = yes_depth + no_depth

    if total < MIN_OB_DEPTH:
        log.info("OB | depth $%.0f < min $%.0f", total, MIN_OB_DEPTH)
        return None

    if yes_lc == 0 or no_lc == 0:
        log.info("OB | ghost book (YES:%d NO:%d levels)", yes_lc, no_lc)
        return None

    yr = yes_depth / total
    nr = no_depth  / total

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
        log.info("OB | no dominant side (YES:%.1f%% NO:%.1f%% thresh:%.1f%%)",
                 yr * 100, nr * 100, eff_thresh * 100)
        return None

    log.info("OB | %s %.1f%% | $%.0f | thresh=%.1f%%",
             direction, imbalance * 100, total, eff_thresh * 100)

    return {
        "direction":   direction,
        "imbalance":   imbalance,
        "total_depth": total,
        "yes_depth":   yes_depth,
        "no_depth":    no_depth,
        "yes_lc":      yes_lc,
        "no_lc":       no_lc,
        "eff_thresh":  eff_thresh,
    }


def check_ob_trend(ticker: str, direction: str, imbalance: float) -> bool:
    now  = time.time()
    prev = _prev_ob.get(ticker)
    _prev_ob[ticker] = (direction, imbalance, now)

    if prev is None:
        return True

    prev_dir, prev_imb, prev_ts = prev
    if now - prev_ts > 600:
        return True

    if direction == prev_dir and imbalance < prev_imb - 0.10:
        log.info("OB trend | fading %.1f%%->%.1f%% — blocking",
                 prev_imb * 100, imbalance * 100)
        return False

    return True


def regime_direction(regime: Regime) -> Optional[str]:
    """Map a trend regime to the contract side it favors."""
    if regime == Regime.TRENDING_UP:
        return "YES"
    if regime == Regime.TRENDING_DOWN:
        return "NO"
    return None


def regime_agrees(regime: Regime, ob_direction: str) -> bool:
    """The order-book side must point the same way as the measured trend."""
    favored = regime_direction(regime)
    return favored is None or favored == ob_direction.upper()


# -----------------------------------------------------------------------------
# BAYESIAN PROBABILITY MODEL
# -----------------------------------------------------------------------------

def bayesian_win_prob(
    ob: dict,
    momentum_verdict: str,
    momentum_adj: float,
    regime: Regime,
    r_squared: float,
    realized_vol: float,
) -> float:
    prior = _live_prior

    if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        r2_bonus   = (r_squared - R2_TREND_THRESHOLD) * 0.10
        regime_adj = 0.02 + r2_bonus
    else:
        regime_adj = 0.0

    eff_thresh    = ob.get("eff_thresh", OB_IMBALANCE_THRESH)
    imbalance_adj = min(0.06, max(0.0, ob["imbalance"] - eff_thresh) * 0.30)

    depth_adj = 0.0
    if ob["total_depth"] > 500:
        depth_adj = min(0.02, math.log10(ob["total_depth"] / 500) * 0.02)

    vol_penalty = min(0.04, realized_vol / VOLATILITY_CAP_PCT * 0.04)

    win_prob = max(0.50, min(0.92,
        prior + momentum_adj + regime_adj + imbalance_adj + depth_adj - vol_penalty
    ))

    log.info("WinProb | prior=%.3f mom=%.3f regime=%.3f imb=%.3f depth=%.3f vol=-%.3f -> %.3f",
             prior, momentum_adj, regime_adj, imbalance_adj, depth_adj, vol_penalty, win_prob)
    return win_prob


# -----------------------------------------------------------------------------
# CONFIDENCE SCORING
# -----------------------------------------------------------------------------

def compute_confidence(
    ob: dict,
    regime: Regime,
    r_squared: float,
    momentum_verdict: str,
    win_prob: float,
    mins_remaining: float,
    session_score: int,
) -> float:
    thresh    = ob["eff_thresh"]
    imb_pts   = max(0.0, (ob["imbalance"] - thresh) / (1.0 - thresh)) * 25.0
    depth_pts = min(15.0, math.log10(max(1, ob["total_depth"] / MIN_OB_DEPTH)) * 10.0)

    regime_map = {
        Regime.TRENDING_UP:   20.0,
        Regime.TRENDING_DOWN: 20.0,
        Regime.RANGING:        0.0,
        Regime.HIGH_VOL:     -20.0,
        Regime.UNKNOWN:        0.0,
    }
    regime_pts = regime_map.get(regime, 0.0)
    if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        regime_pts += min(5.0, (r_squared - R2_TREND_THRESHOLD) * 15.0)

    # v9.3.0: NEUTRAL restored 8.0 -> 2.0 (doctrine Layer 8: momentum only
    # counts if AGREE).
    momentum_map = {"AGREE": 15.0, "NEUTRAL": 2.0, "CONFLICT": -20.0}
    momentum_pts = momentum_map.get(momentum_verdict, 0.0)

    prob_pts = max(0.0, (win_prob - 0.50) / 0.42 * 15.0)

    time_pts = min(10.0, max(0.0,
        (mins_remaining - MIN_MINUTES_TO_EXPIRY) /
        max(0.1, 10.0 - MIN_MINUTES_TO_EXPIRY) * 10.0
    ))

    total = max(0.0, min(100.0,
        imb_pts + depth_pts + regime_pts + momentum_pts + prob_pts + time_pts
    ))

    log.info("Conf | imb=%.1f depth=%.1f regime=%.1f mom=%.1f prob=%.1f time=%.1f -> %.0f",
             imb_pts, depth_pts, regime_pts, momentum_pts, prob_pts, time_pts, total)
    return total


# -----------------------------------------------------------------------------
# EDGE & SIZING
# -----------------------------------------------------------------------------

def calc_edge(win_prob: float, contract_price_cents: int) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    net   = (100 - contract_price_cents) / 100.0
    stake = contract_price_cents / 100.0
    return (win_prob * net) - ((1.0 - win_prob) * stake)


def ladder_record(won: bool, pnl: float) -> None:
    """Feed a settled trade outcome to the laddering overlay (no-op if off)."""
    if stake_ladder is not None:
        try:
            stake_ladder.on_trade_result(won, pnl)
        except Exception as e:
            log.warning("Ladder record error: %s", e)


def kelly_bet(win_prob: float, contract_price_cents: int, balance: float) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b          = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly = max(0.0, (b * win_prob - (1.0 - win_prob)) / b)
    kf         = KELLY_FRACTION
    if session_state == SessionState.RECOVERY:
        kf *= KELLY_RECOVERY_MULT
    base_bet = round(min(full_kelly * kf * balance, TRADE_SIZE_CAP,
                         balance * MAX_BET_FRACTION), 2)

    if stake_ladder is not None:
        ceiling  = min(stake_ladder.cfg.max_multiplier * TRADE_SIZE_CAP,
                       balance * MAX_BET_FRACTION)
        decision = stake_ladder.get_stake(base_bet, max_stake=ceiling)
        return decision.stake

    return base_bet


# -----------------------------------------------------------------------------
# STATISTICAL PERFORMANCE GUARD
# -----------------------------------------------------------------------------

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
    return (round(p * 100, 1),
            round(max(0, center - spread) * 100, 1),
            round(min(1, center + spread) * 100, 1))


def update_live_prior() -> None:
    global _live_prior
    total = live_wins + live_losses
    if total < 10:
        return
    empirical   = live_wins / total
    weight      = min(1.0, total / 50.0)
    _live_prior = OB_BASE_ACCURACY * (1.0 - weight) + empirical * weight
    log.debug("Prior -> %.3f (n=%d)", _live_prior, total)


def performance_guard() -> bool:
    total = live_wins + live_losses
    if total < MIN_SAMPLE_TRADES:
        return True
    wlb = wilson_lower_bound(live_wins, total)
    if wlb < 0.50:
        log.warning("PERF GUARD | Wilson LB %.1f%% < 50%%", wlb * 100)
        return False
    return True


# -----------------------------------------------------------------------------
# SESSION QUALITY
# -----------------------------------------------------------------------------

def get_session_score() -> int:
    return SESSION_QUALITY.get(datetime.now(timezone.utc).hour, 50)


# -----------------------------------------------------------------------------
# RECOVERY PROTOCOL
# -----------------------------------------------------------------------------

def update_session_state(current_balance: float) -> None:
    global session_state, recovery_trades, recovery_entry_wins, recovery_entry_losses
    global recovery_entered_ts

    if session_state == SessionState.HALTED:
        return

    loss_pct = (session_start_balance - current_balance) / max(1.0, session_start_balance)

    if session_state == SessionState.ACTIVE:
        if loss_pct > RECOVERY_TRIGGER_PCT:
            session_state         = SessionState.RECOVERY
            recovery_trades       = 0
            recovery_entry_wins   = live_wins
            recovery_entry_losses = live_losses
            recovery_entered_ts   = time.time()
            log.warning("SESSION RECOVERY | loss %.1f%% | entry snapshot W=%d L=%d",
                        loss_pct * 100, recovery_entry_wins, recovery_entry_losses)
            tg.send_telegram_message(
                f"SESSION RECOVERY MODE\n"
                f"Loss: {loss_pct*100:.1f}% — sizing halved."
            )

    elif session_state == SessionState.RECOVERY:
        if recovery_entered_ts <= 0.0:
            recovery_entered_ts = time.time()
        elif time.time() - recovery_entered_ts > RECOVERY_MAX_SECS:
            session_state = SessionState.ACTIVE
            log.warning("RECOVERY EXITED | timeout %.0fs elapsed (loss %.1f%%)",
                        time.time() - recovery_entered_ts, loss_pct * 100)
            tg.send_telegram_message(
                f"Recovery exited — {RECOVERY_MAX_SECS//60}min timeout "
                f"(loss {loss_pct*100:.1f}%)"
            )
            return

        if loss_pct <= RECOVERY_TRIGGER_PCT:
            session_state = SessionState.ACTIVE
            log.info("RECOVERY EXITED | drawdown healed (loss %.1f%% <= trigger %.1f%%)",
                     loss_pct * 100, RECOVERY_TRIGGER_PCT * 100)
            tg.send_telegram_message(
                f"Recovery exited — drawdown healed ({loss_pct*100:.1f}%)"
            )
            return

        trades_since_entry = (
            (live_wins + live_losses) - (recovery_entry_wins + recovery_entry_losses)
        )
        if trades_since_entry >= RECOVERY_EXIT_TRADES:
            wins_since = live_wins - recovery_entry_wins
            wr = wins_since / trades_since_entry if trades_since_entry > 0 else 0.0
            if wr >= RECOVERY_WIN_RATE_MIN:
                session_state = SessionState.ACTIVE
                log.info("RECOVERY EXITED | WR=%.1f%% (%d/%d since entry)",
                         wr * 100, wins_since, trades_since_entry)
                tg.send_telegram_message(
                    f"Recovery exited — WR {wr*100:.0f}% ({wins_since}/{trades_since_entry})"
                )


# -----------------------------------------------------------------------------
# PORTFOLIO / BALANCE
# -----------------------------------------------------------------------------

def get_live_balance(allow_cached_zero: bool = True) -> float:
    global _last_known_balance
    try:
        data  = _get("/portfolio/balance")
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
            log.error("Balance fetch failed, no cache: %s", e)
            raise
        log.warning("Balance fetch failed: %s — cached $%.2f", e, _last_known_balance)
        return _last_known_balance


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _extract_realized_dollars(rec: dict, trade_cost: Optional[float] = None) -> Optional[float]:
    """
    Extract realized PnL in dollars from a Kalshi settlement record.

    v9.0.7: rebuilt against the real KXBTC15M settlement schema. The record has
    NO realized_pnl/profit field. Reconstruction:
        pnl = (revenue / 100) - yes_total_cost_dollars - no_total_cost_dollars - fee_cost
    revenue is in CENTS; *_dollars cost fields and fee_cost are in dollars.
    """
    for k in ("realized_pnl_dollars", "settlement_pnl_dollars", "pnl_dollars"):
        v = _coerce_float(rec.get(k))
        if v is not None:
            return v
    for k in ("realized_pnl_cents", "realized_pnl", "settlement_pnl", "pnl"):
        v = _coerce_float(rec.get(k))
        if v is not None:
            return v / 100.0 if k.endswith("_cents") else v

    revenue = _coerce_float(rec.get("revenue"))
    if revenue is not None:
        revenue /= 100.0
        yes_cost = _coerce_float(rec.get("yes_total_cost_dollars")) or 0.0
        no_cost  = _coerce_float(rec.get("no_total_cost_dollars"))  or 0.0
        fee      = _coerce_float(rec.get("fee_cost")) or 0.0
        total_cost = yes_cost + no_cost
        if total_cost > 0:
            return round(revenue - total_cost - fee, 4)
        if revenue == 0.0:
            return 0.0
        if trade_cost is not None and trade_cost > 0:
            return round(revenue - trade_cost - fee, 4)
        return 1.0

    mr = str(rec.get("market_result", "")).lower()
    if mr in ("yes", "no"):
        return None

    return None


def _extract_ticker(rec: dict) -> str:
    for k in ("market_ticker", "ticker", "event_ticker"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


def _is_post_boot(rec: dict) -> bool:
    """True if a settlement record was created at/after this process's boot."""
    if not _session_start_ts:
        return False
    rec_ts = (rec.get("settled_time") or rec.get("created_time")
              or rec.get("timestamp") or "")
    if not rec_ts:
        return False
    try:
        rec_ts = str(rec_ts).replace("Z", "+00:00")
        boot_ts = _session_start_ts.replace("Z", "+00:00")
        return datetime.fromisoformat(rec_ts) >= datetime.fromisoformat(boot_ts)
    except Exception:
        return False


def _fetch_settled_records(since_ts: str) -> list:
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


# -----------------------------------------------------------------------------
# RESOLUTION
# -----------------------------------------------------------------------------

def resolve_open_orders() -> None:
    global paper_balance, paper_daily_pnl, consecutive_losses
    global running_pnl, live_wins, live_losses, streak_pause_until

    if not open_orders and not DEMO_MODE:
        pass
    elif not open_orders and DEMO_MODE:
        return

    if DEMO_MODE:
        now = time.time()
        for oid in list(open_orders.keys()):
            trade = open_orders[oid]
            if now - trade.get("placed_at", now) < 900:
                continue
            open_orders.pop(oid)
            ticker    = trade.get("ticker", "")
            active_tickers.discard(ticker)
            count     = trade.get("count", 0)
            cost      = trade.get("cost", 0.0)
            side      = trade.get("side", "YES").upper()
            entry_btc = trade.get("btc_entry_price", 0)
            cur_btc   = fetch_btc_price()

            if entry_btc > 0 and cur_btc and cur_btc > 1000:
                btc_up = cur_btc > entry_btc
                won    = btc_up if side == "YES" else not btc_up
                sim    = "btc"
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
                    profit=trade_pnl, balance=paper_balance,
                    daily_pnl=paper_daily_pnl,
                    ticker=ticker, direction=trade.get("side", "?"),
                    wins=live_wins, losses=live_losses,
                )
            else:
                consecutive_losses += 1
                live_losses += 1
                if consecutive_losses >= MAX_CONSEC_LOSSES:
                    streak_pause_until = time.time() + STREAK_PAUSE_SECS
                tg.send_loss_notification(
                    loss=abs(trade_pnl), balance=paper_balance,
                    daily_pnl=paper_daily_pnl,
                    ticker=ticker, direction=trade.get("side", "?"),
                    streak=consecutive_losses,
                    wins=live_wins, losses=live_losses,
                )

            ladder_record(won, trade_pnl)

            log.info("PAPER SETTLED | %s | %s | %s | sim=%s | bal=$%.2f",
                     ticker[-15:], side, result.upper(), sim, paper_balance)

        update_live_prior()
        return

    # -- Live ------------------------------------------------------------------
    try:
        since_ts = _session_start_ts or (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        records = _fetch_settled_records(since_ts)
        log.info("RESOLVE | %d settled, %d open, %d processed",
                 len(records), len(open_orders), len(_processed_settlement_ids))

        ticker_to_oid: dict = {}
        for oid, trade in open_orders.items():
            tk = trade.get("ticker", "")
            if tk:
                ticker_to_oid[tk]         = oid
                ticker_to_oid[tk.upper()] = oid

        for rec in records:
            rec_ticker  = _extract_ticker(rec)
            rec_created = (rec.get("created_time") or rec.get("settled_time")
                           or rec.get("timestamp", ""))
            rec_id      = f"{rec_ticker}:{rec_created}"

            if rec_id in _processed_settlement_ids:
                continue

            matched_oid = (ticker_to_oid.get(rec_ticker)
                           or ticker_to_oid.get(rec_ticker.upper()))
            if not matched_oid:
                for oid, trade in list(open_orders.items()):
                    if trade.get("ticker", "").upper() == rec_ticker.upper():
                        matched_oid = oid
                        break

            _processed_settlement_ids.add(rec_id)

            if not matched_oid:
                if not _is_post_boot(rec):
                    continue
                pnl_d = _extract_realized_dollars(rec)
                if pnl_d is not None and pnl_d != 0.0:
                    if pnl_d > 0:
                        live_wins += 1
                        log.info("UNMATCHED WIN | %s | $%.2f (pre-restart)",
                                 rec_ticker[-15:], pnl_d)
                    else:
                        live_losses += 1
                        consecutive_losses += 1
                        if consecutive_losses >= MAX_CONSEC_LOSSES:
                            streak_pause_until = time.time() + STREAK_PAUSE_SECS
                        log.info("UNMATCHED LOSS | %s | $%.2f (pre-restart)",
                                 rec_ticker[-15:], pnl_d)
                    ladder_record(pnl_d > 0, pnl_d)
                    update_live_prior()
                continue

            trade = open_orders.pop(matched_oid)
            active_tickers.discard(rec_ticker)
            active_tickers.discard(trade.get("ticker", ""))

            trade_cost = trade.get("cost")
            pnl_d = _extract_realized_dollars(rec, trade_cost=trade_cost)
            if pnl_d is None:
                log.warning("RESOLVE | %s — no pnl field. Keys: %s",
                            rec_ticker[-15:], list(rec.keys()))
                continue

            if pnl_d == 0.0:
                log.info("NO-FILL | %s", rec_ticker[-15:])
                for t in trade_history:
                    if t.get("order_id") == matched_oid:
                        t["result"] = "unfilled"
                        t["pnl"]    = 0.0
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

            balance        = get_live_balance()
            running_pnl   += pnl
            live_daily_pnl = balance - session_start_balance

            if won:
                consecutive_losses = 0
                live_wins += 1
            else:
                consecutive_losses += 1
                live_losses += 1
                if consecutive_losses >= MAX_CONSEC_LOSSES:
                    streak_pause_until = time.time() + STREAK_PAUSE_SECS

            ladder_record(won, pnl)

            wlb = wilson_lower_bound(live_wins, live_wins + live_losses)
            log.info("SETTLED | %s | %s | $%.2f | WR=%d/%d | LB=%.1f%%",
                     rec_ticker[-15:], result.upper(), pnl,
                     live_wins, live_wins + live_losses, wlb * 100)

            if won:
                tg.send_win_notification(
                    profit=pnl, balance=balance, daily_pnl=live_daily_pnl,
                    ticker=rec_ticker, direction=trade.get("side", "?"),
                    wins=live_wins, losses=live_losses,
                )
            else:
                tg.send_loss_notification(
                    loss=abs(pnl), balance=balance, daily_pnl=live_daily_pnl,
                    ticker=rec_ticker, direction=trade.get("side", "?"),
                    streak=consecutive_losses,
                    wins=live_wins, losses=live_losses,
                )

        update_live_prior()

        try:
            canceled     = _get("/portfolio/orders", {"status": "canceled", "limit": 100})
            canceled_ids = {o["order_id"] for o in canceled.get("orders", [])}
            for oid in list(open_orders.keys()):
                if oid in canceled_ids:
                    trade = open_orders.pop(oid)
                    active_tickers.discard(trade.get("ticker", ""))
                    log.info("Order canceled | %s", oid[:12])
        except Exception:
            pass

        now   = time.time()
        stale = [oid for oid, t in open_orders.items()
                 if now - t.get("placed_at", now) > 1200]
        for oid in stale:
            trade = open_orders.pop(oid)
            active_tickers.discard(trade.get("ticker", ""))
            log.info("Stale purged | %s", trade.get("ticker", "?")[-15:])

    except Exception as e:
        log.warning("Resolution error: %s", e)


# -----------------------------------------------------------------------------
# STALE ORDER CANCELLATION
# -----------------------------------------------------------------------------

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
            log.info("Stale cancel (paper) | %s | $%.2f", ticker[-15:], cost)
        else:
            try:
                _delete(f"/portfolio/events/orders/{oid}")
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                log.info("Stale cancel (live) | %s | %s", ticker[-15:], oid[:12])
            except Exception as e:
                log.warning("Stale cancel failed %s: %s", oid[:12], e)


# -----------------------------------------------------------------------------
# MARKET DISCOVERY
# -----------------------------------------------------------------------------

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
            log.info("Market | %s bid=%dc mid=%dc ask=%dc",
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


# -----------------------------------------------------------------------------
# GUARD STACK
# -----------------------------------------------------------------------------

def balance_floor_check(balance: float) -> bool:
    if balance < MIN_BALANCE_FLOOR:
        log.warning("BALANCE FLOOR | $%.2f < $%.2f", balance, MIN_BALANCE_FLOOR)
        return False
    return True


def daily_loss_check(balance: float) -> bool:
    global _session_halted
    if _session_halted:
        return False
    pnl = paper_daily_pnl if DEMO_MODE else daily_pnl
    pct_cap = MAX_DAILY_LOSS_PCT * session_start_balance if session_start_balance > 0 else 0.0
    loss_cap = min(MAX_DAILY_LOSS, pct_cap) if pct_cap > 0 else MAX_DAILY_LOSS
    # v9.3.1: a daily cap tighter than a single full-stake trade halts the day
    # after ONE loss, which is incoherent. Floor it just above the largest stake
    # we can actually place this session (the same ceiling kelly_bet uses).
    # The fixed MAX_DAILY_LOSS still binds above this floor — nothing is loosened
    # beyond the documented dollar cap.
    max_single_stake = (min(TRADE_SIZE_CAP, session_start_balance * MAX_BET_FRACTION)
                        if session_start_balance > 0 else TRADE_SIZE_CAP)
    loss_cap = min(MAX_DAILY_LOSS, max(loss_cap, max_single_stake * 1.5))
    if pnl <= -loss_cap:
        _session_halted = True
        log.warning("DAILY LOSS | $%.2f >= cap $%.2f — halted.", abs(pnl), loss_cap)
        telegram_halt(f"Daily loss cap ${abs(pnl):.2f}", balance)
        return False
    if session_stop_threshold > 0 and balance < session_stop_threshold:
        _session_halted = True
        log.warning("SESSION STOP | $%.2f < $%.2f — halted.", balance, session_stop_threshold)
        telegram_halt(f"Session stop at ${balance:.2f}", balance)
        return False
    return True


def spread_check(bid: int, ask: int) -> bool:
    if ask - bid <= 0:
        log.info("Spread | zero/crossed")
        return False
    return True


def expiry_guard(mid: int) -> bool:
    if mid > 85 or mid < 15:
        log.info("Expiry | %dc near-certain", mid)
        return False
    return True


def cooldown_check() -> bool:
    elapsed = time.time() - last_trade_ts
    if elapsed < 60:
        log.info("Cooldown | %.0fs remaining", 60 - elapsed)
        return False
    return True


def session_quality_check() -> bool:
    score = get_session_score()
    utc_h = datetime.now(timezone.utc).hour
    if score < MIN_SESSION_SCORE:
        log.info("Session quality | UTC%d score=%d < %d", utc_h, score, MIN_SESSION_SCORE)
        return False
    return True


def streak_check() -> bool:
    global consecutive_losses
    if consecutive_losses >= MAX_CONSEC_LOSSES:
        if time.time() < streak_pause_until:
            log.info("Streak pause | %d consec losses", consecutive_losses)
            return False
        consecutive_losses = 0
    return True


# -----------------------------------------------------------------------------
# ORDER EXECUTION
# -----------------------------------------------------------------------------

def place_order(ticker: str, direction: str, bet_dollars: float,
                limit_cents: int, win_prob: float, edge: float) -> Optional[str]:
    global last_trade_ts, paper_balance

    if limit_cents <= 0:
        return None
    count = int((bet_dollars * 100) / limit_cents)
    if count < 1:
        log.info("Order | 0 contracts at $%.2f @ %dc", bet_dollars, limit_cents)
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
        log.info("PAPER | %s %s | %d @ %dc | $%.2f | bal=$%.2f",
                 direction, ticker[-15:], count, limit_cents, cost, paper_balance)
        tg.send_trade_entry_notification(
            ticker=ticker, direction=direction, cost=cost,
            price_cents=limit_cents, balance=paper_balance,
            ob_pct=win_prob * 100, edge_pct=edge * 100,
        )
        return client_id

    # Kalshi V2 single-book order model (POST /portfolio/events/orders).
    is_yes     = direction.upper() == "YES"
    yes_cents  = limit_cents if is_yes else (100 - limit_cents)
    body: dict = {
        "ticker":                     ticker,
        "client_order_id":            client_id,
        "side":                       "bid" if is_yes else "ask",
        "count":                      str(count),
        "price":                      f"{yes_cents / 100:.4f}",
        "time_in_force":              "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }

    try:
        resp     = _post("/portfolio/events/orders", body)
        order_id = (resp.get("order", {}).get("order_id")
                    or resp.get("order_id") or client_id)
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
        log.info("ORDER | %s %s | %d @ %dc | $%.2f | %s",
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
        log.error("Order failed | HTTP %s | %s", status, body_t)
        return None
    except Exception as e:
        log.error("Order failed | %s", e)
        return None


# -----------------------------------------------------------------------------
# TELEGRAM WRAPPERS
# -----------------------------------------------------------------------------

def telegram_boot(balance: float) -> None:
    mode = "PAPER" if DEMO_MODE else "LIVE"
    tg.send_telegram_message(
        f"Johnny5 {BOT_VERSION} STARTED\n"
        f"{mode} | State: {session_state.value}\n"
        f"Balance: ${balance:.2f}\n"
        f"DailyLoss<=${MAX_DAILY_LOSS:.0f} | Floor=${MIN_BALANCE_FLOOR:.0f}\n"
        f"MinConf={MIN_CONFIDENCE} | MinWinP={MIN_WIN_PROB*100:.0f}% | R2>={R2_TREND_THRESHOLD}\n"
        f"OBDepth>=${MIN_OB_DEPTH:.0f} | OBImb>={OB_IMBALANCE_THRESH*100:.0f}%\n"
        f"AGREE-gate={'ON' if REQUIRE_AGREE_MOMENTUM else 'OFF'} | "
        f"Breakeven<={YES_BREAKEVEN_PRICE}c\n"
        f"SessionScore>={MIN_SESSION_SCORE} | Kelly={KELLY_FRACTION}"
    )


def telegram_halt(reason: str, balance: float) -> None:
    tg.send_telegram_message(
        f"HALTED (PERMANENT)\nReason: {reason}\nBalance: ${balance:.2f}"
    )


def telegram_daily_summary(balance: float, pnl: float, wins: int, losses: int) -> None:
    total  = wins + losses
    wr     = wins / total * 100 if total > 0 else 0.0
    emoji  = "UP" if pnl >= 0 else "DOWN"
    ci_str = ""
    if total >= 10:
        wlb    = wilson_lower_bound(wins, total)
        ci_str = f" LB={wlb*100:.0f}%"
    tg.send_telegram_message(
        f"{emoji} Daily Summary\n"
        f"P&L: ${pnl:+.2f} | Balance: ${balance:.2f}\n"
        f"WR: {wr:.0f}%{ci_str} ({wins}W/{losses}L)"
    )


# -----------------------------------------------------------------------------
# MAIN DECISION ENGINE
# -----------------------------------------------------------------------------

def run_decision(market: dict, balance: float) -> None:
    global last_signal_desc

    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        return
    yes_mid = (yes_bid + yes_ask) // 2

    if not balance_floor_check(balance):
        return
    if not expiry_guard(yes_mid):
        return
    if not spread_check(yes_bid, yes_ask):
        return
    if ticker in active_tickers:
        log.info("Position guard | %s", ticker[-15:])
        return
    if ticker in session_traded_tickers:
        log.info("Session guard | already traded %s", ticker[-15:])
        last_signal_desc = f"session re-entry ({ticker[-10:]})"
        return
    if not cooldown_check():
        return
    if not daily_loss_check(balance):
        return
    if not streak_check():
        last_signal_desc = f"streak pause ({consecutive_losses}L)"
        return
    if not performance_guard():
        last_signal_desc = "perf guard (Wilson LB < 50%)"
        return
    if not session_quality_check():
        last_signal_desc = f"session quality UTC{datetime.now(timezone.utc).hour}"
        return
    if len(open_orders) >= MAX_CONCURRENT_POS:
        log.info("Concurrent | %d open", len(open_orders))
        return

    mins = minutes_to_expiry(market)
    if mins < MIN_MINUTES_TO_EXPIRY:
        log.info("Expiry imminent | %.1f min", mins)
        last_signal_desc = "expiry imminent"
        return

    if check_vol_circuit():
        last_signal_desc = "vol circuit open"
        return

    regime, r_squared, realized_vol = compute_regime()
    if regime in (Regime.UNKNOWN, Regime.RANGING, Regime.HIGH_VOL):
        log.info("Regime | %s — no trade", regime.value)
        last_signal_desc = f"regime={regime.value}"
        return

    try:
        ob_raw = get_order_book(ticker)
    except Exception as e:
        log.warning("OB fetch failed: %s", e)
        return

    ob = analyze_order_book(ob_raw, yes_mid)
    if ob is None:
        last_signal_desc = "OB no signal"
        return

    ob_dir = ob["direction"]
    if not check_ob_trend(ticker, ob_dir, ob["imbalance"]):
        last_signal_desc = "OB fading"
        return

    # Direction gate: never bet the order book against the measured trend.
    if not regime_agrees(regime, ob_dir):
        log.info("Regime conflict | OB=%s vs %s — no trade", ob_dir, regime.value)
        last_signal_desc = f"regime conflict OB={ob_dir} {regime.value}"
        return

    momentum_verdict, momentum_adj = compute_momentum(ob_dir)
    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT | OB=%s", ob_dir)
        last_signal_desc = f"CONFLICT OB={ob_dir}"
        return

    # DOCTRINE LAYER 7 (restored v9.3.0): BTC spot momentum must EXPLICITLY
    # AGREE with the order-book direction. Applies in EVERY session state.
    if not momentum_gate_ok(momentum_verdict):
        log.info("Momentum | require AGREE, got %s (OB=%s) — no trade",
                 momentum_verdict, ob_dir)
        last_signal_desc = f"momentum {momentum_verdict} (need AGREE)"
        return

    win_prob = bayesian_win_prob(ob, momentum_verdict, momentum_adj,
                                  regime, r_squared, realized_vol)
    if win_prob < MIN_WIN_PROB:
        log.info("WinProb | %.3f < %.3f", win_prob, MIN_WIN_PROB)
        last_signal_desc = f"win_prob {win_prob:.2f} < {MIN_WIN_PROB:.2f}"
        return

    session_score = get_session_score()
    conf = compute_confidence(ob, regime, r_squared, momentum_verdict,
                               win_prob, mins, session_score)
    if conf < MIN_CONFIDENCE:
        log.info("Confidence | %.0f < %d", conf, MIN_CONFIDENCE)
        last_signal_desc = f"conf {conf:.0f} < {MIN_CONFIDENCE}"
        return

    if ob_dir == "YES":
        if yes_mid > YES_BREAKEVEN_PRICE:
            log.info("Price guard | YES %dc > breakeven", yes_mid)
            return
        direction      = "YES"
        contract_price = yes_mid
    else:
        no_price = 100 - yes_mid
        if no_price > YES_BREAKEVEN_PRICE:
            log.info("Price guard | NO %dc > breakeven", no_price)
            return
        direction      = "NO"
        contract_price = no_price

    if not (25 <= contract_price <= 75):
        log.info("Bias filter | %dc outside 25-75", contract_price)
        return

    edge = calc_edge(win_prob, contract_price)
    if edge < MIN_EDGE_PCT:
        log.info("Edge | %.3f < min %.3f", edge, MIN_EDGE_PCT)
        last_signal_desc = f"edge {edge:.3f} < {MIN_EDGE_PCT:.3f}"
        return

    bet = kelly_bet(win_prob, contract_price, balance)
    if bet < 0.25:
        log.info("Kelly | $%.2f too small", bet)
        return
    if balance < bet:
        log.warning("Insufficient balance")
        return

    if direction == "YES":
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        no_best     = 100 - yes_ask
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))
    limit_price = max(1, min(99, limit_price))

    if abs(limit_price - contract_price) > 8:
        log.info("Limit drift | %dc too far", limit_price)
        return

    total   = live_wins + live_losses
    wlb_str = (f" WLB={wilson_lower_bound(live_wins, total)*100:.1f}%"
               if total >= 10 else " WLB=n/a")

    log.info(
        "EDGE JUSTIFICATION | %s %s @ %dc | regime=%s(R2=%.2f) | "
        "OB=%.1f%% $%.0f | BTC=%s | WinP=%.1f%% Edge=%.1f%% Conf=%.0f | "
        "Bet=$%.2f | %.1fmin%s",
        direction, ticker[-15:], contract_price,
        regime.value, r_squared,
        ob["imbalance"] * 100, ob["total_depth"],
        momentum_verdict, win_prob * 100, edge * 100, conf,
        bet, mins, wlb_str
    )

    last_signal_desc = f"SIGNAL {direction} conf={conf:.0f} p={win_prob:.2f}"
    place_order(ticker, direction, bet, limit_price, win_prob, edge)


# -----------------------------------------------------------------------------
# DAILY SESSION ROLLOVER
# -----------------------------------------------------------------------------

def maybe_roll_session_day(current_balance: float) -> bool:
    """Reset the daily risk budget at the UTC day boundary. Returns True on roll."""
    global _session_day, _session_halted, session_start_balance
    global session_stop_threshold, daily_pnl, paper_daily_pnl, consecutive_losses
    global session_state, streak_pause_until

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today == _session_day:
        return False

    was_halted   = _session_halted
    _session_day = today
    _session_halted        = False
    session_start_balance  = current_balance
    session_stop_threshold = current_balance * SESSION_STOP_FRACTION
    daily_pnl              = 0.0
    paper_daily_pnl        = 0.0
    consecutive_losses     = 0
    streak_pause_until     = 0.0
    session_state          = SessionState.ACTIVE
    session_traded_tickers.clear()

    log.info("New trading day %s | balance $%.2f | daily budget reset%s",
             today, current_balance, " (halt cleared)" if was_halted else "")
    return True


# -----------------------------------------------------------------------------
# MAIN LOOP
# -----------------------------------------------------------------------------

def main() -> None:
    global session_start_balance, session_stop_threshold, daily_pnl
    global paper_balance, paper_daily_pnl, last_trade_ts, last_daily_summary_ts
    global consecutive_losses, last_signal_desc, last_heartbeat_ts, running_pnl
    global live_wins, live_losses, streak_pause_until
    global _last_known_balance, _shutdown_requested, _session_start_ts
    global _session_halted, session_state, recovery_trades
    global recovery_entry_wins, recovery_entry_losses, _session_day

    init_base_url()

    paper_balance         = float(os.environ.get("PAPER_BALANCE", "25.0"))
    _session_start_ts     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session_day          = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _session_halted       = False
    session_state         = SessionState.ACTIVE
    recovery_trades       = 0
    recovery_entry_wins   = 0
    recovery_entry_losses = 0

    session_traded_tickers.clear()
    _processed_settlement_ids.clear()

    log.info("=" * 70)
    log.info("  JOHNNY5 %s | %s", BOT_VERSION, "PAPER" if DEMO_MODE else "LIVE")
    log.info("  Start: %s", _session_start_ts)
    log.info("  Regime R2>=%.2f | VolCap=%.3f%% | Circuit=%.2f%%",
             R2_TREND_THRESHOLD, VOLATILITY_CAP_PCT, VOL_CIRCUIT_BREAKER)
    log.info("  OB depth>=$%.0f imb>=%.0f%% | WinP>=%.0f%% Edge>=%.0f%%",
             MIN_OB_DEPTH, OB_IMBALANCE_THRESH * 100, MIN_WIN_PROB * 100, MIN_EDGE_PCT * 100)
    log.info("  AGREE-gate=%s | MinConf=%d | Breakeven<=%dc | NEUTRALdrag=%.3f",
             "ON" if REQUIRE_AGREE_MOMENTUM else "OFF",
             MIN_CONFIDENCE, YES_BREAKEVEN_PRICE, NEUTRAL_ACCURACY_DRAG)
    log.info("  Kelly=%.2f cap=%.0f%% | SessionScore>=%d",
             KELLY_FRACTION, MAX_BET_FRACTION * 100, MIN_SESSION_SCORE)
    log.info("=" * 70)

    tg.validate_telegram_connection()

    live_wins          = 0
    live_losses        = 0
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
            tg.send_telegram_message(f"Johnny5 {BOT_VERSION} boot failed: balance error")
            return
        if bal <= 0.0:
            log.error("Starting balance $0 — aborting")
            tg.send_telegram_message(f"Johnny5 {BOT_VERSION} boot failed: balance=$0")
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
                halt_bal = paper_balance if DEMO_MODE else get_live_balance()
                if not maybe_roll_session_day(halt_bal):
                    log.info("Halted for the day — paused until UTC rollover.")
                    time.sleep(300)
                    continue

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

            ingest_btc_price()

            market = get_active_market()
            if not market:
                log.info("No active market — waiting %ds", POLL_INTERVAL)
                last_signal_desc = "no market"
                time.sleep(POLL_INTERVAL)
                continue

            current_ticker      = market.get("ticker", "")
            tickers_with_orders = {t.get("ticker", "") for t in open_orders.values()}
            expired = {t for t in active_tickers
                       if t != current_ticker and t not in tickers_with_orders}
            if expired:
                active_tickers.difference_update(expired)
                log.info("Expired locks: %s", expired)

            current_balance = paper_balance if DEMO_MODE else get_live_balance()
            maybe_roll_session_day(current_balance)
            update_session_state(current_balance)
            run_decision(market, current_balance)

            resolve_cycle += 1
            if resolve_cycle % 3 == 0:
                resolve_open_orders()
                cancel_stale_orders()

                if DEMO_MODE:
                    resolved = [t for t in trade_history
                                if t.get("result") in ("win", "loss")]
                    wins  = sum(1 for t in resolved if t["result"] == "win")
                    total = len(resolved)
                    wr    = wins / total if total > 0 else 0.0
                    log.info("PAPER | $%.2f | PnL=$%+.2f | WR=%.1f%% | Prior=%.3f | %s",
                             paper_balance, paper_daily_pnl, wr * 100,
                             _live_prior, session_state.value)
                else:
                    live_bal  = get_live_balance()
                    daily_pnl = live_bal - session_start_balance
                    wlb       = wilson_lower_bound(live_wins, live_wins + live_losses)
                    trades_since = (live_wins + live_losses) - (recovery_entry_wins + recovery_entry_losses)
                    log.info(
                        "Portfolio | $%.2f | PnL=$%+.2f | WR=%d/%d LB=%.1f%% | Prior=%.3f | %s"
                        "%s",
                        live_bal, daily_pnl, live_wins, live_wins + live_losses,
                        wlb * 100, _live_prior, session_state.value,
                        f" (rec+{trades_since})" if session_state == SessionState.RECOVERY else "",
                    )
                    if (datetime.now(timezone.utc).hour == 0
                            and time.time() - last_daily_summary_ts > 3600):
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
    tg.send_telegram_message(f"Johnny5 {BOT_VERSION} stopped. Final: ${final:.2f}")


if __name__ == "__main__":
    main()
