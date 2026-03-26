# Johnny5-Kalshi-Auto v5.3.0

> Production quant bot for Kalshi 15-minute BTC up/down prediction markets.
> Near-money order book pressure + BTC momentum confirmation + fractional Kelly sizing.

---

## Strategy

### Signal 1 — Near-Money OB Pressure (primary edge)
Measures dollar depth within ±10 cents of the current mid-price on the YES/NO order book. If sufficient imbalance exists on one side, smart money is positioned there. Requires ≥$5 total depth to prevent single resting orders from manufacturing false signals.

**v5.3.0: Adaptive threshold** — thin books ($5-15 depth) require 70%+ imbalance, thick books ($50+ depth) allow 58%+, medium books use profile default (62%).

### Signal 2 — BTC Momentum Confirmation
Fetches live BTC/USD price from Kraken (Coinbase fallback). If BTC moved ≥0.20% in the same direction as the OB signal in the last 2 minutes → AGREE (boosts win probability). If BTC moved against the OB signal → CONFLICT (trade skipped). Flat market → NEUTRAL (no adjustment).

### Signal 3 — Price Breakeven Guard
Only enters contracts priced ≤67 cents. At 68.5% historical win rate, the mathematical breakeven is 68 cents. This ensures positive expected value on every trade.

### Signal 4 — Bias Filter
Skips contracts priced <35 cents or >65 cents. Academic research (Bürgi et al. 2025) confirms Kalshi contracts below ~20 cents lose ~60% of capital on average.

### v5.3.0: OB Trend Detection
Compares the current order book snapshot to the previous one for each ticker. Trades only fire when pressure is building or stable — if the imbalance is fading (dropped >5%) or the direction flipped, the trade is skipped. Prevents entering on stale or collapsing signals.

### v5.3.0: Multi-Market Scanner
Scans ALL open BTC markets across KXBTC15M/KXBTCD/KXBTC series and evaluates each for signals. The bot tries every valid market per cycle instead of just picking the one closest to 50c. Position guard and cooldown prevent over-trading.

### Sizing — Fractional Kelly
`f* = (b×p - q) / b` where b = net odds, p = OB win probability, q = 1-p.
Kelly fraction: 35% (grid-search optimal). Capped at TRADE_SIZE_DOLLARS and 20% of balance.

### Execution — Maker Limit Orders
Posts limit orders one cent inside the best bid/ask. Kalshi makers pay zero fee. Takers pay ~1% of winnings. Fee drag on taker orders: ~$5+/day at scale.

**v5.3.0: Stale order cancellation** — unfilled maker orders are automatically canceled after 5 minutes (configurable) to free capital for better opportunities.

---

## Risk Controls

| Control | Behavior |
|---|---|
| Balance floor | Halts if balance < MIN_BALANCE_FLOOR ($5 default) |
| Session stop | Halts if balance drops below 50% of session-start balance |
| Daily loss cap | Halts if session P&L ≤ -MAX_DAILY_LOSS_DOLLARS |
| Position guard | One entry per market ticker, no re-entry until expiry |
| Expiry guard | Skips contracts priced >85c or <15c (near-certain outcome, zero EV) |
| Spread guard | Skips zero/crossed spreads (broken book) |
| Streak filter | After 3 consecutive losses, skips one window then resets counter |
| **Liquidity filter** *(v5.3.0)* | Skips low-liquidity UTC hours (default 4-8 UTC / midnight-4am ET) |
| **Concurrent limit** *(v5.3.0)* | Max simultaneous open positions (default 2) |
| **Stale cancel** *(v5.3.0)* | Auto-cancels unfilled orders after timeout (default 300s) |

---

## v5.3.0 — Win Rate Confidence Tracking

The bot now computes Wilson score confidence intervals on accumulated win/loss data. This tells you whether your edge is statistically real or just luck:

- **Heartbeat** (every 15 min): includes WR% with 95% CI when ≥5 trades resolved
- **Daily summary**: includes confidence interval
- **Logs**: portfolio status includes CI bounds

Example: `WR: 72.0% [58-83%]` means your true win rate is somewhere between 58% and 83% with 95% confidence. As you accumulate more trades, the interval narrows.

---

## v5.3.0 — Smarter Paper Mode

Paper mode no longer uses a random 68.5% coin flip to determine outcomes. Instead:
- Each paper order records the BTC price at entry
- At resolution (15 min later), the bot checks current BTC price
- If you bought YES and BTC went up → WIN. If BTC went down → LOSS.
- If you bought NO and BTC went down → WIN. If BTC went up → LOSS.
- Falls back to RNG only if BTC price feed is unavailable.

This gives paper mode results that correlate with actual market outcomes.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main trading bot — runs on Railway |
| `telegram_utils.py` | Telegram notification module |
| `test_bot.py` | Pytest test suite — risk controls + signal math |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deployment config |

