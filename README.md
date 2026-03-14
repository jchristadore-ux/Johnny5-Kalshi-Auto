# Johnny5-Kalshi-Auto v3.0

> The definitive Kalshi quant trading bot. Order Book Pressure + Volatility Regime Switching + Fractional Kelly + Maker-Side Execution + 5 Elite Trader Archetypes.

---

## What's New in v3.0

- **Correct authentication**: RSA-PSS signed requests (the only auth Kalshi's v2 API accepts). Previous versions used deprecated email/password login — this was a critical bug that would have caused silent auth failures.
- **Favourite-longshot bias filter**: Academic research (Bürgi et al. 2025) proves Kalshi contracts below ~20c lose ~60% of capital. Every archetype enforces a price range filter.
- **Maker-side limit orders**: Makers consistently outperform takers on Kalshi. Bot posts limit orders 1 cent inside the spread — never crosses it.
- **Position resolution loop**: Bot polls settled orders and resolves win/loss for accurate rolling win rate tracking.
- **Daily loss hard stop**: Fetches real balance on startup and enforces a max daily loss dollar limit.
- **Rate limit / error resilience**: Exponential backoff on 429, graceful handling of 401/403, connection errors, timeouts.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | The trading bot — runs on Railway |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deployment config |
| `control-panel.html` | Open locally — AI-powered control panel |
| `README.md` | This file |

---

## Step-by-Step Setup

### Step 1: Create the GitHub Repository

1. Go to **github.com** → click **New** → name it `Johnny5-Kalshi-Auto`
2. Set to Public or Private (your choice)
3. Do NOT initialize with README (you'll upload the files)

### Step 2: Upload the Files

In your new GitHub repo, click **Add file → Upload files** and upload:
- `bot.py`
- `requirements.txt`
- `railway.toml`
- `README.md`

Commit directly to `main`.

### Step 3: Get Your Kalshi RSA API Keys

**This is different from your email/password. You need RSA keys.**

1. Log into [kalshi.com](https://kalshi.com)
2. Go to **Settings → API Keys** (or **Account → Profile → API Keys**)
3. Click **Create New API Key**
4. Kalshi will generate:
   - A **Key ID** (looks like `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)
   - A **Private Key PEM file** (download it — you only get it once)
5. Open the `.txt` or `.pem` file. It looks like:
   ```
   -----BEGIN PRIVATE KEY-----
   MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...
   -----END PRIVATE KEY-----
   ```
6. Save both values securely.

### Step 4: Connect to Railway

1. Go to [railway.app](https://railway.app)
2. Click **New Project → Deploy from GitHub Repo**
3. Select `Johnny5-Kalshi-Auto`
4. Railway will detect `railway.toml` and configure automatically

### Step 5: Set Environment Variables in Railway

Go to your Railway project → **Variables** tab → add each of these:

| Variable | Value | Notes |
|---|---|---|
| `KALSHI_API_KEY_ID` | Your Key ID from Step 3 | Required |
| `KALSHI_PRIVATE_KEY_PEM` | The full PEM string | Paste the entire `-----BEGIN...END-----` block. Replace newlines with `\n` if needed |
| `DEMO_MODE` | `true` | Change to `false` for live trading |
| `TRADER_MODE` | `quant` | Options: `quant`, `domahhhh`, `gaetend`, `debl00b`, `sudeith`, `duckguesses` |
| `TRADE_SIZE_DOLLARS` | `10` | Max dollars per trade |
| `MAX_DAILY_LOSS_DOLLARS` | `50` | Hard stop loss per day |
| `MIN_WIN_RATE` | `0.45` | Pause if rolling win rate drops below this |
| `POLL_INTERVAL_SECS` | `30` | How often to scan markets |
| `VOL_HIGH_THRESH` | `0.008` | BTC log-return stdev threshold for HIGH vol regime |
| `GITHUB_TOKEN` | Your GitHub PAT | For control panel auto-deploy |

**Note on PEM newlines**: Railway may strip newlines from multi-line env vars. Replace actual newlines with `\n` in one long string:
```
-----BEGIN PRIVATE KEY-----\nMIIEvgIBADA...\n-----END PRIVATE KEY-----
```
The bot handles `\n` → newline conversion automatically.

### Step 6: Deploy

Railway will auto-deploy when you push to `main`. To trigger manually, click **Deploy** in the Railway dashboard.

### Step 7: Open the Control Panel

Open `control-panel.html` in your browser (just double-click it). It runs entirely locally — no server needed.

Enter your GitHub token, select an archetype, adjust sliders, and click **Push Params to GitHub** to update config without touching code.

To make code changes: type a plain-English instruction in the AI Command box and click **Generate + Deploy**.

---

## Trader Archetypes

| Mode | Based On | Contract Range | Kelly | Edge Bar | Vol |
|---|---|---|---|---|---|
| `quant` | Native quant | 40–85c | 25% | 4% | Any |
| `domahhhh` | $980K profit | 60–92c | 40% | 6% | Any |
| `gaetend` | $420K profit | 35–75c | 25% | 3% | High only |
| `debl00b` | $42M volume | 40–60c | 15% | 1% | Low only |
| `sudeith` | 100hr/wk analyst | 45–80c | 30% | 8% | Any |
| `duckguesses` | $100→$145K | 68–90c | 50% | 5% | Any |

**Start with `domahhhh` mode** — strongest documented results, most intuitive behavior.

---

## Strategy Explained

### Signal 1: Order Book Pressure
Measures depth-weighted imbalance across top-5 levels of the YES/NO order book. If ≥X% of depth sits on one side, smart money is positioned there — we follow. Threshold varies by archetype.

### Signal 2: BTC Volatility Regime
Pulls live BTC price from Binance (no key needed). Calculates realized volatility from log-returns over last 90 minutes. Classifies regime as HIGH or LOW. Some archetypes only trade in specific regimes.

### Signal 3: Favourite-Longshot Bias Filter
Academic finding: Kalshi contracts priced below ~20c lose approximately 60% of invested capital on average. High-priced contracts (>55c) have positive expected value. Every archetype defines a profitable price range — trades outside it are rejected.

### Signal 4: Cross-Market Consensus (SUDEITH mode)
Blends OB pressure (60% weight) with a BTC vol-implied probability estimate (40% weight). Only fires when both signals agree. Highest edge requirement of all archetypes.

### Sizing: Fractional Kelly Criterion
Kelly formula: `f* = (b*p - q) / b` where b = net odds, p = win probability, q = 1-p.
Each archetype applies a fraction (15%–50%) to ensure no single trade can significantly damage the account.

### Execution: Maker Limit Orders
Bot posts limit orders 1 cent inside the current best bid/ask. This places the bot on the maker side of the transaction. Academic data confirms makers consistently outperform takers on Kalshi.

---

## Risk Disclosures

- All trading involves risk. Past strategy performance does not guarantee future results.
- Always start with `DEMO_MODE=true` to verify behavior before going live.
- Set `MAX_DAILY_LOSS_DOLLARS` conservatively. The bot enforces this as a hard stop.
- Kalshi markets can be illiquid, especially near expiry. Limit orders may not fill.
- The 15-minute BTC markets on Kalshi launched December 2025 — historical data is limited.
