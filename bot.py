"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v8.0.0  —  Production Build                          ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STRATEGY — Regime-aware quantitative trading                               ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Signal 1 │ Near-money OB pressure (±10c of mid, adaptive threshold)       ║
║  Signal 2 │ BTC momentum confirmation via Kraken spot price feed            ║
║           │ If BTC direction AGREES with OB → boost win_prob               ║
║           │ If BTC direction CONFLICTS with OB → SKIP                      ║
║  Signal 3 │ Price breakeven guard (only buy contracts ≤78c)                 ║
║  Signal 4 │ Favourite-longshot bias filter (25-75c contract range)          ║
║  Regime   │ Only trade when BTC is TRENDING (R² > 0.50 on 5min window)     ║
║                                                                              ║
║  v8.0.0 CHANGES (from v7.1.0):                                             ║
║  CRITICAL FIX: adaptive_ob_threshold() was DEAD CODE — never called in     ║
║    calc_ob_quality(). The 70% threshold was hardcoded, making trades        ║
║    impossible on books where near-money imbalance sits at 50-66%.           ║
║  • Wired adaptive_ob_threshold INTO calc_ob_quality                        ║
║  • QUANT ob_thresh: 0.70 → 0.62 (adaptive: thick=$50+ uses 0.58)          ║
║  • R² threshold: 0.65 → 0.50 (BTC trends cleanly at 0.50+ often)          ║
║  • MINIMUM_CONFIDENCE: 65 → 50                                            ║
║  • REQUIRE_AGREE_MOMENTUM: default false                                   ║
║  • ALLOW_NEUTRAL_IN_TRENDING: default true                                 ║
║  • YES_BREAKEVEN_PRICE: 67 → 78                                           ║
║  • Price range: 35-65 → 25-75                                             ║
║  • Cooldown: 120s → 60s                                                   ║
║  • OB trend: removed direction-flip block (was blocking valid signals)     ║
║  • OB trend fade threshold: 5% → 10%                                      ║
║  • Confidence scoring: regime pts available at R²>0.50                     ║
║                                                                              ║
║  LIVE SAFETY                                                                 ║
║  • Balance floor: $5.00 — hard stop, no trades below this                  ║
║  • Daily loss cap: $20 — halt for the day if hit                           ║
║  • Session stop: halt if balance drops below 50% of start                  ║
║  • Performance guard: Wilson CI lower bound must be ≥ 50%                  ║
║  • Streak pause: 30 minutes after 2 consecutive losses                     ║
║  • Regime filter: TRENDING only. No trades in RANGING/HIGH_VOL             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

BOT_VERSION = "8.0.0"

import base64
import logging
import os
import signal
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
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
        "description":  "Regime-aware quant. Adaptive OB threshold. Kelly 35%.",
        "min_price":    25,
        "max_price":    75,
        "kelly_frac":   float(os.environ.get("KELLY_FRACTION", "0.35")),
        "ob_thresh":    float(os.environ.get("OB_THRESH", "0.62")),
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
YES_BREAKEVEN_PRICE  = int(os.environ.get("YES_BREAKEVEN_PRICE", "78"))

_mode_raw = os.environ.get("TRADER_MODE", "quant").lower().strip()
try:
    ACTIVE_MODE = TraderMode(_mode_raw)
except ValueError:
    log.warning("Unknown TRADER_MODE '%s' — defaulting to QUANT.", _mode_raw)
    ACTIVE_MODE = TraderMode.QUANT

PROFILE  = PROFILES[ACTIVE_MODE]
BASE_URL = ""

# ── Quantitative safeguard parameters ────────────────────────────────────────
MINIMUM_CONFIDENCE    = int(os.environ.get("MINIMUM_CONFIDENCE", "50"))
MIN_OB_DEPTH_DOLLARS  = float(os.environ.get("MIN_OB_DEPTH_DOLLARS", "10.0"))
MIN_MINUTES_TO_EXPIRY = float(os.environ.get("MIN_MINUTES_TO_EXPIRY", "3.0"))
REQUIRE_AGREE_MOMENTUM = os.environ.get("REQUIRE_AGREE_MOMENTUM", "false").lower() == "true"
MAX_BET_FRACTION      = float(os.environ.get("MAX_BET_FRACTION", "0.10"))
MIN_SAMPLE_TRADES     = int(os.environ.get("MIN_SAMPLE_TRADES", "20"))

# v8.0.0: R² threshold for TRENDING regime (was hardcoded 0.65, now configurable)
R_SQUARED_THRESHOLD   = float(os.environ.get("R_SQUARED_THRESHOLD", "0.50"))

_low_liq_raw = os.environ.get("LOW_LIQ_HOURS_UTC", "0,1,2,3,4")
LOW_LIQ_HOURS_UTC: set = {int(h.strip()) for h in _low_liq_raw.split(",") if h.strip()}

# ── v5.3.0 compat: named constants used by tests ─────────────────────────────
LOW_LIQ_START_UTC = int(os.environ.get("LOW_LIQ_START_UTC", "0"))
LOW_LIQ_END_UTC   = int(os.environ.get("LOW_LIQ_END_UTC", "5"))
MAX_CONCURRENT_POS = int(os.environ.get("MAX_CONCURRENT_POS", "2"))
STALE_ORDER_TIMEOUT = int(os.environ.get("STALE_ORDER_TIMEOUT", "300"))

