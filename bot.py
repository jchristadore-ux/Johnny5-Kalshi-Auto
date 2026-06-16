"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v9.0.9  —  Production Build                          ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.0.9 — RECOVERY DEADLOCK FIX (balance-based recovery exit)                ║
║                                                                              ║
║  DIAGNOSIS (2026-06-15 LIVE session, v9.0.8, ~3h slice, ZERO trades):       ║
║  - Portfolio status byte-identical all window:                              ║
║      $16.30 │ PnL=$-1.43 │ WR=2/4 │ RECOVERY (rec+2)                         ║
║  - 20 strong directional OB signals generated (up to 99.8% imbalance,       ║
║    $33k-$149k near-money depth). 18 killed by "Recovery │ AGREE required,    ║
║    got NEUTRAL", 2 by OB-fade. 0 trades, 0 settlements.                      ║
║                                                                              ║
║  Root cause: a self-referential lock. RECOVERY forces momentum==AGREE for   ║
║  every trade. In a calm market BTC momentum is NEUTRAL on essentially every  ║
║  scan, so the AGREE gate rejects 100% of signals. update_session_state()'s   ║
║  ONLY recovery exit was the trade-count path (>=5 trades since entry, WR>=   ║
║  60%) — but the AGREE gate is what prevents those trades from accumulating.  ║
║  Recovery needs trades to exit; recovery's own gate blocks the trades. The   ║
║  counter froze at rec+2 and could never reach 5. Current drawdown had even   ║
║  healed to ~8% (below the 10% RECOVERY_TRIGGER_PCT) yet stayed locked        ║
║  because recovery had no balance-based exit.                                 ║
║                                                                              ║
║  FIX: update_session_state() now exits RECOVERY when loss_pct recovers to    ║
║  at/below RECOVERY_TRIGGER_PCT — the exact mirror of the entry condition.    ║
║  The drawdown that triggered recovery healing is sufficient to return to     ║
║  ACTIVE regardless of trade count. The trade-count exit is RETAINED as a     ║
║  faster path when AGREE trades do settle. No edge/Kelly/loss-cap parameter   ║
║  is loosened — this only fixes an unreachable state-machine exit.            ║
║                                                                              ║
║  NOTE: this exits the instant loss_pct is at/below the trigger (symmetric    ║
║  with entry). If observed to flap on a balance oscillating around the        ║
║  trigger, tighten the exit to RECOVERY_TRIGGER_PCT * 0.5 for hysteresis.     ║
║                                                                              ║
║  ─────────────────────────────────────────────────────────────────────     ║
║  v9.0.8 — PERF-GUARD DEADLOCK FIX (boot-time settlement gate)               ║
║                                                                              ║
║  DIAGNOSIS (2026-06-11→12 LIVE session, v9.0.7, ~561 scans, ZERO trades):   ║
║  - PERF GUARD fired 1307×: Wilson LB 30.9% < 50% on every scan.             ║
║  - Portfolio froze at WR=28/70, 42 consecutive losses, balance flat $9.19.  ║
║  - The 28W/70L were NOT this session's trades — they were account history.  ║
║                                                                              ║
║  Root cause: /portfolio/settlements ignores created_since and returns the   ║
║  account-wide last 100 settlements. resolve_open_orders()'s unmatched       ║
║  branch counted ALL of them toward live_wins/live_losses with no time gate  ║
║  (tickers like -26JUN09… are days old). v9.0.7's _extract_realized_dollars  ║
║  rewrite made those records yield non-zero PnL (pre-9.0.7 they returned 0   ║
║  and were dropped), so the leak became active and seeded a sub-50% Wilson   ║
║  LB the bot could never escape — it can't trade, so it can't recover. The   ║
║  stale losses also tripped a permanent streak pause.                        ║
║                                                                              ║
║  FIX: _is_post_boot(rec) gates the unmatched-settlement branch. A record    ║
║  counts only if its timestamp >= _session_start_ts. In-flight pre-restart   ║
║  trades settle AFTER boot and are preserved; account history settled before ║
║  boot is skipped. Missing/unparseable timestamps are treated as NOT         ║
║  post-boot (conservative). The 9.0.6 changelog described this gate but it    ║
║  was never present in the code.                                             ║
║                                                                              ║
║  ─────────────────────────────────────────────────────────────────────     ║
║  v9.0.7 — SETTLEMENT SCHEMA CORRECTED (the critical fix)                     ║
║                                                                              ║
║  DIAGNOSIS (2026-06-08 LIVE session, v9.0.5, 1071 scans, 8 trades):         ║
║  - 8 trades fired, balance $9.46 → $11.60 (+$2.14, ~+23% session).          ║
║    THE EDGE IS REAL AND PROFITABLE.                                          ║
║  - But every settlement was DROPPED: WR=0/0, WLB=n/a for the full 11hr      ║
║    session. The Wilson gate, prior update, and RECOVERY tracking are all    ║
║    fed by counters that never moved. Profit was invisible to every risk     ║
║    and sizing system.                                                        ║
║  - Root cause: the real KXBTC15M settlement record has NO realized_pnl or   ║
║    profit field. v9.0.6's guessed field names (revenue_dollars, profit)     ║
║    did not match. Logged record keys are:                                   ║
║      event_ticker, fee_cost, market_result, no_count_fp,                    ║
║      no_total_cost_dollars, revenue, settled_time, ticker, value,           ║
║      yes_count_fp, yes_total_cost_dollars                                    ║
║                                                                              ║
║  FIX: _extract_realized_dollars() rewritten against the REAL schema:        ║
║    pnl = (revenue/100) - yes_total_cost_dollars - no_total_cost_dollars      ║
║          - fee_cost                                                           ║
║  `revenue` is returned in CENTS by Kalshi (same as the balance endpoint);   ║
║  the *_dollars cost/fee fields are already in dollars.                       ║
║                                                                              ║
║  v9.0.6 throughput changes RETAINED (this build supersedes v9.0.6):         ║
║  1. R2_TREND_THRESHOLD default 0.70 → 0.62                                  ║
║  2. compute_confidence(): NEUTRAL momentum 2.0 → 8.0 pts                    ║
║  3. NEUTRAL_ACCURACY_DRAG default 0.02 → 0.0                               ║
║  4. OB_IMBALANCE_THRESH default → 0.64                                      ║
║                                                                              ║
║  RAILWAY ENV VAR CHANGES REQUIRED (if not already applied):                  ║
║  - OB_IMBALANCE_THRESH: set to 0.64  (logs still show 0.68 — NOT applied)   ║
║  - R2_TREND_THRESHOLD:  delete or set to 0.62                               ║
║  - NEUTRAL_ACCURACY_DRAG: delete or set to 0.0                              ║
║                                                                              ║
║  v9.0.5 changes preserved: pre-restart settlement W/L counting,             ║
║  recovery entry snapshot, difference_update, active_tickers global fix.     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

