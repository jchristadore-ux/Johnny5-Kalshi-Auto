"""
ladder.py — Dynamic laddering stake manager for Johnny5-Kalshi-Auto.

PURPOSE
-------
Adapt *stake size only* (never strategy or signal generation) based on recent
performance over a rolling window of trades. Size up when the edge is paying
off, size down — or pause — when variance turns against us.

The module is deliberately self-contained and dependency-free (stdlib only) so
it can be unit-tested in isolation and dropped into any trading loop. bot.py
wires it in as a multiplier overlay on top of the existing Kelly stake, so the
ladder can *never* exceed the caps the bot already enforces.

DESIGN — clean separation of concerns
--------------------------------------
    PerformanceTracker  rolling window, win_rate, streaks  (state)
    StakeManager        win_rate -> tier multiplier        (pure policy)
    RiskGuardrails      drawdown / streak / vol / cooldown  (safety overrides)
    StakeLadder         orchestrator + persistence          (the public API)

PUBLIC API (the three hooks the prompt asks for)
------------------------------------------------
    ladder.get_stake(base_stake)        -> StakeDecision   (compute BEFORE a trade)
    ladder.on_trade_result(won, pnl)    -> None            (AFTER settlement)
    ladder.update_performance(...)       == on_trade_result (alias)

Everything is deterministic — given the same trade history and clock, the same
stake comes out. No randomness anywhere.

Run `python ladder.py` for a worked 20-trade simulation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

log = logging.getLogger("Johnny5.ladder")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

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


@dataclass
class LadderConfig:
    """All tunables. Every field is overridable via environment variable so the
    ladder can be retuned in Railway without a code change."""

    # Rolling performance window
    window:            int   = 30      # last N trades scored for win_rate (20–50)
    min_trades:        int   = 10      # below this we stay at baseline (warm-up)

    # Tier multipliers (applied to base_stake). The thresholds are win_rate
    # lower bounds; see StakeManager.TIERS for the canonical ladder.
    max_multiplier:    float = 2.0     # hard ceiling — never size past 2x base

    # Safety / drawdown controls
    max_daily_loss:    float = 15.0    # $ loss that trips the drawdown override
    drawdown_action:   str   = "revert"  # "revert" -> 1x base, "pause" -> 0 stake
    streak_demote_at:  int   = 4       # losing streak length that demotes one tier
    vol_cap_at_base:   bool  = True    # a vol-spike flag caps stake at baseline

    # Cooling / anti-chase
    cooldown_secs:     float = 300.0   # after a loss, hold at baseline this long
    cooldown_cycles:   int   = 1       # ...or at least this many trade cycles

    # Persistence
    state_path:        str   = "ladder_state.json"
    persist:           bool  = True

    @classmethod
    def from_env(cls) -> "LadderConfig":
        return cls(
            window           = _env_int("LADDER_WINDOW", 30),
            min_trades       = _env_int("LADDER_MIN_TRADES", 10),
            max_multiplier   = _env_float("LADDER_MAX_MULT", 2.0),
            max_daily_loss   = _env_float("MAX_DAILY_LOSS_DOLLARS", 15.0),
            drawdown_action  = os.environ.get("LADDER_DRAWDOWN_ACTION", "revert").strip().lower(),
            streak_demote_at = _env_int("LADDER_STREAK_DEMOTE_AT", 4),
            vol_cap_at_base  = _env_bool("LADDER_VOL_CAP_AT_BASE", True),
            cooldown_secs    = _env_float("LADDER_COOLDOWN_SECS", 300.0),
            cooldown_cycles  = _env_int("LADDER_COOLDOWN_CYCLES", 1),
            state_path       = os.environ.get("LADDER_STATE_PATH", "ladder_state.json"),
            persist          = _env_bool("LADDER_PERSIST", True),
        )


# ─────────────────────────────────────────────────────────────────────────────
# DECISION RECORD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StakeDecision:
    """The full, auditable output of a sizing decision — everything the prompt
    requires us to log: win rate, tier, stake, and the reason."""
    stake:       float
    base_stake:  float
    multiplier:  float
    tier:        str
    win_rate:    float
    sample:      int
    loss_streak: int
    reason:      str

    def log_line(self) -> str:
        return (
            f"LADDER │ stake=${self.stake:.2f} (base ${self.base_stake:.2f} "
            f"× {self.multiplier:.2f}) │ tier={self.tier} │ "
            f"WR={self.win_rate*100:.1f}% n={self.sample} │ "
            f"lossStreak={self.loss_streak} │ {self.reason}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 1. PERFORMANCE TRACKER  — rolling window state
# ─────────────────────────────────────────────────────────────────────────────

class PerformanceTracker:
    """Tracks the last N trade outcomes and exposes win_rate + streaks.

    Outcomes are stored as a bounded deque of bools (True=win). Streak is signed:
    positive = consecutive wins, negative = consecutive losses.
    """

    def __init__(self, window: int) -> None:
        self.window  = max(1, window)
        self.results: Deque[bool] = deque(maxlen=self.window)
        self.streak  = 0          # signed: +wins / -losses
        self.cycles  = 0          # total trades ever recorded (for cooldown math)

    def record(self, won: bool) -> None:
        self.results.append(bool(won))
        self.cycles += 1
        if won:
            self.streak = self.streak + 1 if self.streak > 0 else 1
        else:
            self.streak = self.streak - 1 if self.streak < 0 else -1

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def wins(self) -> int:
        return sum(self.results)

    @property
    def losses(self) -> int:
        return self.total - self.wins

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total) if self.total else 0.0

    @property
    def loss_streak(self) -> int:
        """Current consecutive-loss count (0 if last trade was a win)."""
        return -self.streak if self.streak < 0 else 0

    def to_dict(self) -> dict:
        return {"results": list(self.results), "streak": self.streak,
                "cycles": self.cycles, "window": self.window}

    def load(self, d: dict) -> None:
        self.window  = max(1, int(d.get("window", self.window)))
        self.results = deque((bool(x) for x in d.get("results", [])), maxlen=self.window)
        self.streak  = int(d.get("streak", 0))
        self.cycles  = int(d.get("cycles", 0))


# ─────────────────────────────────────────────────────────────────────────────
# 2. STAKE MANAGER  — pure win_rate -> tier policy
# ─────────────────────────────────────────────────────────────────────────────

class StakeManager:
    """Maps a win rate to a stake multiplier. Pure, stateless, deterministic.

    TIERS is ordered HIGH -> LOW; the first tier whose threshold the win_rate
    clears wins. The bare multiplier ladder (LADDER) is used by the guardrails
    to step a tier *down* by exactly one rung.
    """

    # (win_rate lower bound, multiplier, name)
    TIERS: List[Tuple[float, float, str]] = [
        (0.65, 2.00, "T5-AGGRESSIVE"),
        (0.60, 1.50, "T4-STRONG"),
        (0.55, 1.25, "T3-MOMENTUM"),
        (0.50, 1.00, "T2-BASELINE"),
        (0.00, 0.50, "T1-CONSERVATIVE"),
    ]

    # Ascending list of the multipliers for one-rung demotion.
    LADDER: List[float] = [0.50, 1.00, 1.25, 1.50, 2.00]

    @classmethod
    def tier_for(cls, win_rate: float) -> Tuple[float, str]:
        for threshold, mult, name in cls.TIERS:
            if win_rate >= threshold:
                return mult, name
        return cls.TIERS[-1][1], cls.TIERS[-1][2]  # unreachable, safety net

    @classmethod
    def demote(cls, multiplier: float) -> float:
        """Return the next-lower multiplier on the ladder (one rung down)."""
        lower = [m for m in cls.LADDER if m < multiplier]
        return max(lower) if lower else cls.LADDER[0]


# ─────────────────────────────────────────────────────────────────────────────
# 3. RISK GUARDRAILS — hard overrides applied on top of the tier
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GuardResult:
    multiplier: float
    reason:     str
    paused:     bool = False


class RiskGuardrails:
    """Applies the always-on safety overrides, in priority order:

        1. Daily drawdown  -> revert to baseline OR pause (highest priority)
        2. Losing streak   -> demote one tier
        3. Volatility spike -> cap at baseline
        4. Cooldown active  -> cap at baseline (anti-chase)
        5. Absolute ceiling -> never exceed max_multiplier
    """

    def __init__(self, cfg: LadderConfig) -> None:
        self.cfg = cfg

    def apply(
        self,
        base_multiplier: float,
        tier_name:       str,
        loss_streak:     int,
        daily_pnl:       float,
        vol_spike:       bool,
        cooldown_active: bool,
    ) -> GuardResult:
        mult   = base_multiplier
        reasons: List[str] = []

        # 1. Daily drawdown — overrides everything.
        if daily_pnl <= -abs(self.cfg.max_daily_loss):
            if self.cfg.drawdown_action == "pause":
                return GuardResult(0.0, f"DRAWDOWN pause (pnl ${daily_pnl:.2f})", paused=True)
            return GuardResult(min(mult, 1.0), f"DRAWDOWN revert→base (pnl ${daily_pnl:.2f})")

        # 2. Losing streak — demote one tier.
        if loss_streak >= self.cfg.streak_demote_at:
            demoted = StakeManager.demote(mult)
            if demoted < mult:
                reasons.append(f"streak {loss_streak}≥{self.cfg.streak_demote_at} demote")
                mult = demoted

        # 3. Volatility spike — cap at baseline.
        if vol_spike and self.cfg.vol_cap_at_base and mult > 1.0:
            reasons.append("vol-spike cap→base")
            mult = 1.0

        # 4. Cooldown (anti-chase) — cap at baseline.
        if cooldown_active and mult > 1.0:
            reasons.append("cooldown cap→base")
            mult = 1.0

        # 5. Absolute ceiling.
        if mult > self.cfg.max_multiplier:
            reasons.append(f"ceiling {self.cfg.max_multiplier:.2f}")
            mult = self.cfg.max_multiplier

        reason = "; ".join(reasons) if reasons else f"tier {tier_name} clean"
        return GuardResult(mult, reason)


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR — the public ladder
# ─────────────────────────────────────────────────────────────────────────────

class StakeLadder:
    """The public face of the laddering system.

    Wire-up in a trading loop:
        ladder = StakeLadder()
        ...
        decision = ladder.get_stake(base_stake)   # BEFORE placing the trade
        place(size=decision.stake)
        ...
        ladder.on_trade_result(won=True, pnl=2.10) # AFTER settlement
    """

    def __init__(self, cfg: Optional[LadderConfig] = None,
                 clock=time.time) -> None:
        self.cfg     = cfg or LadderConfig.from_env()
        self._clock  = clock
        self.tracker = PerformanceTracker(self.cfg.window)
        self.guards  = RiskGuardrails(self.cfg)

        self.daily_pnl:      float = 0.0
        self.daily_key:      str   = self._today()
        self.cooldown_until: float = 0.0
        self.cooldown_cycle: int   = -1   # tracker.cycles value when cooldown ends
        self.vol_spike:      bool  = False

        if self.cfg.persist:
            self._load()

    # ── public hook: BEFORE every trade ─────────────────────────────────────
    def get_stake(self, base_stake: float,
                  max_stake: Optional[float] = None) -> StakeDecision:
        """Compute the stake for the next trade. Deterministic.

        base_stake : the strategy's intended size (e.g. the Kelly stake).
        max_stake  : optional absolute ceiling on the returned dollar amount
                     (e.g. a balance-fraction cap); applied last.
        """
        self._roll_day_if_needed()
        tracker = self.tracker

        # Warm-up: not enough data to trust a win_rate yet -> baseline.
        if tracker.total < self.cfg.min_trades:
            mult, tier = 1.0, "T2-BASELINE(warmup)"
            reason = f"warmup {tracker.total}/{self.cfg.min_trades} trades"
            return self._finalize(base_stake, mult, tier, reason, max_stake)

        win_rate    = tracker.win_rate
        base_mult, tier = StakeManager.tier_for(win_rate)

        guard = self.guards.apply(
            base_multiplier = base_mult,
            tier_name       = tier,
            loss_streak     = tracker.loss_streak,
            daily_pnl       = self.daily_pnl,
            vol_spike       = self.vol_spike,
            cooldown_active = self._cooldown_active(),
        )

        if guard.paused:
            tier = "PAUSED"
        return self._finalize(base_stake, guard.multiplier, tier, guard.reason, max_stake)

    def _finalize(self, base_stake: float, mult: float, tier: str,
                  reason: str, max_stake: Optional[float]) -> StakeDecision:
        stake = round(base_stake * mult, 2)
        if max_stake is not None:
            stake = min(stake, round(max_stake, 2))
        decision = StakeDecision(
            stake=stake, base_stake=round(base_stake, 2), multiplier=mult,
            tier=tier, win_rate=self.tracker.win_rate, sample=self.tracker.total,
            loss_streak=self.tracker.loss_streak, reason=reason,
        )
        log.info(decision.log_line())
        return decision

    # ── public hook: AFTER every settlement ─────────────────────────────────
    def on_trade_result(self, won: bool, pnl: float = 0.0) -> None:
        """Record a settled trade outcome and update all state."""
        self._roll_day_if_needed()
        self.tracker.record(won)
        self.daily_pnl += float(pnl)

        if not won:
            # Cooling: after a loss, hold at baseline for a time delay AND at
            # least `cooldown_cycles` trade cycles (whichever is longer). This
            # also blocks an immediate size-up on the first post-loss win.
            self.cooldown_until = self._clock() + self.cfg.cooldown_secs
            self.cooldown_cycle = self.tracker.cycles + self.cfg.cooldown_cycles

        log.info(
            "LADDER │ recorded %s pnl=$%.2f │ WR=%.1f%% n=%d streak=%d",
            "WIN" if won else "LOSS", pnl, self.tracker.win_rate * 100,
            self.tracker.total, self.tracker.streak,
        )
        if self.cfg.persist:
            self._save()

    # Alias required by the integration spec.
    update_performance = on_trade_result

    # ── optional external signals ───────────────────────────────────────────
    def set_vol_spike(self, active: bool) -> None:
        """Feed an external volatility-spike flag (caps stake at baseline)."""
        self.vol_spike = bool(active)

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.daily_key = self._today()
        if self.cfg.persist:
            self._save()

    # ── internals ───────────────────────────────────────────────────────────
    def _cooldown_active(self) -> bool:
        time_block  = self._clock() < self.cooldown_until
        cycle_block = self.tracker.cycles < self.cooldown_cycle
        return time_block or cycle_block

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _roll_day_if_needed(self) -> None:
        today = self._today()
        if today != self.daily_key:
            log.info("LADDER │ new UTC day %s → daily_pnl reset (was $%.2f)",
                     today, self.daily_pnl)
            self.daily_pnl = 0.0
            self.daily_key = today

    # ── persistence (lightweight JSON) ──────────────────────────────────────
    def _save(self) -> None:
        try:
            with open(self.cfg.state_path, "w") as f:
                json.dump({
                    "tracker":        self.tracker.to_dict(),
                    "daily_pnl":      self.daily_pnl,
                    "daily_key":      self.daily_key,
                    "cooldown_until": self.cooldown_until,
                    "cooldown_cycle": self.cooldown_cycle,
                }, f)
        except OSError as e:
            log.warning("LADDER │ state save failed: %s", e)

    def _load(self) -> None:
        try:
            with open(self.cfg.state_path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.tracker.load(d.get("tracker", {}))
        self.daily_pnl      = float(d.get("daily_pnl", 0.0))
        self.daily_key      = d.get("daily_key", self._today())
        self.cooldown_until = float(d.get("cooldown_until", 0.0))
        self.cooldown_cycle = int(d.get("cooldown_cycle", -1))
        # A persisted daily_pnl from a previous UTC day must not linger.
        self._roll_day_if_needed()


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION  (python ladder.py)
# ─────────────────────────────────────────────────────────────────────────────

def simulate() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.setLevel(logging.WARNING)  # silence per-call logs for a clean table
    # Deterministic 20-trade tape: warm-up, a hot run that ladders up, then a
    # losing streak that demotes and triggers cooldown.
    tape = [
        True, True, False, True, True, True, False, True, True, True,   # warm-up (10)
        True, True, True, True,                                         # hot → tiers climb
        False, False, False, False,                                    # 4-loss streak → demote
        True, True,                                                     # recover (cooldown blocks size-up)
    ]
    pnl_win, pnl_loss = 2.10, -3.00
    base = 5.00

    # Use a fake clock so the cooldown is observable without real sleeps.
    fake = {"t": 1_700_000_000.0}
    ladder = StakeLadder(
        cfg=LadderConfig(window=20, min_trades=10, cooldown_secs=300,
                         persist=False),
        clock=lambda: fake["t"],
    )

    print(f"\n{'#':>2} {'stake':>6} {'mult':>5} {'tier':<20} {'WR':>6} {'reason'}")
    print("-" * 78)
    for i, won in enumerate(tape, 1):
        d = ladder.get_stake(base)
        print(f"{i:>2} ${d.stake:>5.2f} {d.multiplier:>5.2f} {d.tier:<20} "
              f"{d.win_rate*100:>5.1f}% {d.reason}")
        ladder.on_trade_result(won, pnl_win if won else pnl_loss)
        fake["t"] += 60  # 60s between trade cycles


if __name__ == "__main__":
    simulate()