# ── Momentum tuning parameters ───────────────────────────────────────────────
MOMENTUM_AGREE_THRESHOLD = float(os.environ.get("MOMENTUM_AGREE_THRESHOLD", "0.15"))
ALLOW_NEUTRAL_IN_TRENDING = os.environ.get("ALLOW_NEUTRAL_IN_TRENDING", "true").lower() == "true"
NEUTRAL_R2_FLOOR = float(os.environ.get("NEUTRAL_R2_FLOOR", "0.55"))


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP CONFIG DRIFT VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _validate_config_against_doctrine() -> None:
    """Log warnings if runtime config has drifted from v8.0.0 defaults."""
    drift_found = False

    if PROFILE["ob_thresh"] != 0.62:
        log.warning("CONFIG DRIFT │ ob_thresh=%.2f (v8 default: 0.62).", PROFILE["ob_thresh"])
        drift_found = True

    if MIN_OB_DEPTH_DOLLARS != 10.0:
        log.warning("CONFIG DRIFT │ MIN_OB_DEPTH_DOLLARS=%.1f (v8 default: 10.0).", MIN_OB_DEPTH_DOLLARS)
        drift_found = True

    if MINIMUM_CONFIDENCE != 50:
        log.warning("CONFIG DRIFT │ MINIMUM_CONFIDENCE=%d (v8 default: 50).", MINIMUM_CONFIDENCE)
        drift_found = True

    if R_SQUARED_THRESHOLD != 0.50:
        log.warning("CONFIG DRIFT │ R_SQUARED_THRESHOLD=%.2f (v8 default: 0.50).", R_SQUARED_THRESHOLD)
        drift_found = True

    if REQUIRE_AGREE_MOMENTUM:
        log.warning("CONFIG DRIFT │ REQUIRE_AGREE_MOMENTUM=true (v8 default: false).")
        drift_found = True

    if ALLOW_NEUTRAL_IN_TRENDING:
        log.info("CONFIG NOTE │ ALLOW_NEUTRAL_IN_TRENDING=true — NEUTRAL accepted when TRENDING R²≥%.2f.", NEUTRAL_R2_FLOOR)

    if not drift_found:
        log.info("Config validation │ All values match v8.0.0 defaults. ✅")


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


# ─────────────────────────────────────────────────────────────────────────────
# HTTP SESSION
# ─────────────────────────────────────────────────────────────────────────────
_http_session: requests.Session = requests.Session()


def _get(path: str, params: Optional[dict] = None) -> dict:
    r = _http_session.get(BASE_URL + path, params=params,
                          headers=_auth_headers("GET", path), timeout=12)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = _http_session.post(BASE_URL + path, json=body,
                           headers=_auth_headers("POST", path), timeout=12)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    r = _http_session.delete(BASE_URL + path,
                             headers=_auth_headers("DELETE", path), timeout=12)
    r.raise_for_status()
    return r.json()


def init_base_url() -> None:
    global BASE_URL
    for host in ["https://api.elections.kalshi.com", "https://trading-api.kalshi.com"]:
        try:
            r = _http_session.get(host + "/trade-api/v2/exchange/status", timeout=6)
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

streak_pause_until:    float = 0.0
live_wins:             int   = 0
live_losses:           int   = 0

_last_known_balance:   float = 0.0
_prev_ob: dict = {}
_shutdown_requested: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# SIGTERM HANDLER
# ─────────────────────────────────────────────────────────────────────────────

def _sigterm_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.info("SIGTERM received — initiating graceful shutdown.")


signal.signal(signal.SIGTERM, _sigterm_handler)


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────

def telegram_boot(balance: float) -> None:
    mode = "📋 PAPER" if DEMO_MODE else "🔴 LIVE"
    tg.send_telegram_message(
        f"🤖 Johnny5 {BOT_VERSION} STARTED\n"
        f"Mode: {mode} | Archetype: {ACTIVE_MODE.value.upper()}\n"
        f"Balance: ${balance:.2f} | Max bet: ${TRADE_SIZE_DOLLARS:.2f}\n"
        f"Daily loss cap: ${MAX_DAILY_LOSS:.2f} | Floor: ${MIN_BALANCE_FLOOR:.2f}\n"
        f"Conf≥{MINIMUM_CONFIDENCE} | OB≥{PROFILE['ob_thresh']*100:.0f}% | "
        f"Depth≥${MIN_OB_DEPTH_DOLLARS:.0f} | R²≥{R_SQUARED_THRESHOLD}\n"
        f"MomThresh: {MOMENTUM_AGREE_THRESHOLD:.2f}% | "
        f"NeutralOK: {ALLOW_NEUTRAL_IN_TRENDING} | AgreeReq: {REQUIRE_AGREE_MOMENTUM}"
    )


def telegram_halt(reason: str, balance: float) -> None:
    tg.send_telegram_message(f"⚠️ Johnny5 HALTED\nReason: {reason}\nBalance: ${balance:.2f}")


def telegram_daily_summary(balance: float, pnl: float, wins: int, losses: int) -> None:
    total = wins + losses
    wr    = wins / total * 100 if total > 0 else 0.0
    emoji = "📈" if pnl >= 0 else "📉"
    ci_str = ""
    if total >= 10:
        wlb = wilson_lower_bound(wins, total)
        ci_str = f" (WilsonLB: {wlb*100:.0f}%)"
    tg.send_telegram_message(
        f"{emoji} Daily Summary\n"
        f"P&L: ${pnl:+.2f} | Balance: ${balance:.2f}\n"
        f"Trades: {total} | WR: {wr:.0f}%{ci_str} ({wins}W/{losses}L)"
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
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=5)
        if r.status_code == 200:
            data = r.json()
            result = data.get("result", {})
            if result:
                key = next(iter(result))
                return float(result[key]["c"][0])
    except Exception:
        pass
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        if r.status_code == 200:
            return float(r.json()["data"]["amount"])
    except Exception:
        pass
    log.debug("BTC price feed unavailable — backing off 5 min")
    _btc_feed_backoff_until = time.time() + 300
    return None


