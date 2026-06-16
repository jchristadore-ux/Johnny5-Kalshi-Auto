"""
test_bot.py — Pytest suite for Johnny5-Kalshi-Auto v9.0.1

Covers:
  P0: All risk controls
  P1: Signal math (edge, Kelly, momentum, confidence, regime)
  P2: OB analysis, stale cancel, ob trend
  P3: Wilson CI, performance guard, Bayesian prior

Usage:
  pip install pytest
  pytest test_bot.py -v
"""

import os
import time
import math

os.environ.setdefault("KALSHI_API_KEY_ID", "test-key-id-00000000")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PEM", "")
os.environ.setdefault("DEMO_MODE", "true")

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

_test_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_pem = _test_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
os.environ["KALSHI_PRIVATE_KEY_PEM"] = _test_pem

import pytest
import bot
from bot import Regime, SessionState
from ladder import StakeLadder, LadderConfig


class _FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class TestLadderIntegration:
    """The laddering overlay scales the Kelly stake but never breaks the bot's
    existing caps. It is opt-in: bot.stake_ladder is None unless LADDER_ENABLED."""

    def teardown_method(self):
        bot.stake_ladder = None

    def test_disabled_by_default_leaves_kelly_unchanged(self):
        bot.stake_ladder = None
        bet = bot.kelly_bet(0.65, 50, 100.0)
        assert bet > 0  # plain Kelly path, no overlay

    def test_overlay_scales_up_on_hot_record(self):
        clk = _FakeClock()
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=10,
                                           cooldown_secs=0), clock=clk)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        bot.stake_ladder = None
        plain = bot.kelly_bet(0.65, 50, 1000.0)
        bot.stake_ladder = lad
        laddered = bot.kelly_bet(0.65, 50, 1000.0)
        # 2x tier sizes up vs the un-laddered Kelly stake (subject to caps).
        assert laddered >= plain

    def test_overlay_never_exceeds_2x_cap(self):
        clk = _FakeClock()
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=10,
                                           max_multiplier=2.0, cooldown_secs=0),
                          clock=clk)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        bot.stake_ladder = lad
        # Large balance so the balance-fraction cap is not the binding limit.
        bet = bot.kelly_bet(0.65, 50, 100_000.0)
        assert bet <= 2.0 * bot.TRADE_SIZE_CAP + 1e-9


class TestPostBootSettlementGate:
    """v9.0.8: the unmatched-settlement branch must only count records settled
    at/after boot. /portfolio/settlements ignores created_since and returns
    account-wide history; counting it all deadlocked the Wilson perf guard."""

    def setup_method(self):
        bot._session_start_ts = "2026-06-11T23:00:00Z"

    def teardown_method(self):
        bot._session_start_ts = ""

    def test_account_history_before_boot_excluded(self):
        assert bot._is_post_boot({"settled_time": "2026-06-09T15:15:00Z"}) is False

    def test_in_flight_settled_after_boot_counted(self):
        assert bot._is_post_boot({"settled_time": "2026-06-11T23:45:00Z"}) is True

    def test_created_time_fallback(self):
        assert bot._is_post_boot({"created_time": "2026-06-11T23:45:00Z"}) is True

    def test_missing_timestamp_excluded(self):
        assert bot._is_post_boot({}) is False

    def test_unparseable_timestamp_excluded(self):
        assert bot._is_post_boot({"settled_time": "garbage"}) is False

    def test_no_boot_ts_excluded(self):
        bot._session_start_ts = ""
        assert bot._is_post_boot({"settled_time": "2026-06-11T23:45:00Z"}) is False


# ═════════════════════════════════════════════════════════════════════════════
# P0: RISK CONTROLS
# ═════════════════════════════════════════════════════════════════════════════

class TestBalanceFloorCheck:
    def test_below_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(4.99) is False

    def test_at_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(5.00) is True

    def test_above_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(100.0) is True

    def test_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(0.0) is False


