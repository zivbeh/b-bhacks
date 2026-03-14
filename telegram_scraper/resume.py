#!/usr/bin/env python3
"""
ME Conflict Intelligence Pipeline — Smart Resume Mode.

Runs the full pipeline from a start date, skipping already-completed steps:
  1. Scraping      — skipped if data already exists for the date range
  2. Analysis      — only runs on message windows not yet turned into events
  3. Trade ranking — only runs for events without polymarket_trades
  4. Trade exec    — only runs for events without executions (dry-run by default)

Everything streams live to the Strata TUI.

Usage:
  python resume.py --since 2026-03-01
  python resume.py --since 2026-03-01 --until 2026-03-14
  python resume.py --since 2026-03-01 --live       # execute real trades
  python resume.py --since 2026-03-01 --no-strata  # headless / stdout only
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import strata_bridge
from analyzer import (
    load_all_messages,
    group_into_windows,
    analyze_message_group,
    load_existing_events,
    merge_events,
    save_events,
    EVENTS_FILE as EVENTS_DIR,
    WINDOW_MINUTES,
)
from polymarket import load_conflict_markets, format_for_prompt
from trade_executor import (
    execute_event_trades,
    _push_trades_to_strata,
    _load_trades_log,
)

import anthropic
client_ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ── helpers ────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def divider(char="═", width=72):
    strata_bridge.log(char * width)


STRATA_JS = Path(__file__).parent.parent / "strata.js"


def start_strata() -> subprocess.Popen | None:
    if not STRATA_JS.exists():
        return None
    node = os.popen("which node").read().strip() or "node"
    try:
        proc = subprocess.Popen([node, str(STRATA_JS)], cwd=STRATA_JS.parent)
        return proc
    except Exception as e:
        print(f"  [!] could not start strata.js: {e}")
        return None


def push_event_to_strata(ev: dict):
    """Push a single event to strata for live map display."""
    try:
        import urllib.request
        data = json.dumps([ev]).encode("utf-8")
        req = urllib.request.Request(
            f"{os.getenv('STRATA_URL', 'http://localhost:3001')}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def push_markets_to_strata(markets: list) -> None:
    if not markets:
        return
    try:
        import urllib.request
        data = json.dumps(markets[:20]).encode("utf-8")
        req = urllib.request.Request(
            f"{os.getenv('STRATA_URL', 'http://localhost:3001')}/markets",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


# ── trade ranking (same prompt as run.py) ─────────────────────────────────────

MAX_MARKETS = 50

TRADE_PROMPT = """You are a prediction-market trader specializing in geopolitical events.

Given a conflict event and a list of active Polymarket markets, return TWO sections of trades.

═══ PRIMARY (5 trades) ═══
The most obvious, direct-impact trades. First-order effects: the market directly about this event or its immediate consequences. High volume, high confidence.

═══ SECONDARY (10 trades) ═══
Dig deep. Think second and third-order effects:
- Supply chain disruption downstream (who buys from whom?)
- Insurance repricing (which underwriters are exposed?)
- Re-routing effects (alternative routes/ports that benefit)
- Currency and safe-haven flows
- Political knock-on effects (elections, sanctions, alliances)
- Commodity substitution (if one source is cut off, who benefits?)
- Markets that SEEM unrelated but are actually correlated
These should be non-obvious. Avoid repeating first-order markets already in PRIMARY.

For EVERY trade:
- Pick the specific outcome to buy
- Give a sharp one-sentence reason tied directly to this event
- Rate urgency: immediate / short-term (1-7 days) / medium-term (1-4 weeks)

