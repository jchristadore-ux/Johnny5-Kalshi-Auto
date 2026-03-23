# Johnny5-Kalshi-Auto v5.2.0

> Production quant bot for Kalshi 15-minute BTC up/down prediction markets.
> Near-money order book pressure + BTC momentum confirmation + fractional Kelly sizing.

---

## Strategy

### Signal 1 — Near-Money OB Pressure (primary edge)
Measures dollar depth within ±10 cents of the current mid-price on the YES/NO order book. If ≥62% of near-money depth sits on one side, smart money is positioned there. Requires ≥$5 total depth to prevent single resting orders from manufacturing false signals.

### Signal 2 — BTC Momentum Confirmation
Fetches live BTC/USD price from Kraken (Coinbase fallback). If BTC moved ≥0.20% in the same direction as the OB signal in the last 2 minutes → AGREE (boosts win probability). If BTC moved against the OB signal → CONFLICT (trade skipped). Flat market → NEUTRAL (no adjustment).

### Signal 3 — Price Breakeven Guard
Only enters contracts priced ≤67 cents. At 68.5% historical win rate, the mathematical breakeven is 68 cents. This ensures positive expected value on every trade.

### Signal 4 — Bias Filter
Skips contracts priced <35 cents or >65 cents. Academic research (Bürgi et al. 2025) confirms Kalshi contracts below ~20 cents lose ~60% of capital on average.

### Sizing — Fractional Kelly
`f* = (b×p - q) / b` where b = net odds, p = OB win probability, q = 1-p.
Kelly fraction: 35% (grid-search optimal). Capped at TRADE_SIZE_DOLLARS and 20% of balance.

### Execution — Maker Limit Orders
Posts limit orders one cent inside the best bid/ask. Kalshi makers pay zero fee. Takers pay ~1% of winnings. Fee drag on taker orders: ~$5+/day at scale.

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

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main trading bot — runs on Railway |
| `telegram_utils.py` | Telegram notification module |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deployment config |

---

## Setup

### Step 1 — Kalshi RSA API Keys
1. Log into kalshi.com → Settings → API Keys → Create New Key
2. Save the Key ID (UUID format) and download the PEM file
3. The PEM looks like: `-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----`

### Step 2 — GitHub Repo
Upload all four files to a new GitHub repo. Commit to `main`.

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

---

## Telegram Alerts

| Event | Fires |
|---|---|
| Boot | Always (includes balance, caps, version) |
| Heartbeat | Every 15 minutes (balance, P&L, open orders, last signal) |
| Trade entered | Every live order placed (OB%, edge%, cost) |
| WIN | Every settled winning trade |
| LOSS | Every settled losing trade (live mode only) |
| HALT | Session stop, daily loss cap, balance floor |
| Daily summary | Midnight UTC (~8pm ET) |
| Shutdown | On manual stop |

---

## Version History

| Version | Key Changes |
|---|---|
| v5.2.0 | BOT_VERSION tag; no Kalshi mid proxy in BTC feed; OB depth floor $5; streak filter deadlock fix; paper_daily_pnl loss tracking; running_pnl for accurate session P&L; WIN alerts in paper mode |
| v5.1.x | Telegram heartbeat + entry/loss alerts; positions endpoint for resolution; stale order cleanup; global consecutive_losses fix |
| v5.0 | Session stop (50% halt); BTC momentum 0.20% threshold; spread guard fixed (1c markets allowed); Kelly 35%; balance floor $5 |
| v4.0 | win_prob = OB imbalance only (not rolling win rate); balance floor; paper mode fully simulated |
| v3.0 | RSA-PSS auth; near-money OB filter ±10c; maker limit orders; favourite-longshot bias filter |

---

## Risk Disclosures

- All trading involves risk of capital loss.
- The 15-minute BTC markets on Kalshi launched December 2025 — historical data is limited.
- Past win rates do not guarantee future performance.
- Start with DEMO_MODE=true and verify behavior before trading real money.
- Set MAX_DAILY_LOSS_DOLLARS conservatively relative to your account size.