BOT_VERSION = "9.0.9"

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
    RECOVERY  = "RECOVERY"
    HALTED    = "HALTED"


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
MAX_BET_FRACTION    = _env_float("MAX_BET_FRACTION", 0.08)
KELLY_FRACTION      = _env_float("KELLY_FRACTION", 0.30)
KELLY_RECOVERY_MULT = _env_float("KELLY_RECOVERY_MULT", 0.50)

# ── Laddering stake overlay (opt-in) ──────────────────────────────────────────
# Scales the Kelly stake by a performance-driven multiplier (0.5x–2x). Disabled
# by default so live sizing is unchanged until explicitly switched on with
# LADDER_ENABLED=true. See ladder.py and LADDER_STRATEGY.md.
LADDER_ENABLED = _env_bool("LADDER_ENABLED", False)

# ── Risk controls ─────────────────────────────────────────────────────────────
MIN_BALANCE_FLOOR     = _env_float("MIN_BALANCE_FLOOR", 5.0)
MAX_DAILY_LOSS        = _env_float("MAX_DAILY_LOSS_DOLLARS", 15.0)
SESSION_STOP_FRACTION = _env_float("SESSION_STOP_FRACTION", 0.40)
MAX_CONSEC_LOSSES     = _env_int("MAX_CONSEC_LOSSES", 2)
STREAK_PAUSE_SECS     = _env_int("STREAK_PAUSE_SECS", 1800)
STALE_ORDER_TIMEOUT   = _env_int("STALE_ORDER_TIMEOUT", 300)
MAX_CONCURRENT_POS    = _env_int("MAX_CONCURRENT_POS", 1)
MIN_SAMPLE_TRADES     = _env_int("MIN_SAMPLE_TRADES", 20)