Return ONLY a JSON object with two keys, no markdown:
{
  "primary": [
    {
      "rank": 1,
      "market": "<exact question text>",
      "url": "<polymarket URL>",
      "trade": "<BUY YES | BUY NO | BUY OVER | BUY UNDER>",
      "current_price": <0.0-1.0 as float>,
      "reasoning": "<one sharp sentence>",
      "urgency": "<immediate | short-term | medium-term>",
      "volume_usd": <number>
    }
  ],
  "secondary": [
    {
      "rank": 1,
      "market": "<exact question text>",
      "url": "<polymarket URL>",
      "trade": "<BUY YES | BUY NO | BUY OVER | BUY UNDER>",
      "current_price": <0.0-1.0 as float>,
      "reasoning": "<one sharp sentence — explain the non-obvious connection>",
      "urgency": "<immediate | short-term | medium-term>",
      "volume_usd": <number>
    }
  ]
}"""


def rank_trades(ev: dict, markets: list[dict]) -> dict:
    if not markets:
        return {"primary": [], "secondary": []}
    top = markets[:MAX_MARKETS]
    event_ctx = (
        f"EVENT: {ev.get('headline', '')}\n"
        f"Type: {ev.get('event_type', '')}\n"
        f"Location: {ev.get('location', '')}\n"
        f"Involved: {', '.join(ev.get('involved', []))}\n"
        f"Summary: {ev.get('summary', '')}\n"
        f"Primary markets affected: {', '.join(ev.get('primary_markets', []))}\n"
    )
    markets_ctx = (
        "ACTIVE POLYMARKET MARKETS (format: [outcomes: prices]  Volume  Expiry  Question):\n"
        + format_for_prompt(top)
    )
    try:
        resp = client_ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            system=TRADE_PROMPT,
            messages=[{"role": "user", "content": f"{event_ctx}\n\n{markets_ctx}"}],
        )
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        try:
            result = json.loads(text)
        except Exception:
            import re
            m = re.search(r"\{[\s\S]*\}", text)
            result = json.loads(m.group(0)) if m else {}

        url_map = {m["question"]: m["url"] for m in top}

        def patch_urls(trades):
            for t in trades:
                if not t.get("url"):
                    t["url"] = url_map.get(t.get("market", ""), "https://polymarket.com")
            return trades

        return {
            "primary":   patch_urls(result.get("primary",   [])[:5]),
            "secondary": patch_urls(result.get("secondary", [])[:10]),
        }
    except Exception as e:
        strata_bridge.log(f"  [!] trade ranking error: {e}")
        return {"primary": [], "secondary": []}


# ── main pipeline ──────────────────────────────────────────────────────────────

def run_resume(since: datetime, until: datetime | None, dry_run: bool):
    divider()
    label = f"  RESUME  |  {'DRY RUN' if dry_run else 'LIVE TRADES'}  |  {since.date()} → {(until.date() if until else 'today')}"
    strata_bridge.log(label)
    divider()

    # ── Step 1: Check scraped data ─────────────────────────────────────────────
    strata_bridge.log("  [1/4] Loading scraped messages...")
    messages = load_all_messages(since=since, until=until)
    if not messages:
        strata_bridge.log("  [!] No scraped messages found for this date range.")
        strata_bridge.log("       Run the scraper first: python scraper.py --since DATE")
        return

    strata_bridge.log(
        f"  ✓ {len(messages)} messages from "
        f"{len({m['channel'] for m in messages})} channels"
    )

    # ── Step 2: Load Polymarket markets ───────────────────────────────────────
    strata_bridge.log("  [2/4] Fetching Polymarket conflict markets...")
    poly_markets = load_conflict_markets()
    push_markets_to_strata(poly_markets)
    strata_bridge.log(f"  ✓ {len(poly_markets)} conflict markets")

    # ── Step 3: Analyze windows (skip already-processed) ─────────────────────
    strata_bridge.log("  [3/4] Analyzing message windows...")
    windows = group_into_windows(messages)
    strata_bridge.log(f"  ✓ {len(windows)} time windows ({WINDOW_MINUTES}-min gaps)")

    existing_events = load_existing_events()
    existing_ids    = {ev["event_id"] for ev in existing_events}
    strata_bridge.log(f"  ✓ {len(existing_ids)} events already on disk — skipping duplicates")

    # Push already-existing events to strata for live display
    for ev in existing_events:
        push_event_to_strata(ev)
    time.sleep(0.5)

    all_events   = list(existing_events)
    new_events   = []
    window_count = len(windows)

    for i, window in enumerate(windows, 1):
        t0 = window[0]["_ts"].strftime("%Y-%m-%d %H:%M")
        t1 = window[-1]["_ts"].strftime("%H:%M")
        strata_bridge.log(f"  [{i:3}/{window_count}] {t0}–{t1}  ({len(window)} msgs)...")

        extracted = analyze_message_group(window)
        if not extracted:
            continue

        all_events, added = merge_events(all_events, extracted)
        for ev in added:
            strata_bridge.log(f"  ✓ NEW: {ev.get('headline', '')[:70]}")
            push_event_to_strata(ev)
            new_events.append(ev)

    save_events(all_events)
    strata_bridge.log(f"  ✓ Analysis done. {len(new_events)} new events.")

    # ── Step 4: Trade ranking for events without trades ───────────────────────
    strata_bridge.log("  [4/4] Ranking Polymarket trades for events without picks...")
    needs_trades = [
        ev for ev in all_events
        if not ev.get("polymarket_trades") or (
            not ev["polymarket_trades"].get("primary") and
            not ev["polymarket_trades"].get("secondary")
        )
    ]
    strata_bridge.log(f"  ✓ {len(needs_trades)} events need trade ranking")

    for ev in needs_trades:
        hl = ev.get("headline", ev.get("event_id", ""))
        strata_bridge.log(f"  → Ranking: {hl[:60]}")
        trades = rank_trades(ev, poly_markets)
        ev["polymarket_trades"] = trades
        save_events([ev])
        push_event_to_strata(ev)

        pri_count = len(trades.get("primary", []))
        sec_count = len(trades.get("secondary", []))
        strata_bridge.log(f"    ✓ {pri_count} primary + {sec_count} secondary trades")

    # ── Step 5: Execute trades (skip events with executions) ──────────────────
    strata_bridge.log(f"  [5/5] Executing trades ({'DRY RUN' if dry_run else 'LIVE'})...")
    needs_exec = [
        Path(EVENTS_DIR) / f"{ev['event_id']}.json"
        for ev in all_events
        if not ev.get("executions")
        and ev.get("polymarket_trades")
        and (
            ev["polymarket_trades"].get("primary") or
            ev["polymarket_trades"].get("secondary")
        )
    ]
    strata_bridge.log(f"  ✓ {len(needs_exec)} events to execute")

    for event_path in needs_exec:
        if not event_path.exists():
            continue
        execute_event_trades(event_path, poly_markets, dry_run=dry_run)

    # ── Step 6: Portfolio P&L snapshot ────────────────────────────────────────
    try:
        import portfolio as pf
        portfolio = pf.load_portfolio()
        pf.sync_prices(portfolio, poly_markets)
        pf.save_portfolio(portfolio)
        stats = pf.compute_pnl(portfolio)
        strata_bridge.log("")
        strata_bridge.log(f"  PORTFOLIO  |  {stats['open_count']} open  |  "
                          f"invested: ${stats['total_invested']:.2f}  |  "
                          f"P&L: ${stats['unrealized_usdc']:+.2f} ({stats['pnl_pct']:+.1f}%)")
    except Exception:
        pass

    # ── Done ──────────────────────────────────────────────────────────────────
    divider("─")
    total_trades = len(_load_trades_log())
    strata_bridge.log(f"  DONE  |  {len(all_events)} events  |  {total_trades} trades in log")
    strata_bridge.log(f"  Run 'python review.py --since {since.date()}' to review portfolio")
    divider("─")


# ── entrypoint ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="ME Conflict Intelligence Pipeline — Smart Resume",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python resume.py --since 2026-03-01
  python resume.py --since 2026-03-01 --until 2026-03-14
  python resume.py --since 2026-03-01 --live
  python resume.py --since 2026-03-01 --no-strata
        """,
    )
    ap.add_argument("--since",     metavar="YYYY-MM-DD", required=True)
    ap.add_argument("--until",     metavar="YYYY-MM-DD")
    ap.add_argument("--live",      action="store_true", help="execute real Polymarket trades")
    ap.add_argument("--no-strata", action="store_true", help="headless / stdout only")
    args = ap.parse_args()

    missing = [k for k in ("ANTHROPIC_API_KEY",) if not os.getenv(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    dry_run = not args.live
    since   = parse_date(args.since)
    until   = parse_date(args.until).replace(hour=23, minute=59, second=59) if args.until else None

    strata_proc = None
    if not args.no_strata:
        strata_proc = start_strata()
        if strata_proc:
            time.sleep(2)  # let strata initialize

    try:
        run_resume(since=since, until=until, dry_run=dry_run)
    except KeyboardInterrupt:
        strata_bridge.log("  [!] interrupted by user")
    finally:
        if strata_proc:
            input("\n  Press Enter to close Strata TUI...")
            strata_proc.terminate()


if __name__ == "__main__":
    main()
