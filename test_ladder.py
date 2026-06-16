"""
test_ladder.py — Pytest suite for the laddering stake manager (ladder.py).

Covers:
  - PerformanceTracker: rolling window, win_rate, signed streaks
  - StakeManager: tier thresholds + one-rung demotion (pure policy)
  - RiskGuardrails: drawdown, losing-streak demote, vol cap, ceiling
  - StakeLadder: warm-up, get_stake, cooldown/anti-chase, daily reset
  - Persistence round-trip

Usage:
  pytest test_ladder.py -v
"""

import json

import pytest

from ladder import (
    LadderConfig,
    PerformanceTracker,
    StakeManager,
    RiskGuardrails,
    StakeLadder,
)


# A controllable clock so cooldown logic is testable without real sleeps.
class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def make_ladder(clock=None, **overrides):
    cfg = LadderConfig(persist=False, **overrides)
    return StakeLadder(cfg=cfg, clock=clock or FakeClock())


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceTracker
# ─────────────────────────────────────────────────────────────────────────────

class TestPerformanceTracker:
    def test_window_is_bounded(self):
        t = PerformanceTracker(window=5)
        for _ in range(10):
            t.record(True)
        assert t.total == 5

    def test_win_rate(self):
        t = PerformanceTracker(window=10)
        for won in [True, True, True, False]:
            t.record(won)
        assert t.win_rate == 0.75
        assert t.wins == 3 and t.losses == 1

    def test_win_streak_positive(self):
        t = PerformanceTracker(window=10)
        for _ in range(3):
            t.record(True)
        assert t.streak == 3
        assert t.loss_streak == 0

    def test_loss_streak_signed(self):
        t = PerformanceTracker(window=10)
        t.record(True)
        t.record(False)
        t.record(False)
        assert t.streak == -2
        assert t.loss_streak == 2

    def test_streak_flips_on_result_change(self):
        t = PerformanceTracker(window=10)
        t.record(False)
        t.record(False)
        assert t.loss_streak == 2
        t.record(True)
        assert t.streak == 1
        assert t.loss_streak == 0

    def test_empty_win_rate_is_zero(self):
        assert PerformanceTracker(window=10).win_rate == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# StakeManager — pure tier policy
# ─────────────────────────────────────────────────────────────────────────────

class TestStakeManager:
    @pytest.mark.parametrize("wr,mult", [
        (0.40, 0.50),   # Tier 1 conservative
        (0.49, 0.50),
        (0.50, 1.00),   # Tier 2 baseline
        (0.54, 1.00),
        (0.55, 1.25),   # Tier 3 momentum
        (0.59, 1.25),
        (0.60, 1.50),   # Tier 4 strong
        (0.64, 1.50),
        (0.65, 2.00),   # Tier 5 aggressive
        (0.90, 2.00),
    ])
    def test_tier_boundaries(self, wr, mult):
        assert StakeManager.tier_for(wr)[0] == mult

    def test_demote_steps_one_rung(self):
        assert StakeManager.demote(2.00) == 1.50
        assert StakeManager.demote(1.50) == 1.25
        assert StakeManager.demote(1.25) == 1.00
        assert StakeManager.demote(1.00) == 0.50

    def test_demote_floor(self):
        assert StakeManager.demote(0.50) == 0.50


# ─────────────────────────────────────────────────────────────────────────────
# RiskGuardrails
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskGuardrails:
    def setup_method(self):
        self.g = RiskGuardrails(LadderConfig(persist=False, max_daily_loss=15.0,
                                             streak_demote_at=4))

    def _apply(self, mult, tier="T5-AGGRESSIVE", loss_streak=0, daily_pnl=0.0,
               vol=False, cooldown=False):
        return self.g.apply(mult, tier, loss_streak, daily_pnl, vol, cooldown)

    def test_clean_passthrough(self):
        r = self._apply(2.0)
        assert r.multiplier == 2.0 and not r.paused

    def test_drawdown_revert(self):
        r = self._apply(2.0, daily_pnl=-20.0)
        assert r.multiplier == 1.0 and not r.paused

    def test_drawdown_pause(self):
        g = RiskGuardrails(LadderConfig(persist=False, max_daily_loss=15.0,
                                        drawdown_action="pause"))
        r = g.apply(2.0, "T5", 0, -20.0, False, False)
        assert r.multiplier == 0.0 and r.paused

    def test_losing_streak_demotes_one_tier(self):
        r = self._apply(2.0, loss_streak=4)
        assert r.multiplier == 1.5

    def test_vol_spike_caps_at_baseline(self):
        r = self._apply(2.0, vol=True)
        assert r.multiplier == 1.0

    def test_cooldown_caps_at_baseline(self):
        r = self._apply(1.5, cooldown=True)
        assert r.multiplier == 1.0

    def test_ceiling_never_exceeded(self):
        g = RiskGuardrails(LadderConfig(persist=False, max_multiplier=2.0))
        r = g.apply(3.0, "T5", 0, 0.0, False, False)
        assert r.multiplier == 2.0

    def test_drawdown_outranks_everything(self):
        # Even a clean aggressive tier reverts under drawdown.
        r = self._apply(2.0, loss_streak=0, daily_pnl=-50.0)
        assert r.multiplier == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# StakeLadder — orchestration
