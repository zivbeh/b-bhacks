"""
Strata TUI bridge — route log messages to the Strata left panel.

Usage:
    from strata_bridge import log
    log("  @OSINTdefender  Some breaking news...")

When Strata is running (HTTP server on STRATA_URL), messages appear in the left
feed panel. When Strata is not running, messages fall back to stdout so batch
mode and headless runs still work normally.
"""
from __future__ import annotations
import json
import os
import urllib.request

_STRATA_URL = os.getenv("STRATA_URL", "http://localhost:3001")


def log(msg: str) -> None:
    """Send a text message to the Strata left panel. Falls back to print if not running."""
    try:
        data = json.dumps({"msg": msg}).encode()
        req  = urllib.request.Request(
            f"{_STRATA_URL}/log", data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        # Strata not running — print to stdout (batch / headless mode)
        print(msg)


def log_telegram(
    ts: str,
    channel: str,
    text: str,
    media_path: str | None = None,
    media_type: str | None = None,
) -> None:
    """Send a structured Telegram message to the Strata left panel.

    ts         — ISO timestamp or HH:MM string
    channel    — channel username (without @)
    text       — full message text (first line shown by default, rest on expand)
    media_path — relative path to downloaded media file (optional)
    media_type — 'photo' | 'video' | None
    """
    try:
        data = json.dumps({
            "ts":         ts,
            "channel":    channel,
            "text":       text,
            "media_path": media_path,
            "media_type": media_type,
        }).encode()
        req = urllib.request.Request(
            f"{_STRATA_URL}/telegram", data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        # Strata not running — fall back to plain log
        snippet = (text or "").split("\n")[0][:100]
        flag = f"[{media_type}] " if media_type else ""
        print(f"  {ts} @{channel} {flag}{snippet}")


def push_pnl(stats: dict) -> None:
    """Send portfolio P&L summary to Strata (right panel next to AI SIGNALS).

    stats — dict from portfolio.compute_pnl(), e.g. total_pnl_usdc, cash_usdc, pnl_pct.
    """
    try:
        data = json.dumps({
            "total_pnl_usdc":  stats.get("total_pnl_usdc", 0),
            "unrealized_usdc": stats.get("unrealized_usdc", 0),
            "realized_usdc":   stats.get("realized_usdc", 0),
            "cash_usdc":       stats.get("cash_usdc", 0),
            "pnl_pct":         stats.get("pnl_pct", 0),
        }).encode()
        req = urllib.request.Request(
            f"{_STRATA_URL}/pnl", data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass  # Strata not running — silent
