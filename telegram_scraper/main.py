#!/usr/bin/env python3
"""
ME Conflict Intelligence Pipeline — single entry point.

Usage:
  python main.py                  # live: scrape + analyze + dry-run trades
  python main.py --live           # live: scrape + analyze + execute real trades
  python main.py --since 2026-03-01           # batch: no scraper, process existing data
  python main.py --since 2026-03-01 --live    # batch + execute real trades

All three components run in parallel:
  1. scraper.py   — watches Telegram channels, writes to data/
  2. run.py       — analyzes messages every 45 min, writes to events/
  3. trade_executor.py — watches events/ and places Polymarket orders
"""

from __future__ import annotations
import argparse
import asyncio
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ── import components ──────────────────────────────────────────────────────────

from scraper import (
    TelegramClient, API_ID, API_HASH, PHONE,
    DEFAULT_CHANNELS, ALL_CHANNELS,
    run_live, run_fetch_range,
)
from run import run_batch, run_watch
from trade_executor import watch_and_execute


# ── helpers ────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def divider(char="═", width=72):
    print(char * width)


def thread(name: str, fn, *args):
    t = threading.Thread(target=fn, args=args, name=name, daemon=True)
    t.start()
    return t


STRATA_JS = Path(__file__).parent.parent / "strata.js"


def start_strata() -> subprocess.Popen | None:
    """Launch strata.js TUI in the same terminal if the file exists."""
    if not STRATA_JS.exists():
        return None
    node = os.popen("which node").read().strip() or "node"
    try:
        proc = subprocess.Popen([node, str(STRATA_JS)], cwd=STRATA_JS.parent)
        return proc
    except Exception as e:
        print(f"  [!] could not start strata.js: {e}")
        return None


# ── scraper (async, runs in its own event loop in a thread) ───────────────────

def run_scraper_thread(channels: list[str], backfill: int):
    async def _run():
        client = TelegramClient("me_watcher", API_ID, API_HASH)
        await client.start(phone=PHONE)
        await run_live(client, channels, backfill_limit=backfill)
        await client.disconnect()
    asyncio.run(_run())


# ── batch mode (no live scraping) ─────────────────────────────────────────────

def run_batch_mode(since: datetime | None, until: datetime | None, live_trades: bool):
    from polymarket import load_conflict_markets
    from trade_executor import execute_event_trades

    # Run batch pipeline (blocks until done)
    run_batch(since=since, until=until)

    if live_trades:
        poly = load_conflict_markets()
        events_dir = Path("events")
        print(f"\n  Executing trades (LIVE, ${os.getenv('POLY_ORDER_SIZE', 10)} USDC each)...")
        for f in sorted(events_dir.glob("*.json")):
            execute_event_trades(f, poly, dry_run=False)
    else:
        poly = load_conflict_markets()
        from trade_executor import execute_event_trades
        events_dir = Path("events")
        print(f"\n  Previewing trades (dry run)...")
        for f in sorted(events_dir.glob("*.json")):
            execute_event_trades(f, poly, dry_run=True)


# ── entrypoint ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="ME Conflict Intelligence Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          live mode, dry-run trades
  python main.py --live                   live mode, execute real trades
  python main.py --since 2026-03-01       batch historical, dry-run trades
  python main.py --since 2026-03-01 --live  batch + execute real trades
  python main.py --channels all           live mode, all 10 channels
        """,
    )
    ap.add_argument("--since",    metavar="YYYY-MM-DD", help="batch mode: start date")
    ap.add_argument("--until",    metavar="YYYY-MM-DD", help="batch mode: end date")
    ap.add_argument("--live",     action="store_true",  help="execute real Polymarket trades")
    ap.add_argument("--channels", nargs="+", metavar="CHANNEL",
                    help="'all' or specific usernames (default: 3 default channels)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="messages to backfill per channel on live startup (default: 0 = new only)")
    ap.add_argument("--no-trade",  action="store_true", help="skip trade executor entirely")
    ap.add_argument("--no-strata", action="store_true", help="skip Strata TUI (run headless)")
    args = ap.parse_args()

    # Validate required env vars
    missing = [k for k in ("ANTHROPIC_API_KEY", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE")
               if not os.getenv(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    if args.live and not os.getenv("POLY_PRIVATE_KEY"):
        print("ERROR: --live requires POLY_PRIVATE_KEY in .env")
        sys.exit(1)

    dry_run = not args.live

    # ── BATCH MODE ─────────────────────────────────────────────────────────────
    if args.since:
        since = parse_date(args.since)
        until = parse_date(args.until).replace(hour=23, minute=59, second=59) if args.until else None
        divider()
        print(f"  ME Conflict Intelligence Pipeline  |  BATCH MODE")
        print(f"  {'DRY RUN' if dry_run else 'LIVE TRADES'}  |  "
              f"{since.date()} → {until.date() if until else 'today'}")
        divider()
        run_batch_mode(since, until, live_trades=not dry_run)
        return

    # ── LIVE MODE ──────────────────────────────────────────────────────────────
    if args.channels and args.channels == ["all"]:
        channels = ALL_CHANNELS
    elif args.channels:
        channels = args.channels
    else:
        channels = ALL_CHANNELS

    divider()
    print(f"  ME Conflict Intelligence Pipeline  |  LIVE MODE")
    print(f"  Channels: {', '.join(channels)}")
    print(f"  Trades: {'LIVE' if not dry_run else 'DRY RUN'}")
    divider()
    print()

    threads = []
    strata_proc = None

    if not args.no_strata:
        strata_proc = start_strata()
        if strata_proc:
            time.sleep(1)  # let strata initialize before Python output begins

    # Thread 1: Telegram scraper
    print("  [1/3] Starting Telegram scraper...")
    threads.append(thread("scraper", run_scraper_thread, channels, args.backfill))
    time.sleep(2)  # let scraper connect before analyzer starts polling

    # Thread 2: Analyzer watch loop
    print("  [2/3] Starting event analyzer...")
    threads.append(thread("analyzer", run_watch))

    # Thread 3: Trade executor watch loop (delayed so polymarket prints don't overlap)
    if not args.no_trade:
        print(f"  [3/3] Starting trade executor ({'LIVE' if not dry_run else 'dry run'})...")
        def delayed_executor():
            time.sleep(5)
            watch_and_execute(dry_run)
        threads.append(thread("executor", delayed_executor))

    print("\n  All systems running. Press Ctrl+C to stop.\n")

    try:
        while True:
            # Restart any thread that died unexpectedly
            for t in threads:
                if not t.is_alive():
                    print(f"  [!] thread '{t.name}' died — check logs above")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n\n  Shutting down...")
        if strata_proc:
            strata_proc.terminate()
        sys.exit(0)


if __name__ == "__main__":
    main()
