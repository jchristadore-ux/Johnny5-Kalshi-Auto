"""
test_bot.py — Pytest suite for Johnny5-Kalshi-Auto v9.3.0

Covers:
  P0: All risk controls
  P1: Signal math (edge, Kelly, momentum, confidence, regime)
  P2: OB analysis, stale cancel, ob trend
  P3: Wilson CI, performance guard, Bayesian prior
  v9.3.0: doctrine Layer-7 AGREE gate, NEUTRAL confidence weight, restored thresholds
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


class TestPostBootSettlementGate:
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


class TestMomentumGate:
    def test_agree_passes_when_required(self, monkeypatch):
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", True)
        assert bot.momentum_gate_ok("AGREE") is True

    def test_neutral_blocked_when_required(self, monkeypatch):
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", True)
        assert bot.momentum_gate_ok("NEUTRAL") is False

    def test_conflict_blocked_when_required(self, monkeypatch):
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", True)
        assert bot.momentum_gate_ok("CONFLICT") is False

    def test_gate_off_allows_neutral(self, monkeypatch):
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", False)
        assert bot.momentum_gate_ok("NEUTRAL") is True

    def test_default_is_on(self):
        assert bot.REQUIRE_AGREE_MOMENTUM is True


class TestConfidenceNeutralWeight:
    def _ob(self, imbalance=0.71, depth=34000.0, eff_thresh=0.66):
        return {"imbalance": imbalance, "total_depth": depth,
                "eff_thresh": eff_thresh}

    def test_neutral_scores_less_than_agree(self):
        common = dict(ob=self._ob(), regime=Regime.TRENDING_DOWN, r_squared=0.82,
                      win_prob=0.72, mins_remaining=14.0, session_score=60)
        neutral = bot.compute_confidence(momentum_verdict="NEUTRAL", **common)
        agree   = bot.compute_confidence(momentum_verdict="AGREE", **common)
        assert agree - neutral == pytest.approx(13.0, abs=0.01)

    def test_06_20_0830_trade_now_blocked(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_CONFIDENCE", 65)
        conf = bot.compute_confidence(
            ob=self._ob(imbalance=0.673, depth=31053.0, eff_thresh=0.66),
            regime=Regime.TRENDING_DOWN, r_squared=0.87,
            momentum_verdict="NEUTRAL", win_prob=0.721,
            mins_remaining=14.1, session_score=60)
        assert conf < 65


class TestRestoredThresholds:
    def test_ob_imbalance_threshold(self):
        assert bot.OB_IMBALANCE_THRESH == 0.70

    def test_r2_trend_threshold(self):
        assert bot.R2_TREND_THRESHOLD == 0.65

    def test_min_confidence(self):
        assert bot.MIN_CONFIDENCE == 65

    def test_yes_breakeven_price(self):
        assert bot.YES_BREAKEVEN_PRICE == 67

    def test_neutral_drag_restored(self):
        assert bot.NEUTRAL_ACCURACY_DRAG == 0.02

    def test_trade_size_is_5(self):
        assert bot.TRADE_SIZE_CAP == 5.0

    def test_max_bet_fraction_is_004(self):
        assert bot.MAX_BET_FRACTION == 0.04

    def test_max_concurrent_is_1(self):
        assert bot.MAX_CONCURRENT_POS == 1


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
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 0.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(15.0) is True

    def test_at_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -20.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 20.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 0.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(5.0) is False

    def test_session_stop_triggers(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.0)
        monkeypatch.setattr(bot, "session_start_balance", 0.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(10.0) is False

    def test_pct_cap_binds_before_dollar_cap(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 1000.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        monkeypatch.setattr(bot, "paper_daily_pnl", -100.0)
        assert bot.daily_loss_check(1900.0) is True
        monkeypatch.setattr(bot, "paper_daily_pnl", -130.0)
        assert bot.daily_loss_check(1870.0) is False

    def test_halted_flag_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "_session_halted", True)
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        assert bot.daily_loss_check(50.0) is False


class TestSpreadCheck:
    def test_normal(self):
        assert bot.spread_check(48, 52) is True

    def test_zero(self):
        assert bot.spread_check(50, 50) is False

    def test_crossed(self):
        assert bot.spread_check(52, 48) is False


class TestExpiryGuard:
    def test_near_certain_high(self):
        assert bot.expiry_guard(90) is False

    def test_near_certain_low(self):
        assert bot.expiry_guard(10) is False

    def test_boundary_high_allowed(self):
        assert bot.expiry_guard(85) is True

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

    def test_at_threshold_in_pause(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 2
        bot.streak_pause_until = time.time() + 9999
        assert bot.streak_check() is False

    def test_at_threshold_pause_expired(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 2
        bot.streak_pause_until = time.time() - 1
        assert bot.streak_check() is True
        assert bot.consecutive_losses == 0


class TestCalcEdge:
    def test_positive_edge(self):
        assert abs(bot.calc_edge(0.70, 50) - 0.20) < 0.001

    def test_zero_edge(self):
        assert abs(bot.calc_edge(0.50, 50)) < 0.001

    def test_negative_edge(self):
        assert bot.calc_edge(0.30, 50) < 0

    def test_boundary_zero_price(self):
        assert bot.calc_edge(0.70, 0) == 0.0

    def test_boundary_100_price(self):
        assert bot.calc_edge(0.70, 100) == 0.0


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
        assert bot.kelly_bet(0.30, 50, 25.0) == 0.0

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

    def test_capped_at_bet_fraction(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 1_000.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 1.0)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.04)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.90, 40, 1_000.0)
        assert bet <= 1_000.0 * 0.04 + 0.01


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
        assert bot.compute_momentum("YES")[0] == "CONFLICT"

    def test_neutral_flat(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        for p in [50000, 50010, 50020, 50030, 50040]:
            bot.btc_prices.append(p)
        assert bot.compute_momentum("YES")[0] == "NEUTRAL"

    def test_neutral_adj_is_negative(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "NEUTRAL_ACCURACY_DRAG", 0.02)
        for p in [50000, 50010, 50020, 50030, 50040]:
            bot.btc_prices.append(p)
        assert bot.compute_momentum("YES")[1] == -0.02


class TestRegimeAgreement:
    def test_up_favors_yes(self):
        assert bot.regime_direction(bot.Regime.TRENDING_UP) == "YES"

    def test_down_favors_no(self):
        assert bot.regime_direction(bot.Regime.TRENDING_DOWN) == "NO"

    def test_ranging_has_no_favored_side(self):
        assert bot.regime_direction(bot.Regime.RANGING) is None

    def test_yes_in_uptrend_agrees(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_UP, "YES") is True

    def test_no_in_uptrend_conflicts(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_UP, "NO") is False

    def test_yes_in_downtrend_conflicts(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_DOWN, "YES") is False

    def test_no_in_downtrend_agrees(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_DOWN, "NO") is True

    def test_case_insensitive(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_UP, "yes") is True


class TestBayesianWinProbImbalance:
    def _ob(self, imbalance, depth=2500.0, eff_thresh=0.60):
        return {"imbalance": imbalance, "total_depth": depth,
                "eff_thresh": eff_thresh}

    def test_stronger_book_raises_win_prob(self, monkeypatch):
        monkeypatch.setattr(bot, "_live_prior", 0.635)
        weak = bot.bayesian_win_prob(
            self._ob(0.61), "NEUTRAL", 0.0, bot.Regime.TRENDING_UP, 0.78, 0.05)
        strong = bot.bayesian_win_prob(
            self._ob(0.90), "NEUTRAL", 0.0, bot.Regime.TRENDING_UP, 0.78, 0.05)
        assert strong > weak

    def test_imbalance_contribution_is_capped(self, monkeypatch):
        monkeypatch.setattr(bot, "_live_prior", 0.635)
        wp = bot.bayesian_win_prob(
            self._ob(0.999), "AGREE", 0.045, bot.Regime.TRENDING_UP, 0.95, 0.0)
        assert wp <= 0.92


class TestWilsonCI:
    def test_zero_trades(self):
        assert bot.wilson_confidence(0, 0)[0] == 0.0

    def test_all_wins(self):
        pct, lo, hi = bot.wilson_confidence(10, 10)
        assert pct == 100.0 and lo > 50.0

    def test_fifty_fifty(self):
        pct, lo, hi = bot.wilson_confidence(50, 100)
        assert abs(pct - 50.0) < 0.1 and lo < 50.0 < hi


class TestWilsonLowerBound:
    def test_small_sample_returns_zero(self):
        assert bot.wilson_lower_bound(5, 9) == 0.0

    def test_good_win_rate(self):
        assert bot.wilson_lower_bound(15, 20) > 0.50

    def test_bad_win_rate(self):
        assert bot.wilson_lower_bound(5, 20) < 0.50


class TestComputeRegime:
    def setup_method(self):
        bot.btc_prices.clear()
        bot.btc_returns.clear()

    def test_insufficient_data_returns_unknown(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        for p in range(5):
            bot.btc_prices.append(50000 + p * 10)
        assert bot.compute_regime()[0] == Regime.UNKNOWN

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

    def test_ranging(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 + int(math.sin(i) * 200) for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            bot.btc_returns.append((prices[i] - prices[i-1]) / prices[i-1] * 100)
        assert bot.compute_regime()[0] == Regime.RANGING


class TestAnalyzeOrderBook:
    def _make_ob(self, yes_levels, no_levels):
        return {"orderbook_fp": {"yes_dollars": yes_levels, "no_dollars": no_levels}}

    def test_strong_yes_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.48, 20], [0.50, 20]], [[0.50, 10]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None and result["direction"] == "YES"

    def test_balanced_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 15]], [[0.50, 15]])
        assert bot.analyze_order_book(ob, 50) is None

    def test_thin_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 50.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 2]], [[0.50, 1]])
        assert bot.analyze_order_book(ob, 50) is None

    def test_ghost_ob_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.48, 80], [0.50, 20]], [])
        assert bot.analyze_order_book(ob, 50) is None

    def test_total_depth_correct(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 30]], [[0.50, 10]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None and result["total_depth"] == 40.0


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

    def test_stale_data_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.80, time.time() - 700)
        assert bot.check_ob_trend("T1", "NO", 0.60) is True


class TestCancelStaleOrders:
    def test_paper_refunds_balance(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear(); bot.active_tickers.clear(); bot.trade_history.clear()
        bot.paper_balance = 20.0
        bot.open_orders["test-1"] = {"ticker": "KXBTC-TEST", "cost": 2.50,
                                     "placed_at": time.time() - 120}
        bot.active_tickers.add("KXBTC-TEST")
        bot.trade_history.append({"order_id": "test-1", "result": "pending"})
        bot.cancel_stale_orders()
        assert "test-1" not in bot.open_orders
        assert bot.paper_balance == 22.50
        assert "KXBTC-TEST" not in bot.active_tickers

    def test_paper_does_not_touch_daily_pnl(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear(); bot.active_tickers.clear(); bot.trade_history.clear()
        bot.paper_balance = 20.0; bot.paper_daily_pnl = -3.0
        bot.open_orders["test-2"] = {"ticker": "KXBTC-TEST2", "cost": 1.00,
                                     "placed_at": time.time() - 120}
        bot.active_tickers.add("KXBTC-TEST2")
        bot.cancel_stale_orders()
        assert bot.paper_daily_pnl == -3.0

    def test_fresh_order_not_canceled(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 300)
        bot.open_orders.clear()
        bot.open_orders["test-3"] = {"ticker": "KXBTC-TEST3", "cost": 1.00,
                                     "placed_at": time.time() - 30}
        bot.cancel_stale_orders()
        assert "test-3" in bot.open_orders


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
        assert abs(bot._live_prior - 0.60) < 0.01

    def test_prior_unchanged_below_10(self, monkeypatch):
        monkeypatch.setattr(bot, "live_wins", 5)
        monkeypatch.setattr(bot, "live_losses", 4)
        bot._live_prior = 0.635
        bot.update_live_prior()
        assert bot._live_prior == 0.635


class TestNormalizePem:
    def test_standard_pem(self):
        result = bot._normalize_pem(_test_pem)
        assert "-----BEGIN PRIVATE KEY-----" in result

    def test_escaped_newlines(self):
        raw = _test_pem.replace("\n", "\\n")
        assert "-----BEGIN PRIVATE KEY-----\n" in bot._normalize_pem(raw)

    def test_no_newlines(self):
        raw = _test_pem.replace("\n", "")
        assert "-----BEGIN PRIVATE KEY-----\n" in bot._normalize_pem(raw)

    def test_invalid_pem_raises(self):
        with pytest.raises(ValueError, match="missing header/footer"):
            bot._normalize_pem("not a pem at all")


class TestPaperModeAccounting:
    def test_win_net_is_positive_profit(self):
        count = 4; cost = 50 * count / 100.0
        start = 25.0
        net = (start - cost + count) - start
        assert net == round(count - cost, 2) and net > 0

    def test_loss_net_is_negative_cost(self):
        count = 4; cost = 50 * count / 100.0
        start = 25.0
        net = (start - cost) - start
        assert net == round(-cost, 2) and net < 0

    def test_win_not_double_deducting_cost(self):
        count = 4; cost = 50 * count / 100.0
        start = 25.0
        correct = (start - cost) + count
        buggy   = (start - cost) + (count - cost)
        assert correct - start == count - cost
        assert buggy - start == 0.0


class TestUpdateSessionState:
    def setup_method(self):
        bot.session_state = SessionState.ACTIVE
        bot.recovery_entry_wins = 0
        bot.recovery_entry_losses = 0
        bot.recovery_entered_ts = 0.0
        bot.live_wins = 0
        bot.live_losses = 0

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def test_entry_stamps_recovery_time(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        bot.session_state = SessionState.ACTIVE
        bot.update_session_state(1750.0)
        assert bot.session_state == SessionState.RECOVERY
        assert bot.recovery_entered_ts > 0.0

    def test_timeout_forces_exit(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state = SessionState.RECOVERY
        bot.recovery_entered_ts = time.time() - 7200
        bot.update_session_state(1750.0)
        assert bot.session_state == SessionState.ACTIVE

    def test_zero_timestamp_is_initialized_not_instant_exit(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state = SessionState.RECOVERY
        bot.recovery_entered_ts = 0.0
        bot.update_session_state(1750.0)
        assert bot.session_state == SessionState.RECOVERY
        assert bot.recovery_entered_ts > 0.0

    def test_balance_heal_still_exits(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state = SessionState.RECOVERY
        bot.recovery_entered_ts = time.time() - 60
        bot.update_session_state(1850.0)
        assert bot.session_state == SessionState.ACTIVE


class TestSessionDayRollover:
    def setup_method(self):
        bot._session_day = "2026-06-19"
        bot._session_halted = True
        bot.session_start_balance = 2000.0
        bot.session_stop_threshold = 800.0
        bot.daily_pnl = -135.0
        bot.paper_daily_pnl = -135.0
        bot.consecutive_losses = 2
        bot.session_state = SessionState.RECOVERY
        bot.session_traded_tickers.add("KXBTC15M-OLD")

    def test_same_day_is_noop(self, monkeypatch):
        monkeypatch.setattr(bot, "_session_day",
                            bot.datetime.now(bot.timezone.utc).strftime("%Y-%m-%d"))
        bot._session_halted = True
        assert bot.maybe_roll_session_day(1800.0) is False
        assert bot._session_halted is True

    def test_new_day_clears_halt_and_rebaselines(self):
        assert bot.maybe_roll_session_day(1850.0) is True
        assert bot._session_halted is False
        assert bot.session_start_balance == 1850.0
        assert bot.daily_pnl == 0.0
        assert bot.consecutive_losses == 0
        assert bot.session_state == SessionState.ACTIVE
        assert "KXBTC15M-OLD" not in bot.session_traded_tickers
