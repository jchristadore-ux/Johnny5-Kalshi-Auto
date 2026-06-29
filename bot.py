"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JOHNNY5-KALSHI-AUTO  v9.6.0  —  Production Build                            ║
║  "No disassemble."                                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.6.0 — DAILY DOUBLING GOAL (opt-in, owner directive).                     ║
║                                                                              ║
║  When DAILY_GOAL_ENABLED, the bot targets doubling the balance it opened the ║
║  UTC day with, then HALTS NEW ENTRIES for the rest of that day once the 2×   ║
║  target is reached (open trades still settle). At the next UTC rollover the  ║
║  new, higher balance becomes the baseline and the target resets, so the goal ║
║  compounds day over day with no intervention.                               ║
║                                                                              ║
║  Sizing is GOAL-GAP driven: each trade = remaining gap to today's target ÷   ║
║  DAILY_GOAL_TRADES_TO_TARGET, clamped to [DAILY_GOAL_MIN_STAKE, a %-of-      ║
║  balance cap (DAILY_GOAL_MAX_STAKE_FRACTION), an optional hard dollar cap,   ║
║  cash on hand]. The %-cap scales hands-off as the account compounds — no     ║
║  fixed dollar ceiling to babysit. Defaults are the "Balanced" profile        ║
║  (×2/day, gap÷5 trades, 40% cap). The Kelly edge gate and ALL entry-quality  ║
║  gates are UNCHANGED — goal mode sets the STAKE and a daily target/halt; it  ║
║  never relaxes which trades qualify.                                        ║
║                                                                              ║
║  RAILWAY: DAILY_GOAL_ENABLED=true to switch on. Optional overrides:          ║
║  DAILY_GOAL_MULTIPLE (2.0), DAILY_GOAL_TRADES_TO_TARGET (5),                 ║
║  DAILY_GOAL_MAX_STAKE_FRACTION (0.40), DAILY_GOAL_MIN_STAKE (1.0),           ║
║  DAILY_GOAL_MAX_STAKE (0 = no hard cap). Leave DEMO_MODE=true to paper-test. ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.5.0 — RECOVERY MODE: two-tier position sizing (owner directive).         ║
║                                                                              ║
║  After a FULL-SIZE (normal-mode) trade settles a LOSS, the bot drops from    ║
║  NORMAL_TRADE_SIZE to RECOVERY_TRADE_SIZE and sets a recovery target = the    ║
║  realized balance recorded IMMEDIATELY BEFORE that losing trade. It keeps     ║
║  trading at the reduced size until the realized balance climbs back to the    ║
║  target, then auto-resumes full size. State {active, target} is persisted to  ║
║  RECOVERY_STATE_PATH (atomic JSON) and reconciled on boot, so an in-container ║
║  restart resumes mid-recovery and can never wedge.                           ║
║                                                                              ║
║  Sizing is derived from the mode via active_trade_size() — never read raw    ║
║  from a single env var at the sizing call. Entry is event-driven (a settled  ║
║  full-size loss → exact pre-trade target); exit is balance-driven and checked ║
║  every cycle AND on boot. A further loss while already recovering does NOT    ║
║  move the target. Entry filters / halts / streak logic unchanged.            ║
║                                                                              ║
║  RAILWAY: NORMAL_TRADE_SIZE (defaults to TRADE_SIZE_DOLLARS, so existing      ║
║  configs keep working), RECOVERY_TRADE_SIZE (default 100). For redeploy-      ║
║  durable recovery state, mount a Railway Volume and set RECOVERY_STATE_PATH   ║
║  to a path on it (e.g. /data/recovery_state.json).                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.4.1 — FLAT $500 STAKE (owner directive): $500 trades fire regardless of  ║
║  balance.                                                                    ║
║                                                                              ║
║  v9.4.0 lifted the caps but the bet was still Kelly-scaled                   ║
║  (full_kelly × KELLY_FRACTION × balance), so $500 was only reachable around  ║
║  a $4–5k balance. kelly_bet() now uses Kelly ONLY as an edge gate (positive  ║
║  full_kelly = positive expectancy) and stakes the full TRADE_SIZE_CAP on     ║
║  every qualifying trade — no balance/Kelly/MAX_BET_FRACTION down-scaling.    ║
║  The sole clamp is cash on hand (cannot stake more than the account holds),  ║
║  so below a $500 balance the bot goes all-in. MAX_BET_FRACTION is now dead   ║
║  config. Entry-quality gates are unchanged.                                  ║
║                                                                              ║
║  RAILWAY: TRADE_SIZE_DOLLARS=500 is the flat stake (still required).         ║
║  MAX_BET_FRACTION no longer affects sizing.                                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.4.0 — $500 STAKE + LOSS-STOP REMOVAL (owner directive, explicit          ║
║  authority to overwrite prior risk doctrine).                                ║
║                                                                              ║
║  INTENT: run $500 per trade and leave the consecutive-loss streak pause as   ║
║  the ONLY active auto-hold. The daily-loss governors and the balance floor   ║
║  no longer fit a $500-stake book and were removed; RECOVERY mode is gone so   ║
║  drawdown never shrinks the stake.                                          ║
║                                                                              ║
║  CODE CHANGES:                                                              ║
║  1. daily_loss_check(): the % and $ daily-loss caps are removed. The 40%     ║
║     SESSION_STOP_FRACTION halt is RETAINED as a catastrophic backstop.       ║
║  2. balance_floor_check() removed (function + run_decision call). No floor.  ║
║  3. RECOVERY removed: kelly_bet() no longer applies KELLY_RECOVERY_MULT and  ║
║     update_session_state() is a no-op, so the session stays ACTIVE.          ║
║  4. Entry-quality gates (AGREE/NEUTRAL, OB/R²/confidence/edge/Wilson) are    ║
║     UNCHANGED — they decide IF a trade exists, not its size.                 ║
║                                                                              ║
║  $500/trade is bankroll-gated, not a switch: bet = min(full_kelly ×          ║
║  KELLY_FRACTION × balance, TRADE_SIZE_CAP, MAX_BET_FRACTION × balance). With  ║
║  KELLY_FRACTION=0.30 the Kelly leg only reaches $500 around a $4–5k balance.  ║
║                                                                              ║
║  RAILWAY ENV VAR CHANGES REQUIRED (owner sets these in the Railway UI):      ║
║    - TRADE_SIZE_DOLLARS : 5    → 500                                         ║
║    - MAX_BET_FRACTION   : 0.04 → 1.0                                         ║
║    - MAX_CONSEC_LOSSES  : 2    → 3                                           ║
║    - LADDER_ENABLED     : confirm false (default)                           ║
║  MAX_DAILY_LOSS_DOLLARS / MAX_DAILY_LOSS_PCT / MIN_BALANCE_FLOOR /           ║
║  RECOVERY_TRIGGER_PCT are now dead config (no longer read by any guard).     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.3.3 — MOMENTUM R² ALIGNMENT: magnitude gate still mislabeled trends.     ║
║                                                                              ║
║  DIAGNOSIS (2026-06-24 LIVE, v9.3.2): still ZERO trades. 13 cycles reached   ║
║  the gate fully aligned (e.g. KXBTC15M-...1815 climbing 74→83¢, regime       ║
║  TRENDING_UP R²=0.74–0.92, OB YES 77–97%) yet momentum returned NEUTRAL on   ║
║  every one. Root cause is structural, not the window: compute_regime() flags ║
║  TRENDING by R² (trend CONSISTENCY), but compute_momentum() required raw     ║
║  %-MAGNITUDE ≥0.15%/3min. A smooth, gentle drift has high R² but a small     ║
║  %-move, so it passes regime and fails momentum. Widening 3→6 (v9.3.2) was   ║
║  not enough; 0.15%/3min is a large move for the calm trends in these books.  ║
║                                                                              ║
║  FIX: momentum now treats a trend as REAL when EITHER the regression R² over ║
║  its window ≥ MOMENTUM_R2_MIN (default 0.55) OR the magnitude clears         ║
║  MOMENTUM_THRESH_PCT — and takes DIRECTION from the regression slope, like   ║
║  compute_regime(). BTC is "flat"/NEUTRAL only when BOTH inconsistent (low    ║
║  R²) AND small (sub-threshold) — genuine chop the doctrine still rejects.    ║
║  Set MOMENTUM_R2_MIN=2.0 to restore pure-magnitude (v9.3.2) behavior.        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.3.2 — MOMENTUM WINDOW FIX: the AGREE gate was unsatisfiable, 0 trades.   ║
║                                                                              ║
║  DIAGNOSIS (2026-06-23 LIVE session, v9.3.1): ZERO trades fired all day.     ║
║  - compute_regime() flags TRENDING over TREND_LOOKBACK=12 samples (~6 min,   ║
║    R²≥0.65). compute_momentum() measured BTC over only prices[-1] vs [-4] —  ║
║    3 samples (~90s) — and required |move|≥0.15%. A clean ~6-min trend almost ║
║    never has a single 90s slice ≥0.15%, so momentum read NEUTRAL and the     ║
║    v9.3.0 AGREE gate rejected every setup.                                   ║
║  - Logs: 34 cycles had OB depth aligned with a real trend; momentum returned ║
║    NEUTRAL on ALL of them, AGREE/CONFLICT zero times. The gate was a wall.   ║
║                                                                              ║
║  FIX: momentum lookback is now MOMENTUM_LOOKBACK (default 6 ≈ 3 min), env-   ║
║  tunable. A genuine multi-minute trend now yields AGREE; flat BTC still      ║
║  reads NEUTRAL, so the doctrine intent ("never trade flat BTC") is intact —  ║
║  only the timescale momentum is measured over changed. Set MOMENTUM_LOOKBACK ║
║  =3 to restore the old window. MOMENTUM_THRESH_PCT (0.15%) unchanged.        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v9.3.1 — PHANTOM DAILY-LOSS FIX: open-position cash outlay halted on a WIN  ║
║                                                                              ║
║  DIAGNOSIS (2026-06-23 LIVE session, v9.3.0, single trade):                 ║
║  - 08:15:23 ORDER  NO -26JUN230430-30  169 @ 59c  $100.00  (cost $99.71).   ║
║    Fully doctrine-clean entry: TREND_DOWN R²=0.751, OB=74.2%, BTC=AGREE,    ║
║    Conf=68, WinP=78.9%. Exactly the kind of trade the doctrine permits.     ║
║  - 08:16:25 Portfolio  $1378.03  PnL=$-99.71  WR=0/0  — i.e. the open       ║
║    position's CASH OUTLAY (169 × $0.59 = $99.71) was reported as daily PnL  ║
║    while nothing had settled. Kalshi debits contract cost at fill, so       ║
║    (balance − session_start_balance) reads as a full-stake loss until the   ║
║    payout returns at settlement.                                           ║
║  - 08:30:30 DAILY LOSS  $99.71 ≥ cap $88.66 — halted.  (cap = 6% of equity, ║
║    which is CORRECT; the input was wrong.)                                  ║
║  - 08:30:31 SETTLED  WIN  +$69.29  WR=1/1.  The trade WON. The halt had     ║
║    latched one second earlier off the pre-settlement cash mark, then idled  ║
║    the bot until UTC rollover (~4.5h, 56 halt log lines).                   ║
║                                                                              ║
║  ROOT CAUSE — the LIVE daily-loss circuit breaker consumed an UNREALIZED    ║
║  cash-balance delta, not realized PnL. An open position is cash-out / zero- ║
║  marked, so any single in-flight trade ≥ the daily cap trips the breaker    ║
║  before it can settle. This is the mirror image of the v9.3.0 phantom-WIN   ║
║  fix in _extract_realized_dollars: same class of defect (unreconciled mark  ║
║  treated as realized), opposite sign.                                      ║
║                                                                              ║
║  FIX (accounting only — NO guardrail was loosened):                        ║
║    1. New accumulator live_daily_realized; resolve_open_orders() adds the   ║
║       reconciled _extract_realized_dollars() result of each MATCHED settled ║
║       trade to it. Open/unsettled positions contribute 0.                  ║
║    2. daily_loss_check() reads live_daily_realized in LIVE mode (was the    ║
║       balance−start cash delta). DEMO path (paper_daily_pnl) was already    ║
║       realized-only and unchanged.                                         ║
║    3. live_daily_realized resets with daily_pnl on UTC rollover and boot.   ║
║    4. Portfolio/heartbeat lines now report realized PnL as the PnL figure   ║
║       and show the cash delta separately as "cash=", so an open position    ║
║       can never again look like a daily loss in the logs.                  ║
║                                                                              ║
║  The 6% daily cap, $-dollar cap, SESSION_STOP_FRACTION, MAX_CONSEC_LOSSES,  ║
║  the AGREE/NEUTRAL gate, and OB/R²/confidence thresholds are UNCHANGED.     ║
║  No Railway env var changes are required for this fix.                     ║
║                                                                              ║
║  ─────────────────────────────────────────────────────────────────────     ║
║  v9.3.0 — DOCTRINE RESTORE: stop the NEUTRAL-momentum bleed                  ║
║                                                                              ║
║  DIAGNOSIS (2026-06-20→22 LIVE session, v9.2.0, ~2.7 days):                 ║
║  - 6 trades fired, ALL on BTC=NEUTRAL. Balance $1586.73 → ~$1396, WR 1/4+.  ║
║      06-20 08:00 NO @47c  TREND_DOWN  OB70.9% NEUTRAL  Edge"24.5%" $100      ║
║      06-20 08:30 NO @47c  TREND_DOWN  OB67.3% NEUTRAL  Edge"24.1%" $100 WIN  ║
║      06-21 08:02 YES@60c  TREND_UP    OB74.4% NEUTRAL  Edge"14.9%" $100 LOSS ║
║      06-21 08:31 NO @43c  TREND_DOWN  OB88.9% NEUTRAL  Edge"29.4%" $100      ║
║      06-21 09:00 YES@63c  TREND_UP    OB73.1% NEUTRAL  Edge"11.1%" $100 LOSS ║
║      06-22 08:32 NO @49c  TREND_DOWN  OB85.7% NEUTRAL  Edge"25.0%" $100 HALT ║
║                                                                              ║
║  ROOT CAUSE — three doctrine guards had drifted open (all from the v9.0.6   ║
║  "throughput" push, retained through v9.2.0). Together they manufacture a    ║
║  fake 25% edge on what is really a coin flip, then bet the full per-trade    ║
║  cap on it:                                                                  ║
║    1. run_decision() had NO NEUTRAL gate. Only CONFLICT was blocked; the     ║
║       v9.1.0 note "removed the RECOVERY AGREE gate" left ZERO momentum       ║
║       confirmation in ANY state. Trading on OB alone is doctrine "What This  ║
║       Bot Will Never Do" item 1 — the exact setup post-mortemed in v6.0.0    ║
║       (50% loss, 2026-03-27/28).                                            ║
║    2. NEUTRAL_ACCURACY_DRAG=0.0 → win_prob never discounted flat BTC, so a   ║
║       coin-flip market scored 0.72–0.75 (logs: mom=-0.000). "Edge" =         ║
║       win_prob − price was therefore fictional.                            ║
║    3. compute_confidence() gave NEUTRAL +8 pts. The 06-20 08:30 trade        ║
║       scored Conf=65 EXACTLY on mom=8.0; at the doctrine value of 2.0 it is  ║
║       59 < 65 and never trades.                                            ║
║                                                                              ║
║  FIX (restore, do not engineer around — zero-trade calm sessions are        ║
║  CORRECT per the doctrine):                                                 ║
║    1. momentum_gate_ok(): doctrine Layer 7. REQUIRE_AGREE_MOMENTUM (default  ║
║       true) rejects NEUTRAL and CONFLICT in EVERY session state. Applied in  ║
║       run_decision() right after the momentum verdict.                      ║
║    2. NEUTRAL_ACCURACY_DRAG default 0.0 → 0.02 (honest win_prob if the gate  ║
║       is ever disabled).                                                     ║
║    3. compute_confidence(): NEUTRAL 8.0 → 2.0 (doctrine Layer 8: momentum    ║
║       only scores when AGREE).                                              ║
║    4. Restore drifted thresholds to doctrine: OB_IMBALANCE_THRESH 0.64→0.70, ║
║       R2_TREND_THRESHOLD 0.62→0.65, MIN_CONFIDENCE 60→65,                    ║
║       YES_BREAKEVEN_PRICE 78→67.                                            ║
║                                                                              ║
║  The recovery deadlock that justified removing the AGREE gate is ALREADY     ║
║  solved independently by update_session_state()'s balance-heal exit and     ║
║  RECOVERY_MAX_SECS wall-clock backstop, so re-blocking NEUTRAL cannot        ║
║  relock recovery.                                                           ║
║                                                                              ║
║  RAILWAY ENV VAR CHANGES REQUIRED (an env override beats these defaults):    ║
║    - REQUIRE_AGREE_MOMENTUM : set true (or leave unset)                      ║
║    - NEUTRAL_ACCURACY_DRAG  : set 0.02 (or delete)                          ║
║    - OB_IMBALANCE_THRESH    : set 0.70 (or delete)                          ║
║    - R2_TREND_THRESHOLD     : set 0.65 (or delete)                          ║
║    - MIN_CONFIDENCE         : set 65   (or delete)                          ║
║    - YES_BREAKEVEN_PRICE    : set 67   (or delete)                          ║
║                                                                              ║
║  ─────────────────────────────────────────────────────────────────────     ║
║  v9.1.0 — RECOVERY DEADLOCK (real fix) + RISK TIGHTENING                     ║
║                                                                              ║
║  DIAGNOSIS (2026-06-18 LIVE session, v9.0.9, 3.5h slice, ZERO trades):      ║
║  - Status byte-identical all window:                                        ║
║      $1722.52 │ PnL=$-246.87 │ WR=1/4 │ RECOVERY (rec+1)                     ║
║  - Drawdown 12.5% (> 10% trigger). v9.0.9's balance-heal exit needs the     ║
║    drawdown to recover to ≤10%, but the drawdown cannot heal without        ║
║    trading, and the AGREE gate blocks every NEUTRAL-momentum scan. The      ║
║    v9.0.7/8/9 patches each fixed a symptom; the self-referential lock       ║
║    survived at any drawdown that did not pre-heal below the trigger.        ║
║                                                                              ║
║  FIX (deadlock):                                                            ║
║  1. RECOVERY no longer FORCES momentum==AGREE as a *recovery-only* extra     ║
║     gate (the doctrine Layer-7 AGREE requirement now applies uniformly to    ║
║     every state via momentum_gate_ok, so recovery is not special-cased).     ║
║  2. RECOVERY_MAX_SECS hard timeout in update_session_state() — force back    ║
║     to ACTIVE if recovery cannot clear in the window. The state machine      ║
║     can no longer lock permanently.                                        ║
║                                                                              ║
║  FIX (risk — a normal 1W/4L streak cost 12.5% of bankroll):                 ║
║  3. MAX_BET_FRACTION 0.08 → 0.04 (cap a single binary bet at 4% of bank).   ║
║  4. MAX_DAILY_LOSS_PCT (6%) — daily stop now halts on the tighter of the    ║
║     fixed dollar cap and a fraction of the session-start balance, so the    ║
║     mis-scaled $15 default can no longer be silently out-scaled.            ║
║                                                                              ║
║  ─────────────────────────────────────────────────────────────────────     ║
║  v9.0.8 — PERF-GUARD DEADLOCK FIX (boot-time settlement gate)               ║
║  Account-wide settlement history was counted toward live W/L with no time   ║
║  gate, seeding a sub-50% Wilson LB the bot could never escape. _is_post_boot ║
║  now gates the unmatched-settlement branch to records settled at/after boot. ║
║                                                                              ║
║  v9.0.7 — SETTLEMENT SCHEMA CORRECTED                                        ║
║  _extract_realized_dollars rewritten against the real KXBTC15M schema:       ║
║    pnl = (revenue/100) - yes_total_cost_dollars - no_total_cost_dollars      ║
║          - fee_cost                                                           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

