#!/usr/bin/env python3
"""
ME Conflict Intelligence Pipeline — single command to run everything.

Usage:
  python run.py --since 2026-02-27          # process Feb 27 → today
  python run.py --since 2026-02-27 --until 2026-03-14
  python run.py --watch                     # real-time (scraper must be running)
  python run.py                             # process all scraped data

Steps (automatic):
  1  Load scraped messages (text only, no videos/images for speed)
  2  Fetch active Polymarket conflict markets
  3  Extract events with Claude Sonnet + web search
  4  For each new event: rank top-5 Polymarket trades with Claude Sonnet
  5  Save to events.json continuously
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from analyzer import (
    load_all_messages,
    group_into_windows,
    analyze_message_group,
    load_existing_events,
    merge_events,
    save_events,
    extract_json,
    DATA_DIR,
    EVENTS_FILE,
    QUEUE_FILE,
    WINDOW_MINUTES,
)
from polymarket import load_conflict_markets, format_for_prompt

load_dotenv()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

STRATA_URL = os.getenv("STRATA_URL", "http://localhost:3001")

def push_to_strata(events: list):
    """Push new events to the Strata TUI if it's running. Silent if not."""
    if not events:
        return
    try:
        data = json.dumps(events).encode("utf-8")
        req  = urllib.request.Request(
            STRATA_URL, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # strata not running — silently skip

# Max markets to send to Claude for trade ranking (keeps prompt under budget)
MAX_MARKETS_FOR_RANKING = 50


# ── Polymarket trade ranking ───────────────────────────────────────────────────

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


def rank_polymarket_trades(event: dict, markets: list[dict]) -> dict:
    """Ask Claude Sonnet to pick primary (5) and secondary (10) Polymarket trades."""
    if not markets:
        return {"primary": [], "secondary": []}

    top_markets = markets[:MAX_MARKETS_FOR_RANKING]

    event_ctx = (
        f"EVENT: {event.get('headline', '')}\n"
        f"Type: {event.get('event_type', '')}\n"
        f"Location: {event.get('location', '')}\n"
        f"Involved: {', '.join(event.get('involved', []))}\n"
        f"Summary: {event.get('summary', '')}\n"
        f"Primary markets affected: {', '.join(event.get('primary_markets', []))}\n"
        f"Secondary markets: {', '.join(s.get('name','') for s in event.get('secondary_markets', []))}\n"
    )

    markets_ctx = (
        "ACTIVE POLYMARKET MARKETS (format: [outcomes: prices]  Volume  Expiry  Question):\n"
        + format_for_prompt(top_markets)
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            system=TRADE_PROMPT,
            messages=[{"role": "user", "content": f"{event_ctx}\n\n{markets_ctx}"}],
        )
        text = "\n".join(b.text for b in response.content if b.type == "text")
        # extract_json returns a list; we need the object
        text = text.strip()
        try:
            result = __import__("json").loads(text)
        except Exception:
            import re, json
            m = re.search(r"\{[\s\S]*\}", text)
            result = json.loads(m.group(0)) if m else {}

        url_map = {m["question"]: m["url"] for m in top_markets}

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
        print(f"  [!] trade ranking error: {e}")
        return {"primary": [], "secondary": []}


# ── terminal helpers ───────────────────────────────────────────────────────────

def divider(char="═", width=72):
    print(char * width)

def step(n: int, total: int, label: str):
    print(f"\n[{n}/{total}] {label}")

def indent(msg: str, level: int = 2):
    print(" " * level + msg)

def print_event(ev: dict):
    hl    = ev.get("headline", "")
    loc   = ev.get("location", "")
    etype = ev.get("event_type", "")
    conf  = ev.get("confidence", "")
    print(f"  ✓ [{etype}]  {hl}  ({loc}, confidence: {conf})")

def _print_trade_list(trades: list[dict]):
    if not trades:
        print("    (none)")
        return
    for t in trades:
        price  = t.get("current_price", 0)
        trade  = t.get("trade", "")
        market = t.get("market", "")[:65]
        vol    = t.get("volume_usd", 0)
        urg    = t.get("urgency", "")
        reason = t.get("reasoning", "")
        url    = t.get("url", "")
        print(f"    #{t.get('rank',0)}  {trade:10s}  {price:.0%}  ${vol:>9,.0f}  [{urg}]")
        print(f"       \"{market}\"")
        print(f"       → {reason}")
        print(f"       {url}")

def print_trades(trades: dict):
    primary   = trades.get("primary", []) if isinstance(trades, dict) else trades
    secondary = trades.get("secondary", []) if isinstance(trades, dict) else []
    print("  ── PRIMARY (5 direct trades) ──")
    _print_trade_list(primary)
    if secondary:
        print("  ── SECONDARY (10 deep trades) ──")
        _print_trade_list(secondary)


# ── batch pipeline ─────────────────────────────────────────────────────────────

def run_batch(since: datetime | None, until: datetime | None):
    divider()
    label_since = since.date() if since else "all time"
    label_until = until.date() if until else "today"
    print(f"  ME Conflict Intelligence Pipeline  |  {label_since} → {label_until}")
    divider()

    # ── Step 1: load messages ──────────────────────────────────────────────────
    step(1, 3, "Loading scraped messages (text only)...")
    messages = load_all_messages(since=since, until=until)
    if not messages:
        print("  No messages found. Run scraper.py first.")
        return
    indent(f"{len(messages)} messages from "
           f"{len({m['channel'] for m in messages})} channels")

    # ── Step 2: load polymarket ────────────────────────────────────────────────
    step(2, 3, "Fetching Polymarket conflict markets...")
    poly_markets = load_conflict_markets()

    # ── Step 3: extract events ─────────────────────────────────────────────────
    step(3, 3, "Extracting events + ranking Polymarket trades...")
    windows = group_into_windows(messages)
    indent(f"{len(windows)} time windows ({WINDOW_MINUTES}-min gaps)")

    events         = load_existing_events()
    new_event_list: list[dict] = []

    for i, window in enumerate(windows, 1):
        t0 = window[0]["_ts"].strftime("%Y-%m-%d %H:%M")
        t1 = window[-1]["_ts"].strftime("%H:%M")
        sys.stdout.write(f"  [{i:3}/{len(windows)}] {t0}–{t1} UTC  ({len(window):3} msgs)  ")
        sys.stdout.flush()

        extracted = analyze_message_group(window)

        if extracted:
            events, added = merge_events(events, extracted)
            new_event_list.extend(added)

            if added:
                print(f"→ {len(added)} new event(s)")
                for ev in added:
                    print_event(ev)
                    trades = rank_polymarket_trades(ev, poly_markets)
                    ev["polymarket_trades"] = trades
                    print_trades(trades)
                save_events(events)
                push_to_strata(added)
            else:
                sys.stdout.write("\r" + " " * 80 + "\r")  # clear the line (all dupes)

    indent(f"\nDone. {len(new_event_list)} new events → events/")
    divider("─")


# ── real-time watch mode ───────────────────────────────────────────────────────

def run_watch():
    divider()
    print(f"  ME Conflict Intelligence Pipeline  |  LIVE MODE")
    print(f"  Watching {QUEUE_FILE} — scraper.py must be running")
    divider()

    DATA_DIR.mkdir(exist_ok=True)
    QUEUE_FILE.touch()

    poly_markets      = load_conflict_markets()
    poly_last_refresh = time.time()
    POLY_REFRESH_SEC  = 600  # refresh markets every 10 minutes

    DEBOUNCE_SEC = 30   # wait this long after last message before firing Claude

    queue_pos   = QUEUE_FILE.stat().st_size
    pending: list = []
    last_msg_at: float | None = None   # time of most recent incoming message

    while True:
        # Refresh Polymarket markets on schedule
        if time.time() - poly_last_refresh >= POLY_REFRESH_SEC:
            poly_markets = load_conflict_markets()
            poly_last_refresh = time.time()

        # Read new queue entries
        try:
            with QUEUE_FILE.open("rb") as f:
                f.seek(queue_pos)
                new_data = f.read()
                queue_pos += len(new_data)
            for line in new_data.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts  = datetime.fromisoformat(rec.get("timestamp", ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    rec["_ts"] = ts
                    pending.append(rec)
                    last_msg_at = time.time()
                    ch   = rec.get("channel", "")
                    text = (rec.get("text_en") or "")[:90]
                    print(f"  + @{ch}  {text}")
                except Exception:
                    pass
        except Exception as e:
            print(f"  [!] queue read error: {e}")

        # Fire as soon as DEBOUNCE_SEC has passed with no new messages
        if pending and last_msg_at and (time.time() - last_msg_at) >= DEBOUNCE_SEC:
            print(f"\n  Analyzing {len(pending)} messages...")

            events    = load_existing_events()
            extracted = analyze_message_group(pending)

            if extracted:
                events, new_events = merge_events(events, extracted)

                for ev in new_events:
                    print(f"\n  ✓ {ev.get('headline','')}")
                    print(f"    {ev.get('event_type','')} | {ev.get('location','')} | "
                          f"confidence: {ev.get('confidence','')}")
                    print(f"    {ev.get('summary','')[:140]}")

                    trades = rank_polymarket_trades(ev, poly_markets)
                    ev["polymarket_trades"] = trades
                    print_trades(trades)

                    for i, e in enumerate(events):
                        if e.get("event_id") == ev.get("event_id"):
                            events[i] = ev
                            break

                save_events(events)
                push_to_strata(new_events)

            pending     = []
            last_msg_at = None
        elif not pending:
            last_flush = time.time()

        time.sleep(10)


# ── entrypoint ─────────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="ME Conflict Intelligence Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py --since 2026-02-27          process Feb 27 → today
  python run.py --since 2026-02-27 --until 2026-03-14
  python run.py --watch                     real-time mode
  python run.py                             process all data
        """,
    )
    ap.add_argument("--since",  metavar="YYYY-MM-DD")
    ap.add_argument("--until",  metavar="YYYY-MM-DD")
    ap.add_argument("--watch",  action="store_true")
    args = ap.parse_args()

    for key in ("ANTHROPIC_API_KEY",):
        if key not in os.environ:
            print(f"ERROR: {key} not set in .env")
            sys.exit(1)

    if args.watch:
        run_watch()
    else:
        since = parse_date(args.since) if args.since else None
        until = parse_date(args.until).replace(hour=23, minute=59, second=59) if args.until else None
        run_batch(since=since, until=until)