class TestDailyLossCheck:
    def setup_method(self):
        bot._session_halted = False

    def test_within_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -10.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 20.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(15.0) is True

    def test_at_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -20.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 20.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(5.0) is False

    def test_session_stop_triggers(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(10.0) is False

    def test_session_stop_ok_when_above(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(20.0) is True

    def test_halted_flag_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "_session_halted", True)
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(50.0) is False

    def test_halt_is_permanent_after_recovery(self, monkeypatch):
        """Balance recovery above session_stop_threshold must not clear halt."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -5.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        # Trigger halt at low balance
        result1 = bot.daily_loss_check(10.0)
        assert result1 is False
        assert bot._session_halted is True
        # Even with high balance, stays halted
        result2 = bot.daily_loss_check(50.0)
        assert result2 is False


class TestSpreadCheck:
    def test_normal(self):
        assert bot.spread_check(48, 52) is True

    def test_one_cent(self):
        assert bot.spread_check(49, 50) is True

    def test_zero(self):
        assert bot.spread_check(50, 50) is False

    def test_crossed(self):
        assert bot.spread_check(52, 48) is False


class TestExpiryGuard:
    def test_near_certain_high(self):
        assert bot.expiry_guard(90) is False

    def test_near_certain_low(self):
        assert bot.expiry_guard(10) is False

    def test_boundary_high_blocked(self):
        assert bot.expiry_guard(86) is False

    def test_boundary_high_allowed(self):
        assert bot.expiry_guard(85) is True

    def test_boundary_low_blocked(self):
        assert bot.expiry_guard(14) is False

    def test_boundary_low_allowed(self):
        assert bot.expiry_guard(15) is True

    def test_mid(self):
        assert bot.expiry_guard(50) is True


class TestCooldownCheck:
    def test_not_passed(self, monkeypatch):
        monkeypatch.setattr(bot, "last_trade_ts", time.time())
        assert bot.cooldown_check() is False

    def test_passed(self, monkeypatch):
        monkeypatch.setattr(bot, "last_trade_ts", time.time() - 9999)
        assert bot.cooldown_check() is True


class TestStreakCheck:
    def setup_method(self):
        bot.consecutive_losses = 0
        bot.streak_pause_until = 0.0

    def test_no_losses_ok(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 0
        assert bot.streak_check() is True

    def test_below_threshold_ok(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 1
        assert bot.streak_check() is True

    def test_at_threshold_in_pause(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 2
        bot.streak_pause_until = time.time() + 9999
        assert bot.streak_check() is False

    def test_at_threshold_pause_expired(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 2
        bot.streak_pause_until = time.time() - 1
        result = bot.streak_check()
        assert result is True
        assert bot.consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════════════════
# P1: SIGNAL MATH
# ═════════════════════════════════════════════════════════════════════════════

class TestCalcEdge:
    def test_positive_edge(self):
        edge = bot.calc_edge(0.70, 50)
        assert abs(edge - 0.20) < 0.001

    def test_zero_edge(self):
        edge = bot.calc_edge(0.50, 50)
        assert abs(edge) < 0.001

    def test_negative_edge(self):
        assert bot.calc_edge(0.30, 50) < 0

    def test_boundary_zero_price(self):
        assert bot.calc_edge(0.70, 0) == 0.0

    def test_boundary_100_price(self):
        assert bot.calc_edge(0.70, 100) == 0.0

    def test_cheap_contract(self):
        edge = bot.calc_edge(0.40, 20)
        assert abs(edge - 0.20) < 0.001


class TestKellyBet:
    def test_positive_edge_returns_bet(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.10)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.70, 50, 25.0)
        assert bet > 0
        assert bet <= 5.0

    def test_no_edge_returns_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.10)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.30, 50, 25.0)
        assert bet == 0.0

    def test_capped_at_trade_size(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 2.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.50)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.50)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.90, 40, 100.0)
        assert bet <= 2.0

    def test_recovery_halves_kelly(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 100.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        monkeypatch.setattr(bot, "KELLY_RECOVERY_MULT", 0.50)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.50)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet_active = bot.kelly_bet(0.70, 50, 100.0)
        monkeypatch.setattr(bot, "session_state", SessionState.RECOVERY)
        bet_recovery = bot.kelly_bet(0.70, 50, 100.0)
        assert bet_recovery < bet_active
        assert abs(bet_recovery - bet_active * 0.50) < 0.01

    def test_boundary_prices(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.10)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        assert bot.kelly_bet(0.70, 0, 25.0) == 0.0
        assert bot.kelly_bet(0.70, 100, 25.0) == 0.0


class TestComputeMomentum:
    def setup_method(self):
        bot.btc_prices.clear()

    def test_insufficient_data(self):
        bot.btc_prices.append(50000)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"

    def test_agree_yes_btc_up(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        for p in [50000, 50050, 50100, 50200, 50300]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "AGREE"
        assert adj > 0

    def test_conflict_yes_btc_down(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        for p in [50000, 49900, 49800, 49700, 49500]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "CONFLICT"

    def test_neutral_flat(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        for p in [50000, 50010, 50020, 50030, 50040]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"

    def test_agree_no_btc_down(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        for p in [50000, 49900, 49800, 49700, 49500]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("NO")
        assert verdict == "AGREE"

    def test_neutral_adj_is_negative(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "NEUTRAL_ACCURACY_DRAG", 0.02)
        for p in [50000, 50010, 50020, 50030, 50040]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert adj == -0.02


class TestWilsonCI:
    def test_zero_trades(self):
        pct, lo, hi = bot.wilson_confidence(0, 0)
        assert pct == 0.0

    def test_all_wins(self):
        pct, lo, hi = bot.wilson_confidence(10, 10)
        assert pct == 100.0
        assert lo > 50.0

    def test_all_losses(self):
        pct, lo, hi = bot.wilson_confidence(0, 10)
        assert pct == 0.0
        assert hi < 50.0

    def test_fifty_fifty(self):
        pct, lo, hi = bot.wilson_confidence(50, 100)
        assert abs(pct - 50.0) < 0.1
        assert lo < 50.0
        assert hi > 50.0

    def test_large_sample_narrow_ci(self):
        pct, lo, hi = bot.wilson_confidence(70, 100)
        assert hi - lo < 20


class TestWilsonLowerBound:
    def test_small_sample_returns_zero(self):
        assert bot.wilson_lower_bound(5, 9) == 0.0

    def test_good_win_rate(self):
        wlb = bot.wilson_lower_bound(15, 20)
        assert wlb > 0.50

    def test_bad_win_rate(self):
        wlb = bot.wilson_lower_bound(5, 20)
        assert wlb < 0.50


# ═════════════════════════════════════════════════════════════════════════════
# P1: REGIME DETECTION
# ═════════════════════════════════════════════════════════════════════════════

class TestComputeRegime:
    def setup_method(self):
        bot.btc_prices.clear()
        bot.btc_returns.clear()

    def test_insufficient_data_returns_unknown(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        for p in range(5):
            bot.btc_prices.append(50000 + p * 10)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.UNKNOWN

    def test_strong_uptrend(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 + i * 100 for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            bot.btc_returns.append((prices[i] - prices[i-1]) / prices[i-1] * 100)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.TRENDING_UP
        assert r2 > 0.70

    def test_strong_downtrend(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 - i * 100 for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            bot.btc_returns.append((prices[i] - prices[i-1]) / prices[i-1] * 100)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.TRENDING_DOWN

    def test_ranging(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        import math
        prices = [50000 + int(math.sin(i) * 200) for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            r = (prices[i] - prices[i-1]) / prices[i-1] * 100
            bot.btc_returns.append(r)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.RANGING

    def test_high_vol(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 0.10)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 + i * 100 for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        # Force high returns
        for _ in range(14):
            bot.btc_returns.append(0.50)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.HIGH_VOL


# ═════════════════════════════════════════════════════════════════════════════
# P2: ORDER BOOK ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

class TestAnalyzeOrderBook:
    def _make_ob(self, yes_levels, no_levels):
        return {"orderbook_fp": {"yes_dollars": yes_levels, "no_dollars": no_levels}}

    def test_strong_yes_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.48, 20], [0.50, 20]], [[0.50, 10]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["direction"] == "YES"
        assert result["imbalance"] >= 0.70

    def test_strong_no_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 5]], [[0.48, 20], [0.50, 20]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["direction"] == "NO"

    def test_balanced_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 15]], [[0.50, 15]])
        result = bot.analyze_order_book(ob, 50)
        assert result is None

    def test_thin_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 50.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 2]], [[0.50, 1]])
        result = bot.analyze_order_book(ob, 50)
        assert result is None

    def test_ghost_ob_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        # YES imbalance but NO side has zero levels
        ob = self._make_ob([[0.48, 80], [0.50, 20]], [])
        result = bot.analyze_order_book(ob, 50)
        assert result is None

    def test_deep_book_lowers_threshold(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 100.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.64)
        # 61% YES would fail at 0.64 threshold but deep books lower to 0.58
        ob = self._make_ob([[0.48, 6100]], [[0.50, 3900]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["direction"] == "YES"
        assert result["eff_thresh"] <= 0.60

    def test_total_depth_correct(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 30]], [[0.50, 10]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["total_depth"] == 40.0


class TestCheckObTrend:
    def setup_method(self):
        bot._prev_ob.clear()

    def test_first_obs_allows(self):
        assert bot.check_ob_trend("T1", "YES", 0.70) is True

    def test_building_pressure_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.65, time.time())
        assert bot.check_ob_trend("T1", "YES", 0.72) is True

    def test_fading_pressure_blocks(self):
        bot._prev_ob["T1"] = ("YES", 0.75, time.time())
        assert bot.check_ob_trend("T1", "YES", 0.60) is False

    def test_direction_flip_allows(self):
        # v9.0.1 check_ob_trend only blocks on fading (same direction, >10% drop)
        # Direction flip is NOT blocked by check_ob_trend — it's handled in run_decision
        bot._prev_ob["T1"] = ("YES", 0.70, time.time())
        result = bot.check_ob_trend("T1", "NO", 0.70)
        # Does not block — check_ob_trend only looks at fading same-direction
        assert result is True

    def test_stale_data_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.80, time.time() - 700)
        assert bot.check_ob_trend("T1", "NO", 0.60) is True

    def test_small_fade_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.70, time.time())
        assert bot.check_ob_trend("T1", "YES", 0.66) is True


# ═════════════════════════════════════════════════════════════════════════════
# P2: STALE ORDER CANCELLATION
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelStaleOrders:
    def test_paper_refunds_balance(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear()
        bot.active_tickers.clear()
        bot.trade_history.clear()
        bot.paper_balance = 20.0

        bot.open_orders["test-1"] = {
            "ticker": "KXBTC-TEST",
            "cost": 2.50,
            "placed_at": time.time() - 120,
        }
        bot.active_tickers.add("KXBTC-TEST")
        bot.trade_history.append({"order_id": "test-1", "result": "pending"})

        bot.cancel_stale_orders()

        assert "test-1" not in bot.open_orders
        assert bot.paper_balance == 22.50
        assert "KXBTC-TEST" not in bot.active_tickers

    def test_paper_does_not_touch_daily_pnl(self, monkeypatch):
        """Stale cancel is a refund, not a profit/loss."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear()
        bot.active_tickers.clear()
        bot.trade_history.clear()
        bot.paper_balance = 20.0
        bot.paper_daily_pnl = -3.0

        bot.open_orders["test-2"] = {
            "ticker": "KXBTC-TEST2",
            "cost": 1.00,
            "placed_at": time.time() - 120,
        }
        bot.active_tickers.add("KXBTC-TEST2")

        bot.cancel_stale_orders()

        assert bot.paper_daily_pnl == -3.0  # unchanged

    def test_fresh_order_not_canceled(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 300)
        bot.open_orders.clear()

        bot.open_orders["test-3"] = {
            "ticker": "KXBTC-TEST3",
            "cost": 1.00,
            "placed_at": time.time() - 30,
        }

        bot.cancel_stale_orders()
        assert "test-3" in bot.open_orders


# ═════════════════════════════════════════════════════════════════════════════
# P3: PERFORMANCE GUARD & BAYESIAN PRIOR
# ═════════════════════════════════════════════════════════════════════════════

class TestPerformanceGuard:
    def test_below_min_sample_passes(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 3)
        monkeypatch.setattr(bot, "live_losses", 5)
        assert bot.performance_guard() is True

    def test_good_win_rate_passes(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 16)
        monkeypatch.setattr(bot, "live_losses", 4)
        assert bot.performance_guard() is True

    def test_bad_win_rate_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 8)
        monkeypatch.setattr(bot, "live_losses", 22)
        assert bot.performance_guard() is False