BOT_VERSION = "9.6.0"

import base64
import json
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
# v9.5.0: two-tier sizing driven by a persistent Recovery Mode (see RecoveryState
# below). The ACTIVE stake is derived from the current mode, never read raw from
# a single env var at the sizing call:
#   • NORMAL_TRADE_SIZE   — the full stake in normal operation.
#   • RECOVERY_TRADE_SIZE — the reduced stake while clawing back a full-size loss.
# NORMAL_TRADE_SIZE falls back to the legacy TRADE_SIZE_DOLLARS so existing
# Railway configs keep working unchanged.
NORMAL_TRADE_SIZE   = _env_float("NORMAL_TRADE_SIZE", _env_float("TRADE_SIZE_DOLLARS", 5.0))
RECOVERY_TRADE_SIZE = _env_float("RECOVERY_TRADE_SIZE", 100.0)
# v9.1.0: 0.08 → 0.04. At 8% of bankroll per binary bet, an ordinary 4-loss
# streak costs ~12.5% of the account in one session (2026-06-18: −$246.87 on
# 1W/4L). Halving the per-bet fraction bounds a cold-streak session.
MAX_BET_FRACTION    = _env_float("MAX_BET_FRACTION", 0.04)
KELLY_FRACTION      = _env_float("KELLY_FRACTION", 0.30)
KELLY_RECOVERY_MULT = _env_float("KELLY_RECOVERY_MULT", 0.50)