---

## Testing

```bash
pip install -r requirements.txt
pytest test_bot.py -v
```

Tests cover all risk controls (P0), signal math (P1), and v5.3.0 features (P2). 60+ test cases including edge boundaries, adaptive threshold scaling, OB trend detection, stale order cancellation, and Wilson confidence intervals.

---

## Setup

### Step 1 — Kalshi RSA API Keys
1. Log into kalshi.com → Settings → API Keys → Create New Key
2. Save the Key ID (UUID format) and download the PEM file
3. The PEM looks like: `-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----`

### Step 2 — GitHub Repo
Upload all files to a new GitHub repo. Commit to `main`.

### Step 3 — Railway
1. New Project → Deploy from GitHub Repo → select your repo
2. Variables tab → add all variables below

### Step 4 — Telegram Bot (optional but strongly recommended)
1. Message @BotFather on Telegram → `/newbot` → follow prompts → save the token
2. Message your bot once, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy the `chat.id` value

---

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `KALSHI_API_KEY_ID` | required | UUID from Kalshi Settings → API Keys |
| `KALSHI_PRIVATE_KEY_PEM` | required | Full PEM. Replace newlines with `\n` if needed |
| `DEMO_MODE` | `true` | Set `false` for live trading |
| `TRADER_MODE` | `quant` | Only `quant` is recommended for live |
| `TRADE_SIZE_DOLLARS` | `5` | Max dollars per trade |
| `MAX_DAILY_LOSS_DOLLARS` | `20` | Hard stop loss per session |
| `MIN_BALANCE_FLOOR` | `5` | Halt if balance drops below this |
| `YES_BREAKEVEN_PRICE` | `67` | Skip contracts above this price (cents) |
| `KELLY_FRACTION` | `0.35` | Grid-search optimal — do not raise without backtesting |
| `MAX_CONSEC_LOSSES` | `3` | Streak filter threshold |
| `PAPER_BALANCE` | `25.0` | Starting balance in paper mode |
| `POLL_INTERVAL_SECS` | `30` | Market scan frequency |
| `TELEGRAM_BOT_TOKEN` | optional | From @BotFather |
| `TELEGRAM_CHAT_ID` | optional | Your Telegram chat ID |
| `STALE_ORDER_TIMEOUT` | `300` | *(v5.3.0)* Seconds before canceling unfilled orders |
| `MAX_CONCURRENT_POS` | `2` | *(v5.3.0)* Max simultaneous open positions |
| `LOW_LIQ_START_UTC` | `4` | *(v5.3.0)* Low-liquidity skip window start (UTC hour) |
| `LOW_LIQ_END_UTC` | `8` | *(v5.3.0)* Low-liquidity skip window end (UTC hour) |

---

## Telegram Alerts

| Event | Fires |
|---|---|
| Boot | Always (includes balance, caps, version, new v5.3.0 params) |
| Heartbeat | Every 15 minutes (balance, P&L, open orders, WR + confidence interval) |
| Trade entered | Every live order placed (OB%, edge%, cost) |
| WIN | Every settled winning trade |
| LOSS | Every settled losing trade (live mode only) |
| HALT | Session stop, daily loss cap, balance floor |
| Daily summary | Midnight UTC (~8pm ET) with confidence intervals |
| Shutdown | On manual stop |

---

## Version History

| Version | Key Changes |
|---|---|
| **v5.3.0** | **Multi-market scanner; stale order cancellation (5 min); adaptive OB threshold (thin/thick books); time-of-day liquidity filter; OB trend detection (building/fading); smarter paper mode (BTC movement, not RNG); Wilson confidence intervals; concurrent position limit; 60+ pytest tests** |
| v5.2.1 | BOT_VERSION tag; BTC feed timed backoff; global consecutive_losses fix; WIN notification branch fix |
| v5.2.0 | No Kalshi mid proxy in BTC feed; OB depth floor $5; streak filter deadlock fix; running_pnl |
| v5.1.x | Telegram heartbeat + entry/loss alerts; positions endpoint for resolution; stale order cleanup |
| v5.0 | Session stop (50% halt); BTC momentum 0.20% threshold; spread guard fixed; Kelly 35% |
| v4.0 | win_prob = OB imbalance only; balance floor; paper mode fully simulated |
| v3.0 | RSA-PSS auth; near-money OB filter ±10c; maker limit orders; bias filter |

---

## Risk Disclosures

- All trading involves risk of capital loss.
- The 15-minute BTC markets on Kalshi launched December 2025 — historical data is limited.
- Past win rates do not guarantee future performance.
- Start with DEMO_MODE=true and verify behavior before trading real money.
- Set MAX_DAILY_LOSS_DOLLARS conservatively relative to your account size.
- The Wilson confidence intervals help you evaluate whether your edge is real, but statistical significance does not guarantee future performance.
