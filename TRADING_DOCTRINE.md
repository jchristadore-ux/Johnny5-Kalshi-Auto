# TRADING DOCTRINE — Johnny5-Kalshi-Auto v6.0.0

> "A patient quantitative trader who waits all day for one unfair opportunity."
>
> If trade frequency drops 80% but expectancy rises, that is success.

---

## 1. What This Bot Trades

**Instrument:** Kalshi 15-minute BTC binary prediction markets (KXBTC15M series)

**Structure:** Each contract pays $1.00 if correct, $0.00 if wrong.
At a 50¢ price, breakeven win rate = 50%. Edge exists only when true win probability materially exceeds the implied contract price.

**Single market focus:** The bot finds the market closest to 50¢ mid-price (most liquid, lowest cost per contract).

---

## 2. When the Bot Trades — The Complete Checklist

A trade is placed **only when all nine layers pass simultaneously**. Every layer must pass. One failure = no trade.

### Layer 1 — Hard Guards (always first)
- [ ] Balance ≥ $5.00 (floor)
- [ ] Contract mid-price between 15¢ and 85¢ (not near-certain outcome)
- [ ] Bid/ask spread > 0¢ (not a crossed or broken book)
- [ ] No existing position on this ticker
- [ ] Cooldown timer ≥ 120 seconds since last trade
- [ ] Daily session P&L above halt threshold

### Layer 2 — Streak Pause
- [ ] Fewer than 2 consecutive losses **OR** 30-minute cooldown has expired since the pause was triggered

**Rationale:** Two consecutive losses suggest either bad luck or a regime where the signal has no edge. 30 minutes is enough for BTC to complete one or more micro-regimes. One window (30 seconds) is not.

### Layer 3 — Statistical Performance Guard
- [ ] After 20+ settled trades: Wilson score confidence interval lower bound ≥ 50%

**Rationale:** If the live win rate — at 90% statistical confidence — cannot be demonstrated to be above a coin flip, the bot has no proven edge and must not risk capital. Before 20 trades, benefit of the doubt is extended.

### Layer 4 — Time Quality
- [ ] Current UTC hour is **not** in `{0, 1, 2, 3, 4}` (post-US-close thin books)
- [ ] At least 3 minutes remaining until market closes

**Rationale:** Near expiry, OB signals reflect resolution certainty rather than directional pressure. Low-liquidity hours produce thin books where 1-2 orders can dominate the OB.

### Layer 5 — Market Regime (Regime Detection)
- [ ] BTC price regime is **TRENDING** (R² > 0.65 on linear regression of last 10 price samples)
- [ ] Regime is **not** HIGH_VOL (mean absolute return per 30s bar > 0.15%)

**Rationale:** The 68.8% historical OB accuracy was measured in trending conditions. In a ranging market, OB imbalance has no directional predictive value — the smart-money positioning thesis requires that smart money is actually positioning directionally. In HIGH_VOL, unpredictable spike-and-reverse dynamics invalidate all 15-minute signals.

This single filter is estimated to eliminate 50-70% of trade attempts in typical sessions.

### Layer 6 — Order Book Quality
- [ ] Near-money depth (±10¢ of mid) ≥ **$50 total**
- [ ] Dominant side ratio ≥ **70%** (YES or NO)

**Rationale:** A $5 depth threshold (v5) allowed a single retail order to generate a signal. $50 requires real multi-party participation. At 70% imbalance with $50+ depth, the order book represents a meaningful institutional consensus.

### Layer 7 — BTC Momentum Confirmation (AGREE required)
- [ ] BTC spot price momentum **explicitly AGREES** with OB direction (>0.20% move in same direction over last 2 minutes)

**Rationale:** In v5, NEUTRAL momentum (flat BTC) was acceptable — the bot traded on OB alone. In choppy markets BTC is flat 60-80% of the time, meaning the bot was effectively unconfirmed on the majority of its trades. Now we require both signals to align. NEUTRAL is a rejection.

### Layer 8 — Confidence Score ≥ 65
- [ ] Composite confidence score ≥ **65/100**

The score combines all signals into one number:

| Component | Max Points | Description |
|-----------|-----------|-------------|
| OB imbalance strength | 30 | Linear scale from threshold (70%) to 100% |
| OB near-money depth | 20 | $50 = 10pts, $200 = 20pts |
| Market regime | 25+5 | TRENDING=25, +bonus for high R² |
| BTC momentum | 15 | Only counts if AGREE; scales with move size |
| Time remaining | 10 | Full at ≥10 min, zero at 3 min |