def btc_momentum_signal(ob_direction: str) -> tuple[str, float]:
    """
    Compare OB signal direction to recent BTC price momentum.
    Lookback: prices[-4] to prices[-1] = ~90 seconds at 30s poll.
    """
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

    if abs(move_pct) < MOMENTUM_AGREE_THRESHOLD:
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
# REGIME DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def compute_btc_regime() -> tuple[str, float]:
    """
    Classify BTC price regime: TRENDING / RANGING / HIGH_VOL / UNKNOWN.
    v8.0.0: R² threshold configurable via R_SQUARED_THRESHOLD (default 0.50).
    """
    if len(btc_prices) < 8:
        return "UNKNOWN", 0.0

    prices = list(btc_prices)[-10:]
    n = len(prices)

    xs     = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(prices) / n
    ss_xx  = sum((x - mean_x) ** 2 for x in xs)
    ss_xy  = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, prices))
    ss_yy  = sum((y - mean_y) ** 2 for y in prices)

    if ss_xx == 0 or ss_yy == 0:
        return "UNKNOWN", 0.0

    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)

    returns = [abs((prices[i] - prices[i - 1]) / prices[i - 1])
               for i in range(1, n) if prices[i - 1] > 0]
    mean_abs_return = sum(returns) / len(returns) if returns else 0.0

    if mean_abs_return > 0.0015:
        log.info("Regime │ HIGH_VOL (mean_abs_ret=%.4f%%, R²=%.2f)",
                 mean_abs_return * 100, r_squared)
        return "HIGH_VOL", r_squared

    if r_squared > R_SQUARED_THRESHOLD:
        direction = "UP" if ss_xy > 0 else "DOWN"
        log.info("Regime │ TRENDING %s (R²=%.2f, mean_ret=%.4f%%)",
                 direction, r_squared, mean_abs_return * 100)
        return "TRENDING", r_squared

    log.info("Regime │ RANGING (R²=%.2f, mean_abs_ret=%.4f%%)",
             r_squared, mean_abs_return * 100)
    return "RANGING", r_squared


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def minutes_to_expiry(market: dict) -> float:
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
# CONFIDENCE SCORING
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

    v8.0.0: regime points scale with R² threshold. At R²=0.50, TRENDING
    gives 20 base pts (was 25 at 0.65 threshold). This ensures the lower
    R² threshold doesn't inflate confidence on marginal trends.
    """
    imbalance = ob_quality.get("imbalance", 0.5)
    depth     = ob_quality.get("near_money_depth", 0.0)
    thresh    = ob_quality.get("effective_thresh", PROFILE["ob_thresh"])

    # OB imbalance: 0-30 pts
    imb_pts = max(0.0, (imbalance - thresh) / (1.0 - thresh)) * 30.0

    # OB depth: 0-20 pts
    if depth <= 50.0:
        depth_pts = min(10.0, max(0.0, depth / 5.0))
    else:
        depth_pts = min(20.0, 10.0 + (depth - 50.0) / 15.0)

    # Regime: -10 to 30 pts
    regime_base = {"TRENDING": 20.0, "UNKNOWN": 5.0, "RANGING": 0.0, "HIGH_VOL": -10.0}
    regime_pts  = regime_base.get(regime, 0.0)
    if regime == "TRENDING":
        # Bonus for high R² — up to 10 pts
        regime_pts += min(10.0, (r_squared - R_SQUARED_THRESHOLD) / (1.0 - R_SQUARED_THRESHOLD) * 10.0)

    # Momentum: 0-15 pts
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


def wilson_confidence(wins: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    if total == 0:
        return 0.0, 0.0, 0.0
    p      = wins / total
    denom  = 1.0 + z ** 2 / total
    center = (p + z ** 2 / (2.0 * total)) / denom
    spread = (z * (p * (1.0 - p) / total + z ** 2 / (4.0 * total ** 2)) ** 0.5) / denom
    lo = max(0.0, center - spread) * 100
    hi = min(1.0, center + spread) * 100
    return round(p * 100, 1), round(lo, 1), round(hi, 1)


def performance_guard() -> bool:
    total = live_wins + live_losses
    if total < MIN_SAMPLE_TRADES:
        return True
    wlb = wilson_lower_bound(live_wins, total)
    if wlb < 0.50:
        log.warning(
            "PERFORMANCE GUARD │ Wilson CI lower bound %.1f%% < 50%% "
            "(wins=%d / total=%d). Halting new entries.",
            wlb * 100, live_wins, total,
        )
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO
# ─────────────────────────────────────────────────────────────────────────────

def get_live_balance() -> float:
    global _last_known_balance
    try:
        data = _get("/portfolio/balance")
        bal = (data.get("balance", 0) or 0) / 100.0
        _last_known_balance = bal
        return bal
    except Exception as e:
        log.warning("Balance fetch failed: %s — using cached $%.2f", e, _last_known_balance)
        return _last_known_balance


def resolve_open_orders() -> None:
    global active_tickers, paper_balance, paper_daily_pnl, consecutive_losses, running_pnl
    global live_wins, live_losses, streak_pause_until

    if not open_orders:
        return

    STREAK_THRESHOLD = int(os.environ.get("MAX_CONSEC_LOSSES", "2"))
    STREAK_PAUSE_SEC = int(os.environ.get("STREAK_PAUSE_SECS", "1800"))

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
                side  = trade.get("side", "YES").upper()

                entry_price = trade.get("btc_entry_price", 0)
                current_btc = fetch_btc_price()
                if entry_price > 0 and current_btc and current_btc > 1000:
                    btc_up = current_btc > entry_price
                    won = btc_up if side == "YES" else not btc_up
                    sim_method = "btc"
                else:
                    import random
                    won = random.random() < 0.685
                    sim_method = "rng"

                if won:
                    paper_balance   += count
                    trade_pnl        = round(count - cost, 2)
                    paper_daily_pnl += trade_pnl
                else:
                    trade_pnl        = round(-cost, 2)
                    paper_daily_pnl += trade_pnl

                result = "win" if won else "loss"
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = result
                        t["pnl"]    = round(trade_pnl, 4)
                        break
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
                        log.warning("Streak pause │ %d losses — pausing %.0f min.",
                                    consecutive_losses, STREAK_PAUSE_SEC / 60)
                    tg.send_loss_notification(
                        loss=abs(trade_pnl),
                        balance=paper_balance,
                        daily_pnl=paper_daily_pnl,
                        ticker=ticker,
                        direction=trade.get("side", "?"),
                        streak=consecutive_losses,
                    )
                log.info(
                    "📋 PAPER SETTLED │ %s │ %s │ %s │ sim=%s │ bal=$%.2f │ streak=%d │ WR=%d/%d",
                    ticker[-15:], trade.get("side", "?"), result.upper(),
                    sim_method, paper_balance, consecutive_losses,
                    live_wins, live_wins + live_losses,
                )
        return

    # ── Live resolution ────────────────────────────────────────────────────
    try:
        since_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pos_data = _get("/portfolio/positions", {
            "limit": 100,
            "settlement_status": "settled",
            "created_since": since_ts,
        })
        settled_positions = pos_data.get("market_positions", [])

        log.info("RESOLVE │ %d settled positions, %d open orders tracked",
                 len(settled_positions), len(open_orders))

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

                if realized == 0:
                    log.info("📋 NO-FILL │ %s │ realized_pnl=0 → expired unfilled.", ticker[-15:])
                    for t in trade_history:
                        if t.get("order_id") == matched_oid:
                            t["result"] = "unfilled"
                            t["pnl"]    = 0.0
                            break
                    continue

                won    = realized_dollars > 0
                pnl    = round(realized_dollars, 2)
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
                log.info("✅ SETTLED │ %s │ %s │ pnl=$%.2f │ WR=%d/%d │ WilsonLB=%.1f%%",
                         ticker[-15:], result.upper(), pnl,
                         live_wins, live_wins + live_losses, wlb * 100)
                if won:
                    consecutive_losses = 0
                    live_wins += 1
                    tg.send_win_notification(
                        profit=pnl, balance=balance, daily_pnl=live_daily_pnl,
                        ticker=ticker, direction=trade.get("side", "?"),
                    )
                else:
                    consecutive_losses += 1
                    live_losses += 1
                    if consecutive_losses >= STREAK_THRESHOLD:
                        streak_pause_until = time.time() + STREAK_PAUSE_SEC
                        log.warning("Streak pause │ %d losses — pausing %.0f min.",
                                    consecutive_losses, STREAK_PAUSE_SEC / 60)
                    tg.send_loss_notification(
                        loss=abs(pnl), balance=balance, daily_pnl=live_daily_pnl,
                        ticker=ticker, direction=trade.get("side", "?"),
                        streak=consecutive_losses,
                    )

        # Clean up canceled orders
        canceled_data = _get("/portfolio/orders", {"status": "canceled", "limit": 100})
        canceled_ids  = {o["order_id"] for o in canceled_data.get("orders", [])}
        for oid in list(open_orders.keys()):
            trade  = open_orders[oid]
            ticker = trade.get("ticker", "")
            if oid in canceled_ids:
                open_orders.pop(oid)
                active_tickers.discard(ticker)
                log.info("Order %s canceled │ %s", oid[:12], ticker[-15:])

        # Time-based cleanup: >20 min old
        now = time.time()
        stale = [oid for oid, t in open_orders.items()
                 if now - t.get("placed_at", now) > 1200]
        for oid in stale:
            trade = open_orders.pop(oid)
            active_tickers.discard(trade.get("ticker", ""))
            log.info("Stale order purged │ >20min old")

    except Exception as e:
        log.warning("Order resolution error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# STALE ORDER CANCELLATION
# ─────────────────────────────────────────────────────────────────────────────

def cancel_stale_orders() -> None:
    global paper_balance, paper_daily_pnl
    now = time.time()
    for oid in list(open_orders.keys()):
        trade = open_orders[oid]
        age = now - trade.get("placed_at", now)
        if age < STALE_ORDER_TIMEOUT:
            continue

        ticker = trade.get("ticker", "")
        cost   = trade.get("cost", 0.0)

        if DEMO_MODE:
            open_orders.pop(oid)
            active_tickers.discard(ticker)
            paper_balance   += cost
            paper_daily_pnl += cost
            for t in trade_history:
                if t.get("order_id") == oid:
                    t["result"] = "canceled"
                    t["pnl"]    = 0.0
                    break
            log.info("Stale cancel (paper) │ %s │ refund $%.2f", ticker[-15:], cost)
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
                log.info("Stale cancel (live) │ %s │ order %s", ticker[-15:], oid[:12])
            except Exception as e:
                log.warning("Failed to cancel stale order %s: %s", oid[:12], e)


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
# ORDER BOOK ANALYSIS
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
    """
    Adjust OB imbalance threshold based on book depth.
    Thin books (<$15) → require 70%+ (single orders can fake signal)
    Medium books ($15-50) → use profile default
    Thick books ($50+) → allow 58%+ (deep books = real consensus)
    """
    default = PROFILE["ob_thresh"]
    if total_depth < 15.0:
        return max(default, 0.70)
    elif total_depth >= 50.0:
        return min(default, 0.58)
    else:
        return default


def ob_trend_check(ticker: str, current_imb: float, current_dir: str) -> bool:
    """
    v8.0.0: Simplified. Only blocks on severe fading (>10% drop).
    Direction flips are now ALLOWED — the OB direction IS the signal;
    if it flipped, the new direction is the new signal.
    """
    now = time.time()
    prev = _prev_ob.get(ticker)

    if prev is None:
        _prev_ob[ticker] = (current_imb, current_dir, now)
        return True

    prev_imb, prev_dir, prev_time = prev
    _prev_ob[ticker] = (current_imb, current_dir, now)

    if now - prev_time > 600:
        return True

    # v8.0.0: Direction flips are allowed — removed block
    # Only block on severe fading (>10% drop in same direction)
    if current_dir == prev_dir and current_imb < prev_imb - 0.10:
        log.info("OB trend │ Fading %.0f%%→%.0f%%. Blocking.",
                 prev_imb * 100, current_imb * 100)
        return False

    return True


def calc_ob_quality(ob_data: dict, yes_mid: int) -> dict:
    """
    Near-money order book analysis.
    v8.0.0 FIX: Now calls adaptive_ob_threshold() instead of using
    the raw profile threshold. This was the primary cause of zero trades —
    adaptive_ob_threshold existed but was never wired in.
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

    # v8.0.0 FIX: use adaptive threshold based on actual depth
    thresh = adaptive_ob_threshold(total)

    log.info(
        "OB │ Near-money: YES=$%.0f(%d lvls) NO=$%.0f(%d lvls) "
        "total=$%.0f min=$%.0f thresh=%.0f%%",
        yes_d, yes_lc, no_d, no_lc, total, MIN_OB_DEPTH_DOLLARS, thresh * 100,
    )

    if total < MIN_OB_DEPTH_DOLLARS:
        log.info("OB │ Depth $%.0f < minimum $%.0f — insufficient.", total, MIN_OB_DEPTH_DOLLARS)
        return {"imbalance": 0.5, "direction": "NONE",
                "near_money_depth": total, "effective_thresh": thresh,
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
            "near_money_depth": total, "effective_thresh": thresh,
            "level_count_yes": yes_lc, "level_count_no": no_lc}