# ── Recovery Mode persistence ─────────────────────────────────────────────────
# Where the recovery state (active flag + target balance) is written so it
# survives an in-container process restart. NOTE: Railway's container filesystem
# is ephemeral across REDEPLOYS — mount a Railway Volume and point
# RECOVERY_STATE_PATH at it (e.g. /data/recovery_state.json) for the state to
# survive a redeploy. Without a Volume, a redeploy resets to NORMAL sizing
# (boot reconciliation makes this a safe, non-stuck default).
RECOVERY_STATE_PATH = os.environ.get("RECOVERY_STATE_PATH", "recovery_state.json")
RECOVERY_PERSIST    = _env_bool("RECOVERY_PERSIST", True)


# ── Laddering stake overlay (opt-in) ──────────────────────────────────────────
# Scales the Kelly stake by a performance-driven multiplier (0.5x–2x). Disabled
# by default so live sizing is unchanged until explicitly switched on with
# LADDER_ENABLED=true. See ladder.py and LADDER_STRATEGY.md.
LADDER_ENABLED = _env_bool("LADDER_ENABLED", False)

# After a recovery-mode exit (sizing returns to NORMAL_TRADE_SIZE), suppress the
# ladder's win-rate size-up for this many settled trades — win or loss — so the
# ladder re-proves the edge on fresh data before it can scale the stake above
# NORMAL_TRADE_SIZE again. Downside guardrails (loss-streak demote, drawdown)
# stay active throughout. Set 0 to disable the pause. No effect unless the ladder
# is enabled.
RECOVERY_LADDER_PAUSE_TRADES = _env_int("RECOVERY_LADDER_PAUSE_TRADES", 5)

# ── Daily doubling goal (opt-in — v9.6.0) ─────────────────────────────────────
# When DAILY_GOAL_ENABLED, the bot targets DOUBLING the balance it started the
# UTC day with, then HALTS new entries for the rest of that day (settlement of
# any open trade still proceeds). At the next UTC rollover the new (higher)
# balance becomes the baseline and the 2× target resets — so the goal compounds
# day over day with no intervention.
#
# Sizing is GOAL-GAP driven (see daily_goal_stake): each trade is the remaining
# gap to today's target spread over DAILY_GOAL_TRADES_TO_TARGET expected winning
# trades, clamped to [DAILY_GOAL_MIN_STAKE, fraction-of-balance cap, optional
# hard dollar cap, cash on hand]. This auto-scales as the account compounds and
# shrinks as the gap closes through the day — no fixed dollar cap to babysit.
#
# Defaults are the "Balanced" risk profile (owner directive): ~5 winning trades
# to the target, each trade capped at 40% of the live balance. The Kelly edge
# gate and ALL entry-quality gates remain UNCHANGED — goal mode changes the
# STAKE and adds a daily target/halt; it never relaxes which trades qualify.
#
# Disabled by default so live behaviour is unchanged until DAILY_GOAL_ENABLED is
# set true on Railway. When goal mode is on it OWNS sizing (it overrides the
# flat NORMAL/RECOVERY stake and the ladder overlay for that trade).
DAILY_GOAL_ENABLED            = _env_bool("DAILY_GOAL_ENABLED", False)
DAILY_GOAL_MULTIPLE           = _env_float("DAILY_GOAL_MULTIPLE", 2.0)
DAILY_GOAL_TRADES_TO_TARGET   = _env_int("DAILY_GOAL_TRADES_TO_TARGET", 5)
DAILY_GOAL_MIN_STAKE          = _env_float("DAILY_GOAL_MIN_STAKE", 1.0)
# Operating per-trade ceiling = this fraction of the LIVE balance (scales hands-
# off as the account doubles). 0 disables the fraction cap (then only the hard
# dollar cap / cash on hand bind).
DAILY_GOAL_MAX_STAKE_FRACTION = _env_float("DAILY_GOAL_MAX_STAKE_FRACTION", 0.40)
# Optional ABSOLUTE hard dollar ceiling on a single goal-mode trade. 0 = no hard
# cap (recommended — let the % cap scale). Set >0 only if you want a fixed safety
# ceiling, but note it must be raised as the balance compounds or it will throttle.
DAILY_GOAL_MAX_STAKE          = _env_float("DAILY_GOAL_MAX_STAKE", 0.0)
# Persist today's goal baseline/target so an in-container restart RESUMES the
# same day's target instead of re-baselining to the restart-time balance (which
# would move the goalpost). Same Railway caveat as recovery: mount a Volume and
# point DAILY_GOAL_STATE_PATH at it for the state to survive a redeploy; without
# one, a redeploy re-baselines to the current balance (a safe default).
DAILY_GOAL_STATE_PATH         = os.environ.get("DAILY_GOAL_STATE_PATH", "daily_goal_state.json")
DAILY_GOAL_PERSIST            = _env_bool("DAILY_GOAL_PERSIST", True)

