# Johnny5-Kalshi-Auto — v5.2.1 Audit & Improvement Plan

> Produced 2026-03-26. Covers `bot.py`, `telegram_utils.py`, `requirements.txt`, `railway.toml`.

---

## Executive Summary

The bot is well-structured for a single-file trading system, with solid risk controls (balance floor, session stop, streak filter, expiry guard). However, the audit uncovered **2 crash-level bugs that are actively firing in production**, plus 8 additional issues ranked by impact. The patched files accompanying this document fix all 10.

---

## Top 10 Issues — Prioritized by Impact × Risk

### 🔴 P0 — CRASH: `send_win_notification()` called with wrong kwargs

**Severity:** Every WIN notification crashes with `TypeError`.
**Location:** `bot.py` lines 476–483 (paper) and 540–547 (live).

The bot calls:
```python
tg.send_win_notification(
    profit=pnl,
    balance=paper_balance,
    daily_pnl=paper_daily_pnl,   # ← does NOT exist in function signature
    running_pnl=running_pnl,
    ...
)
```

But `telegram_utils.py` defines:
```python
def send_win_notification(profit, balance, running_pnl, ticker, direction, timestamp=None)
```

There is no `daily_pnl` parameter. Every win triggers `TypeError: send_win_notification() got an unexpected keyword argument 'daily_pnl'`.

**Impact in paper mode:** The TypeError propagates up from `resolve_open_orders()` (no local try/except around the paper branch), aborting the entire resolution cycle. Remaining open orders in that batch are never resolved. The main loop's outer `except Exception` catches it and continues, so the bot stays alive but WIN notifications never send and resolution is incomplete.

**Impact in live mode:** The live resolution block is inside a try/except, so the error is logged as "Order resolution error" and swallowed. WIN notifications never send. The order IS removed from `open_orders` (the pop happens before the notification), so state is partially updated but the Telegram alert is lost.

**Fix:** Remove `daily_pnl=...` from both call sites.

---

### 🔴 P0 — BUG: Paper mode balance accounting is wrong (double-deducting cost on wins)

**Severity:** Paper P&L is systematically pessimistic — every win under-credits the balance by the cost of the trade.
**Location:** `bot.py` lines 458–463, inside `resolve_open_orders()` paper branch.

The accounting flow:
```
Entry:   paper_balance -= cost        # deduct cost (correct)
Win:     paper_balance += (count - cost)  # adds PROFIT, not PAYOUT
```

Net balance change on a win: `-cost + (count - cost) = count - 2*cost`.

**Example:** Buy 2 contracts at 50¢. cost = $1.00, count = 2.
- Entry: balance −$1.00
- Win payout should be: 2 × $1.00 = $2.00 → net +$1.00 profit
- Actual: balance += (2 − 1.00) = +$1.00 → net = −$1.00 + $1.00 = $0.00

Every paper win shows zero profit instead of the real gain. Over a session this makes paper mode look breakeven/losing when the strategy is actually profitable.

**Fix:** On a win, add `count` (the full payout) to balance, not `count - cost`.

---

### 🟡 P1 — `get_live_balance()` returns 0.0 on API failure → false halt

**Severity:** A transient Kalshi API timeout makes the bot think balance is $0, triggering `balance_floor_check` and halting the session permanently.
**Location:** `bot.py` line 434–440.

**Fix:** Cache last-known-good balance. Return cached value on failure instead of 0.0.

---

### 🟡 P1 — No SIGTERM handler → unclean Railway deploys

**Severity:** Railway sends SIGTERM on redeploy. The bot only catches `KeyboardInterrupt` (SIGINT). Every deploy kills the process without the shutdown Telegram message and without any cleanup.
**Location:** `bot.py` main loop.

**Fix:** Register a `signal.SIGTERM` handler that sets a shutdown flag.

---

### 🟡 P1 — No `requests.Session()` → new TCP connection per API call

**Severity:** Each loop iteration makes 3–6 HTTP calls, each creating a fresh TCP connection + TLS handshake. On a 30-second poll that's ~17,000 unnecessary TLS handshakes per day.
**Location:** All `requests.get()` / `requests.post()` calls.

**Fix:** Use a module-level `requests.Session()` for connection pooling.

---

### 🟡 P2 — Excessive `get_live_balance()` calls per loop