def calc_ob_imbalance(ob_data: dict, yes_mid: int) -> tuple:
    """Legacy wrapper for test compat."""
    q = calc_ob_quality(ob_data, yes_mid)
    return q["imbalance"], q["direction"], q["near_money_depth"]


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
    if pnl <= -MAX_DAILY_LOSS:
        log.warning("DAILY LOSS LIMIT │ $%.2f lost (cap $%.2f). Halting.", abs(pnl), MAX_DAILY_LOSS)
        telegram_halt(f"Daily loss cap ${MAX_DAILY_LOSS:.0f} hit. PnL: ${pnl:.2f}", balance)
        return False
    if session_stop_threshold > 0 and balance < session_stop_threshold:
        log.warning("SESSION STOP │ Balance $%.2f < threshold $%.2f.", balance, session_stop_threshold)
        telegram_halt(f"Session stop. Balance ${balance:.2f} < ${session_stop_threshold:.2f}.", balance)
        return False
    return True


def balance_floor_check(balance: float) -> bool:
    if balance < MIN_BALANCE_FLOOR:
        log.warning("BALANCE FLOOR │ $%.2f < floor $%.2f.", balance, MIN_BALANCE_FLOOR)
        return False
    return True


def spread_check(yes_bid: int, yes_ask: int) -> bool:
    spread = yes_ask - yes_bid
    if spread <= 0:
        log.info("Spread │ %dc — crossed/zero. Skipping.", spread)
        return False
    return True