# ── Risk controls ─────────────────────────────────────────────────────────────
MIN_BALANCE_FLOOR     = _env_float("MIN_BALANCE_FLOOR", 5.0)
MAX_DAILY_LOSS        = _env_float("MAX_DAILY_LOSS_DOLLARS", 15.0)
# v9.1.0: percentage-based daily stop. The fixed $15 cap is mis-scaled for
# anything but a tiny paper account — on a ~$1969 bankroll it never bound, so
# the session bled to −$246.87 (12.5%) before RECOVERY froze it. Halt when the
# session drawdown exceeds the dollar cap OR this fraction of the start balance,
# whichever binds first.
MAX_DAILY_LOSS_PCT    = _env_float("MAX_DAILY_LOSS_PCT", 0.06)
SESSION_STOP_FRACTION = _env_float("SESSION_STOP_FRACTION", 0.40)
MAX_CONSEC_LOSSES     = _env_int("MAX_CONSEC_LOSSES", 2)
STREAK_PAUSE_SECS     = _env_int("STREAK_PAUSE_SECS", 1800)
STALE_ORDER_TIMEOUT   = _env_int("STALE_ORDER_TIMEOUT", 300)
MAX_CONCURRENT_POS    = _env_int("MAX_CONCURRENT_POS", 1)
MIN_SAMPLE_TRADES     = _env_int("MIN_SAMPLE_TRADES", 20)

# ── Regime detection ──────────────────────────────────────────────────────────
# v9.3.0: restored 0.62 → 0.65 (doctrine §8). 0.62 was a v9.0.6 throughput
# relaxation; at 0.65 about 65% of price variance must be explained by the
# straight-line fit before the market counts as TRENDING.
R2_TREND_THRESHOLD    = _env_float("R2_TREND_THRESHOLD", 0.65)
VOLATILITY_CAP_PCT    = _env_float("VOLATILITY_CAP_PCT", 0.18)
VOL_CIRCUIT_BREAKER   = _env_float("VOL_CIRCUIT_BREAKER", 0.40)
TREND_LOOKBACK        = _env_int("TREND_LOOKBACK", 12)
MIN_PRICES_FOR_REGIME = _env_int("MIN_PRICES_FOR_REGIME", 10)

# ── Signal thresholds ─────────────────────────────────────────────────────────
# v9.3.0: OB_IMBALANCE_THRESH restored 0.64 → 0.70 (doctrine Layer 6).
# MIN_CONFIDENCE restored 60 → 65 (doctrine Layer 8). YES_BREAKEVEN_PRICE
# restored 78 → 67 (doctrine §3 — never pay past mathematical breakeven).
MIN_OB_DEPTH          = _env_float("MIN_OB_DEPTH_DOLLARS", 75.0)
OB_IMBALANCE_THRESH   = _env_float("OB_IMBALANCE_THRESH", 0.70)
MOMENTUM_THRESH_PCT   = _env_float("MOMENTUM_THRESH_PCT", 0.15)
# v9.3.2: momentum lookback (intervals back). The old fixed 3-sample (~90s)
# window measured a far shorter horizon than the regime's TREND_LOOKBACK (~6 min)
# trend it was meant to confirm, so genuine trends read NEUTRAL and the AGREE
# gate blocked EVERY trade (2026-06-23: 0 trades all day). ~6 intervals (~3 min
# at the 30s poll) confirms real trends without firing on 90s chop. Set to 3 to
# restore the pre-9.3.2 window.
MOMENTUM_LOOKBACK     = _env_int("MOMENTUM_LOOKBACK", 6)
# v9.3.3: a trend is "real" for momentum when EITHER the linear regression over
# the momentum window is consistent (local R² ≥ this) OR the magnitude clears
# MOMENTUM_THRESH_PCT. This aligns momentum's trend test with compute_regime's
# R²-based one: a smooth, gentle BTC drift has high R² but a small %-move, so the
# pure-magnitude gate kept mislabeling it NEUTRAL and blocked EVERY trade even
# after v9.3.2 (2026-06-23/24: 0 trades). Set to 2.0 to disable the R² path and
# restore pure-magnitude (pre-9.3.3) behavior.
MOMENTUM_R2_MIN       = _env_float("MOMENTUM_R2_MIN", 0.55)
MIN_EDGE_PCT          = _env_float("MIN_EDGE_PCT", 0.06)
MIN_CONFIDENCE        = _env_int("MIN_CONFIDENCE", 65)
MIN_WIN_PROB          = _env_float("MIN_WIN_PROB", 0.60)
MIN_MINUTES_TO_EXPIRY = _env_float("MIN_MINUTES_TO_EXPIRY", 6.0)
YES_BREAKEVEN_PRICE   = _env_int("YES_BREAKEVEN_PRICE", 67)

# v9.3.0: DOCTRINE LAYER 7 — BTC momentum must explicitly AGREE with the OB
# direction. NEUTRAL (flat BTC) and CONFLICT are both rejections. Default ON.
# Setting this false re-enables the unconfirmed-OB experiment that produced the
# 2026-06-20→22 bleed and the 2026-03-27/28 50% loss — only do so deliberately.
REQUIRE_AGREE_MOMENTUM = _env_bool("REQUIRE_AGREE_MOMENTUM", True)

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
# v9.3.0: restored 0.0 → 0.02. NEUTRAL BTC is NOT "no evidence" for a directional
# 15-min binary — it means the confirming signal is ABSENT, so the OB-only prior
# must be discounted. With the Layer-7 gate above, NEUTRAL no longer reaches the
# win-prob path at all; this keeps win_prob honest if the gate is disabled.
NEUTRAL_ACCURACY_DRAG  = _env_float("NEUTRAL_ACCURACY_DRAG", 0.02)

# ── Recovery protocol ─────────────────────────────────────────────────────────
RECOVERY_TRIGGER_PCT  = _env_float("RECOVERY_TRIGGER_PCT", 0.10)
RECOVERY_EXIT_TRADES  = _env_int("RECOVERY_EXIT_TRADES", 5)
RECOVERY_WIN_RATE_MIN = _env_float("RECOVERY_WIN_RATE_MIN", 0.60)
# v9.1.0: hard wall-clock backstop. If recovery cannot clear via the trade-count
# or balance-heal exits within this window, force-return to ACTIVE so the state
# machine can never permanently lock itself out of trading again.
RECOVERY_MAX_SECS     = _env_int("RECOVERY_MAX_SECS", 3600)


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
live_daily_realized:    float = 0.0   # v9.3.1: realized-only $ that feeds the LIVE daily-loss breaker
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

# Daily doubling goal (v9.6.0). Baseline + 2× target are set on boot and re-set
# at each UTC rollover; _daily_goal_reached latches the per-day halt (and is
# cleared at rollover) so the "goal reached" alert fires once per day.
daily_goal_start_balance: float = 0.0
daily_goal_target:        float = 0.0
_daily_goal_reached:      bool  = False

_prev_ob: dict = {}

_vol_circuit_open:  bool  = False
_vol_circuit_until: float = 0.0

_live_prior: float = OB_BASE_ACCURACY

# Laddering stake overlay — only instantiated when LADDER_ENABLED. Sized as a
# multiplier on top of the Kelly stake; respects every existing cap.
stake_ladder: Optional[StakeLadder] = StakeLadder() if LADDER_ENABLED else None


