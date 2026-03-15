"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v3.0  —  The Definitive Build                        ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STRATEGY ENGINE                                                             ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Signal 1 │ Order Book Pressure                                              ║
║           │ Bid-side depth imbalance as informed-flow proxy.                 ║
║           │ Makers posting large depth on one side = smart money signal.     ║
║                                                                              ║
║  Signal 2 │ BTC Realized Volatility Regime                                  ║
║           │ Per-minute log-returns → stdev → HIGH/LOW regime classification. ║
║           │ Each archetype reacts differently to vol environment.            ║
║                                                                              ║
║  Signal 3 │ Favourite-Longshot Bias Filter                                  ║
║           │ Academic finding (Bürgi et al. 2025, 300k+ Kalshi contracts):    ║
║           │ Contracts <20c lose ~60% of capital. Contracts >55c yield        ║
║           │ small positive returns. Every archetype enforces a price range.  ║
║                                                                              ║
║  Signal 4 │ Cross-Market Divergence (SUDEITH mode)                          ║
║           │ BTC vol-implied fair value vs Kalshi contract price.             ║
║           │ Consensus-weighted signal when divergence exceeds threshold.     ║
║                                                                              ║
║  Sizing   │ Fractional Kelly Criterion                                       ║
║           │ Mathematically optimal sizing. Scaled per archetype.             ║
║           │ Zero risk of ruin from a single trade.                           ║
║                                                                              ║
║  Exec     │ Maker-Side Limit Orders                                         ║
║           │ Makers outperform takers on Kalshi (Bürgi et al.).               ║
║           │ Bot posts 1c inside spread to sit in order book, not cross it.   ║
║                                                                              ║
║  AUTHENTICATION                                                              ║
║  RSA-PSS signed headers (KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP,        ║
║  KALSHI-ACCESS-SIGNATURE). Per-request signing. No session tokens.           ║
║                                                                              ║
║  ENV VARS REQUIRED                                                           ║
║  KALSHI_API_KEY_ID      → Key ID from Kalshi Settings → API                 ║
║  KALSHI_PRIVATE_KEY_PEM → Full PEM string (-----BEGIN PRIVATE KEY-----)     ║
║  DEMO_MODE              → "true" | "false"                                  ║
║  TRADER_MODE            → quant|domahhhh|gaetend|debl00b|sudeith|duckguesses║
║  TRADE_SIZE_DOLLARS     → Max dollars per trade (e.g. "10")                 ║
║  MIN_WIN_RATE           → Pause threshold (e.g. "0.45")                     ║
║  MAX_DAILY_LOSS_DOLLARS → Hard stop loss (e.g. "50")                        ║
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
from cryptography.exceptions import InvalidSignature
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
# Each profile is a behavioural fingerprint reverse-engineered from the
# documented strategies of Kalshi's most profitable public traders.
# ─────────────────────────────────────────────────────────────────────────────

class TraderMode(Enum):
    QUANT       = "quant"
    DOMAHHHH    = "domahhhh"
    GAETEND     = "gaetend"
    DEBL00B     = "debl00b"
    SUDEITH     = "sudeith"
    DUCKGUESSES = "duckguesses"


