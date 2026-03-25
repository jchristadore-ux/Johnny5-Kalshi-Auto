"""
telegram_utils.py — Telegram notification module for Johnny5-Kalshi-Auto

Responsibilities:
  - Validate credentials at startup (validate_telegram_connection)
  - Send messages with up to 2 retries (send_telegram_message)
  - Fire WIN-only trade alerts (send_win_notification)

Design rules:
  - Never raises an exception — all errors are logged and swallowed
  - Never sends for losses, entries, or break-evens
  - All credentials come from environment variables; nothing is hardcoded

Notification policy:
  - WIN trades:  send_win_notification (balance, daily PnL, running PnL)
  - Heartbeat:   send_telegram_message every 4 hours (status only)
  - Operational: boot, halt, shutdown, daily summary
  - Suppressed:  trade entries, losses, break-evens
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("Johnny5.telegram")

# ── Module state ─────────────────────────────────────────────────────────────

_telegram_enabled: bool = False   # set by validate_telegram_connection()
_bot_token: str = ""
_chat_id: str = ""


# ── Public API ────────────────────────────────────────────────────────────────

def validate_telegram_connection() -> bool:
    """
    Validate Telegram credentials and confirm connectivity with a test message.

    Call once at bot startup. Sets the module-level enabled flag.
    The bot continues running whether this succeeds or fails.

    Returns True if the test message was delivered, False otherwise.
    """
    global _telegram_enabled, _bot_token, _chat_id

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat:
        log.warning(
            "Telegram disabled — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
        )
        _telegram_enabled = False
        return False

    _bot_token = token
    _chat_id   = chat

    # Connectivity test — if this message lands, the channel is working
    ok = _send_raw(
        "🤖 Johnny5 connected to Telegram.\n"
        "Credentials validated ✅ — WIN trade alerts are active."
    )

    if ok:
        log.info("✅ Telegram validated — WIN notifications enabled.")
        _telegram_enabled = True
    else:
        log.warning("⚠️  Telegram validation failed — notifications disabled for this session.")
        _telegram_enabled = False

    return _telegram_enabled


def send_telegram_message(text: str) -> bool:
    """
    Send an arbitrary message to the configured chat.

    Retries up to 2 times (3 total attempts) with a 2-second pause between
    attempts. Returns True on success, False if all attempts fail.
    Silently no-ops if Telegram is disabled.
    """
    if not _telegram_enabled:
        return False
    return _send_raw(text)


def send_win_notification(
    profit: float,
    balance: float,
    daily_pnl: float,
    running_pnl: float,
    ticker: str,
    direction: str,
    timestamp: Optional[datetime] = None,
) -> None:
    """
    Send a WIN trade alert. Silently suppressed if profit <= 0.

    Args:
        profit:      Net profit from this trade, dollars (must be > 0)
        balance:     Current account balance after settlement
        daily_pnl:   Session PnL (live balance minus session start balance)
        running_pnl: Cumulative session PnL across all settled trades
        ticker:      Market ticker, e.g. "KXBTC15M-26MAR2200"
        direction:   "YES" (→ UP) or "NO" (→ DOWN)
        timestamp:   Settlement time; defaults to now (UTC)
    """
    if not _telegram_enabled:
        return

    if profit <= 0:
        # Guard: this function is for wins only — caller should check first
        log.debug("send_win_notification called with profit=%.4f — suppressed.", profit)
        return

    ts       = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    pos      = "UP" if direction.upper() == "YES" else "DOWN"
    d_sign   = "+" if daily_pnl   >= 0 else ""
    r_sign   = "+" if running_pnl >= 0 else ""

    msg = (
        f"✅ WIN\n"
        f"💰 Trade Profit: +${profit:.2f}\n"
        f"🏦 Balance:       ${balance:,.2f}\n"
        f"📅 Daily PnL:    {d_sign}${daily_pnl:.2f}\n"
        f"📈 Running PnL:  {r_sign}${running_pnl:.2f}\n"
        f"📊 Market:        {ticker}\n"
        f"📍 Position:      {pos}\n"
        f"⏱  Time:          {ts}"
    )

    send_telegram_message(msg)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _send_raw(text: str) -> bool:
    """
    Low-level send with up to 2 retries (3 total attempts).

    Uses module-level credentials when available, otherwise falls back to
    reading env vars directly (needed during the validation test itself).
    """
    token = _bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = _chat_id   or os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(3):   # attempt 0, 1, 2 → up to 2 retries
        try:
            r = requests.post(url, json={"chat_id": chat, "text": text}, timeout=8)
            if r.status_code == 200:
                return True
            log.debug("Telegram HTTP %d (attempt %d): %s",
                      r.status_code, attempt + 1, r.text[:120])
        except Exception as exc:
            log.debug("Telegram send error (attempt %d): %s", attempt + 1, exc)

        if attempt < 2:
            time.sleep(2)   # wait before retry

    log.warning("Telegram: all 3 send attempts failed — message not delivered.")
    return False
