"""
telegram_utils.py — Telegram notification module for Johnny5-Kalshi-Auto

Responsibilities:
  - Validate credentials at startup
  - Send messages with up to 2 retries
  - Fire WIN trade alerts, heartbeat, entry, halt, daily summary

Design rules:
  - Never raises — all errors logged and swallowed
  - All credentials from env vars, nothing hardcoded
  - _telegram_enabled flag gates everything after validation
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("Johnny5.telegram")

# ── Module state ──────────────────────────────────────────────────────────────
_telegram_enabled: bool = False
_bot_token: str = ""
_chat_id:   str = ""


# ── Public API ─────────────────────────────────────────────────────────────────

def validate_telegram_connection() -> bool:
    """Validate credentials and send a connectivity test. Call once at boot."""
    global _telegram_enabled, _bot_token, _chat_id

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat:
        log.warning("Telegram disabled — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        _telegram_enabled = False
        return False

    _bot_token = token
    _chat_id   = chat

    ok = _send_raw("🤖 Johnny5 connected to Telegram.\nCredentials validated ✅ — alerts active.")

    if ok:
        log.info("✅ Telegram validated — notifications enabled.")
        _telegram_enabled = True
    else:
        log.warning("⚠️  Telegram validation failed — notifications disabled.")
        _telegram_enabled = False

    return _telegram_enabled


def send_telegram_message(text: str) -> bool:
    """Send an arbitrary message. No-op if Telegram is disabled."""
    if not _telegram_enabled:
        return False
    return _send_raw(text)


def send_heartbeat(balance: float, session_pnl: float, open_count: int,
                   trades_today: int, last_signal: str) -> None:
    """
    15-minute heartbeat. Confirms bot is alive and scanning.
    Sent regardless of whether trades are firing.
    """
    if not _telegram_enabled:
        return
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    pnl_sign = "+" if session_pnl >= 0 else ""
    msg = (
        f"💓 Heartbeat — {now}\n"
        f"💵 Balance:     ${balance:,.2f}\n"
        f"📊 Session PnL: {pnl_sign}${session_pnl:.2f}\n"
        f"📂 Open orders: {open_count}\n"
        f"🔁 Trades today:{trades_today}\n"
        f"🔍 Last signal: {last_signal}"
    )
    send_telegram_message(msg)


def send_trade_entry_notification(ticker: str, direction: str, cost: float,
                                   price_cents: int, balance: float,
                                   ob_pct: float = 0.0, edge_pct: float = 0.0,
                                   timestamp: Optional[datetime] = None) -> None:
    """Send a trade entry alert. Fires on every live order placed."""
    if not _telegram_enabled:
        return
    ts  = (timestamp or datetime.now(timezone.utc)).strftime("%H:%M UTC")
    pos = "🟢 UP" if direction.upper() == "YES" else "🔴 DOWN"
    msg = (
        f"📈 TRADE ENTERED — {ts}\n"
        f"📍 {pos}  │  {ticker[-15:]}\n"
        f"💵 Cost: ${cost:.2f}  │  Price: {price_cents}¢\n"
        f"🎯 OB: {ob_pct:.0f}%  │  Edge: {edge_pct:.1f}%\n"
        f"🏦 Balance: ${balance:,.2f}"
    )
    send_telegram_message(msg)


def send_win_notification(profit: float, balance: float, running_pnl: float,
                           ticker: str, direction: str,
                           timestamp: Optional[datetime] = None) -> None:
    """Send a WIN alert. Suppressed if profit <= 0."""
    if not _telegram_enabled:
        return
    if profit <= 0:
        log.debug("send_win_notification called with profit=%.4f — suppressed.", profit)
        return
    ts  = (timestamp or datetime.now(timezone.utc)).strftime("%H:%M UTC")
    pos = "UP" if direction.upper() == "YES" else "DOWN"
    pnl_sign = "+" if running_pnl >= 0 else ""
    msg = (
        f"✅ WIN — {ts}\n"
        f"💰 +${profit:.2f}  │  {pos}  │  {ticker[-15:]}\n"
        f"🏦 Balance: ${balance:,.2f}\n"
        f"📈 Session: {pnl_sign}${running_pnl:.2f}"
    )
    send_telegram_message(msg)


def send_loss_notification(loss: float, balance: float, running_pnl: float,
                            ticker: str, direction: str, streak: int) -> None:
    """Send a LOSS alert in live mode."""
    if not _telegram_enabled:
        return
    ts  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    pos = "UP" if direction.upper() == "YES" else "DOWN"
    pnl_sign = "+" if running_pnl >= 0 else ""
    streak_str = f"  │  Streak: {streak}" if streak > 1 else ""
    msg = (
        f"❌ LOSS — {ts}\n"
        f"💸 -${loss:.2f}  │  {pos}  │  {ticker[-15:]}{streak_str}\n"
        f"🏦 Balance: ${balance:,.2f}\n"
        f"📉 Session: {pnl_sign}${running_pnl:.2f}"
    )
    send_telegram_message(msg)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _send_raw(text: str) -> bool:
    """Low-level send with up to 2 retries (3 total attempts)."""
    token = _bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = _chat_id   or os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(3):
        try:
            r = requests.post(url, json={"chat_id": chat, "text": text}, timeout=8)
            if r.status_code == 200:
                return True
            log.debug("Telegram HTTP %d (attempt %d): %s",
                      r.status_code, attempt + 1, r.text[:120])
        except Exception as exc:
            log.debug("Telegram send error (attempt %d): %s", attempt + 1, exc)
        if attempt < 2:
            time.sleep(2)

    log.warning("Telegram: all 3 send attempts failed.")
    return False