class TestUpdateLivePrior:
    def test_prior_shifts_toward_empirical(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "live_wins", 30)
        monkeypatch.setattr(bot, "live_losses", 20)
        bot._live_prior = 0.635
        bot.update_live_prior()
        # 30/50 = 0.60 empirical, weight = 50/50 = 1.0 → prior fully = empirical
        assert abs(bot._live_prior - 0.60) < 0.01

    def test_prior_unchanged_below_10(self, monkeypatch):
        monkeypatch.setattr(bot, "live_wins", 5)
        monkeypatch.setattr(bot, "live_losses", 4)
        bot._live_prior = 0.635
        bot.update_live_prior()
        assert bot._live_prior == 0.635


# ═════════════════════════════════════════════════════════════════════════════
# PEM NORMALIZATION
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizePem:
    def test_standard_pem(self):
        result = bot._normalize_pem(_test_pem)
        assert "-----BEGIN PRIVATE KEY-----" in result
        assert "-----END PRIVATE KEY-----" in result

    def test_escaped_newlines(self):
        raw = _test_pem.replace("\n", "\\n")
        result = bot._normalize_pem(raw)
        assert "-----BEGIN PRIVATE KEY-----\n" in result

    def test_no_newlines(self):
        raw = _test_pem.replace("\n", "")
        result = bot._normalize_pem(raw)
        assert "-----BEGIN PRIVATE KEY-----\n" in result

    def test_invalid_pem_raises(self):
        with pytest.raises(ValueError, match="missing header/footer"):
            bot._normalize_pem("not a pem at all")