# ─────────────────────────────────────────────────────────────────────────────
# RECOVERY MODE  (two-tier position sizing — v9.5.0)
#
# After a FULL-SIZE (normal-mode) trade settles a LOSS, the bot drops to a
# reduced RECOVERY_TRADE_SIZE until the account balance climbs back to where it
# was IMMEDIATELY BEFORE that losing trade. Then it returns to NORMAL_TRADE_SIZE
# automatically. The state (active flag + recovery target balance) is persisted
# to disk so an in-container restart resumes mid-recovery.
#
# Design guarantees (see also the edge-case notes in TRADING_DOCTRINE.md §6):
#   • Entry is event-driven (a settled full-size loss), so the target is the
#     exact recorded pre-trade balance — never reconstructed from PnL.
#   • Exit is balance-driven and checked every cycle AND on boot, so the bot can
#     never wedge in recovery once balance reaches the target even once.
#   • enter() is a no-op while already active → a further loss never moves the
#     target (the goal stays the original pre-loss balance).
#   • Single-threaded loop → mutate-then-persist is atomic w.r.t. sizing reads;
#     there is no race and no per-cycle oscillation (entry needs a settlement).
# This `recovery` object is mutated IN-PLACE only and must NEVER be reassigned
# (so it never appears in a function `global` statement — same rule as the other
# module-level mutable containers above).
# ─────────────────────────────────────────────────────────────────────────────

class RecoveryState:
    """Persistent two-tier sizing mode. Owns {active, target_balance}."""

    SCHEMA = 1

    def __init__(self, path: str, persist: bool) -> None:
        self.active:         bool  = False
        self.target_balance: float = 0.0
        self._path    = path
        self._persist = persist
        if self._persist:
            self._load()

    # ── transitions ──────────────────────────────────────────────────────────
    def enter(self, target_balance: float, current_balance: float) -> bool:
        """Activate recovery with the given target. No-op if already active or
        the target is not a usable, not-already-met value. Returns True on a
        real activation."""
        if self.active:
            return False
        if target_balance is None or target_balance <= 0.0:
            return False
        # If we are somehow already at/above the target, there is nothing to
        # recover — stay in normal mode rather than enter-then-instantly-exit.
        if current_balance >= target_balance:
            return False
        self.active         = True
        self.target_balance = round(float(target_balance), 2)
        self._save()
        log.warning("Recovery mode ACTIVATED after losing full-size trade.")
        log.warning("Previous balance: $%.2f", self.target_balance)
        log.warning("Recovery target: $%.2f", self.target_balance)
        log.warning("Switching trade size to: $%.2f", RECOVERY_TRADE_SIZE)
        tg.send_telegram_message(
            f"🛟 RECOVERY MODE ACTIVATED\n"
            f"Recovery target: ${self.target_balance:.2f}\n"
            f"Trade size → ${RECOVERY_TRADE_SIZE:.2f} (was ${NORMAL_TRADE_SIZE:.2f})"
        )
        return True

    def maybe_exit(self, current_balance: float) -> bool:
        """Deactivate recovery once balance has recovered to the target. Checked
        every cycle and on boot. Returns True on a real deactivation."""
        if not self.active:
            return False
        if current_balance < self.target_balance:
            return False
        reached = self.target_balance
        self.active         = False
        self.target_balance = 0.0
        self._save()
        log.warning("Recovery target reached.")
        log.warning("Recovery mode DEACTIVATED.")
        log.warning("Switching trade size back to: $%.2f", NORMAL_TRADE_SIZE)
        msg = (f"✅ RECOVERY COMPLETE — balance ${current_balance:.2f} ≥ target "
               f"${reached:.2f}\nTrade size → ${NORMAL_TRADE_SIZE:.2f}")
        # Make the ladder re-prove the edge on fresh data: hold its win-rate
        # size-up at baseline for the next RECOVERY_LADDER_PAUSE_TRADES trades
        # before it can scale the stake above NORMAL_TRADE_SIZE again.
        if stake_ladder is not None and RECOVERY_LADDER_PAUSE_TRADES > 0:
            stake_ladder.pause_size_up(RECOVERY_LADDER_PAUSE_TRADES)
            msg += (f"\nLadder size-up paused for "
                    f"{RECOVERY_LADDER_PAUSE_TRADES} trades.")
        tg.send_telegram_message(msg)
        return True

    def reconcile_on_boot(self, current_balance: float) -> None:
        """Self-heal persisted state at startup so the bot can never resume into
        a stuck or nonsensical recovery."""
        if not self.active:
            return
        if self.target_balance <= 0.0:
            log.warning("Recovery boot │ corrupt target $%.2f — clearing.",
                        self.target_balance)
            self.active = False
            self._save()
            return
        if current_balance >= self.target_balance:
            log.info("Recovery boot │ balance $%.2f already ≥ target $%.2f — "
                     "exiting recovery.", current_balance, self.target_balance)
            self.maybe_exit(current_balance)
            return
        log.warning("Recovery boot │ RESUMING recovery. Balance $%.2f, target "
                    "$%.2f, trade size $%.2f.",
                    current_balance, self.target_balance, RECOVERY_TRADE_SIZE)

    def status_line(self, current_balance: float) -> str:
        return (f"Recovery mode active. Current balance: ${current_balance:.2f}. "
                f"Target: ${self.target_balance:.2f}. "
                f"Trade size: ${RECOVERY_TRADE_SIZE:.2f}.")

    # ── persistence (atomic JSON write) ────────────────────────────────────────
    def _save(self) -> None:
        if not self._persist:
            return
        try:
            tmp = f"{self._path}.tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "schema":         self.SCHEMA,
                    "active":         self.active,
                    "target_balance": self.target_balance,
                }, f)
            os.replace(tmp, self._path)   # atomic on POSIX
        except OSError as e:
            log.warning("Recovery │ state save failed: %s", e)

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.active         = bool(d.get("active", False))
        self.target_balance = float(d.get("target_balance", 0.0) or 0.0)


recovery = RecoveryState(RECOVERY_STATE_PATH, RECOVERY_PERSIST)


def active_trade_size() -> float:
    """The dollar stake for the current mode. Single source of truth for sizing
    — every position-sizing path derives from this, not from a raw env var."""
    return RECOVERY_TRADE_SIZE if recovery.active else NORMAL_TRADE_SIZE


def on_trade_settled(won: bool, trade_rec: dict, current_balance: float) -> None:
    """Recovery ENTRY hook, called once per settled trade. Activates recovery
    only when a normal-mode (full-size) trade loses and we are not already in
    recovery; the target is the balance recorded just before that trade."""
    if won or recovery.active:
        return
    if (trade_rec or {}).get("mode_at_entry") != "normal":
        return  # recovery-mode losses and un-attributable trades never trigger
    recovery.enter((trade_rec or {}).get("balance_before"), current_balance)


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
    if len(btc_prices) < MOMENTUM_LOOKBACK + 1:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    window  = list(btc_prices)[-(MOMENTUM_LOOKBACK + 1):]
    earlier = window[0]
    recent  = window[-1]
    if earlier <= 0:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    move_pct = (recent - earlier) / earlier * 100.0
    slope, _, local_r2 = _linear_regression(window)
    ob_dir   = ob_direction.upper()

    # v9.3.3: a trend is REAL when BTC moves CONSISTENTLY (regression R² over the
    # momentum window) OR far enough in MAGNITUDE. compute_regime() flags TRENDING
    # by R² alone, so a smooth, gentle drift (high R², small %-move) is genuinely
    # trending — but the old pure-magnitude test mislabeled it NEUTRAL and the
    # AGREE gate blocked every trade (2026-06-23/24: 0 trades). BTC is "flat" only
    # when it is BOTH inconsistent (low R²) AND small (sub-threshold) — the chop
    # the doctrine rejects. This keeps that guarantee while confirming real trends.
    is_trending = (local_r2 >= MOMENTUM_R2_MIN) or (abs(move_pct) >= MOMENTUM_THRESH_PCT)
    if not is_trending:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    # Direction from the regression slope (consistent with compute_regime), with
    # the endpoint delta as a tiebreaker when the slope is exactly flat.
    if slope > 0 or (slope == 0 and move_pct > 0):
        btc_dir = "YES"
    elif slope < 0 or (slope == 0 and move_pct < 0):
        btc_dir = "NO"
    else:
        return "NEUTRAL", -NEUTRAL_ACCURACY_DRAG

    if btc_dir == ob_dir:
        magnitude_scale = min(2.0, abs(move_pct) / MOMENTUM_THRESH_PCT)
        return "AGREE", MOMENTUM_ACCURACY_LIFT * magnitude_scale

    return "CONFLICT", 0.0