# ─────────────────────────────────────────────────────────────────────────────

class TestStakeLadder:
    def test_warmup_holds_baseline(self):
        lad = make_ladder(min_trades=10)
        for _ in range(5):
            lad.on_trade_result(True, 2.0)
        d = lad.get_stake(5.0)
        assert d.multiplier == 1.0
        assert "warmup" in d.reason
        assert d.stake == 5.0

    def test_hot_streak_ladders_up(self):
        clk = FakeClock()
        lad = make_ladder(clock=clk, min_trades=10, window=20, cooldown_secs=0)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        d = lad.get_stake(5.0)
        assert d.multiplier == 2.0          # WR=100% -> aggressive
        assert d.stake == 10.0

    def test_max_stake_ceiling_respected(self):
        clk = FakeClock()
        lad = make_ladder(clock=clk, min_trades=10, cooldown_secs=0)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        d = lad.get_stake(5.0, max_stake=7.5)
        assert d.stake == 7.5               # 2x would be 10, capped to 7.5

    def test_cooldown_blocks_size_up_after_loss(self):
        clk = FakeClock()
        lad = make_ladder(clock=clk, min_trades=5, window=20, cooldown_secs=300)
        # Build a hot record, then take one loss.
        for _ in range(9):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        lad.on_trade_result(False, -3.0)    # arms cooldown
        clk.advance(60)
        d = lad.get_stake(5.0)
        assert d.multiplier == 1.0          # capped despite high WR
        assert "cooldown" in d.reason

    def test_cooldown_expires(self):
        clk = FakeClock()
        lad = make_ladder(clock=clk, min_trades=5, window=20, cooldown_secs=300,
                          cooldown_cycles=1)
        for _ in range(9):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        lad.on_trade_result(False, -3.0)
        clk.advance(400)                    # past the 300s cooldown
        lad.on_trade_result(True, 2.0)      # clears the cycle block too
        clk.advance(60)
        d = lad.get_stake(5.0)
        assert d.multiplier > 1.0

    def test_drawdown_override_in_get_stake(self):
        clk = FakeClock()
        lad = make_ladder(clock=clk, min_trades=5, max_daily_loss=10.0,
                          cooldown_secs=0)
        for _ in range(8):
            lad.on_trade_result(True, 1.0)
            clk.advance(60)
        # Force the daily pnl below the loss cap.
        lad.daily_pnl = -12.0
        d = lad.get_stake(5.0)
        assert d.multiplier <= 1.0
        assert "DRAWDOWN" in d.reason

    def test_deterministic(self):
        def run():
            clk = FakeClock()
            lad = make_ladder(clock=clk, min_trades=5)
            tape = [True, False, True, True, False, True, True, True]
            out = []
            for won in tape:
                out.append(lad.get_stake(5.0).stake)
                lad.on_trade_result(won, 1.0 if won else -1.0)
                clk.advance(60)
            return out
        assert run() == run()

    def test_update_performance_alias(self):
        lad = make_ladder()
        assert lad.update_performance == lad.on_trade_result

    def test_daily_reset(self):
        lad = make_ladder()
        lad.daily_pnl = -50.0
        lad.reset_daily()
        assert lad.daily_pnl == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "ladder_state.json")
        cfg = LadderConfig(state_path=path, persist=True, min_trades=5)
        clk = FakeClock()
        lad = StakeLadder(cfg=cfg, clock=clk)
        for won in [True, True, False, True]:
            lad.on_trade_result(won, 1.0 if won else -1.0)

        # New instance loads prior state from disk.
        lad2 = StakeLadder(cfg=LadderConfig(state_path=path, persist=True,
                                            min_trades=5), clock=clk)
        assert lad2.tracker.total == 4
        assert lad2.tracker.wins == 3
        assert lad2.tracker.streak == lad.tracker.streak

    def test_corrupt_state_is_ignored(self, tmp_path):
        path = tmp_path / "ladder_state.json"
        path.write_text("{ not json")
        cfg = LadderConfig(state_path=str(path), persist=True)
        lad = StakeLadder(cfg=cfg, clock=FakeClock())   # must not raise
        assert lad.tracker.total == 0

    def test_stale_daily_pnl_resets_on_new_day(self, tmp_path):
        path = str(tmp_path / "ladder_state.json")
        clk = FakeClock(t=1_700_000_000.0)
        cfg = LadderConfig(state_path=path, persist=True)
        lad = StakeLadder(cfg=cfg, clock=clk)
        lad.daily_pnl = -20.0
        lad._save()
        # Reload a full day later -> daily pnl must not linger.
        clk2 = FakeClock(t=1_700_000_000.0 + 86_400)
        lad2 = StakeLadder(cfg=LadderConfig(state_path=path, persist=True),
                           clock=clk2)
        assert lad2.daily_pnl == 0.0