A TRENDING regime contributes 25+ points. Without it, the maximum achievable score is ~45 — below the 65 minimum. This means **RANGING market = no trades, always**, regardless of how strong the OB signal looks.

### Layer 9 — Edge, Sizing, and Execution
- [ ] Calculated edge (EV) ≥ 6% of stake
- [ ] Kelly bet size ≥ $0.25 (not a micro-bet)
- [ ] Sufficient balance to cover the bet
- [ ] Limit price is within 8¢ of mid (order can actually fill)

---

## 3. Why the Bot Trades — Edge Sources

### Primary Edge: Near-Money Order Book Pressure
When 70%+ of near-money depth ($50+) is stacked on one side of a liquid 15-minute binary market, smart money has expressed a directional view. Historically (limited dataset), this signal has correlated with outcomes at ~68.8% accuracy.

**Critical caveat:** This accuracy figure comes from a limited backtest. It is the hypothesis being tested in production, not a guarantee. The performance guard (Layer 3) exists specifically to detect when this edge degrades.

### Secondary Edge: BTC Spot Momentum Alignment
When BTC spot (Kraken) is moving directionally in the same direction as the OB signal, both signals agree. This reduces false positives from OB noise and increases win probability by ~3-6%.

### Structural Edge: Maker-Only Execution
Taker fees on Kalshi are approximately $5+/day at moderate frequency. Posting maker limit orders (1¢ inside spread) eliminates this friction. At a $25 starting balance, taker fees alone could consume the entire expected edge.

---

## 4. When the Bot Must NOT Trade — Explicit No-Trade Conditions

The bot **stands down** and logs the reason in any of these conditions:

| Condition | Reason |
|-----------|--------|
| BTC regime = RANGING | OB signals are noise. The smart-money thesis doesn't apply. |
| BTC regime = HIGH_VOL | Unpredictable, spike-driven. All signals invalidated. |
| BTC regime = UNKNOWN | Insufficient price history. No regime, no trade. |
| BTC momentum = NEUTRAL | One signal is not confirmation. Need both aligned. |
| BTC momentum = CONFLICT | Signals disagree. Expected value is uncertain or negative. |
| Near-money depth < $50 | Thin book. Signal can be generated by a single retail order. |
| OB imbalance < 70% | Signal not strong enough. Near-even book has no predictive value. |
| Confidence score < 65 | All factors taken together don't constitute a clear setup. |
| Low-liquidity hours | UTC 0-4 (post-US-close). Thin, unreliable books. |
| < 3 minutes to expiry | Price is resolving, not positioning. OB reflects certainty, not edge. |
| 2+ consecutive losses | Pause 30 minutes. Let the regime shift before re-entry. |
| Wilson CI LB < 50% | Live edge unproven statistically. Cannot risk capital without evidence. |
| Balance < $5.00 | Capital preservation. Micro-bets cannot compound meaningfully. |
| Daily session stop | 50% balance drawdown from session start. Preserve remaining capital. |
| Market mid > 85¢ or < 15¢ | Outcome near-certain. EV is minimal. Skip. |

---

## 5. Position Sizing — How Much to Risk

### Formula (v6.0.0 — corrected)

```
full_kelly = (b × win_prob − (1 − win_prob)) / b
           where b = (100 − price_cents) / price_cents

kelly_bet  = full_kelly × kelly_fraction × balance

actual_bet = min(kelly_bet, TRADE_SIZE_DOLLARS, MAX_BET_FRACTION × balance)
```

### Parameters

| Parameter | Default | Rationale |
|-----------|---------|-----------|
| `kelly_fraction` | 0.35 | 35% of full Kelly — reduces variance, survives bad runs |
| `TRADE_SIZE_DOLLARS` | $5.00 | Hard cap per trade |
| `MAX_BET_FRACTION` | 10% | Max 10% of balance per trade |

### Why 10% balance cap (not 20%)

v5 used 20% of balance. At a $25 balance, that permitted $5 bets (20% per trade). With a real win rate of 50% instead of 68.8%, this produces rapid drawdown. At 10%, five consecutive losses at flat 50¢ price:

```
$25.00 → $22.50 → $20.25 → $18.23 → $16.40 → $14.76
```

That's a 41% drawdown from 5 consecutive losses — painful, but survivable. The session stop at 50% would not trigger. Recovery is possible.

### v5 Sizing Bug (now fixed)

The v5 formula was:
```python
bet = full_kelly * kelly_frac * TRADE_SIZE_DOLLARS * 4.0
```

This used `TRADE_SIZE_DOLLARS * 4.0` (= $20) as a proxy for bankroll rather than actual balance. As balance decayed from $53 to $26, bet sizes did **not** shrink proportionally — they stayed anchored to the fixed $20 proxy. This accelerated drawdown instead of allowing the position sizing to self-correct.