# ── Regime detection ──────────────────────────────────────────────────────────
# v9.0.6: default lowered from 0.70 → 0.62.
# At 0.70 only 8.7% of scans qualified; 0.62 targets ~20%.
# Override via R2_TREND_THRESHOLD env var.
R2_TREND_THRESHOLD    = _env_float("R2_TREND_THRESHOLD", 0.62)
VOLATILITY_CAP_PCT    = _env_float("VOLATILITY_CAP_PCT", 0.18)
VOL_CIRCUIT_BREAKER   = _env_float("VOL_CIRCUIT_BREAKER", 0.40)
TREND_LOOKBACK        = _env_int("TREND_LOOKBACK", 12)
MIN_PRICES_FOR_REGIME = _env_int("MIN_PRICES_FOR_REGIME", 10)

# ── Signal thresholds ─────────────────────────────────────────────────────────
# v9.0.6: default lowered from 0.62 → 0.64.
# Note: if OB_IMBALANCE_THRESH is set to 0.68 in Railway, update it to 0.64.
MIN_OB_DEPTH          = _env_float("MIN_OB_DEPTH_DOLLARS", 75.0)
OB_IMBALANCE_THRESH   = _env_float("OB_IMBALANCE_THRESH", 0.64)
MOMENTUM_THRESH_PCT   = _env_float("MOMENTUM_THRESH_PCT", 0.15)
MIN_EDGE_PCT          = _env_float("MIN_EDGE_PCT", 0.06)
MIN_CONFIDENCE        = _env_int("MIN_CONFIDENCE", 60)
MIN_WIN_PROB          = _env_float("MIN_WIN_PROB", 0.60)
MIN_MINUTES_TO_EXPIRY = _env_float("MIN_MINUTES_TO_EXPIRY", 6.0)
YES_BREAKEVEN_PRICE   = _env_int("YES_BREAKEVEN_PRICE", 78)

# ── Time-of-day session quality ───────────────────────────────────────────────
SESSION_QUALITY: dict = {
    0: 20, 1: 10, 2: 10, 3: 10, 4: 15, 5: 30,
    6: 45, 7: 50, 8: 60, 9: 65, 10: 70, 11: 75,
    12: 80, 13: 90, 14: 95, 15: 95, 16: 95, 17: 90,
    18: 90, 19: 85, 20: 80, 21: 75, 22: 65, 23: 45,
}
MIN_SESSION_SCORE = _env_int("MIN_SESSION_SCORE", 60)

# ── Bayesian priors ───────────────────────────────────────────────────────────
OB_BASE_ACCURACY       = _env_float("OB_BASE_ACCURACY", 0.635)
MOMENTUM_ACCURACY_LIFT = _env_float("MOMENTUM_ACCURACY_LIFT", 0.045)
# v9.0.6: default 0.02 → 0.0. NEUTRAL BTC means no evidence against the OB
# signal — applying a penalty here was suppressing otherwise-valid setups.
# Delete NEUTRAL_ACCURACY_DRAG env var or set to 0.0 in Railway.
NEUTRAL_ACCURACY_DRAG  = _env_float("NEUTRAL_ACCURACY_DRAG", 0.0)

# ── Recovery protocol ─────────────────────────────────────────────────────────
RECOVERY_TRIGGER_PCT  = _env_float("RECOVERY_TRIGGER_PCT", 0.10)
RECOVERY_EXIT_TRADES  = _env_int("RECOVERY_EXIT_TRADES", 5)
RECOVERY_WIN_RATE_MIN = _env_float("RECOVERY_WIN_RATE_MIN", 0.60)


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
                log.info("✅ API host: %s", host)
                return
        except Exception:
            continue
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    log.warning("Host probe failed — using default.")


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL STATE
#
# RULE: open_orders, active_tickers, trade_history, session_traded_tickers,
# _processed_settlement_ids, btc_prices, btc_returns, _prev_ob are module-level
# mutable containers. They are mutated IN-PLACE everywhere. They must NEVER
# appear in any function's `global` declaration — doing so causes Python to
# treat every reference inside that function as an unbound local variable,
# producing UnboundLocalError before any in-place mutation occurs.
# ─────────────────────────────────────────────────────────────────────────────

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
_session_start_ts:     str          = ""
_session_halted:       bool         = False
_shutdown_requested:   bool         = False
_last_known_balance:   float        = 0.0

_prev_ob: dict = {}

_vol_circuit_open:  bool  = False
_vol_circuit_until: float = 0.0

_live_prior: float = OB_BASE_ACCURACY

# Laddering stake overlay — only instantiated when LADDER_ENABLED. Sized as a
# multiplier on top of the Kelly stake; respects every existing cap.
stake_ladder: Optional[StakeLadder] = StakeLadder() if LADDER_ENABLED else None


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