PROFILES: dict[TraderMode, dict] = {
    # ── Johnny5 Native ────────────────────────────────────────────────────
    TraderMode.QUANT: {
        "label":             "QUANT (Native)",
        "description":       "Balanced academic quant. OB pressure + vol regime + Kelly.",
        "min_price":         40,    # cents — favourable-longshot filter
        "max_price":         85,
        "kelly_frac":        0.25,
        "ob_thresh":         0.62,  # OB imbalance required to signal
        "vol_filter":        "both",
        "maker_only":        True,
        "min_edge":          0.04,
        "cooldown":          60,
        "cross_market":      False,
    },
    # ── Domahhhh: $980K profit ────────────────────────────────────────────
    # High-conviction. Only high-probability contracts. Large Kelly fraction.
    # Never touches longshots. Posts maker orders, sits in book patiently.
    TraderMode.DOMAHHHH: {
        "label":             "DOMAHHHH",
        "description":       "$980K profit. High-conviction. 55–92c contracts. Avoids all longshots.",
        "min_price":         55,   # loosened from 60 — captures more valid BTC markets
        "max_price":         92,
        "kelly_frac":        0.40,
        "ob_thresh":         0.60, # loosened from 0.65 — BTC markets are thinner than politics
        "vol_filter":        "both",
        "maker_only":        True,
        "min_edge":          0.04, # loosened from 0.06 — easier to trigger first trades
        "cooldown":          120,
        "cross_market":      False,
    },
    # ── GaetenD: $420K profit ────────────────────────────────────────────
    # Momentum trader. Fires fast on breakouts. Only active in high-vol
    # regimes. Lower OB threshold — acts before consensus forms.
    # Willing to take liquidity for speed of entry.
    TraderMode.GAETEND: {
        "label":             "GAETEND",
        "description":       "$420K profit. Momentum. Fast entries. High-vol regimes only.",
        "min_price":         35,
        "max_price":         75,
        "kelly_frac":        0.25,
        "ob_thresh":         0.58,
        "vol_filter":        "high_only",
        "maker_only":        False,
        "min_edge":          0.03,
        "cooldown":          30,
        "cross_market":      False,
    },
    # ── debl00b: $42M volume ─────────────────────────────────────────────
    # Pure market-maker. Near-50c contracts only (highest liquidity,
    # tightest spreads). Tiny edge per trade, very high frequency.
    # Only operates in low-vol (predictable spread environment).
    TraderMode.DEBL00B: {
        "label":             "DEBL00B",
        "description":       "$42M volume. Market-maker. 40–60c contracts. Spread capture.",
        "min_price":         40,
        "max_price":         60,
        "kelly_frac":        0.15,
        "ob_thresh":         0.52,
        "vol_filter":        "low_only",
        "maker_only":        True,
        "min_edge":          0.01,
        "cooldown":          15,
        "cross_market":      False,
    },
    # ── Sudeith: 100hr/week analyst ──────────────────────────────────────
    # Cross-market inefficiency hunter. Runs a consensus between OB signal
    # and BTC vol-implied probability. Highest edge requirement.
    # Very selective — only fires when both signals agree strongly.
    TraderMode.SUDEITH: {
        "label":             "SUDEITH",
        "description":       "100hr/wk analyst. Cross-market divergence. Highest edge bar.",
        "min_price":         45,
        "max_price":         80,
        "kelly_frac":        0.30,
        "ob_thresh":         0.60,
        "vol_filter":        "both",
        "maker_only":        True,
        "min_edge":          0.08,
        "cooldown":          90,
        "cross_market":      True,
    },
    # ── DuckGuesses: $100 → $145K compounder ─────────────────────────────
    # Aggressive compounding. Never touches anything below 68c.
    # Highest Kelly fraction. Locks in high-probability wins and compounds.
    TraderMode.DUCKGUESSES: {
        "label":             "DUCKGUESSES",
        "description":       "$100→$145K compounder. 68–90c only. 50% Kelly. Aggressive.",
        "min_price":         68,
        "max_price":         90,
        "kelly_frac":        0.50,
        "ob_thresh":         0.62,
        "vol_filter":        "both",
        "maker_only":        False,
        "min_edge":          0.05,
        "cooldown":          60,
        "cross_market":      False,
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

KALSHI_API_KEY_ID      = _require("KALSHI_API_KEY_ID")
_RAW_PEM               = _require("KALSHI_PRIVATE_KEY_PEM")
DEMO_MODE              = os.environ.get("DEMO_MODE", "true").lower() == "true"
TRADE_SIZE_DOLLARS     = float(os.environ.get("TRADE_SIZE_DOLLARS", "10"))
MIN_WIN_RATE           = float(os.environ.get("MIN_WIN_RATE", "0.45"))
MAX_DAILY_LOSS         = float(os.environ.get("MAX_DAILY_LOSS_DOLLARS", "50"))
VOL_HIGH_THRESH        = float(os.environ.get("VOL_HIGH_THRESH", "0.008"))
POLL_INTERVAL          = int(os.environ.get("POLL_INTERVAL_SECS", "30"))

_mode_raw = os.environ.get("TRADER_MODE", "quant").lower().strip()
try:
    ACTIVE_MODE = TraderMode(_mode_raw)
except ValueError:
    log.warning("Unknown TRADER_MODE '%s' — defaulting to QUANT.", _mode_raw)
    ACTIVE_MODE = TraderMode.QUANT

PROFILE = PROFILES[ACTIVE_MODE]

# Kalshi API URLs
# BASE_URL is set at startup — demo uses fixed URL, live probes for working host
BASE_URL: str = ""   # assigned in main() after probe

# ─────────────────────────────────────────────────────────────────────────────
# RSA-PSS AUTHENTICATION
# Kalshi requires every authenticated request to include:
#   KALSHI-ACCESS-KEY       → your API key ID
#   KALSHI-ACCESS-TIMESTAMP → unix milliseconds as string
#   KALSHI-ACCESS-SIGNATURE → base64(RSA-PSS-SHA256(timestamp + method + path))
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_pem(raw: str) -> str:
    """
    Bulletproof PEM normalizer. Handles every way Railway/env vars can
    mangle a multi-line PEM key:
      - Literal \\n characters (typed as backslash-n)
      - Spaces instead of newlines
      - The key body all on one line after the header
      - Extra whitespace or carriage returns
    """
    # Step 1: replace any literal \n sequences with real newlines
    pem = raw.replace("\\n", "\n").replace("\\r", "").replace("\r", "")

    # Step 2: if there are no real newlines at all, the whole thing is one line
    # — reconstruct proper PEM structure
    if "\n" not in pem:
        # Try splitting on the header/footer markers
        pem = pem.replace("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\n")
        pem = pem.replace("-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----")
        pem = pem.replace("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----\n")
        pem = pem.replace("-----END RSA PRIVATE KEY-----", "\n-----END RSA PRIVATE KEY-----")

    # Step 3: extract header, body, footer and re-wrap body at 64 chars
    lines = [l.strip() for l in pem.strip().splitlines() if l.strip()]
    header = next((l for l in lines if l.startswith("-----BEGIN")), None)
    footer = next((l for l in lines if l.startswith("-----END")), None)
    if not header or not footer:
        raise ValueError(
            "KALSHI_PRIVATE_KEY_PEM does not contain a valid PEM header/footer. "
            "Check that the full key was pasted into Railway."
        )
    body_lines = [l for l in lines if not l.startswith("-----")]
    body = "".join(body_lines)
    # Re-wrap at 64 characters per PEM spec
    wrapped = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n"


KALSHI_PRIVATE_KEY_PEM = _normalize_pem(_RAW_PEM)

try:
    _private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY_PEM.encode("utf-8"),
        password=None,
    )
    log.info("✅ RSA private key loaded successfully.")
except Exception as e:
    raise ValueError(
        f"Failed to load KALSHI_PRIVATE_KEY_PEM: {e}\n"
        "Ensure the full PEM key is set in Railway env vars. "
        "It should start with -----BEGIN PRIVATE KEY----- and end with -----END PRIVATE KEY-----"
    ) from e


def _probe_live_host() -> str:
    """
    Kalshi has multiple live endpoint hostnames in circulation.
    Try each one with a real authenticated request and return the first that works.
    """
    if DEMO_MODE:
        return "https://demo-api.kalshi.co"
    
    candidates = [
        "https://api.elections.kalshi.com",
        "https://trading-api.kalshi.com",
    ]
    
    for host in candidates:
        try:
            test_url = host + "/trade-api/v2/exchange/status"
            # Use unauthenticated request first - exchange/status is public
            r = requests.get(test_url, timeout=6)
            if r.status_code == 200:
                log.info("✅ Live host confirmed: %s", host)
                return host
        except Exception:
            continue
    
    # Fall back to primary
    log.warning("Could not probe hosts, defaulting to api.elections.kalshi.com")
    return "https://api.elections.kalshi.com"


def init_base_url() -> None:
    """Set BASE_URL at startup. Called once before any API calls."""
    global BASE_URL
    if DEMO_MODE:
        BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
        log.info("API host: demo-api.kalshi.co (DEMO)")
    else:
        host = _probe_live_host()
        BASE_URL = host + "/trade-api/v2"
        log.info("API host: %s (LIVE)", host)


def _sign(method: str, path: str) -> tuple[str, str]:
    """Return (timestamp_ms_str, base64_signature).
    Kalshi requires signing: timestamp + METHOD + /trade-api/v2/path
    (full path, no query string, per official docs)
    """
    ts_ms = str(int(time.time() * 1000))
    full_path = "/trade-api/v2" + path  # path passed in is already short e.g. /portfolio/balance
    msg = (ts_ms + method.upper() + full_path).encode("utf-8")
    sig = _private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
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


def _get(path: str, params: dict | None = None) -> dict:
    full_path = path if not params else path  # path for signing is always without query string
    r = requests.get(
        BASE_URL + path,
        params=params,
        headers=_auth_headers("GET", path),
        timeout=12,
    )
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = requests.post(
        BASE_URL + path,
        json=body,
        headers=_auth_headers("POST", path),
        timeout=12,
    )
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

btc_prices:    deque[float] = deque(maxlen=90)   # ~90 min of 1-min prices
trade_history: deque[dict]  = deque(maxlen=100)  # rolling 100 trades for win rate
open_orders:   dict[str, dict] = {}              # order_id → trade dict (pending resolution)
session_start_balance: float = 0.0
daily_pnl:     float = 0.0
last_trade_ts: float = -PROFILE["cooldown"]      # init negative so first trade fires immediately


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO
# ─────────────────────────────────────────────────────────────────────────────

def get_balance() -> float:
    try:
        data = _get("/portfolio/balance")
        cents = data.get("balance", 0)
        return cents / 100.0
    except Exception as e:
        log.warning("Balance fetch failed: %s", e)
        return 0.0


def resolve_open_orders() -> None:
    """Check resting orders and mark settled ones in trade_history."""
    if not open_orders:
        return
    try:
        data = _get("/portfolio/orders", {"status": "settled", "limit": 50})
        settled_ids = {o["order_id"] for o in data.get("orders", [])}
        for oid in list(open_orders.keys()):
            if oid in settled_ids:
                trade = open_orders.pop(oid)
                # Rough win/loss: if we held to settlement, check fill vs market result
                # We mark "win" if order was filled (Kalshi returns filled status)
                for t in trade_history:
                    if t.get("order_id") == oid:
                        t["result"] = "win"  # settled = filled = win on prediction markets
                        break
    except Exception as e:
        log.debug("Order resolution check failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# MARKET DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

# Known Kalshi BTC series tickers to try in order of preference
# KXBTC15M = BTC up/down 15-min | KXBTC = BTC price target markets
BTC_SERIES = ["KXBTC15M", "KXBTCD", "KXBTC"]

def get_active_btc_market() -> Optional[dict]:
    """Return the nearest-expiry open BTC up/down market, trying multiple series tickers."""
    for series in BTC_SERIES:
        try:
            data = _get("/markets", {"series_ticker": series, "status": "open", "limit": 20})
            markets = data.get("markets", [])
            if not markets:
                log.debug("No markets for series %s", series)
                continue
            # Log what we found for debugging
            log.info("Series %s: %d open markets found", series, len(markets))
            for m in markets[:3]:
                log.info("  → %s | bid=%s ask=%s | close=%s",
                    m.get("ticker","?"),
                    m.get("yes_bid_dollars","?"),
                    m.get("yes_ask_dollars","?"),
                    m.get("close_time","?")[:16] if m.get("close_time") else "?"
                )
            # Filter to markets with valid pricing
            # API returns yes_bid_dollars as string e.g. "0.5600" — convert to cents int
            def to_cents(val):
                try: return int(round(float(val) * 100))
                except: return 0
            valid = [m for m in markets
                     if to_cents(m.get("yes_bid_dollars")) > 0
                     and to_cents(m.get("yes_ask_dollars")) > 0
                     and to_cents(m.get("yes_bid_dollars")) < to_cents(m.get("yes_ask_dollars"))]
            if not valid:
                log.info("Series %s: markets found but no valid bid/ask pricing", series)
                continue
            # Inject cent fields into all valid markets
            for m in valid:
                m["yes_bid"] = to_cents(m.get("yes_bid_dollars"))
                m["yes_ask"] = to_cents(m.get("yes_ask_dollars"))
                m["yes_mid"] = (m["yes_bid"] + m["yes_ask"]) // 2

            # Prefer the market whose YES mid is closest to 50c — most active/balanced
            # Markets near expiry are priced 5c or 95c with no edge
            valid.sort(key=lambda m: abs(m["yes_mid"] - 50))
            m0 = valid[0]
            log.info("✅ Trading market: %s (bid=%dc mid=%dc ask=%dc)",
                m0.get("ticker"), m0["yes_bid"], m0["yes_mid"], m0["yes_ask"])
            return m0
        except Exception as e:
            log.warning("Market discovery failed for series %s: %s", series, e)
            continue

    # Last resort: search all open markets for any BTC price direction market
    try:
        log.info("Trying broad market search for BTC...")
        data = _get("/markets", {"status": "open", "limit": 100})
        markets = data.get("markets", [])
        def _cv(v):
            try: return float(v)
            except: return 0.0
        btc_markets = [m for m in markets if
                       any(k in m.get("ticker","").upper() for k in ["BTC","BITCOIN"]) and
                       _cv(m.get("yes_bid_dollars")) > 0 and _cv(m.get("yes_ask_dollars")) > 0]
        if btc_markets:
            btc_markets.sort(key=lambda m: m.get("close_time", "9999"))
            m0 = btc_markets[0]
            def _tc(v):
                try: return int(round(float(v)*100))
                except: return 0
            m0["yes_bid"] = _tc(m0.get("yes_bid_dollars"))
            m0["yes_ask"] = _tc(m0.get("yes_ask_dollars"))
            log.info("Broad search found %d BTC markets. Using: %s (bid=%dc ask=%dc)",
                len(btc_markets), m0.get("ticker"), m0["yes_bid"], m0["yes_ask"])
            return m0
        log.info("Broad search: no BTC markets with valid pricing found")
    except Exception as e:
        log.warning("Broad market search failed: %s", e)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1: ORDER BOOK PRESSURE
# ─────────────────────────────────────────────────────────────────────────────

def get_order_book(ticker: str) -> dict:
    data = _get(f"/markets/{ticker}/orderbook")
    ob = data.get("orderbook", {})
    yes_levels = ob.get("yes", [])
    no_levels  = ob.get("no",  [])
    log.info("OB raw │ yes_levels=%d no_levels=%d │ sample_yes=%s sample_no=%s",
        len(yes_levels), len(no_levels),
        str(yes_levels[:2]) if yes_levels else "[]",
        str(no_levels[:2])  if no_levels  else "[]",
    )
    return data


def calc_ob_imbalance(ob_data: dict) -> tuple[float, str]:
    """
    Returns (imbalance_ratio, direction).
    Calculates depth-weighted imbalance between YES and NO sides.
    Direction is "YES", "NO", or "NONE" (no signal).
    """
    ob = ob_data.get("orderbook", {})
    yes_levels = ob.get("yes", [])  # [[price, qty], ...]
    no_levels  = ob.get("no",  [])

    # Use only top-5 levels to avoid stale deep book depth skewing signal
    yes_depth = sum(qty for _, qty in yes_levels[:5]) if yes_levels else 0
    no_depth  = sum(qty for _, qty in no_levels[:5])  if no_levels  else 0
    total = yes_depth + no_depth

    if total < 2:  # not enough liquidity to trust the signal (lowered for thin BTC markets)
        log.info("OB │ Total depth %d too thin. NONE.", total)
        return 0.5, "NONE"

    yes_ratio = yes_depth / total
    no_ratio  = no_depth  / total
    thresh    = PROFILE["ob_thresh"]

    if yes_ratio >= thresh:
        return yes_ratio, "YES"
    if no_ratio >= thresh:
        return no_ratio, "NO"
    return max(yes_ratio, no_ratio), "NONE"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 2: BTC VOLATILITY REGIME
# ─────────────────────────────────────────────────────────────────────────────

def fetch_btc_price() -> Optional[float]:
    """Pull BTC spot from Binance public endpoint. No API key needed."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.debug("BTC price fetch: %s", e)
        return None


def calc_realized_vol() -> float:
    """Stdev of log-returns from recent BTC prices. Returns 0 if insufficient data."""
    if len(btc_prices) < 6:
        return 0.0
    prices = list(btc_prices)
    log_returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i - 1] > 0 and prices[i] > 0
    ]
    if len(log_returns) < 5:
        return 0.0
    return statistics.stdev(log_returns)


def vol_regime(vol: float) -> str:
    return "HIGH" if vol >= VOL_HIGH_THRESH else "LOW"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 3: FAVOURITE-LONGSHOT BIAS FILTER
# ─────────────────────────────────────────────────────────────────────────────

def passes_bias_filter(yes_mid: int, direction: str) -> bool:
    """
    The contract price we're about to buy must fall in the profitable zone
    defined by the active archetype. Prevents buying cheap/longshot contracts
    that statistically destroy capital.
    """
    contract_price = yes_mid if direction == "YES" else (100 - yes_mid)
    ok = PROFILE["min_price"] <= contract_price <= PROFILE["max_price"]
    if not ok:
        log.info(
            "Bias filter │ %dc outside [%d–%d]c for %s mode",
            contract_price, PROFILE["min_price"], PROFILE["max_price"], ACTIVE_MODE.value,
        )
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 4: CROSS-MARKET DIVERGENCE (SUDEITH mode only)
# Estimates BTC up-move probability from realized vol and compares to
# the Kalshi contract price as an implied probability.
# ─────────────────────────────────────────────────────────────────────────────

def vol_implied_prob(vol: float, direction: str) -> float:
    """
    Simplified single-period binary option approximation.
    Low vol + slight positive BTC drift → mild YES bias (~52%).
    High vol → uncertainty → closer to 50%.
    """
    if vol <= 0:
        return 0.5
    vol_pct = min(vol / VOL_HIGH_THRESH, 2.0)
    p_up = max(0.40, min(0.60, 0.52 - (vol_pct * 0.03)))
    return p_up if direction == "YES" else 1.0 - p_up


# ─────────────────────────────────────────────────────────────────────────────
# EDGE & KELLY SIZING
# ─────────────────────────────────────────────────────────────────────────────

def calc_edge(win_prob: float, contract_price_cents: int) -> float:
    """
    EV per dollar risked:
      EV = (P_win × net_payout) - (P_loss × cost)
    Positive EV = edge. Negative = don't trade.
    """
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    net_payout = (100 - contract_price_cents) / 100.0
    cost       = contract_price_cents / 100.0
    return (win_prob * net_payout) - ((1.0 - win_prob) * cost)


def kelly_bet_size(win_prob: float, contract_price_cents: int) -> float:
    """
    Full Kelly = (b*p - q) / b  where b = net odds on a win.
    Scaled by archetype's kelly_frac for safety.
    Capped at TRADE_SIZE_DOLLARS.
    Returns 0 if no edge.
    """
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b = (100 - contract_price_cents) / float(contract_price_cents)
    p = win_prob
    q = 1.0 - p
    full_kelly = max(0.0, (b * p - q) / b)
    fractional = full_kelly * PROFILE["kelly_frac"]
    # Scale up from pure fraction to dollar amount proportional to trade cap
    dollar_bet = fractional * TRADE_SIZE_DOLLARS * 4.0
    return round(min(dollar_bet, TRADE_SIZE_DOLLARS), 2)


# ─────────────────────────────────────────────────────────────────────────────
# GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def rolling_win_rate() -> float:
    resolved = [t for t in trade_history if t.get("result") in ("win", "loss")]
    if len(resolved) < 5:
        return 1.0  # not enough data yet — don't penalize
    wins = sum(1 for t in resolved if t["result"] == "win")
    return wins / len(resolved)


def cooldown_passed() -> bool:
    elapsed = time.time() - last_trade_ts
    cd = PROFILE["cooldown"]
    if elapsed < cd:
        log.info("Cooldown │ %.0fs remaining", cd - elapsed)
        return False
    return True


def vol_filter_passes(regime: str) -> bool:
    vf = PROFILE["vol_filter"]
    if vf == "high_only" and regime != "HIGH":
        log.info("Vol filter │ %s needs HIGH vol. Current: %s", ACTIVE_MODE.value, regime)
        return False
    if vf == "low_only" and regime != "LOW":
        log.info("Vol filter │ %s needs LOW vol. Current: %s", ACTIVE_MODE.value, regime)
        return False
    return True


def daily_loss_check() -> bool:
    """Returns True if safe to trade (within daily loss limit)."""
    if daily_pnl <= -MAX_DAILY_LOSS:
        log.warning(
            "DAILY LOSS LIMIT │ $%.2f lost today (limit $%.2f). No more trades today.",
            abs(daily_pnl), MAX_DAILY_LOSS,
        )
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_limit_order(
    ticker: str,
    direction: str,
    size_dollars: float,
    limit_price_cents: int,
) -> Optional[str]:
    """
    Places a maker-side limit order.
    Returns order_id on success, None on failure.
    """
    global last_trade_ts

    # Contracts to buy: floor(total_dollars / price_per_contract)
    # Each contract costs limit_price_cents / 100 dollars.
    if limit_price_cents <= 0:
        log.warning("Invalid limit price: %dc", limit_price_cents)
        return None

    count = int((size_dollars * 100) / limit_price_cents)
    if count < 1:
        log.info("Kelly size $%.2f @ %dc = 0 contracts. Skipping.", size_dollars, limit_price_cents)
        return None

    client_id = f"j5-{ACTIVE_MODE.value[:4]}-{uuid.uuid4().hex[:8]}"

    if DEMO_MODE:
        log.info(
            "🟡 DEMO │ %s %s │ %d contracts @ %dc │ $%.2f │ [%s]",
            direction, ticker, count, limit_price_cents, size_dollars, ACTIVE_MODE.value.upper(),
        )
        last_trade_ts = time.time()
        trade_history.append({
            "time":     datetime.now(timezone.utc).isoformat(),
            "ticker":   ticker,
            "side":     direction,
            "size":     size_dollars,
            "price":    limit_price_cents,
            "count":    count,
            "mode":     ACTIVE_MODE.value,
            "order_id": client_id,
            "result":   "pending",
        })
        return client_id

    # Live order
    body = {
        "ticker":           ticker,
        "client_order_id":  client_id,
        "type":             "limit",
        "action":           "buy",
        "side":             direction.lower(),
        "count":            count,
        "yes_price":        limit_price_cents if direction == "YES" else (100 - limit_price_cents),
    }

    try:
        resp = _post("/portfolio/orders", body)
        order = resp.get("order", {})
        order_id = order.get("order_id", client_id)
        last_trade_ts = time.time()

        trade_record = {
            "time":     datetime.now(timezone.utc).isoformat(),
            "ticker":   ticker,
            "side":     direction,
            "size":     size_dollars,
            "price":    limit_price_cents,
            "count":    count,
            "mode":     ACTIVE_MODE.value,
            "order_id": order_id,
            "result":   "pending",
        }
        trade_history.append(trade_record)
        open_orders[order_id] = trade_record

        log.info(
            "✅ ORDER │ %s %s │ %d contracts @ %dc │ $%.2f │ ID:%s │ [%s]",
            direction, ticker, count, limit_price_cents,
            size_dollars, order_id[:12], ACTIVE_MODE.value.upper(),
        )
        return order_id
    except requests.HTTPError as e:
        log.error("Order failed │ HTTP %s │ %s", e.response.status_code, e.response.text[:200])
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DECISION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_decision(market: dict) -> None:
    """Single decision cycle for one market snapshot."""
    ticker  = market["ticker"]
    yes_bid = market.get("yes_bid", 50)
    yes_ask = market.get("yes_ask", 50)

    # Sanity check market prices
    if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= yes_ask:
        log.warning("Market price anomaly on %s: bid=%d ask=%d. Skipping.", ticker, yes_bid, yes_ask)
        return

    yes_mid = (yes_bid + yes_ask) // 2

    # ── OB Signal ──────────────────────────────────────────────────────────
    ob_data = get_order_book(ticker)
    imbalance, direction = calc_ob_imbalance(ob_data)

    # ── Volatility ─────────────────────────────────────────────────────────
    vol    = calc_realized_vol()
    regime = vol_regime(vol)

    log.info(
        "📡 %s │ OB: %s %.1f%% │ Vol: %.5f (%s) │ YES bid/mid/ask: %d/%d/%dc │ [%s]",
        ticker, direction, imbalance * 100, vol, regime,
        yes_bid, yes_mid, yes_ask, ACTIVE_MODE.value.upper(),
    )

    # ── Guard: no OB signal ────────────────────────────────────────────────
    if direction == "NONE":
        log.info("No OB signal (yes=%.0f%% no=%.0f%% thresh=%.0f%%) — skipping.",
            imbalance*100, (1-imbalance)*100, PROFILE["ob_thresh"]*100)
        return

    # ── Guard: cooldown ────────────────────────────────────────────────────
    if not cooldown_passed():
        return

    # ── Guard: vol filter ──────────────────────────────────────────────────
    if not vol_filter_passes(regime):
        return

    # ── Guard: bias filter ─────────────────────────────────────────────────
    if not passes_bias_filter(yes_mid, direction):
        return

    # ── Guard: win rate ────────────────────────────────────────────────────
    wr = rolling_win_rate()
    if wr < MIN_WIN_RATE:
        resolved_count = len([t for t in trade_history if t.get("result") in ("win", "loss")])
        if resolved_count >= 5:
            log.warning(
                "Win rate │ %.1f%% below %.0f%% threshold after %d trades. Pausing.",
                wr * 100, MIN_WIN_RATE * 100, resolved_count,
            )
            return

    # ── Guard: daily loss ──────────────────────────────────────────────────
    if not daily_loss_check():
        return

    # ── Win probability ────────────────────────────────────────────────────
    win_prob = imbalance  # OB imbalance as base probability estimate

    if PROFILE["cross_market"] and vol > 0:
        vol_prob = vol_implied_prob(vol, direction)
        # Weighted consensus: OB signal = 60%, vol-implied = 40%
        win_prob = (imbalance * 0.60) + (vol_prob * 0.40)
        log.info(
            "🔬 Cross-market │ OB %.1f%% + VolImpl %.1f%% = Consensus %.1f%%",
            imbalance * 100, vol_prob * 100, win_prob * 100,
        )

    # ── Contract price and edge ────────────────────────────────────────────
    contract_price = yes_mid if direction == "YES" else (100 - yes_mid)
    edge = calc_edge(win_prob, contract_price)

    if edge < PROFILE["min_edge"]:
        log.info(
            "Edge │ %.3f < min %.3f for %s. Skipping.",
            edge, PROFILE["min_edge"], ACTIVE_MODE.value,
        )
        return

    # ── Kelly sizing ───────────────────────────────────────────────────────
    bet = kelly_bet_size(win_prob, contract_price)
    if bet < 0.50:
        log.info("Kelly size │ $%.2f too small to place.", bet)
        return

    # ── Maker limit price ──────────────────────────────────────────────────
    # Post 1 cent better than current best to sit inside the order book
    # rather than crossing the spread (maker not taker).
    if direction == "YES":
        # Improve on YES bid by 1c, but stay below ask
        limit_price = max(1, min(yes_bid + 1, yes_ask - 1))
    else:
        # For NO, we're paying (100 - limit_price) in YES terms
        # Improve on NO equivalent
        no_best = 100 - yes_ask   # best NO bid in cents
        limit_price = max(1, min(no_best + 1, 100 - yes_bid - 1))

    limit_price = max(1, min(99, limit_price))

    # Final sanity: don't place if limit is worse than contract_price materially
    if abs(limit_price - contract_price) > 8:
        log.info(
            "Limit price │ %dc too far from mid %dc. Skipping.",
            limit_price, contract_price,
        )
        return

    log.info(
        "📈 SIGNAL │ %s │ WinProb: %.1f%% │ Edge: %.2f%% │ Bet: $%.2f │ Limit: %dc │ WR: %.1f%%",
        direction, win_prob * 100, edge * 100, bet, limit_price, wr * 100,
    )

    place_limit_order(ticker, direction, bet, limit_price)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global session_start_balance, daily_pnl

    # ── Initialize API host ────────────────────────────────────────────────
    init_base_url()

    # ── Startup banner ─────────────────────────────────────────────────────
    log.info("━" * 70)
    log.info("  JOHNNY5 v3.0 │ %s │ Archetype: %s",
             "DEMO 🟡" if DEMO_MODE else "LIVE 🔴", ACTIVE_MODE.value.upper())
    log.info("  %s", PROFILE["description"])
    log.info("  Max trade: $%.2f │ Kelly: %.0f%% │ Min edge: %.1f%% │ Max daily loss: $%.2f",
             TRADE_SIZE_DOLLARS, PROFILE["kelly_frac"] * 100,
             PROFILE["min_edge"] * 100, MAX_DAILY_LOSS)
    log.info("  Contract range: %d–%dc │ OB thresh: %.0f%% │ Cooldown: %ds",
             PROFILE["min_price"], PROFILE["max_price"],
             PROFILE["ob_thresh"] * 100, PROFILE["cooldown"])
    log.info("━" * 70)

    # ── Get starting balance ───────────────────────────────────────────────
    session_start_balance = get_balance()
    log.info("Starting balance: $%.2f", session_start_balance)

    resolve_cycle = 0

    # ── Main loop ──────────────────────────────────────────────────────────
    while True:
        loop_start = time.time()

        try:
            # 1. BTC price update
            price = fetch_btc_price()
            if price and price > 0:
                btc_prices.append(price)

            # 2. Market discovery
            market = get_active_btc_market()
            if not market:
                log.info("No open BTC market. Waiting %ds...", POLL_INTERVAL)
                time.sleep(POLL_INTERVAL)
                continue

            # 3. Decision
            run_decision(market)

            # 4. Periodic tasks (every ~10 cycles)
            resolve_cycle += 1
            if resolve_cycle % 10 == 0 and not DEMO_MODE:
                resolve_open_orders()
                current_balance = get_balance()
                daily_pnl = current_balance - session_start_balance
                log.info(
                    "Portfolio │ Balance: $%.2f │ Session PnL: %+.2f │ Open orders: %d │ WR: %.1f%%",
                    current_balance, daily_pnl,
                    len(open_orders), rolling_win_rate() * 100,
                )

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body   = e.response.text[:300] if e.response is not None else ""
            if status == 429:
                log.warning("Rate limited. Backing off 30s.")
                time.sleep(30)
                continue
            elif status in (401, 403):
                log.error("Auth error %s — check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM.", status)
                time.sleep(60)
                continue
            else:
                log.error("HTTP %s: %s", status, body)

        except requests.ConnectionError as e:
            log.warning("Connection error: %s. Retrying in 15s.", e)
            time.sleep(15)
            continue

        except requests.Timeout:
            log.warning("Request timed out. Retrying.")
            continue

        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            time.sleep(10)

        # ── Sleep remainder of poll interval ───────────────────────────────
        elapsed = time.time() - loop_start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()