---

## 6. Trade Management

### Entry
- Maker limit order 1¢ inside best bid/ask (no taker fees)
- One position per market ticker
- Maximum one open position at any time per series

### Settlement
- Kalshi settles automatically at market close (15 minutes)
- No manual exit required or possible — binary outcome only
- Unfilled maker orders are cleaned up after 20 minutes

### What "winning" looks like
- Win rate: target ≥ 65% on qualified setups (layer 1-9 all passed)
- Trade frequency: expect 1-5 trades per trading day (down from 10-20 in v5)
- Expectancy per trade: target > $0.30 at $25 balance
- Acceptable days: many days with zero trades are correct behavior

---

## 7. Statistical Framework

### The Core Question
At any given moment the bot must be able to answer: *"Do we have statistical evidence that the edge exists in current conditions?"*

If the answer is "no" or "insufficient data," the bot stands down.

### Wilson Score Confidence Interval
After every settled trade, the bot computes:

```
Wilson CI lower bound (90% confidence) on live win rate
```

If this lower bound falls below 50%, the statistical evidence does not support an above-breakeven win rate. Trading stops until conditions improve.

This is activated after 20 trades (the minimum for the confidence interval to carry weight).

### Expected Win Rate Degradation Triggers
The following market changes may degrade the edge:
- Competitors copying the OB pressure strategy (reducing signal rarity)
- Kalshi changing market microstructure
- BTC entering a prolonged low-volatility regime (no trending days)
- Changes in 15-minute binary market settlement rules

When degradation is detected (Wilson LB < 50%), do not override. Investigate first.

---

## 8. Regime Classification

The bot classifies BTC price behavior every 30 seconds using linear regression on the last 10 price samples (~5 minutes of data).

| Regime | Condition | Trading Status |
|--------|-----------|---------------|
| TRENDING | R² > 0.65 on linear fit | ✅ Trading allowed |
| RANGING | R² ≤ 0.65, low volatility | ❌ No trades |
| HIGH_VOL | Mean absolute return > 0.15% per 30s | ❌ No trades |
| UNKNOWN | < 8 price samples | ❌ No trades |

**Why R² = 0.65?**
R² measures how well a straight line explains the last 5 minutes of BTC price movement. At 0.65, about 65% of price variance is explained by direction. This is a reasonably confident trend. Below this, the market is oscillating enough to make OB pressure meaningless.

---

## 9. Logging and Auditability

### Every trade that fires logs an EDGE JUSTIFICATION record:

```
EDGE JUSTIFICATION │ YES KXBTC15M-ABC @ 48¢ │
Regime=TRENDING(R²=0.78) │
OB=75% depth=$120 │
Momentum=AGREE(+4.2%) │
WinProb=79.2% Edge=9.3% │
Confidence=73/100 │ Bet=$2.34 │
8.5min remain │ WilsonLB=61.2%
```

This record must be present for every live trade. If a trade fires without this record, there is a code path that bypasses the guard stack — that is a bug.

### Every blocked trade logs the reason:
```
Regime filter │ RANGING (R²=0.31) — only TRENDING allowed. Skipping.
Momentum filter │ Required AGREE, got NEUTRAL (OB=YES). Skipping.
Confidence │ Score 48 < minimum 65 — no trade.
```

---

## 10. What Success Looks Like

**Not:** "The bot traded 40 times today and made $2."
**Yes:** "The bot identified 3 clear setups today and made $4 from them."

Target metrics (after 100+ qualified trades):
- Win rate: ≥ 63%
- Expectancy per trade: > $0.25 at $25 balance
- Max daily loss: < 15% of balance
- Consecutive loss streak: rarely > 3

If win rate on qualified setups (all 9 layers passed) falls below 55% over 50+ trades, the strategy's primary signal (OB imbalance) has lost its edge. The parameters need revision, not just tightening.

---

## 11. What This Bot Will Never Do

- Trade on OB signal alone without BTC momentum confirmation
- Trade during a ranging or high-volatility BTC regime
- Trade with less than $50 of near-money order book depth
- Ignore the Wilson CI performance gate after sufficient sample size
- Resume trading immediately after 2 consecutive losses
- Use position sizing that doesn't scale with actual balance
- Represent paper mode results as valid P&L (v5 P&L bug is fixed in v6)

---

*Last revised: v6.0.0 — post-mortemed from 50% session capital loss on 2026-03-27/28.*
*Root causes: no regime detection, NEUTRAL momentum accepted, $5 depth threshold, broken Kelly formula, broken paper P&L accounting.*