**Severity:** In live mode, balance is fetched 2–4 times per 30s cycle (heartbeat + current_balance + per-settled-order). That's up to 11,520 balance API calls/day when only ~2,880 are needed.
**Location:** Main loop + `resolve_open_orders()`.

**Fix:** Cache balance with a 15-second TTL. One fetch per cycle max.

---

### 🟡 P2 — 14 mutable globals → maintenance nightmare

**Severity:** Functions like `run_decision()`, `resolve_open_orders()`, and `main()` all mutate overlapping global state (`consecutive_losses`, `running_pnl`, `paper_balance`, etc.). This has already caused bugs (the v5.2.1 `global` fix in `run_decision`). The next developer to add a feature will hit the same class of bug.
**Location:** Lines 270–285 and every function with `global` declarations.

**Fix:** Consolidate into a `BotState` dataclass. Passed explicitly or accessed as a singleton. (Provided in patched code as a contained refactor — globals replaced with `state.*` references.)

---

### 🟡 P2 — `to_cents()` redefined inside loop

**Severity:** Minor performance waste — function is re-created on each iteration of the series loop in `get_active_btc_market()`.
**Location:** `bot.py` line 606.

**Fix:** Move to module scope as a utility.

---

### 🟢 P3 — No unfilled-order cancellation (maker orders)

**Severity:** A maker limit order that never fills sits resting for the entire 15-minute market window. The position guard blocks new entries on that ticker. The bot loses ~15 minutes of potential trading per unfilled order.
**Location:** Design gap — no cancel logic exists.

**Fix:** After 5 minutes unfilled, cancel the resting order via `DELETE /portfolio/orders/{order_id}` and free the ticker lock. (Template provided in patched code, gated behind an env var.)

---

### 🟢 P3 — `requirements.txt` is missing version pins

**Severity:** A future `requests` or `cryptography` major version bump could break the bot on next Railway build with no code change.
**Location:** `requirements.txt`.

**Fix:** Pin to known-good versions with `~=` compatible-release specifiers.

---

## Additional Observations (not bugs, but worth noting)

- **`VOL_HIGH_THRESH` is loaded but never used** (line 169). Dead config — safe to remove.
- **Header comment says "v5.0" but `BOT_VERSION = "5.2.1"`** — cosmetic mismatch.
- **`PROFILE["min_spread"]` is set to 2 but `spread_check()` only blocks ≤0** — the profile value is never read by the guard. This is intentional per the v5.0 changelog but the profile field is misleading.
- **Paper mode uses `random.random() < 0.685` for settlement** — this is fine for smoke testing but doesn't simulate actual market dynamics. Consider replaying historical outcomes if you want more accurate paper P&L.

---

## Weekly Iteration Prompt Template

Paste this into Claude each week to continuously improve the bot:

```
You are auditing Johnny5-Kalshi-Auto, a Python trading bot for Kalshi BTC 15-minute
prediction markets. The bot runs on Railway and trades via the Kalshi REST API.

Here are the current files: [attach bot.py, telegram_utils.py, requirements.txt, railway.toml]

Perform this week's iteration:

1. BUGS: Read every function call site and verify the arguments match the function
   signature. Check every global variable mutation has a matching `global` declaration.
   Check every arithmetic operation for off-by-one or unit mismatches (cents vs dollars).

2. RELIABILITY: Check error handling around every API call. Identify any path where a
   transient failure (timeout, 5xx, rate limit) causes permanent state corruption or
   a false halt. Verify the bot recovers gracefully from Railway restarts.

3. STRATEGY DRIFT: Compare the code's actual behavior to the README's documented
   strategy. Flag any parameter that has drifted from the documented value.
   Check that edge calculation, Kelly sizing, and OB imbalance math are correct.

4. PERFORMANCE: Count total API calls per loop iteration in live mode. Identify any
   that can be cached or batched. Check for unnecessary object creation in hot paths.

5. NEW FEATURE (if applicable): [describe what you want added this week]

Output:
- A numbered list of findings with severity (P0/P1/P2/P3)
- For each finding: exact file, line number, current code, and the fix
- A patched version of any file that changed
- Updated README version history entry
```

This template is designed to catch the same class of bugs that have historically appeared in this codebase (kwarg mismatches, global-scope issues, arithmetic errors).