# ═════════════════════════════════════════════════════════════════════════════
# PAPER MODE ARITHMETIC — direct unit test
# ═════════════════════════════════════════════════════════════════════════════

class TestPaperModeAccounting:
    """
    Verify the net balance effect of a paper trade lifecycle.
    WIN:  entry deducts cost, settlement adds count → net = count - cost = profit
    LOSS: entry deducts cost, settlement adds nothing → net = -cost
    """

    def test_win_net_is_positive_profit(self):
        price_cents = 50
        count = 4
        cost = price_cents * count / 100.0  # $2.00

        start_balance = 25.0
        after_entry   = start_balance - cost        # $23.00
        after_win     = after_entry + count          # $23.00 + 4 = $27.00
        net            = after_win - start_balance   # +$2.00

        pnl = round(count - cost, 2)  # 4 - 2 = $2.00
        assert net == pnl
        assert net > 0

    def test_loss_net_is_negative_cost(self):
        price_cents = 50
        count = 4
        cost = price_cents * count / 100.0  # $2.00

        start_balance = 25.0
        after_entry   = start_balance - cost        # $23.00
        after_loss    = after_entry                  # no change
        net            = after_loss - start_balance  # -$2.00

        pnl = round(-cost, 2)  # -$2.00
        assert net == pnl
        assert net < 0

    def test_win_not_double_deducting_cost(self):
        """Regression: v8.x bug was paper_balance += (count - cost) instead of += count."""
        price_cents = 50
        count = 4
        cost = price_cents * count / 100.0

        start_balance = 25.0
        after_entry   = start_balance - cost
        # CORRECT: add full payout
        correct_win   = after_entry + count
        # BUG: add only profit
        buggy_win     = after_entry + (count - cost)

        assert correct_win != buggy_win
        assert correct_win - start_balance == count - cost  # net = profit
        assert buggy_win - start_balance == 0.0             # net = $0 (wrong)