def momentum_gate_ok(momentum_verdict: str) -> bool:
    """DOCTRINE LAYER 7 — the AGREE-required momentum gate.

    A trade requires BTC spot momentum to EXPLICITLY AGREE with the order-book
    direction. When REQUIRE_AGREE_MOMENTUM is true (default), both CONFLICT and
    NEUTRAL are rejections.

    NEUTRAL = flat BTC = no directional confirmation. Trading on the order book
    alone is doctrine "What This Bot Will Never Do" item 1 — the exact setup
    post-mortemed in v6.0.0 (50% loss, 2026-03-27/28) and the cause of the
    2026-06-20→22 bleed, in which all 6 trades fired on BTC=NEUTRAL.

    Applies in EVERY session state. RECOVERY is not exempt: its deadlock is
    resolved by update_session_state()'s balance-heal exit and RECOVERY_MAX_SECS
    wall-clock backstop, not by trading unconfirmed setups. A calm, all-NEUTRAL
    session producing zero trades is correct behaviour.
    """
    if not REQUIRE_AGREE_MOMENTUM:
        return True
    return momentum_verdict == "AGREE"


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


def regime_direction(regime: Regime) -> Optional[str]:
    """Map a trend regime to the contract side it favors.

    For these BTC "above-strike" markets a rising price (TRENDING_UP) settles
    YES and a falling price (TRENDING_DOWN) settles NO. Non-directional regimes
    (RANGING/HIGH_VOL/UNKNOWN) return None and never reach this check because
    run_decision already gates them out.
    """
    if regime == Regime.TRENDING_UP:
        return "YES"
    if regime == Regime.TRENDING_DOWN:
        return "NO"
    return None


def regime_agrees(regime: Regime, ob_direction: str) -> bool:
    """The order-book side must point the same way as the measured trend.

    The single largest source of losses (2026-06-19: both losing trades) was
    betting the order-book imbalance *against* the regression trend — NO in an
    uptrend, YES in a downtrend. Order-book imbalance on thin 15-minute crypto
    markets is a weak, often contrarian signal; the regression trend is the real
    driver of where price settles. When they conflict, stand aside.
    """
    favored = regime_direction(regime)
    return favored is None or favored == ob_direction.upper()


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

    # v9.2.0: the order-book imbalance is the bot's primary directional signal,
    # yet it never fed the win-probability — win_prob was a near-constant ~0.68
    # every scan, so "edge" was driven purely by how cheap the contract was (a
    # 36c against-trend YES scored a fake 31% edge on 2026-06-19). Reward genuine
    # book dominance over the trigger threshold so a marginally-imbalanced book
    # scores lower than a lopsided one. Capped so it cannot dominate the prior.
    eff_thresh    = ob.get("eff_thresh", OB_IMBALANCE_THRESH)
    imbalance_adj = min(0.06, max(0.0, ob["imbalance"] - eff_thresh) * 0.30)

    depth_adj = 0.0
    if ob["total_depth"] > 500:
        depth_adj = min(0.02, math.log10(ob["total_depth"] / 500) * 0.02)

    vol_penalty = min(0.04, realized_vol / VOLATILITY_CAP_PCT * 0.04)

    win_prob = max(0.50, min(0.92,
        prior + momentum_adj + regime_adj + imbalance_adj + depth_adj - vol_penalty
    ))

    log.info("WinProb │ prior=%.3f mom=%.3f regime=%.3f imb=%.3f depth=%.3f vol=-%.3f → %.3f",
             prior, momentum_adj, regime_adj, imbalance_adj, depth_adj, vol_penalty, win_prob)
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

    # v9.3.0: NEUTRAL restored 8.0 → 2.0 (doctrine Layer 8: BTC momentum "only
    # counts if AGREE"). The v9.0.6 bump to 8.0 was the third leg of the
    # NEUTRAL bleed: the 2026-06-20 08:30 trade scored Conf=65 EXACTLY on
    # mom=8.0; at 2.0 it is 59 < 65 and never trades. With momentum_gate_ok
    # this is belt-and-suspenders, but it keeps the score truthful.
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


def daily_goal_stake(balance: float) -> float:
    """Goal-gap sizing for the daily-doubling mode (v9.6.0).

    Sizes a trade as the remaining gap to today's 2× target, spread over
    DAILY_GOAL_TRADES_TO_TARGET expected winning trades, then clamped to:
        • a floor of DAILY_GOAL_MIN_STAKE,
        • a ceiling of DAILY_GOAL_MAX_STAKE_FRACTION × balance (scales hands-off),
        • an optional hard dollar ceiling DAILY_GOAL_MAX_STAKE (0 = disabled),
        • the cash on hand (can never stake more than the account holds).

    Returns 0.0 when the target is already met (the daily-goal halt blocks entry
    in that case anyway) or when the gap rounds below a placeable amount.
    """
    gap = daily_goal_target - balance
    if gap <= 0.0:
        return 0.0
    n   = max(1, DAILY_GOAL_TRADES_TO_TARGET)
    raw = gap / n

    cap = balance
    if DAILY_GOAL_MAX_STAKE_FRACTION > 0.0:
        cap = min(cap, DAILY_GOAL_MAX_STAKE_FRACTION * balance)
    if DAILY_GOAL_MAX_STAKE > 0.0:
        cap = min(cap, DAILY_GOAL_MAX_STAKE)

    stake = min(raw, cap)
    # Apply the floor, but never stake more than the cash on hand.
    stake = max(stake, min(DAILY_GOAL_MIN_STAKE, balance))
    stake = min(stake, balance)
    return round(stake, 2)


def kelly_bet(win_prob: float, contract_price_cents: int, balance: float) -> float:
    if contract_price_cents <= 0 or contract_price_cents >= 100:
        return 0.0
    b          = (100 - contract_price_cents) / float(contract_price_cents)
    full_kelly = max(0.0, (b * win_prob - (1.0 - win_prob)) / b)
    # v9.4.1 (owner directive): FLAT stake. Kelly is used ONLY as an edge gate —
    # a positive full_kelly means the bet has positive expectancy. The stake size
    # itself is the full active trade size regardless of balance (no Kelly or
    # MAX_BET_FRACTION down-scaling), so trades fire at any bankroll. The only
    # clamp is the cash on hand: you cannot stake more than the account holds.
    # v9.5.0: the stake is derived from the current mode via active_trade_size()
    # (NORMAL_TRADE_SIZE normally, RECOVERY_TRADE_SIZE while in recovery).
    if full_kelly <= 0.0:
        return 0.0

    # v9.6.0: daily-doubling mode OWNS sizing — goal-gap stake overrides the flat
    # NORMAL/RECOVERY size and the ladder overlay. The Kelly edge gate above still
    # decides IF a trade exists; this only sets how much. The daily-goal halt
    # (daily_goal_check) blocks entry once the target is met.
    if DAILY_GOAL_ENABLED:
        return daily_goal_stake(balance)

    size     = active_trade_size()
    base_bet = round(min(size, balance), 2)

    # Laddering overlay (opt-in). Scales the flat stake by a performance
    # multiplier, but never past 2× the active trade size or the cash on hand.
    if stake_ladder is not None:
        ceiling  = min(stake_ladder.cfg.max_multiplier * size, balance)
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
    # RECOVERY mode (the 10% drawdown state that halved Kelly sizing) was removed
    # by owner directive so that drawdown never shrinks the $500 stake. The
    # session stays ACTIVE; the only auto-hold is the consecutive-loss streak
    # pause, and the 40% session-stop remains as a catastrophic backstop. HALTED,
    # if ever set, is left untouched.
    return


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
    global live_daily_realized

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
            # Recovery ENTRY hook: a full-size loss arms recovery (uses this
            # trade's recorded pre-trade balance as the target). paper_balance is
            # already updated above for this settlement.
            on_trade_settled(won, trade, paper_balance)

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
            # v9.3.1: realized-only daily accumulator for the daily-loss breaker.
            # `pnl` is the reconciled _extract_realized_dollars() result for a
            # MATCHED, SETTLED trade — never an open-position mark.
            live_daily_realized += pnl
            # Display value for Telegram alerts: report the realized daily total
            # (same figure the breaker uses), not the cash-balance delta.
            live_daily_pnl = live_daily_realized

            if won:
                consecutive_losses = 0
                live_wins += 1
            else:
                consecutive_losses += 1
                live_losses += 1
                if consecutive_losses >= MAX_CONSEC_LOSSES:
                    streak_pause_until = time.time() + STREAK_PAUSE_SECS

            ladder_record(won, pnl)
            # Recovery ENTRY hook: `balance` was fetched (realized) above for
            # this settled trade.
            on_trade_settled(won, trade, balance)

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
                _delete(f"/portfolio/events/orders/{oid}")
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

