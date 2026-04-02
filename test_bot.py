"""
test_bot.py — Pytest suite for Johnny5-Kalshi-Auto v7.0.0

Covers:
  P0: All risk controls (guards that protect real money)
  P1: Signal math (OB imbalance, edge, Kelly, momentum, confidence)
  P2: Features (adaptive threshold, OB trend, liquidity filter, stale cancel)
  P3: Wilson confidence intervals and performance guard

Usage:
  pip install pytest
  pytest test_bot.py -v
"""

import os
import sys
import time

# ── Set dummy env vars BEFORE importing bot ──────────────────────────────────
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


# ═════════════════════════════════════════════════════════════════════════════
# P0: RISK CONTROLS — these protect real money
# ═════════════════════════════════════════════════════════════════════════════

class TestBalanceFloorCheck:
    def test_below_floor_returns_false(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(4.99) is False

    def test_at_floor_returns_true(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(5.00) is True

    def test_above_floor_returns_true(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(100.0) is True

    def test_zero_balance(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(0.0) is False


class TestDailyLossCheck:
    def test_within_limit_returns_true(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -10.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 20.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(15.0) is True

    def test_at_limit_returns_false(self, monkeypatch):
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


class TestSpreadCheck:
    def test_normal_spread(self):
        assert bot.spread_check(48, 52) is True

    def test_one_cent_spread(self):
        assert bot.spread_check(49, 50) is True

    def test_zero_spread(self):
        assert bot.spread_check(50, 50) is False

    def test_crossed_spread(self):
        assert bot.spread_check(52, 48) is False

    def test_negative_spread(self):
        assert bot.spread_check(55, 45) is False


class TestExpiryGuard:
    def test_near_certain_yes(self):
        assert bot.expiry_guard(90) is False

    def test_near_certain_no(self):
        assert bot.expiry_guard(10) is False

    def test_boundary_high_blocked(self):
        assert bot.expiry_guard(86) is False

    def test_boundary_high_allowed(self):
        assert bot.expiry_guard(85) is True

    def test_boundary_low_blocked(self):
        assert bot.expiry_guard(14) is False

    def test_boundary_low_allowed(self):
        assert bot.expiry_guard(15) is True

    def test_normal_mid(self):
        assert bot.expiry_guard(50) is True

    def test_edge_valid(self):
        assert bot.expiry_guard(16) is True
        assert bot.expiry_guard(84) is True


class TestCooldown:
    def test_cooldown_not_passed(self, monkeypatch):
        monkeypatch.setattr(bot, "last_trade_ts", time.time())
        assert bot.cooldown_passed() is False

    def test_cooldown_passed(self, monkeypatch):
        monkeypatch.setattr(bot, "last_trade_ts", time.time() - 9999)
        assert bot.cooldown_passed() is True


class TestLiquidityHoursCheck:
    def test_inside_low_liquidity_window(self, monkeypatch):
        monkeypatch.setattr(bot, "LOW_LIQ_START_UTC", 4)
        monkeypatch.setattr(bot, "LOW_LIQ_END_UTC", 8)
        from unittest.mock import patch
        from datetime import datetime, timezone
        mock_dt = datetime(2025, 6, 15, 5, 30, tzinfo=timezone.utc)
        with patch("bot.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert bot.liquidity_hours_check() is False

    def test_outside_low_liquidity_window(self, monkeypatch):
        monkeypatch.setattr(bot, "LOW_LIQ_START_UTC", 4)
        monkeypatch.setattr(bot, "LOW_LIQ_END_UTC", 8)
        from unittest.mock import patch
        from datetime import datetime, timezone
        mock_dt = datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc)
        with patch("bot.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert bot.liquidity_hours_check() is True


class TestConcurrentPositionCheck:
    def test_under_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONCURRENT_POS", 2)
        bot.open_orders.clear()
        bot.open_orders["a"] = {}
        assert bot.concurrent_position_check() is True
        bot.open_orders.clear()

    def test_at_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONCURRENT_POS", 2)
        bot.open_orders.clear()
        bot.open_orders["a"] = {}
        bot.open_orders["b"] = {}
        assert bot.concurrent_position_check() is False
        bot.open_orders.clear()

    def test_empty(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONCURRENT_POS", 2)
        bot.open_orders.clear()
        assert bot.concurrent_position_check() is True


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
        edge = bot.calc_edge(0.30, 50)
        assert edge < 0

    def test_boundary_price_zero(self):
        assert bot.calc_edge(0.70, 0) == 0.0

    def test_boundary_price_100(self):
        assert bot.calc_edge(0.70, 100) == 0.0

    def test_cheap_contract(self):
        edge = bot.calc_edge(0.40, 20)
        assert abs(edge - 0.20) < 0.001


class TestKellyBetSize:
    def test_positive_edge_returns_bet(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_DOLLARS", 5.0)
        monkeypatch.setattr(bot, "PROFILE", {"kelly_frac": 0.35})
        bet = bot.kelly_bet_size(0.70, 50, 25.0)
        assert bet > 0
        assert bet <= 5.0

    def test_no_edge_returns_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_DOLLARS", 5.0)
        monkeypatch.setattr(bot, "PROFILE", {"kelly_frac": 0.35})
        bet = bot.kelly_bet_size(0.30, 50, 25.0)
        assert bet == 0.0

    def test_capped_at_balance_fraction(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_DOLLARS", 100.0)
        monkeypatch.setattr(bot, "PROFILE", {"kelly_frac": 0.50})
        bet = bot.kelly_bet_size(0.80, 40, 10.0)
        assert bet <= 2.0  # 20% of $10 — but MAX_BET_FRACTION is 10% = $1

    def test_boundary_zero_price(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_DOLLARS", 5.0)
        monkeypatch.setattr(bot, "PROFILE", {"kelly_frac": 0.35})
        assert bot.kelly_bet_size(0.70, 0, 25.0) == 0.0

    def test_boundary_100_price(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_DOLLARS", 5.0)
        monkeypatch.setattr(bot, "PROFILE", {"kelly_frac": 0.35})
        assert bot.kelly_bet_size(0.70, 100, 25.0) == 0.0


class TestBtcMomentumSignal:
    def test_not_enough_data(self):
        bot.btc_prices.clear()
        bot.btc_prices.append(50000)
        verdict, boost = bot.btc_momentum_signal("YES")
        assert verdict == "NEUTRAL"

    def test_agree_yes(self):
        bot.btc_prices.clear()
        for p in [50000, 50050, 50100, 50200, 50300]:
            bot.btc_prices.append(p)
        verdict, boost = bot.btc_momentum_signal("YES")
        assert verdict == "AGREE"
        assert boost > 0

    def test_conflict_yes_btc_down(self):
        bot.btc_prices.clear()
        for p in [50000, 49900, 49800, 49700, 49500]:
            bot.btc_prices.append(p)
        verdict, boost = bot.btc_momentum_signal("YES")
        assert verdict == "CONFLICT"

    def test_neutral_flat(self):
        bot.btc_prices.clear()
        for p in [50000, 50010, 50020, 50030, 50040]:
            bot.btc_prices.append(p)
        verdict, boost = bot.btc_momentum_signal("YES")
        assert verdict == "NEUTRAL"

    def test_agree_no_btc_down(self):
        bot.btc_prices.clear()
        for p in [50000, 49900, 49800, 49700, 49500]:
            bot.btc_prices.append(p)
        verdict, boost = bot.btc_momentum_signal("NO")
        assert verdict == "AGREE"


class TestWilsonConfidence:
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

    def test_small_sample(self):
        pct, lo, hi = bot.wilson_confidence(3, 5)
        assert hi - lo > 20

    def test_large_sample_narrow(self):
        pct, lo, hi = bot.wilson_confidence(70, 100)
        assert hi - lo < 20


# ═════════════════════════════════════════════════════════════════════════════
# P2: v7.0.0 / v5.3.0 FEATURES
# ═════════════════════════════════════════════════════════════════════════════

class TestAdaptiveObThreshold:
    def test_thin_book_raises_threshold(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        assert bot.adaptive_ob_threshold(8.0) >= 0.70

    def test_thick_book_lowers_threshold(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        assert bot.adaptive_ob_threshold(60.0) <= 0.58

    def test_medium_book_uses_default(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        assert bot.adaptive_ob_threshold(30.0) == 0.62

    def test_boundary_15(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        assert bot.adaptive_ob_threshold(15.0) == 0.62

    def test_boundary_50(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        assert bot.adaptive_ob_threshold(50.0) <= 0.58


class TestObTrendCheck:
    def setup_method(self):
        bot._prev_ob.clear()

    def test_first_observation_allows(self):
        assert bot.ob_trend_check("KXBTC-TEST", 0.70, "YES") is True

    def test_building_pressure_allows(self):
        bot._prev_ob["KXBTC-TEST"] = (0.65, "YES", time.time())
        assert bot.ob_trend_check("KXBTC-TEST", 0.72, "YES") is True

    def test_fading_pressure_blocks(self):
        bot._prev_ob["KXBTC-TEST"] = (0.75, "YES", time.time())
        assert bot.ob_trend_check("KXBTC-TEST", 0.60, "YES") is False

    def test_direction_flip_blocks(self):
        bot._prev_ob["KXBTC-TEST"] = (0.70, "YES", time.time())
        assert bot.ob_trend_check("KXBTC-TEST", 0.70, "NO") is False

    def test_stale_data_allows(self):
        bot._prev_ob["KXBTC-TEST"] = (0.80, "YES", time.time() - 700)
        assert bot.ob_trend_check("KXBTC-TEST", 0.60, "NO") is True

    def test_small_fade_allows(self):
        bot._prev_ob["KXBTC-TEST"] = (0.70, "YES", time.time())
        assert bot.ob_trend_check("KXBTC-TEST", 0.66, "YES") is True


class TestCalcObImbalance:
    def _make_ob(self, yes_levels, no_levels):
        return {
            "orderbook_fp": {
                "yes_dollars": yes_levels,
                "no_dollars":  no_levels,
            }
        }

    def test_strong_yes_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        monkeypatch.setattr(bot, "MIN_OB_DEPTH_DOLLARS", 5.0)
        ob = self._make_ob(
            [[0.48, 20], [0.50, 20]],
            [[0.50, 10]],
        )
        imb, direction, depth = bot.calc_ob_imbalance(ob, 50)
        assert direction == "YES"
        assert imb >= 0.70

    def test_strong_no_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        monkeypatch.setattr(bot, "MIN_OB_DEPTH_DOLLARS", 5.0)
        ob = self._make_ob(
            [[0.50, 5]],
            [[0.48, 20], [0.50, 20]],
        )
        imb, direction, depth = bot.calc_ob_imbalance(ob, 50)
        assert direction == "NO"

    def test_balanced_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        monkeypatch.setattr(bot, "MIN_OB_DEPTH_DOLLARS", 5.0)
        ob = self._make_ob(
            [[0.50, 15]],
            [[0.50, 15]],
        )
        imb, direction, depth = bot.calc_ob_imbalance(ob, 50)
        assert direction == "NONE"

    def test_thin_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        monkeypatch.setattr(bot, "MIN_OB_DEPTH_DOLLARS", 5.0)
        ob = self._make_ob(
            [[0.50, 2]],
            [[0.50, 1]],
        )
        imb, direction, depth = bot.calc_ob_imbalance(ob, 50)
        assert direction == "NONE"

    def test_returns_depth(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.62})
        monkeypatch.setattr(bot, "MIN_OB_DEPTH_DOLLARS", 5.0)
        ob = self._make_ob(
            [[0.50, 30]],
            [[0.50, 10]],
        )
        imb, direction, depth = bot.calc_ob_imbalance(ob, 50)
        assert depth == 40.0


class TestCancelStaleOrders:
    def test_paper_mode_refunds(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear()
        bot.paper_balance = 20.0
        bot.paper_daily_pnl = -5.0
        bot.active_tickers.clear()
        bot.trade_history.clear()

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

    def test_fresh_order_not_canceled(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 300)
        bot.open_orders.clear()

        bot.open_orders["test-2"] = {
            "ticker": "KXBTC-TEST2",
            "cost": 1.00,
            "placed_at": time.time() - 30,
        }

        bot.cancel_stale_orders()

        assert "test-2" in bot.open_orders


# ═════════════════════════════════════════════════════════════════════════════
# P3: CONFIDENCE SCORING — v7.0.0 depth fix
# ═════════════════════════════════════════════════════════════════════════════

class TestConfidenceDepthScoring:
    """Verify the corrected depth scoring formula."""

    def test_depth_50_gives_10pts(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.70})
        monkeypatch.setattr(bot, "MINIMUM_CONFIDENCE", 0)
        score = bot.compute_confidence_score(
            ob_quality={"imbalance": 0.70, "near_money_depth": 50.0},
            regime="UNKNOWN", r_squared=0.0,
            momentum_verdict="NEUTRAL", momentum_boost=0.0,
            mins_remaining=10.0,
        )
        # With UNKNOWN regime (5pts) + 0 imb_pts + 10 depth + 0 momentum + 10 time = 25
        assert 24.0 <= score <= 26.0

    def test_depth_200_gives_20pts(self, monkeypatch):
        monkeypatch.setattr(bot, "PROFILE", {"ob_thresh": 0.70})
        monkeypatch.setattr(bot, "MINIMUM_CONFIDENCE", 0)
        score = bot.compute_confidence_score(
            ob_quality={"imbalance": 0.70, "near_money_depth": 200.0},
            regime="UNKNOWN", r_squared=0.0,
            momentum_verdict="NEUTRAL", momentum_boost=0.0,
            mins_remaining=10.0,
        )
        # UNKNOWN(5) + 0 imb + 20 depth + 0 momentum + 10 time = 35
        assert 34.0 <= score <= 36.0


# ═════════════════════════════════════════════════════════════════════════════
# PEM NORMALIZATION
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizePem:
    def test_standard_pem(self):
        raw = _test_pem
        result = bot._normalize_pem(raw)
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
        assert "-----END PRIVATE KEY-----" in result

    def test_invalid_pem_raises(self):
        with pytest.raises(ValueError, match="missing header/footer"):
            bot._normalize_pem("not a pem at all")