def expiry_guard(yes_mid: int) -> bool:
    if yes_mid > 85 or yes_mid < 15:
        log.info("Expiry guard │ %dc — near-certain. Skipping.", yes_mid)
        return False
    return True


def liquidity_hours_check() -> bool:
    utc_hour = datetime.now(timezone.utc).hour
    if LOW_LIQ_START_UTC <= LOW_LIQ_END_UTC:
        in_window = LOW_LIQ_START_UTC <= utc_hour < LOW_LIQ_END_UTC
    else:
        in_window = utc_hour >= LOW_LIQ_START_UTC or utc_hour < LOW_LIQ_END_UTC
    if in_window:
        log.info("Liquidity │ UTC hour %d in low-liq window. Skipping.", utc_hour)
        return False
    return True


def concurrent_position_check() -> bool:
    if len(open_orders) >= MAX_CONCURRENT_POS:
        log.info("Concurrent │ %d open ≥ limit %d. Skipping.", len(open_orders), MAX_CONCURRENT_POS)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_limit_order(ticker: str, direction: str, size_dollars: float,
                      limit_price_cents: int,
                      ob_pct: float = 0.0, edge_pct: float = 0.0) -> Optional[str]:
    global last_trade_ts, paper_balance

    if limit_price_cents <= 0:
        return None
    count = int((size_dollars * 100) / limit_price_cents)
    if count < 1:
        log.info("Kelly size $%.2f @ %dc = 0 contracts. Skipping.", size_dollars, limit_price_cents)
        return None
    cost      = (limit_price_cents * count) / 100.0
    client_id = f"j5-{ACTIVE_MODE.value[:4]}-{uuid.uuid4().hex[:8]}"

    btc_at_entry = list(btc_prices)[-1] if btc_prices else 0

    if DEMO_MODE:
        paper_balance   -= cost
        last_trade_ts    = time.time()
        active_tickers.add(ticker)
        record = {
            "time":            datetime.now(timezone.utc).isoformat(),
            "ticker":          ticker,
            "side":            direction,
            "size":            size_dollars,
            "price":           limit_price_cents,
            "count":           count,
            "cost":            cost,
            "mode":            ACTIVE_MODE.value,
            "order_id":        client_id,
            "result":          "pending",
            "placed_at":       time.time(),
            "btc_entry_price": btc_at_entry,
        }
        trade_history.append(record)
        open_orders[client_id] = record
        log.info("🟡 PAPER │ %s %s │ %d @ %dc │ cost=$%.2f │ bal=$%.2f",
            direction, ticker[-15:], count, limit_price_cents, cost, paper_balance)
        tg.send_trade_entry_notification(
            ticker=ticker, direction=direction, cost=cost,
            price_cents=limit_price_cents, balance=paper_balance,
            ob_pct=ob_pct, edge_pct=edge_pct,
        )
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
            "time":            datetime.now(timezone.utc).isoformat(),
            "ticker":          ticker,
            "side":            direction,
            "size":            size_dollars,
            "price":           limit_price_cents,
            "count":           count,
            "cost":            cost,
            "mode":            ACTIVE_MODE.value,
            "order_id":        order_id,
            "result":          "pending",
            "placed_at":       time.time(),
            "btc_entry_price": btc_at_entry,
        }
        trade_history.append(record)
        open_orders[order_id] = record
        active_tickers.add(ticker)
        log.info("✅ ORDER │ %s %s │ %d @ %dc │ $%.2f │ ID:%s",
            direction, ticker[-15:], count, limit_price_cents, size_dollars, order_id[:12])
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
    global consecutive_losses, last_signal_desc, streak_pause_until

    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)

    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        return

    yes_mid = (yes_bid + yes_ask) // 2

    # ── Layer 1: Hard guards ──────────────────────────────────────────────
    if not balance_floor_check(current_balance):
        return
    if not expiry_guard(yes_mid):
        return
    if not spread_check(yes_bid, yes_ask):
        return
    if ticker in active_tickers:
        log.info("Position guard │ Already in %s.", ticker[-15:])
        return
    if not cooldown_passed():
        return
    if not daily_loss_check(current_balance):
        return

    # ── Layer 2: Streak pause ─────────────────────────────────────────────
    STREAK_THRESHOLD = int(os.environ.get("MAX_CONSEC_LOSSES", "2"))
    STREAK_PAUSE_SEC = int(os.environ.get("STREAK_PAUSE_SECS", "1800"))

    if consecutive_losses >= STREAK_THRESHOLD:
        now = time.time()
        if now < streak_pause_until:
            log.info("Streak pause │ %d losses. Resuming in %.0f min.",
                     consecutive_losses, (streak_pause_until - now) / 60)
            last_signal_desc = f"streak pause ({consecutive_losses} losses)"
            return
        log.info("Streak pause expired — resetting, resuming.")
        consecutive_losses = 0

    # ── Layer 3: Statistical performance guard ────────────────────────────
    if not performance_guard():
        last_signal_desc = "performance guard (Wilson LB < 50%)"
        return

    # ── Layer 4: Time filters ─────────────────────────────────────────────
    utc_hour = datetime.now(timezone.utc).hour
    if utc_hour in LOW_LIQ_HOURS_UTC:
        log.info("Low-liq │ UTC hour %d. Skipping.", utc_hour)
        last_signal_desc = f"low-liq hour UTC:{utc_hour}"
        return

    mins_remaining = minutes_to_expiry(market)
    if mins_remaining < MIN_MINUTES_TO_EXPIRY:
        log.info("Expiry imminent │ %.1f min < %.1f min. Skipping.",
                 mins_remaining, MIN_MINUTES_TO_EXPIRY)
        last_signal_desc = f"expiry imminent ({mins_remaining:.1f}min)"
        return

    # ── Layer 5: Regime detection ─────────────────────────────────────────
    regime, r_squared = compute_btc_regime()

    if regime in ("HIGH_VOL", "UNKNOWN", "RANGING"):
        log.info("Regime filter │ %s (R²=%.2f) — only TRENDING allowed.", regime, r_squared)
        last_signal_desc = f"regime={regime} (R²={r_squared:.2f})"
        return

    # ── Layer 6: OB signal quality ────────────────────────────────────────
    ob_data    = get_order_book(ticker)
    ob_quality = calc_ob_quality(ob_data, yes_mid)
    ob_dir     = ob_quality["direction"]

    if ob_dir == "NONE":
        log.info("OB │ No signal — imb=%.0f%% depth=$%.0f.",
                 ob_quality["imbalance"] * 100, ob_quality["near_money_depth"])
        last_signal_desc = (
            f"OB flat ({ob_quality['imbalance']*100:.0f}%"
            f" depth=${ob_quality['near_money_depth']:.0f})"
        )
        return

    if not ob_trend_check(ticker, ob_quality["imbalance"], ob_dir):
        last_signal_desc = f"OB trend fading ({ticker[-15:]})"
        return

    # ── Layer 7: BTC momentum ─────────────────────────────────────────────
    momentum_verdict, momentum_boost = btc_momentum_signal(ob_dir)

    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB=%s vs BTC. Skipping.", ob_dir)
        last_signal_desc = f"CONFLICT: OB={ob_dir} vs BTC"
        return

    if REQUIRE_AGREE_MOMENTUM and momentum_verdict == "NEUTRAL":
        if ALLOW_NEUTRAL_IN_TRENDING and r_squared >= NEUTRAL_R2_FLOOR:
            log.info("Momentum │ NEUTRAL bypassed — TRENDING R²=%.2f ≥ %.2f.",
                     r_squared, NEUTRAL_R2_FLOOR)
        else:
            log.info("Momentum filter │ Required AGREE, got %s (OB=%s). Skipping.",
                     momentum_verdict, ob_dir)
            last_signal_desc = f"momentum={momentum_verdict} (need AGREE, OB={ob_dir})"
            return

    # ── Layer 8: Confidence score ─────────────────────────────────────────
    confidence = compute_confidence_score(
        ob_quality=ob_quality, regime=regime, r_squared=r_squared,
        momentum_verdict=momentum_verdict, momentum_boost=momentum_boost,
        mins_remaining=mins_remaining,
    )

    if confidence < MINIMUM_CONFIDENCE:
        log.info("Confidence │ Score %.0f < minimum %d.", confidence, MINIMUM_CONFIDENCE)
        last_signal_desc = f"confidence {confidence:.0f}/{MINIMUM_CONFIDENCE}"
        return

    win_prob = min(0.92, ob_quality["imbalance"] + momentum_boost)

    log.info(
        "📡 %s │ Regime:%s(R²=%.2f) │ OB:%s %.1f%% depth=$%.0f │ "
        "BTC:%s(+%.2f%%) │ WinProb:%.1f%% │ Conf:%.0f │ %.1fmin remain",
        ticker, regime, r_squared, ob_dir,
        ob_quality["imbalance"] * 100, ob_quality["near_money_depth"],
        momentum_verdict, momentum_boost * 100,
        win_prob * 100, confidence, mins_remaining,
    )

    # ── Layer 9a: Price breakeven guard ───────────────────────────────────
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
        log.info("Bias filter │ %dc outside [%d–%d]c.",
                 contract_price, PROFILE["min_price"], PROFILE["max_price"])
        return

    # ── Layer 9b: Edge filter ─────────────────────────────────────────────
    edge = calc_edge(win_prob, contract_price)
    if edge < PROFILE["min_edge"]:
        log.info("Edge │ %.3f < min %.3f.", edge, PROFILE["min_edge"])
        return

    # ── Layer 9c: Kelly sizing ────────────────────────────────────────────
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

    # ── EDGE JUSTIFICATION ────────────────────────────────────────────────
    wlb_str = (
        f"WilsonLB={wilson_lower_bound(live_wins, live_wins + live_losses)*100:.1f}%"
        if (live_wins + live_losses) >= 10 else "WilsonLB=n/a"
    )
    log.info(
        "📋 EDGE JUSTIFICATION │ %s %s @ %d¢ │ "
        "Regime=%s(R²=%.2f) │ OB=%.0f%% depth=$%.0f │ "
        "Momentum=%s(+%.1f%%) │ WinProb=%.1f%% Edge=%.2f%% │ "
        "Confidence=%.0f/100 │ Bet=$%.2f │ %.1fmin remain │ %s",
        trade_direction, ticker[-15:], contract_price,
        regime, r_squared,
        ob_quality['imbalance']*100, ob_quality['near_money_depth'],
        momentum_verdict, momentum_boost*100,
        win_prob*100, edge*100, confidence, bet, mins_remaining, wlb_str,
    )

    last_signal_desc = (
        f"SIGNAL {trade_direction} conf={confidence:.0f} "
        f"OB={ob_quality['imbalance']*100:.0f}% edge={edge*100:.1f}%"
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
    global _last_known_balance, _shutdown_requested

    init_base_url()

    paper_balance = float(os.environ.get("PAPER_BALANCE", "25.0"))

    log.info("━" * 70)
    log.info("  BOT_VERSION: %s", BOT_VERSION)
    tg.validate_telegram_connection()
    log.info("  JOHNNY5 v8 │ %s │ Archetype: %s",
             "PAPER 🟡" if DEMO_MODE else "LIVE 🔴", ACTIVE_MODE.value.upper())
    log.info("  %s", PROFILE["description"])
    log.info("  Max trade: $%.2f │ Kelly: %.0f%% │ Min edge: %.1f%% │ Daily cap: $%.2f",
             TRADE_SIZE_DOLLARS, PROFILE["kelly_frac"] * 100,
             PROFILE["min_edge"] * 100, MAX_DAILY_LOSS)
    log.info("  Breakeven cap: %dc │ Floor: $%.2f │ OB thresh: %.0f%% │ Min depth: $%.0f",
             YES_BREAKEVEN_PRICE, MIN_BALANCE_FLOOR,
             PROFILE["ob_thresh"] * 100, MIN_OB_DEPTH_DOLLARS)
    log.info("  Confidence min: %d │ R² thresh: %.2f │ Require AGREE: %s",
             MINIMUM_CONFIDENCE, R_SQUARED_THRESHOLD, REQUIRE_AGREE_MOMENTUM)
    log.info("  Cooldown: %ds │ Max bet frac: %.0f%% │ Streak threshold: %s",
             PROFILE["cooldown"], MAX_BET_FRACTION * 100,
             os.environ.get("MAX_CONSEC_LOSSES", "2"))
    log.info("  Low-liq UTC hours: %s", sorted(LOW_LIQ_HOURS_UTC))
    log.info("  MomThresh: %.3f%% │ NeutralOK: %s (R²≥%.2f)",
             MOMENTUM_AGREE_THRESHOLD, ALLOW_NEUTRAL_IN_TRENDING, NEUTRAL_R2_FLOOR)
    log.info("  %s", "📋 PAPER — zero real orders" if DEMO_MODE else "⚠️  LIVE — real money")
    log.info("━" * 70)

    _validate_config_against_doctrine()

    live_wins          = 0
    live_losses        = 0
    streak_pause_until = 0.0

    if DEMO_MODE:
        running_pnl           = 0.0
        session_start_balance = paper_balance
        session_stop_threshold = paper_balance * 0.50
        log.info("Paper balance: $%.2f │ Session stop: $%.2f", paper_balance, session_stop_threshold)
        telegram_boot(paper_balance)
    else:
        bal = get_live_balance()
        _last_known_balance    = bal
        session_start_balance  = bal
        session_stop_threshold = bal * 0.50
        open_orders.clear()
        active_tickers.clear()
        consecutive_losses = 0
        running_pnl        = 0.0
        log.info("Live balance: $%.2f │ Session stop: $%.2f", bal, session_stop_threshold)
        telegram_boot(bal)

    resolve_cycle = 0

    while not _shutdown_requested:
        try:
            # ── Heartbeat ──────────────────────────────────────────────────
            if time.time() - last_heartbeat_ts >= 900:
                last_heartbeat_ts = time.time()
                hb_bal    = paper_balance if DEMO_MODE else get_live_balance()
                hb_pnl    = paper_daily_pnl if DEMO_MODE else (hb_bal - session_start_balance)
                hb_open   = len(open_orders)
                hb_trades = len([t for t in trade_history
                                 if t.get("result") in ("win", "loss", "pending")])
                tg.send_heartbeat(
                    balance=hb_bal, session_pnl=hb_pnl, open_count=hb_open,
                    trades_today=hb_trades, last_signal=last_signal_desc,
                )

            market = get_active_btc_market()
            if not market:
                log.info("No active BTC market. Waiting %ds...", POLL_INTERVAL)
                last_signal_desc = "no market"
                time.sleep(POLL_INTERVAL)
                continue

            update_btc_price(market)

            # Clear expired position locks
            current_ticker = market.get("ticker", "")
            tickers_with_orders = {t.get("ticker", "") for t in open_orders.values()}
            expired = {t for t in active_tickers
                       if t != current_ticker and t not in tickers_with_orders}
            if expired:
                log.info("Clearing expired position locks: %s", expired)
                active_tickers -= expired

            current_balance = paper_balance if DEMO_MODE else get_live_balance()
            run_decision(market, current_balance)

            resolve_cycle += 1
            if resolve_cycle % 3 == 0:
                resolve_open_orders()
                cancel_stale_orders()

                if DEMO_MODE:
                    resolved = [t for t in trade_history if t.get("result") in ("win","loss")]
                    wins  = sum(1 for t in resolved if t["result"] == "win")
                    total = len(resolved)
                    wr    = wins / total if total > 0 else 0.0
                    log.info("📋 PAPER │ Balance: $%.2f │ PnL: $%+.2f │ WR: %.1f%% (%d trades)",
                             paper_balance, paper_daily_pnl, wr * 100, total)
                else:
                    live_bal  = get_live_balance()
                    daily_pnl = live_bal - session_start_balance
                    resolved  = [t for t in trade_history if t.get("result") in ("win","loss")]
                    wins   = sum(1 for t in resolved if t["result"] == "win")
                    losses = len(resolved) - wins
                    total  = len(resolved)
                    wr     = wins / total if total > 0 else 0.0
                    log.info("Portfolio │ Balance: $%.2f │ PnL: $%+.2f │ Open: %d │ WR: %.1f%%",
                             live_bal, daily_pnl, len(open_orders), wr * 100)

                    now_utc_hour = datetime.now(timezone.utc).hour
                    if now_utc_hour == 0 and time.time() - last_daily_summary_ts > 3600:
                        last_daily_summary_ts = time.time()
                        telegram_daily_summary(live_bal, daily_pnl, wins, losses)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            time.sleep(POLL_INTERVAL)

    final = paper_balance if DEMO_MODE else get_live_balance()
    log.info("Shutting down. Final balance: $%.2f", final)
    tg.send_telegram_message(f"🛑 Johnny5 stopped. Final balance: ${final:.2f}")


if __name__ == "__main__":
    main()