def daily_loss_check(balance: float) -> bool:
    # Daily-loss governors (the % and $ caps) and the balance floor were removed
    # by owner directive: the only active auto-hold is the consecutive-loss
    # streak pause (streak_check). The 40% session-stop is retained as a
    # catastrophic backstop only.
    global _session_halted
    if _session_halted:
        return False
    if session_stop_threshold > 0 and balance < session_stop_threshold:
        _session_halted = True
        log.warning("SESSION STOP │ $%.2f < $%.2f — halted.", balance, session_stop_threshold)
        telegram_halt(f"Session stop at ${balance:.2f}", balance)
        return False
    return True


def daily_goal_check(balance: float) -> bool:
    """Daily-doubling backstop (v9.6.0). Returns False (block NEW entries) once
    the day's 2× target is reached; the latch is cleared at the UTC rollover so
    trading auto-resumes the next day against the new, higher target. Open trades
    still settle while halted. No-op unless DAILY_GOAL_ENABLED.
    """
    global _daily_goal_reached
    if not DAILY_GOAL_ENABLED or daily_goal_target <= 0.0:
        return True
    if balance >= daily_goal_target:
        if not _daily_goal_reached:
            _daily_goal_reached = True
            _save_daily_goal()
            log.info("🎯 DAILY GOAL │ $%.2f ≥ target $%.2f — halting new entries "
                     "until UTC rollover.", balance, daily_goal_target)
            tg.send_telegram_message(
                f"🎯 DAILY GOAL REACHED\n"
                f"Balance ${balance:.2f} ≥ target ${daily_goal_target:.2f} "
                f"(start ${daily_goal_start_balance:.2f})\n"
                f"Trading paused until the next UTC day."
            )
        return False
    return True


def _save_daily_goal() -> None:
    """Atomically persist today's goal state so a restart resumes the same day."""
    if not (DAILY_GOAL_ENABLED and DAILY_GOAL_PERSIST):
        return
    try:
        tmp = f"{DAILY_GOAL_STATE_PATH}.tmp"
        with open(tmp, "w") as f:
            json.dump({
                "day":           _session_day,
                "start_balance": daily_goal_start_balance,
                "target":        daily_goal_target,
                "reached":       _daily_goal_reached,
            }, f)
        os.replace(tmp, DAILY_GOAL_STATE_PATH)   # atomic on POSIX
    except OSError as e:
        log.warning("Daily goal │ state save failed: %s", e)


def set_daily_goal(balance: float) -> None:
    """(Re)baseline the daily-doubling target to `balance` and clear the per-day
    goal halt. Called at each UTC rollover so the 2× target compounds off each
    day's opening balance. Safe to call when goal mode is disabled — both
    daily_goal_check() and kelly_bet() gate on DAILY_GOAL_ENABLED.
    """
    global daily_goal_start_balance, daily_goal_target, _daily_goal_reached
    daily_goal_start_balance = round(balance, 2)
    daily_goal_target        = round(balance * DAILY_GOAL_MULTIPLE, 2)
    _daily_goal_reached      = False
    _save_daily_goal()


def reconcile_daily_goal_on_boot(balance: float) -> None:
    """Restore today's persisted goal target on boot so an in-container restart
    does not move the goalpost; if there is no state for the current UTC day,
    set a fresh target off the current balance. No-op when goal mode is off.
    """
    global daily_goal_start_balance, daily_goal_target, _daily_goal_reached
    if not DAILY_GOAL_ENABLED:
        return
    today = _session_day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if DAILY_GOAL_PERSIST:
        try:
            with open(DAILY_GOAL_STATE_PATH) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = None
        if d and d.get("day") == today and float(d.get("target", 0) or 0) > 0:
            daily_goal_start_balance = float(d.get("start_balance", balance) or balance)
            daily_goal_target        = float(d["target"])
            _daily_goal_reached      = bool(d.get("reached", False))
            log.info("🎯 Daily goal boot │ RESUMING today's target $%.2f "
                     "(start $%.2f, reached=%s).",
                     daily_goal_target, daily_goal_start_balance, _daily_goal_reached)
            return
    set_daily_goal(balance)
    log.info("🎯 Daily goal boot │ new target $%.2f (start $%.2f).",
             daily_goal_target, daily_goal_start_balance)


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
                limit_cents: int, win_prob: float, edge: float,
                balance_before: float = 0.0) -> Optional[str]:
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
    # v9.5.0: stamp the sizing mode + the realized balance immediately BEFORE
    # this trade so settlement can (a) tell a full-size loss from a recovery-size
    # loss and (b) set the recovery target to the exact pre-trade balance.
    entry_mode = "recovery" if recovery.active else "normal"

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
            "mode_at_entry": entry_mode, "balance_before": round(balance_before, 2),
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

    # Kalshi V2 single-book order model (POST /portfolio/events/orders).
    # The legacy /portfolio/orders endpoint was deprecated and now returns
    # HTTP 410. V2 quotes a single YES book: side="bid" buys YES, side="ask"
    # buys NO (buying NO at L cents == selling YES at (100 - L) cents). Price
    # and count are fixed-point dollar/contract strings, and time_in_force and
    # self_trade_prevention_type are required.
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
            "mode_at_entry": entry_mode, "balance_before": round(balance_before, 2),
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
        f"Size=${active_trade_size():.0f} "
        f"(normal=${NORMAL_TRADE_SIZE:.0f}/recovery=${RECOVERY_TRADE_SIZE:.0f}"
        f"{' • RECOVERING→$%.0f' % recovery.target_balance if recovery.active else ''}) | "
        f"MaxConsecL={MAX_CONSEC_LOSSES}\n"
        f"MinConf={MIN_CONFIDENCE} | MinWinP={MIN_WIN_PROB*100:.0f}% | R²≥{R2_TREND_THRESHOLD}\n"
        f"OBDepth≥${MIN_OB_DEPTH:.0f} | OBImb≥{OB_IMBALANCE_THRESH*100:.0f}%\n"
        f"AGREE-gate={'ON' if REQUIRE_AGREE_MOMENTUM else 'OFF'} | "
        f"Breakeven≤{YES_BREAKEVEN_PRICE}c\n"
        f"SessionScore≥{MIN_SESSION_SCORE} | Kelly={KELLY_FRACTION}"
        + (f"\n🎯 Daily goal: ${balance:.2f} → ${daily_goal_target:.2f} "
           f"(×{DAILY_GOAL_MULTIPLE:.2f}/day), halt on reach"
           if DAILY_GOAL_ENABLED else "")
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
    if not daily_goal_check(balance):
        last_signal_desc = (f"daily goal reached "
                            f"(${balance:.2f}≥${daily_goal_target:.2f})")
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

    # Direction gate: never bet the order book against the measured trend. Both
    # of 2026-06-19's losing trades did exactly that (NO in TRENDING_UP, YES in
    # TRENDING_DOWN) while the one aligned trade won. Require agreement.
    if not regime_agrees(regime, ob_dir):
        log.info("Regime conflict │ OB=%s vs %s — no trade", ob_dir, regime.value)
        last_signal_desc = f"regime conflict OB={ob_dir} {regime.value}"
        return

    momentum_verdict, momentum_adj = compute_momentum(ob_dir)
    if momentum_verdict == "CONFLICT":
        log.info("Momentum CONFLICT │ OB=%s", ob_dir)
        last_signal_desc = f"CONFLICT OB={ob_dir}"
        return

    # ── DOCTRINE LAYER 7 (restored v9.3.0) ───────────────────────────────────
    # BTC spot momentum must EXPLICITLY AGREE with the order-book direction.
    # NEUTRAL (flat BTC) is not confirmation; trading on the order book alone is
    # the single condition this bot was post-mortemed never to do (v6.0.0;
    # 2026-03-27/28 50% loss). Every trade in the 2026-06-20→22 bleed fired on
    # BTC=NEUTRAL because v9.0.6→v9.2.0 left no NEUTRAL gate here.
    #
    # Applies in EVERY session state, RECOVERY included. RECOVERY does not relax
    # this — its deadlock is resolved by the balance-heal exit and
    # RECOVERY_MAX_SECS in update_session_state(), not by trading unconfirmed
    # setups. A calm, all-NEUTRAL session producing zero trades is CORRECT.
    if not momentum_gate_ok(momentum_verdict):
        log.info("Momentum │ require AGREE, got %s (OB=%s) — no trade",
                 momentum_verdict, ob_dir)
        last_signal_desc = f"momentum {momentum_verdict} (need AGREE)"
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
    # balance_before = the realized balance for this cycle (fetched before any
    # order cost is debited) → the exact recovery target if this trade loses.
    place_order(ticker, direction, bet, limit_price, win_prob, edge,
                balance_before=balance)


