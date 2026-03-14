#!/usr/bin/env python3
"""
ME Conflict Intelligence Pipeline — event analysis + trade ranking.

Usage:
  python run.py                                        # process all scraped data
  python run.py --since 2026-02-27                     # process from date (fast, no search)
  python run.py --since 2026-02-27 --until 2026-03-14  # specific range
  python run.py --watch                                # real-time (reads queue.jsonl)
  python run.py --since 2026-02-27 --search            # enable web search (slower, richer)
  python run.py --since 2026-02-27 --workers 10        # more parallel Claude workers

Steps (automatic):
  1  Load scraped messages from data/
  2  Fetch active Polymarket conflict markets
  3  Group messages into 45-min windows, analyze in parallel (default: 5 workers)
  4  For each new event: rank top Polymarket trades with Claude
  5  Save events to events/<event_id>.json and stream to Strata TUI
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import strata_bridge
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

def push_markets_to_strata(markets: list) -> None:
    """Push Python's filtered conflict markets to the Strata TUI."""
    if not markets:
        return
    try:
        data = json.dumps(markets[:20]).encode("utf-8")  # top 20 by volume
        req  = urllib.request.Request(
            f"{STRATA_URL}/markets", data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


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
        strata_bridge.log(f"  [!] trade ranking error: {e}")
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

def run_batch(since: datetime | None, until: datetime | None,
              use_search: bool = False, workers: int = 5):
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

    # ── Step 3: extract events in parallel ────────────────────────────────────
    step(3, 3, f"Extracting events ({workers} parallel workers)...")
    windows = group_into_windows(messages)
    indent(f"{len(windows)} time windows ({WINDOW_MINUTES}-min gaps)")

    print_lock   = threading.Lock()
    done_count   = [0]   # mutable counter shared across threads
    total        = len(windows)

    def process_window(args):
        idx, window = args
        t0 = window[0]["_ts"].strftime("%Y-%m-%d %H:%M")
        t1 = window[-1]["_ts"].strftime("%H:%M")
        extracted = analyze_message_group(window, use_search=use_search)
        with print_lock:
            done_count[0] += 1
            label = f"→ {len(extracted)} event(s)" if extracted else "  (no events)"
            print(f"  [{done_count[0]:3}/{total}] {t0}–{t1}  ({len(window):3} msgs)  {label}")
        return idx, extracted

    # Collect results keyed by window index so we can merge in chronological order
    results: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_window, (i, w)): i
                   for i, w in enumerate(windows)}
        for fut in as_completed(futures):
            try:
                idx, extracted = fut.result()
                results[idx] = extracted
            except Exception as exc:
                with print_lock:
                    print(f"  [!] window failed: {exc}")

    # Merge in order and rank trades
    events         = load_existing_events()
    new_event_list: list[dict] = []
    for i in range(total):
        extracted = results.get(i) or []
        if not extracted:
            continue
        events, added = merge_events(events, extracted)
        if added:
            new_event_list.extend(added)
            for ev in added:
                print_event(ev)
                trades = rank_polymarket_trades(ev, poly_markets)
                ev["polymarket_trades"] = trades
                print_trades(trades)
            save_events(events)
            push_to_strata(added)

    indent(f"\nDone. {len(new_event_list)} new events → events/")
    divider("─")


# ── real-time watch mode ───────────────────────────────────────────────────────

def run_watch(use_search: bool = False):
    DATA_DIR.mkdir(exist_ok=True)
    QUEUE_FILE.touch()

    poly_markets      = load_conflict_markets()
    push_markets_to_strata(poly_markets)
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
            push_markets_to_strata(poly_markets)
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
                    strata_bridge.log(f"  + @{ch}  {text}")
                except Exception:
                    pass
        except Exception as e:
            strata_bridge.log(f"  [!] queue read error: {e}")

        # Fire as soon as DEBOUNCE_SEC has passed with no new messages
        if pending and last_msg_at and (time.time() - last_msg_at) >= DEBOUNCE_SEC:
            strata_bridge.log(f"  Analyzing {len(pending)} messages...")

            events    = load_existing_events()
            extracted = analyze_message_group(pending, use_search=use_search)

            if extracted:
                events, new_events = merge_events(events, extracted)

                for ev in new_events:
                    strata_bridge.log(f"  ✓ {ev.get('headline','')}")
                    strata_bridge.log(
                        f"    {ev.get('event_type','')} | {ev.get('location','')} | "
                        f"confidence: {ev.get('confidence','')}"
                    )

                    trades = rank_polymarket_trades(ev, poly_markets)
                    ev["polymarket_trades"] = trades
                    # trades are shown in the Polymarket panel via push_to_strata

                    for i, e in enumerate(events):
                        if e.get("event_id") == ev.get("event_id"):
                            events[i] = ev
                            break

                save_events(events)
                push_to_strata(new_events)

            pending     = []
            last_msg_at = None
        elif not pending:
            pass  # idle

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
  python run.py --since 2026-02-27          process Feb 27 → today (fast, no web search)
  python run.py --since 2026-02-27 --search enable web search for real-time verification
  python run.py --since 2026-02-27 --until 2026-03-14
  python run.py --watch                     real-time mode
  python run.py --watch --search            real-time mode with web search
  python run.py                             process all data
        """,
    )
    ap.add_argument("--since",  metavar="YYYY-MM-DD")
    ap.add_argument("--until",  metavar="YYYY-MM-DD")
    ap.add_argument("--watch",  action="store_true")
    ap.add_argument("--search", action="store_true",
                    help="enable web_search tool for real-time event verification (slower)")
    ap.add_argument("--workers", type=int, default=5, metavar="N",
                    help="parallel Claude workers for batch processing (default: 5)")
    args = ap.parse_args()

    for key in ("ANTHROPIC_API_KEY",):
        if key not in os.environ:
            print(f"ERROR: {key} not set in .env")
            sys.exit(1)

    if args.watch:
        run_watch(use_search=args.search)
    else:
        since = parse_date(args.since) if args.since else None
        until = parse_date(args.until).replace(hour=23, minute=59, second=59) if args.until else None
        run_batch(since=since, until=until, use_search=args.search, workers=args.workers)