# ─────────────────────────────────────────────────────────────────────────────
# REGIME ENGINE
# ─────────────────────────────────────────────────────────────────────────────

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
        log.warning("VOL CIRCUIT │ spike %.3f%%", max(abs(r) for r in returns[-3:]))
        return Regime.HIGH_VOL, 0.0, realized_vol

    if realized_vol > VOLATILITY_CAP_PCT:
        log.info("Regime │ HIGH_VOL (vol=%.4f%%)", realized_vol)
        return Regime.HIGH_VOL, 0.0, realized_vol

    slope, _, r_squared = _linear_regression(prices)

    if r_squared >= R2_TREND_THRESHOLD:
        regime = Regime.TRENDING_UP if slope > 0 else Regime.TRENDING_DOWN
        log.info("Regime │ %s (R²=%.3f)", regime.value, r_squared)
        return regime, r_squared, realized_vol

    log.info("Regime │ RANGING (R²=%.3f)", r_squared)
    return Regime.RANGING, r_squared, realized_vol


# ─────────────────────────────────────────────────────────────────────────────
# VOLATILITY CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────────────────

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
            f"⚡ VOL CIRCUIT BREAKER OPENED\n"
            f"Max move: {max_move:.3f}% — trading paused 30 min."
        )
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# MOMENTUM SIGNAL
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# ORDER BOOK ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

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
        log.info("OB │ depth $%.0f < min $%.0f", total, MIN_OB_DEPTH)
        return None

    if yes_lc == 0 or no_lc == 0:
        log.info("OB │ ghost book (YES:%d NO:%d levels)", yes_lc, no_lc)
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
        log.info("OB │ no dominant side (YES:%.1f%% NO:%.1f%% thresh:%.1f%%)",
                 yr * 100, nr * 100, eff_thresh * 100)
        return None

    log.info("OB │ %s %.1f%% │ $%.0f │ thresh=%.1f%%",
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
        log.info("OB trend │ fading %.1f%%→%.1f%% — blocking",
                 prev_imb * 100, imbalance * 100)
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
    prior = _live_prior

    if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        r2_bonus   = (r_squared - R2_TREND_THRESHOLD) * 0.10
        regime_adj = 0.02 + r2_bonus
    else:
        regime_adj = 0.0

    depth_adj = 0.0
    if ob["total_depth"] > 500:
        depth_adj = min(0.02, math.log10(ob["total_depth"] / 500) * 0.02)

    vol_penalty = min(0.04, realized_vol / VOLATILITY_CAP_PCT * 0.04)

    win_prob = max(0.50, min(0.92,
        prior + momentum_adj + regime_adj + depth_adj - vol_penalty
    ))

    log.info("WinProb │ prior=%.3f mom=%.3f regime=%.3f depth=%.3f vol=-%.3f → %.3f",
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

    # v9.0.6: NEUTRAL raised from 2.0 → 8.0.
    # NEUTRAL BTC means no evidence against the OB signal.
    # A 2pt score (vs AGREE's 15) imposed a ~13pt penalty on setups where BTC
    # happens to be flat — killing 9/10 signals in the 2026-06-08 session.
    # AGREE still wins decisively (15 vs 8). CONFLICT unchanged (-20).
    momentum_map = {"AGREE": 15.0, "NEUTRAL": 8.0, "CONFLICT": -20.0}
    momentum_pts = momentum_map.get(momentum_verdict, 0.0)

    prob_pts = max(0.0, (win_prob - 0.50) / 0.42 * 15.0)

    time_pts = min(10.0, max(0.0,
        (mins_remaining - MIN_MINUTES_TO_EXPIRY) /
        max(0.1, 10.0 - MIN_MINUTES_TO_EXPIRY) * 10.0
    ))

    total = max(0.0, min(100.0,
        imb_pts + depth_pts + regime_pts + momentum_pts + prob_pts + time_pts
    ))

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

    # Laddering overlay (opt-in). Scales the Kelly stake by a performance
    # multiplier, but never past 2× the trade-size cap or the balance fraction.
    if stake_ladder is not None:
        ceiling  = min(stake_ladder.cfg.max_multiplier * TRADE_SIZE_CAP,
                       balance * MAX_BET_FRACTION)
        decision = stake_ladder.get_stake(base_bet, max_stake=ceiling)
        return decision.stake

    return base_bet


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
    log.debug("Prior → %.3f (n=%d)", _live_prior, total)


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
    return SESSION_QUALITY.get(datetime.now(timezone.utc).hour, 50)


# ─────────────────────────────────────────────────────────────────────────────
# RECOVERY PROTOCOL
# ─────────────────────────────────────────────────────────────────────────────

def update_session_state(current_balance: float) -> None:
    global session_state, recovery_trades, recovery_entry_wins, recovery_entry_losses

    if session_state == SessionState.HALTED:
        return

    loss_pct = (session_start_balance - current_balance) / max(1.0, session_start_balance)

    if session_state == SessionState.ACTIVE:
        if loss_pct > RECOVERY_TRIGGER_PCT:
            session_state         = SessionState.RECOVERY
            recovery_trades       = 0
            recovery_entry_wins   = live_wins
            recovery_entry_losses = live_losses
            log.warning("SESSION RECOVERY │ loss %.1f%% │ entry snapshot W=%d L=%d",
                        loss_pct * 100, recovery_entry_wins, recovery_entry_losses)
            tg.send_telegram_message(
                f"⚠️ SESSION RECOVERY MODE\n"
                f"Loss: {loss_pct*100:.1f}% — sizing halved."
            )

    elif session_state == SessionState.RECOVERY:
        # v9.0.9: BALANCE-BASED EXIT (primary).
        # Recovery entry is gated on loss_pct > RECOVERY_TRIGGER_PCT; the exit
        # must be reachable the same way. The trade-count exit below deadlocks
        # in calm markets — RECOVERY forces momentum==AGREE, BTC momentum is
        # NEUTRAL on most scans, so trades never accumulate and recovery never
        # clears (2026-06-15 session: 18 strong OB signals rejected, counter
        # frozen at rec+2, 0 trades for hours). If the drawdown that triggered
        # recovery has healed back to at/below the trigger, return to ACTIVE
        # regardless of trade count. No edge/Kelly/loss-cap parameter loosened.
        if loss_pct <= RECOVERY_TRIGGER_PCT:
            session_state = SessionState.ACTIVE
            log.info("RECOVERY EXITED │ drawdown healed (loss %.1f%% ≤ trigger %.1f%%)",
                     loss_pct * 100, RECOVERY_TRIGGER_PCT * 100)
            tg.send_telegram_message(
                f"✅ Recovery exited — drawdown healed ({loss_pct*100:.1f}%)"
            )
            return

        # Trade-count exit (faster path when AGREE trades do settle).
        trades_since_entry = (
            (live_wins + live_losses) - (recovery_entry_wins + recovery_entry_losses)
        )
        if trades_since_entry >= RECOVERY_EXIT_TRADES:
            wins_since = live_wins - recovery_entry_wins
            wr = wins_since / trades_since_entry if trades_since_entry > 0 else 0.0
            if wr >= RECOVERY_WIN_RATE_MIN:
                session_state = SessionState.ACTIVE
                log.info("RECOVERY EXITED │ WR=%.1f%% (%d/%d since entry)",
                         wr * 100, wins_since, trades_since_entry)
                tg.send_telegram_message(
                    f"✅ Recovery exited — WR {wr*100:.0f}% ({wins_since}/{trades_since_entry})"
                )


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO / BALANCE
# ─────────────────────────────────────────────────────────────────────────────

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

    v9.0.7: REWRITTEN against the actual KXBTC15M settlement schema observed in
    the 2026-06-08 live logs. The real record has NO realized_pnl/profit field.
    Its keys are:
        event_ticker, fee_cost, market_result, no_count_fp,
        no_total_cost_dollars, revenue, settled_time, ticker, value,
        yes_count_fp, yes_total_cost_dollars

    Reconstruction:
        pnl = (revenue / 100) - yes_total_cost_dollars - no_total_cost_dollars - fee_cost

    `revenue` is returned in CENTS by the Kalshi settlements endpoint (same unit
    as the balance endpoint which requires /100). The *_dollars cost fields and
    fee_cost are returned in dollars. Dividing revenue by 100 before subtracting
    the dollar-denominated costs prevents the ~100× profit inflation bug.

    Direct PnL fields are still tried first in case Kalshi adds them later.
    """
    # 1) Direct PnL fields (future-proofing; absent in current schema)
    for k in ("realized_pnl_dollars", "settlement_pnl_dollars", "pnl_dollars"):
        v = _coerce_float(rec.get(k))
        if v is not None:
            return v
    for k in ("realized_pnl_cents", "realized_pnl", "settlement_pnl", "pnl"):
        v = _coerce_float(rec.get(k))
        if v is not None:
            # legacy integer-cent fields divided by 100; dollar fields handled above
            return v / 100.0 if k.endswith("_cents") else v

    # 2) Real KXBTC15M reconstruction: revenue - total cost - fees
    # Kalshi settlement API returns revenue in cents (same as balance endpoint);
    # cost/fee fields named *_dollars are already in dollars.
    revenue = _coerce_float(rec.get("revenue"))
    if revenue is not None:
        revenue /= 100.0
        yes_cost = _coerce_float(rec.get("yes_total_cost_dollars")) or 0.0
        no_cost  = _coerce_float(rec.get("no_total_cost_dollars"))  or 0.0
        fee      = _coerce_float(rec.get("fee_cost")) or 0.0
        total_cost = yes_cost + no_cost
        if total_cost > 0:
            return round(revenue - total_cost - fee, 4)
        # No cost recorded on either side. If revenue is also 0 this is an
        # unfilled/expired maker order (no position taken) → return 0.0 so the
        # caller's NO-FILL branch handles it instead of counting a phantom loss.
        if revenue == 0.0:
            return 0.0
        # Cost missing but revenue present — fall back to the matched trade cost.
        if trade_cost is not None and trade_cost > 0:
            return round(revenue - trade_cost - fee, 4)
        # Revenue present, no cost anywhere: treat as win for W/L counting only.
        return 1.0

    # 3) market_result as a final win/loss signal when no economics present
    mr = str(rec.get("market_result", "")).lower()
    if mr in ("yes", "no"):
        # Without held-side info here we can only flag presence; caller matches side.
        # Return None so the caller logs missing economics rather than miscounting.
        return None

    return None


def _extract_ticker(rec: dict) -> str:
    for k in ("market_ticker", "ticker", "event_ticker"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


def _is_post_boot(rec: dict) -> bool:
    """
    True if a settlement record was created at/after this process's boot time.

    v9.0.8: the /portfolio/settlements endpoint ignores created_since and always
    returns the account-wide last 100 settlements. The unmatched-settlement
    branch in resolve_open_orders() counts these toward live_wins/live_losses so
    RECOVERY can exit on pre-restart trades — but with no time gate it ingests
    days of account history on every boot. In the 2026-06-11 LIVE session this
    seeded WR=28/70 (Wilson LB 30.9%), permanently failing performance_guard()'s
    50% floor and freezing the bot: 1307 PERF GUARD warnings, zero trades.

    Gate: only count a settlement whose timestamp is >= _session_start_ts. A
    pre-restart trade still in flight settles AFTER boot, so it is preserved;
    trades settled entirely before this boot (account history) are excluded.
    Records with a missing/unparseable timestamp are treated as NOT post-boot
    (conservative — never back-count ambiguous history).
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

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
                # Pre-restart trade: count toward W/L so RECOVERY can exit.
                # v9.0.8: ONLY if settled at/after boot. The settlements endpoint
                # returns account-wide history (created_since is ignored), so an
                # ungated count here ingests days of stale W/L and deadlocks the
                # Wilson performance guard. In-flight pre-restart trades settle
                # after boot and are still counted; account history is skipped.
                if not _is_post_boot(rec):
                    continue
                pnl_d = _extract_realized_dollars(rec)
                if pnl_d is not None and pnl_d != 0.0:
                    if pnl_d > 0:
                        live_wins += 1
                        log.info("UNMATCHED WIN │ %s │ $%.2f (pre-restart)",
                                 rec_ticker[-15:], pnl_d)
                    else:
                        live_losses += 1
                        consecutive_losses += 1
                        if consecutive_losses >= MAX_CONSEC_LOSSES:
                            streak_pause_until = time.time() + STREAK_PAUSE_SECS
                        log.info("UNMATCHED LOSS │ %s │ $%.2f (pre-restart)",
                                 rec_ticker[-15:], pnl_d)
                    ladder_record(pnl_d > 0, pnl_d)
                    update_live_prior()
                continue

            trade = open_orders.pop(matched_oid)
            active_tickers.discard(rec_ticker)
            active_tickers.discard(trade.get("ticker", ""))

            # v9.0.6: pass trade cost to _extract_realized_dollars so it can
            # reconstruct PnL from revenue fields when direct PnL fields absent.
            trade_cost = trade.get("cost")
            pnl_d = _extract_realized_dollars(rec, trade_cost=trade_cost)
            if pnl_d is None:
                log.warning("RESOLVE │ %s — no pnl field. Keys: %s",
                            rec_ticker[-15:], list(rec.keys()))
                continue

            if pnl_d == 0.0:
                log.info("NO-FILL │ %s", rec_ticker[-15:])
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
            log.info("✅ SETTLED │ %s │ %s │ $%.2f │ WR=%d/%d │ LB=%.1f%%",
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
                    log.info("Order canceled │ %s", oid[:12])
        except Exception:
            pass

        now   = time.time()
        stale = [oid for oid, t in open_orders.items()
                 if now - t.get("placed_at", now) > 1200]
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
            log.info("Stale cancel (paper) │ %s │ $%.2f", ticker[-15:], cost)
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
    elapsed = time.time() - last_trade_ts
    if elapsed < 60:
        log.info("Cooldown │ %.0fs remaining", 60 - elapsed)
        return False
    return True


def session_quality_check() -> bool:
    score = get_session_score()
    utc_h = datetime.now(timezone.utc).hour
    if score < MIN_SESSION_SCORE:
        log.info("Session quality │ UTC%d score=%d < %d", utc_h, score, MIN_SESSION_SCORE)
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

    body: dict = {
        "ticker":          ticker,
        "client_order_id": client_id,
        "type":            "limit",
        "action":          "buy",
        "side":            direction.lower(),
        "count":           count,
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
    tg.send_telegram_message(
        f"🤖 Johnny5 {BOT_VERSION} STARTED\n"
        f"{mode} │ State: {session_state.value}\n"
        f"Balance: ${balance:.2f}\n"
        f"DailyLoss≤${MAX_DAILY_LOSS:.0f} | Floor=${MIN_BALANCE_FLOOR:.0f}\n"
        f"MinConf={MIN_CONFIDENCE} | MinWinP={MIN_WIN_PROB*100:.0f}% | R²≥{R2_TREND_THRESHOLD}\n"
        f"OBDepth≥${MIN_OB_DEPTH:.0f} | OBImb≥{OB_IMBALANCE_THRESH*100:.0f}%\n"
        f"SessionScore≥{MIN_SESSION_SCORE} | Kelly={KELLY_FRACTION}"
    )


def telegram_halt(reason: str, balance: float) -> None:
    tg.send_telegram_message(
        f"⛔ HALTED (PERMANENT)\nReason: {reason}\nBalance: ${balance:.2f}"
    )


def telegram_daily_summary(balance: float, pnl: float, wins: int, losses: int) -> None:
    total  = wins + losses
    wr     = wins / total * 100 if total > 0 else 0.0
    emoji  = "📈" if pnl >= 0 else "📉"
    ci_str = ""
    if total >= 10:
        wlb    = wilson_lower_bound(wins, total)
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
        log.info("Concurrent │ %d open", len(open_orders))
        return

    mins = minutes_to_expiry(market)
    if mins < MIN_MINUTES_TO_EXPIRY:
        log.info("Expiry imminent │ %.1f min", mins)
        last_signal_desc = "expiry imminent"
        return

    if check_vol_circuit():
        last_signal_desc = "vol circuit open"
        return

    regime, r_squared, realized_vol = compute_regime()
    if regime in (Regime.UNKNOWN, Regime.RANGING, Regime.HIGH_VOL):
        log.info("Regime │ %s — no trade", regime.value)
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

    momentum_verdict, momentum_adj = compute_momentum(ob_dir)
    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB=%s", ob_dir)
        last_signal_desc = f"CONFLICT OB={ob_dir}"
        return

    if session_state == SessionState.RECOVERY and momentum_verdict != "AGREE":
        log.info("Recovery │ AGREE required, got %s", momentum_verdict)
        last_signal_desc = "recovery: AGREE required"
        return

    win_prob = bayesian_win_prob(ob, momentum_verdict, momentum_adj,
                                  regime, r_squared, realized_vol)
    if win_prob < MIN_WIN_PROB:
        log.info("WinProb │ %.3f < %.3f", win_prob, MIN_WIN_PROB)
        last_signal_desc = f"win_prob {win_prob:.2f} < {MIN_WIN_PROB:.2f}"
        return

    session_score = get_session_score()
    conf = compute_confidence(ob, regime, r_squared, momentum_verdict,
                               win_prob, mins, session_score)
    if conf < MIN_CONFIDENCE:
        log.info("Confidence │ %.0f < %d", conf, MIN_CONFIDENCE)
        last_signal_desc = f"conf {conf:.0f} < {MIN_CONFIDENCE}"
        return

    if ob_dir == "YES":
        if yes_mid > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ YES %dc > breakeven", yes_mid)
            return
        direction      = "YES"
        contract_price = yes_mid
    else:
        no_price = 100 - yes_mid
        if no_price > YES_BREAKEVEN_PRICE:
            log.info("Price guard │ NO %dc > breakeven", no_price)
            return
        direction      = "NO"
        contract_price = no_price

    if not (25 <= contract_price <= 75):
        log.info("Bias filter │ %dc outside 25-75", contract_price)
        return

    edge = calc_edge(win_prob, contract_price)
    if edge < MIN_EDGE_PCT:
        log.info("Edge │ %.3f < min %.3f", edge, MIN_EDGE_PCT)
        last_signal_desc = f"edge {edge:.3f} < {MIN_EDGE_PCT:.3f}"
        return

    bet = kelly_bet(win_prob, contract_price, balance)
    if bet < 0.25:
        log.info("Kelly │ $%.2f too small", bet)
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
        log.info("Limit drift │ %dc too far", limit_price)
        return

    total   = live_wins + live_losses
    wlb_str = (f" WLB={wilson_lower_bound(live_wins, total)*100:.1f}%"
               if total >= 10 else " WLB=n/a")

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
    # ── CRITICAL GLOBAL DECLARATION RULE ─────────────────────────────────────
    # The following module-level mutable containers must NEVER appear here:
    #   open_orders, active_tickers, trade_history, session_traded_tickers,
    #   _processed_settlement_ids, btc_prices, btc_returns, _prev_ob
    #
    # These are mutated IN-PLACE (dict/set/deque methods). Declaring them in
    # a global statement causes Python to mark every reference inside main()
    # as a local variable. The set comprehension that reads active_tickers
    # then raises UnboundLocalError before any local assignment has occurred.
    # ─────────────────────────────────────────────────────────────────────────
    global session_start_balance, session_stop_threshold, daily_pnl
    global paper_balance, paper_daily_pnl, last_trade_ts, last_daily_summary_ts
    global consecutive_losses, last_signal_desc, last_heartbeat_ts, running_pnl
    global live_wins, live_losses, streak_pause_until
    global _last_known_balance, _shutdown_requested, _session_start_ts
    global _session_halted, session_state, recovery_trades
    global recovery_entry_wins, recovery_entry_losses

    init_base_url()

    paper_balance         = float(os.environ.get("PAPER_BALANCE", "25.0"))
    _session_start_ts     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session_halted       = False
    session_state         = SessionState.ACTIVE
    recovery_trades       = 0
    recovery_entry_wins   = 0
    recovery_entry_losses = 0

    # In-place resets — no global declaration needed
    session_traded_tickers.clear()
    _processed_settlement_ids.clear()

    log.info("━" * 70)
    log.info("  JOHNNY5 %s │ %s", BOT_VERSION, "PAPER 🟡" if DEMO_MODE else "LIVE 🔴")
    log.info("  Start: %s", _session_start_ts)
    log.info("  Regime R²≥%.2f | VolCap=%.3f%% | Circuit=%.2f%%",
             R2_TREND_THRESHOLD, VOLATILITY_CAP_PCT, VOL_CIRCUIT_BREAKER)
    log.info("  OB depth≥$%.0f imb≥%.0f%% | WinP≥%.0f%% Edge≥%.0f%%",
             MIN_OB_DEPTH, OB_IMBALANCE_THRESH * 100, MIN_WIN_PROB * 100, MIN_EDGE_PCT * 100)
    log.info("  Kelly=%.2f cap=%.0f%% | SessionScore≥%d",
             KELLY_FRACTION, MAX_BET_FRACTION * 100, MIN_SESSION_SCORE)
    log.info("━" * 70)

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
                    log.info("📋 PAPER │ $%.2f │ PnL=$%+.2f │ WR=%.1f%% │ Prior=%.3f │ %s",
                             paper_balance, paper_daily_pnl, wr * 100,
                             _live_prior, session_state.value)
                else:
                    live_bal  = get_live_balance()
                    daily_pnl = live_bal - session_start_balance
                    wlb       = wilson_lower_bound(live_wins, live_wins + live_losses)
                    trades_since = (live_wins + live_losses) - (recovery_entry_wins + recovery_entry_losses)
                    log.info(
                        "Portfolio │ $%.2f │ PnL=$%+.2f │ WR=%d/%d LB=%.1f%% │ Prior=%.3f │ %s"
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
    tg.send_telegram_message(f"🛑 Johnny5 {BOT_VERSION} stopped. Final: ${final:.2f}")


if __name__ == "__main__":
    main()