# ─────────────────────────────────────────────────────────────────────────────
# DAILY SESSION ROLLOVER
# ─────────────────────────────────────────────────────────────────────────────

def maybe_roll_session_day(current_balance: float) -> bool:
    """Reset the daily risk budget at the UTC day boundary.

    The daily-loss cap is, by name, a *daily* limit — but the old code latched
    `_session_halted` permanently, so once the cap was hit the bot slept "1hr"
    forever and needed a redeploy to trade again (2026-06-19: idle 13:25→next
    day). Here a new UTC day clears the halt, re-baselines the drawdown
    references to the live balance, and wipes the per-session ticker/streak
    state — so a fresh day always starts with a fresh budget and no manual
    intervention. Returns True when a rollover happened.
    """
    global _session_day, _session_halted, session_start_balance
    global session_stop_threshold, daily_pnl, paper_daily_pnl, consecutive_losses
    global session_state, streak_pause_until, live_daily_realized
    global daily_goal_start_balance, daily_goal_target, _daily_goal_reached

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
    live_daily_realized    = 0.0
    consecutive_losses     = 0
    streak_pause_until     = 0.0
    session_state          = SessionState.ACTIVE
    session_traded_tickers.clear()

    # v9.6.0: re-baseline the daily-doubling target to today's opening balance
    # (which compounds yesterday's gains) and clear the per-day goal halt.
    set_daily_goal(current_balance)

    log.info("🔄 New trading day %s │ balance $%.2f │ daily budget reset%s%s",
             today, current_balance, " (halt cleared)" if was_halted else "",
             (" │ goal→$%.2f" % daily_goal_target) if DAILY_GOAL_ENABLED else "")
    return True


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
    global live_wins, live_losses, streak_pause_until, live_daily_realized
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

    # In-place resets — no global declaration needed
    session_traded_tickers.clear()
    _processed_settlement_ids.clear()

    log.info("━" * 70)
    log.info("  JOHNNY5-KALSHI-AUTO %s │ %s", BOT_VERSION, "PAPER 🟡" if DEMO_MODE else "LIVE 🔴")
    log.info("  Start: %s", _session_start_ts)
    log.info("  Regime R²≥%.2f | VolCap=%.3f%% | Circuit=%.2f%%",
             R2_TREND_THRESHOLD, VOLATILITY_CAP_PCT, VOL_CIRCUIT_BREAKER)
    log.info("  OB depth≥$%.0f imb≥%.0f%% | WinP≥%.0f%% Edge≥%.0f%%",
             MIN_OB_DEPTH, OB_IMBALANCE_THRESH * 100, MIN_WIN_PROB * 100, MIN_EDGE_PCT * 100)
    log.info("  AGREE-gate=%s | MinConf=%d | Breakeven≤%dc | NEUTRALdrag=%.3f",
             "ON" if REQUIRE_AGREE_MOMENTUM else "OFF",
             MIN_CONFIDENCE, YES_BREAKEVEN_PRICE, NEUTRAL_ACCURACY_DRAG)
    log.info("  Momentum lookback=%d intervals | thresh≥%.2f%% or R²≥%.2f",
             MOMENTUM_LOOKBACK, MOMENTUM_THRESH_PCT, MOMENTUM_R2_MIN)
    log.info("  Sizing: normal=$%.0f recovery=$%.0f | active=$%.0f%s",
             NORMAL_TRADE_SIZE, RECOVERY_TRADE_SIZE, active_trade_size(),
             " (RECOVERY active, target $%.2f)" % recovery.target_balance
             if recovery.active else "")
    log.info("  Kelly=%.2f | SessionScore≥%d", KELLY_FRACTION, MIN_SESSION_SCORE)
    if DAILY_GOAL_ENABLED:
        log.info("  🎯 Daily goal: ×%.2f/day | gap÷%d trades | cap=%.0f%% bal%s | min=$%.2f",
                 DAILY_GOAL_MULTIPLE, DAILY_GOAL_TRADES_TO_TARGET,
                 DAILY_GOAL_MAX_STAKE_FRACTION * 100,
                 (" / $%.0f hard" % DAILY_GOAL_MAX_STAKE) if DAILY_GOAL_MAX_STAKE > 0 else "",
                 DAILY_GOAL_MIN_STAKE)
    log.info("━" * 70)

    tg.validate_telegram_connection()

    live_wins          = 0
    live_losses        = 0
    streak_pause_until = 0.0

    if DEMO_MODE:
        running_pnl            = 0.0
        session_start_balance  = paper_balance
        session_stop_threshold = paper_balance * SESSION_STOP_FRACTION
        recovery.reconcile_on_boot(paper_balance)
        reconcile_daily_goal_on_boot(paper_balance)
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
        live_daily_realized = 0.0
        recovery.reconcile_on_boot(bal)
        reconcile_daily_goal_on_boot(bal)
        telegram_boot(bal)

    resolve_cycle = 0

    while not _shutdown_requested:
        try:
            if _session_halted:
                # Halt is paused-for-the-day, not forever: poll often enough to
                # catch the UTC rollover that clears it (maybe_roll_session_day),
                # then resume automatically — no redeploy needed.
                halt_bal = paper_balance if DEMO_MODE else get_live_balance()
                if not maybe_roll_session_day(halt_bal):
                    log.info("Halted for the day — paused until UTC rollover.")
                    time.sleep(300)
                    continue

            if time.time() - last_heartbeat_ts >= 900:
                last_heartbeat_ts = time.time()
                hb_bal  = paper_balance if DEMO_MODE else get_live_balance()
                hb_pnl  = paper_daily_pnl if DEMO_MODE else live_daily_realized
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
            # Recovery EXIT check runs every cycle, independent of trading, so
            # the bot can never wedge in recovery once balance reaches target.
            recovery.maybe_exit(current_balance)
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
                    # v9.3.1: PnL shown is REALIZED (settled) dollars — the same
                    # value the daily-loss breaker uses. `cash` is the raw
                    # balance−start delta, which dips by an open position's outlay
                    # until it settles and must NOT be read as a loss.
                    cash_delta = live_bal - session_start_balance
                    daily_pnl  = live_daily_realized
                    wlb       = wilson_lower_bound(live_wins, live_wins + live_losses)
                    trades_since = (live_wins + live_losses) - (recovery_entry_wins + recovery_entry_losses)
                    log.info(
                        "Portfolio │ $%.2f │ PnL=$%+.2f │ cash=$%+.2f │ WR=%d/%d LB=%.1f%% │ Prior=%.3f │ %s"
                        "%s",
                        live_bal, daily_pnl, cash_delta,
                        live_wins, live_wins + live_losses,
                        wlb * 100, _live_prior, session_state.value,
                        f" (rec+{trades_since})" if session_state == SessionState.RECOVERY else "",
                    )
                    if (datetime.now(timezone.utc).hour == 0
                            and time.time() - last_daily_summary_ts > 3600):
                        last_daily_summary_ts = time.time()
                        telegram_daily_summary(live_bal, daily_pnl, live_wins, live_losses)

                if recovery.active:
                    log.info(recovery.status_line(current_balance))

                if DAILY_GOAL_ENABLED and daily_goal_target > 0:
                    gb   = paper_balance if DEMO_MODE else _last_known_balance
                    span = daily_goal_target - daily_goal_start_balance
                    pct  = ((gb - daily_goal_start_balance) / span * 100.0
                            if span > 0 else 0.0)
                    log.info("🎯 Daily goal │ $%.2f → $%.2f │ %.0f%%%s",
                             gb, daily_goal_target, max(0.0, pct),
                             " │ REACHED (paused)" if _daily_goal_reached else "")

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
