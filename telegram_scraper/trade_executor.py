"""
Polymarket trade executor.

Reads polymarket_trades from an event JSON and places real orders via the
Polymarket CLOB API using py-clob-client.

Required .env keys:
  POLY_PRIVATE_KEY   — wallet private key (0x...)
  POLY_ORDER_SIZE    — USDC amount per trade (default: 10)
  POLY_MIN_URGENCY   — only execute "immediate" or also "short-term" (default: immediate)

Usage:
  python trade_executor.py events/abc123.json             # execute trades for one event
  python trade_executor.py events/abc123.json --dry-run   # preview without placing
  python trade_executor.py --watch                        # watch events/ and auto-execute new ones
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import strata_bridge
load_dotenv()

PRIVATE_KEY  = os.getenv("POLY_PRIVATE_KEY", "")
ORDER_SIZE   = float(os.getenv("POLY_ORDER_SIZE", "10"))    # USDC per trade
MIN_URGENCY  = os.getenv("POLY_MIN_URGENCY", "immediate")   # immediate | short-term | medium-term
CHAIN_ID     = 137   # Polygon mainnet
CLOB_HOST    = "https://clob.polymarket.com"

URGENCY_RANK = {"immediate": 0, "short-term": 1, "medium-term": 2}

EVENTS_DIR  = Path("events")
TRADES_LOG  = Path("trades_log.json")


def _load_trades_log() -> list:
    if TRADES_LOG.exists():
        try:
            return json.loads(TRADES_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _append_trades_log(new_entries: list) -> None:
    if not new_entries:
        return
    log = _load_trades_log()
    log.extend(new_entries)
    tmp = TRADES_LOG.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(TRADES_LOG)


def _push_trades_to_strata(trades: list) -> None:
    if not trades:
        return
    try:
        import urllib.request as _ur
        data = json.dumps(trades).encode()
        req  = _ur.Request(
            f"{os.getenv('STRATA_URL', 'http://localhost:3001')}/trades",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        _ur.urlopen(req, timeout=1)
    except Exception:
        pass


# ── CLOB client setup ──────────────────────────────────────────────────────────

def get_client():
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        raise RuntimeError("py-clob-client not installed. Run: pip install py-clob-client")

    if not PRIVATE_KEY:
        raise RuntimeError("POLY_PRIVATE_KEY not set in .env")

    client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    creds  = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


# ── market lookup ──────────────────────────────────────────────────────────────

def find_token_id(market_question: str, trade_outcome: str, poly_markets: list[dict]) -> str | None:
    """
    Given a market question string and desired outcome (YES/NO/etc.),
    return the CLOB token_id needed to place the order.
    """
    q = market_question.lower().strip()
    for m in poly_markets:
        if m.get("question", "").lower().strip() == q:
            # Match token by outcome name
            outcome_word = trade_outcome.replace("BUY ", "").strip()  # "BUY YES" → "YES"
            for tok in m.get("tokens", []):
                if tok.get("outcome", "").upper() == outcome_word.upper():
                    return tok.get("token_id")
            # Fallback: if only 2 tokens and we want YES/NO, pick by index
            tokens = m.get("tokens", [])
            if len(tokens) == 2:
                idx = 0 if "YES" in outcome_word.upper() else 1
                return tokens[idx].get("token_id")
    return None


# ── order placement ────────────────────────────────────────────────────────────

def place_order(client, token_id: str, price: float, size: float, dry_run: bool) -> dict:
    """Place a market buy order on the CLOB."""
    if dry_run:
        return {"status": "DRY_RUN", "token_id": token_id, "price": price, "size": size}

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType, Side
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size, 2),
            side=Side.BUY,
        )
        signed = client.create_order(order_args)
        resp   = client.post_order(signed, OrderType.GTC)
        return resp
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


# ── core execution ─────────────────────────────────────────────────────────────

def execute_event_trades(
    event_path: Path,
    poly_markets: list[dict],
    dry_run: bool = True,
    sections: tuple[str, ...] = ("primary", "secondary"),
) -> list[dict]:
    """
    Load an event JSON, find its polymarket_trades, and place orders.
    Returns list of execution results.
    """
    event = json.loads(event_path.read_text(encoding="utf-8"))
    trades_blob = event.get("polymarket_trades", {})

    if not trades_blob:
        return []

    # Support old format (flat list) and new format (dict with primary/secondary)
    if isinstance(trades_blob, list):
        all_trades = [("primary", t) for t in trades_blob]
    else:
        all_trades = []
        for section in sections:
            for t in trades_blob.get(section, []):
                all_trades.append((section, t))

    client = None if dry_run else get_client()

    min_rank = URGENCY_RANK.get(MIN_URGENCY, 0)
    results  = []

    headline = event.get("headline", event_path.stem)
    strata_bridge.log(f"  ── {'[DRY RUN] ' if dry_run else ''}TRADES: {headline[:60]}")

    for section, trade in all_trades:
        urgency = trade.get("urgency", "medium-term")
        if URGENCY_RANK.get(urgency, 99) > min_rank:
            continue

        market_q  = trade.get("market", "")
        trade_dir = trade.get("trade", "")          # e.g. "BUY YES"
        price     = float(trade.get("current_price", 0.5))
        reason    = trade.get("reasoning", "")

        token_id = find_token_id(market_q, trade_dir, poly_markets)
        if not token_id:
            strata_bridge.log(f"  [!] token not found: {market_q[:60]}")
            results.append({"section": section, "rank": trade.get("rank"), "status": "TOKEN_NOT_FOUND", "market": market_q})
            continue

        resp = place_order(client, token_id, price, ORDER_SIZE, dry_run)

        status = resp.get("status", "PLACED")
        strata_bridge.log(
            f"  [{section.upper()}] #{trade.get('rank')} {trade_dir} @{price:.0%} "
            f"${ORDER_SIZE} [{status}] {market_q[:50]}"
        )
        if resp.get("error"):
            strata_bridge.log(f"    error: {resp['error']}")

        from datetime import datetime, timezone
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event":     headline,
            "section":   section,
            "rank":      trade.get("rank"),
            "market":    market_q,
            "trade":     trade_dir,
            "price":     price,
            "size":      ORDER_SIZE,
            "token_id":  token_id,
            "status":    status,
            "url":       trade.get("url", ""),
            "error":     resp.get("error"),
        }
        results.append(entry)

    # Persist execution results back into the event file
    event.setdefault("executions", [])
    event["executions"].extend(results)
    tmp = event_path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(event_path)

    # Save to trades_log.json and push to strata
    if results:
        _append_trades_log(results)
        _push_trades_to_strata(results)
        # Record in portfolio (only trades that were actually placed or simulated)
        _record_in_portfolio(results, event)

    return results


def _record_in_portfolio(results: list[dict], event: dict) -> None:
    """Open a portfolio position for each successfully placed (or dry-run) trade."""
    try:
        import portfolio as pf
        portfolio = pf.load_portfolio()
        event_id  = event.get("event_id", "unknown")
        headline  = event.get("headline", "")
        changed   = False
        for entry in results:
            if entry.get("status") not in ("PLACED", "DRY_RUN"):
                continue
            # Avoid duplicate positions for same market+direction
            market_q  = entry.get("market", "")
            direction = entry.get("trade", "").replace("BUY ", "").strip()
            already   = any(
                p["market"] == market_q and p["direction"] == direction and p["status"] == "open"
                for p in portfolio["positions"]
            )
            if not already:
                pf.open_position(
                    portfolio,
                    event_id        = event_id,
                    event_headline  = headline,
                    market          = market_q,
                    trade           = entry.get("trade", "BUY YES"),
                    entry_price     = float(entry.get("price", 0.5)),
                    size_usdc       = float(entry.get("size", ORDER_SIZE)),
                    url             = entry.get("url", ""),
                    token_id        = entry.get("token_id", ""),
                    entry_timestamp = entry.get("timestamp"),
                    position_id     = f"{event_id}_{entry.get('section','p')}{entry.get('rank',0)}",
                )
                changed = True
        if changed:
            pf.save_portfolio(portfolio)
    except Exception:
        pass  # portfolio tracking is best-effort


# ── watch mode ─────────────────────────────────────────────────────────────────

def watch_and_execute(dry_run: bool):
    """Watch events/ for new files and auto-execute their trades."""
    from polymarket import load_conflict_markets

    # Verify CLOB client available before starting (skip gracefully if not)
    if not dry_run:
        try:
            get_client()
        except RuntimeError as e:
            strata_bridge.log(f"  [!] trade executor disabled: {e}")
            return

    strata_bridge.log(f"  [EXEC] watching events/ | {'DRY RUN' if dry_run else 'LIVE'} | ${ORDER_SIZE} USDC | min: {MIN_URGENCY}")

    # Push existing trades log to strata on startup
    _push_trades_to_strata(_load_trades_log())

    poly_markets      = load_conflict_markets()
    poly_last_refresh = time.time()

    # Only skip events that already have executions recorded.
    # Events with polymarket_trades but no executions will be processed on startup.
    executed: set[str] = set()
    for f in EVENTS_DIR.glob("*.json"):
        try:
            ev = json.loads(f.read_text(encoding="utf-8"))
            if ev.get("executions"):
                executed.add(f.name)
        except Exception:
            pass

    while True:
        # Refresh market data every 10 min
        if time.time() - poly_last_refresh > 600:
            poly_markets      = load_conflict_markets()
            poly_last_refresh = time.time()

        for f in sorted(EVENTS_DIR.glob("*.json")):
            if f.name not in executed:
                executed.add(f.name)
                time.sleep(0.5)  # let run.py finish writing the file
                execute_event_trades(f, poly_markets, dry_run=dry_run)

        time.sleep(5)


# ── entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Polymarket trade executor")
    ap.add_argument("event_file", nargs="?", help="Path to a single event JSON file")
    ap.add_argument("--watch",   action="store_true", help="Watch events/ and auto-execute")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Preview trades without placing (default: True)")
    ap.add_argument("--live",    action="store_true",
                    help="Place real orders (overrides --dry-run)")
    ap.add_argument("--section", choices=["primary", "secondary", "both"], default="both")
    args = ap.parse_args()

    dry_run  = not args.live
    sections = ("primary",) if args.section == "primary" else \
               ("secondary",) if args.section == "secondary" else \
               ("primary", "secondary")

    if args.watch:
        watch_and_execute(dry_run=dry_run)
    elif args.event_file:
        from polymarket import load_conflict_markets
        poly = load_conflict_markets()
        execute_event_trades(Path(args.event_file), poly, dry_run=dry_run, sections=sections)
    else:
        ap.print_help()
