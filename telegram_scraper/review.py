#!/usr/bin/env python3
"""
Portfolio Review Agent.

Loads the current portfolio, fetches latest Polymarket prices, shows P&L,
then asks Claude to decide on each open position (HOLD / ADD / SELL) and
suggest new trades from any untraded events.

Usage:
  python review.py                        # review + dry-run decisions
  python review.py --live                 # execute real trades
  python review.py --no-strata            # headless / stdout only
  python review.py --since 2026-03-01     # also consider events from this date
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
import portfolio as pf
from polymarket import load_conflict_markets, format_for_prompt
from trade_executor import _push_trades_to_strata
from analyzer import load_existing_events, EVENTS_FILE as EVENTS_DIR

import anthropic
ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

STRATA_JS = Path(__file__).parent.parent / "strata.js"

# ── strata startup ─────────────────────────────────────────────────────────────

def start_strata() -> subprocess.Popen | None:
    if not STRATA_JS.exists():
        return None
    node = os.popen("which node").read().strip() or "node"
    try:
        return subprocess.Popen([node, str(STRATA_JS)], cwd=STRATA_JS.parent)
    except Exception as e:
        print(f"  [!] could not start strata.js: {e}")
        return None


def push_event_to_strata(ev: dict):
    try:
        import urllib.request
        data = json.dumps([ev]).encode("utf-8")
        req = urllib.request.Request(
            f"{os.getenv('STRATA_URL','http://localhost:3001')}",
            data=data, headers={"Content-Type":"application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def push_markets_to_strata(markets: list):
    try:
        import urllib.request
        data = json.dumps(markets[:20]).encode("utf-8")
        req = urllib.request.Request(
            f"{os.getenv('STRATA_URL','http://localhost:3001')}/markets",
            data=data, headers={"Content-Type":"application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


# ── Claude portfolio review prompt ────────────────────────────────────────────

REVIEW_SYSTEM = """You are a quantitative prediction-market portfolio manager.

Your job each review cycle:
1. Assess every OPEN POSITION: decide HOLD, ADD (specify usdc amount), or SELL
2. Survey NEW EVENTS (events without any executed trades): propose up to 3 NEW_TRADE entries per event worth acting on
3. Write a brief SUMMARY paragraph on overall portfolio health, notable risks, and strategy

Decision criteria:
- SELL if: the underlying event has resolved against you, price has moved significantly against you (>30% loss), or the thesis is broken
- ADD if: the trade is still directionally correct, price has improved since entry (better entry now), and cash is available
- HOLD if: thesis intact, monitoring
- NEW_TRADE if: event clearly moves a market and we are not yet positioned

Return ONLY a JSON object — no markdown, no commentary outside it:
{
  "summary": "<2-4 sentences on portfolio state and key risks>",
  "profitability": "<profitable | at_loss | breakeven>",
  "decisions": [
    {
      "position_id": "<id from open positions list>",
      "action":      "HOLD | ADD | SELL",
      "add_usdc":    <number, only if ADD, else null>,
      "reasoning":   "<one sharp sentence>"
    }
  ],
  "new_trades": [
    {
      "event_id":    "<event_id>",
      "market":      "<exact Polymarket question text>",
      "url":         "<polymarket URL>",
      "trade":       "<BUY YES | BUY NO | BUY OVER | BUY UNDER>",
      "size_usdc":   <number>,
      "entry_price": <0.0-1.0>,
      "urgency":     "<immediate | short-term | medium-term>",
      "reasoning":   "<one sharp sentence>"
    }
  ]
}"""


def build_review_prompt(portfolio: dict, events: list[dict], poly_markets: list[dict]) -> str:
    parts = []

    # ── Portfolio ──────────────────────────────────────────────────────────────
    parts.append(pf.format_portfolio_for_prompt(portfolio))
    parts.append("")

    # ── New events (no executed trades yet) ───────────────────────────────────
    untraded = [
        ev for ev in events
        if not ev.get("executions")
        and ev.get("polymarket_trades")
        and (
            ev["polymarket_trades"].get("primary") or
            ev["polymarket_trades"].get("secondary")
        )
    ]
    if untraded:
        parts.append(f"NEW EVENTS WITHOUT TRADES ({len(untraded)})")
        for ev in untraded:
            parts.append(f"\n  [{ev.get('timestamp','')[:10]}] [{ev.get('event_type','')}] {ev.get('headline','')}")
            parts.append(f"  Location: {ev.get('location','')}  |  Confidence: {ev.get('confidence','')}")
            parts.append(f"  Summary: {ev.get('summary','')[:200]}")
            pt = ev["polymarket_trades"]
            for trade in (pt.get("primary", []) + pt.get("secondary", []))[:5]:
                parts.append(
                    f"    #{trade.get('rank',0)} {trade.get('trade','')} @{trade.get('current_price',0):.2f} "
                    f"[{trade.get('urgency','')}] — {trade.get('market','')[:70]}"
                )
    else:
        parts.append("NEW EVENTS WITHOUT TRADES: none")

    parts.append("")

    # ── Market prices ──────────────────────────────────────────────────────────
    parts.append(f"ACTIVE POLYMARKET MARKETS (top {min(len(poly_markets), 50)} by volume):")
    parts.append(format_for_prompt(poly_markets[:50]))

    return "\n".join(parts)


def call_review_agent(portfolio: dict, events: list[dict], poly_markets: list[dict]) -> dict:
    prompt = build_review_prompt(portfolio, events, poly_markets)
    strata_bridge.log(f"  → Sending portfolio to review agent ({len(portfolio['positions'])} open positions)...")

    resp = ai.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()

    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        return json.loads(m.group(0)) if m else {}


# ── decision execution ─────────────────────────────────────────────────────────

def execute_review_decisions(
    review:       dict,
    portfolio:    dict,
    poly_markets: list[dict],
    events_map:   dict[str, dict],
    dry_run:      bool,
) -> list[dict]:
    """Apply HOLD/ADD/SELL decisions and place new trades. Returns trade log entries."""
    new_log_entries = []
    ts_now = datetime.now(timezone.utc).isoformat()

    # ── Existing position decisions ────────────────────────────────────────────
    for dec in review.get("decisions", []):
        pid    = dec.get("position_id", "")
        action = dec.get("action", "HOLD").upper()

        if action == "HOLD":
            strata_bridge.log(f"  [HOLD] {pid} — {dec.get('reasoning','')[:70]}")
            continue

        elif action == "SELL":
            pos = next((p for p in portfolio["positions"] if p["id"] == pid), None)
            if not pos:
                strata_bridge.log(f"  [!] SELL: position {pid} not found")
                continue
            exit_price = pos.get("current_price") or pos["entry_price"]
            strata_bridge.log(
                f"  [SELL{'  DRY' if dry_run else ''}] {pid} @ {exit_price:.3f} — {dec.get('reasoning','')[:60]}"
            )
            if not dry_run:
                pf.close_position(portfolio, pid, exit_price)
            entry = {
                "timestamp": ts_now,
                "event":     pos.get("event_headline", ""),
                "section":   "review",
                "rank":      None,
                "market":    pos["market"],
                "trade":     f"SELL {pos['direction']}",
                "price":     exit_price,
                "size":      pos["size_usdc"],
                "token_id":  pos.get("token_id", ""),
                "status":    "DRY_RUN" if dry_run else "SOLD",
                "url":       pos.get("url", ""),
                "error":     None,
            }
            new_log_entries.append(entry)

        elif action == "ADD":
            pos       = next((p for p in portfolio["positions"] if p["id"] == pid), None)
            add_usdc  = float(dec.get("add_usdc") or 10)
            if not pos:
                strata_bridge.log(f"  [!] ADD: position {pid} not found")
                continue
            cur_price = pos.get("current_price") or pos["entry_price"]
            strata_bridge.log(
                f"  [ADD{'   DRY' if dry_run else ''}] {pid} +${add_usdc} @ {cur_price:.3f} — {dec.get('reasoning','')[:55]}"
            )
            if not dry_run:
                pf.add_to_position(portfolio, pid, add_usdc, cur_price)
            entry = {
                "timestamp": ts_now,
                "event":     pos.get("event_headline", ""),
                "section":   "review",
                "rank":      None,
                "market":    pos["market"],
                "trade":     f"ADD {pos['direction']}",
                "price":     cur_price,
                "size":      add_usdc,
                "token_id":  pos.get("token_id", ""),
                "status":    "DRY_RUN" if dry_run else "PLACED",
                "url":       pos.get("url", ""),
                "error":     None,
            }
            new_log_entries.append(entry)

    # ── New trades ─────────────────────────────────────────────────────────────
    for nt in review.get("new_trades", []):
        market_q   = nt.get("market", "")
        trade_dir  = nt.get("trade", "BUY YES")
        size_usdc  = float(nt.get("size_usdc") or 10)
        entry_price= float(nt.get("entry_price") or 0.5)
        event_id   = nt.get("event_id", "review")
        event_ev   = events_map.get(event_id, {})
        headline   = event_ev.get("headline", event_id)

        # Find token_id for the market
        token_id = ""
        for m in poly_markets:
            if m.get("question", "").lower().strip() == market_q.lower().strip():
                direction = trade_dir.replace("BUY ", "").strip()
                for tok in m.get("tokens", []):
                    if tok.get("outcome", "").upper() == direction.upper():
                        token_id = tok.get("token_id", "")
                        break
                if not token_id and m.get("tokens"):
                    idx = 0 if "YES" in direction.upper() else 1
                    tokens = m.get("tokens", [])
                    token_id = tokens[idx]["token_id"] if idx < len(tokens) else ""
                break

        strata_bridge.log(
            f"  [NEW{'   DRY' if dry_run else ''}] {trade_dir} @{entry_price:.2f} ${size_usdc} — {market_q[:55]}"
        )

        if not dry_run and token_id:
            from trade_executor import get_client, place_order
            try:
                client_clob = get_client()
                resp = place_order(client_clob, token_id, entry_price, size_usdc, dry_run=False)
                status = resp.get("status", "PLACED")
            except Exception as e:
                status = "ERROR"
                strata_bridge.log(f"    error: {e}")
        else:
            status = "DRY_RUN"

        pos_id = pf.open_position(
            portfolio,
            event_id=event_id,
            event_headline=headline,
            market=market_q,
            trade=trade_dir,
            entry_price=entry_price,
            size_usdc=size_usdc,
            url=nt.get("url", ""),
            token_id=token_id,
            entry_timestamp=ts_now,
        )["id"]

        entry = {
            "timestamp": ts_now,
            "event":     headline,
            "section":   "new",
            "rank":      None,
            "market":    market_q,
            "trade":     trade_dir,
            "price":     entry_price,
            "size":      size_usdc,
            "token_id":  token_id,
            "status":    status,
            "url":       nt.get("url", ""),
            "error":     None,
        }
        new_log_entries.append(entry)

        # Mark the event as having an execution so it won't be re-traded next run
        if event_id in events_map:
            ev_path = Path(str(EVENTS_DIR)) / f"{event_id}.json"
            if ev_path.exists():
                ev_data = json.loads(ev_path.read_text(encoding="utf-8"))
                ev_data.setdefault("executions", []).append(entry)
                tmp = ev_path.with_suffix(".tmp.json")
                tmp.write_text(json.dumps(ev_data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(ev_path)

    return new_log_entries


# ── print / display helpers ────────────────────────────────────────────────────

def print_pnl_report(portfolio: dict):
    stats = pf.compute_pnl(portfolio)
    strata_bridge.log("  ╔══════════════════════════════════════════╗")
    strata_bridge.log("  ║         PORTFOLIO  P&L  REPORT           ║")
    strata_bridge.log("  ╠══════════════════════════════════════════╣")
    strata_bridge.log(f"  ║  Cash available:  ${stats['cash_usdc']:>8.2f} USDC         ║")
    strata_bridge.log(f"  ║  Open positions:   {stats['open_count']:>3}                       ║")
    strata_bridge.log(f"  ║  Total invested:  ${stats['total_invested']:>8.2f} USDC         ║")
    strata_bridge.log(f"  ║  Unrealized P&L:  ${stats['unrealized_usdc']:>+8.2f} ({stats['pnl_pct']:+.1f}%)      ║")
    strata_bridge.log(f"  ║  Realized P&L:    ${stats['realized_usdc']:>+8.2f}              ║")
    strata_bridge.log(f"  ║  Total P&L:       ${stats['total_pnl_usdc']:>+8.2f}              ║")
    strata_bridge.log("  ╚══════════════════════════════════════════╝")

    open_pos = [p for p in portfolio["positions"] if p["status"] == "open"]
    if open_pos:
        strata_bridge.log("")
        strata_bridge.log("  OPEN POSITIONS")
        for pos in open_pos:
            cur  = pos.get("current_price") or pos["entry_price"]
            pnl  = pos.get("_unrealized_pnl", (pos["shares"] * cur) - pos["size_usdc"])
            pct  = pnl / pos["size_usdc"] * 100 if pos["size_usdc"] else 0
            flag = "▲" if pnl >= 0 else "▼"
            strata_bridge.log(
                f"    {flag} [{pos['direction']}] entry:{pos['entry_price']:.3f} "
                f"→ now:{cur:.3f}  P&L:{pnl:+.2f} ({pct:+.1f}%)"
            )
            strata_bridge.log(f"       {pos['market'][:75]}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Portfolio review agent")
    ap.add_argument("--live",      action="store_true", help="execute real trades")
    ap.add_argument("--no-strata", action="store_true", help="headless / stdout")
    ap.add_argument("--since",     metavar="YYYY-MM-DD", help="only consider events from this date")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set"); sys.exit(1)

    dry_run = not args.live

    strata_proc = None
    if not args.no_strata:
        strata_proc = start_strata()
        if strata_proc:
            time.sleep(2)

    strata_bridge.log("  ══════════════════════════════════════════")
    strata_bridge.log(f"  PORTFOLIO REVIEW  |  {'DRY RUN' if dry_run else 'LIVE'}")
    strata_bridge.log("  ══════════════════════════════════════════")

    # ── 1. Load portfolio ──────────────────────────────────────────────────────
    portfolio = pf.load_portfolio()
    strata_bridge.log(f"  Portfolio loaded: {len(portfolio['positions'])} open, "
                      f"{len(portfolio.get('history', []))} closed, "
                      f"${portfolio.get('cash_usdc',0):.2f} cash")

    # ── 2. Fetch Polymarket data ───────────────────────────────────────────────
    strata_bridge.log("  Fetching Polymarket conflict markets...")
    poly_markets = load_conflict_markets()
    push_markets_to_strata(poly_markets)
    strata_bridge.log(f"  {len(poly_markets)} markets loaded")

    # ── 3. Sync prices ─────────────────────────────────────────────────────────
    pf.sync_prices(portfolio, poly_markets)
    pf.save_portfolio(portfolio)

    # ── 4. Print P&L report ───────────────────────────────────────────────────
    print_pnl_report(portfolio)

    # ── 5. Load events ────────────────────────────────────────────────────────
    all_events = load_existing_events()

    if args.since:
        from datetime import datetime, timezone
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        all_events = [
            ev for ev in all_events
            if ev.get("timestamp", "") >= since_dt.isoformat()[:10]
        ]

    events_map = {ev["event_id"]: ev for ev in all_events}

    # Push all events to strata map
    for ev in all_events:
        push_event_to_strata(ev)

    # ── 6. Call review agent ──────────────────────────────────────────────────
    strata_bridge.log("")
    strata_bridge.log("  Running review agent (Claude Opus)...")
    review = call_review_agent(portfolio, all_events, poly_markets)

    # ── 7. Show summary ───────────────────────────────────────────────────────
    strata_bridge.log("")
    strata_bridge.log(f"  PROFITABILITY: {review.get('profitability', 'unknown').upper()}")
    strata_bridge.log(f"  SUMMARY: {review.get('summary', '')[:200]}")
    strata_bridge.log("")

    # ── 8. Execute decisions ──────────────────────────────────────────────────
    decisions    = review.get("decisions", [])
    new_trades   = review.get("new_trades", [])
    strata_bridge.log(f"  Decisions: {len(decisions)} position reviews + {len(new_trades)} new trades")

    new_entries = execute_review_decisions(
        review, portfolio, poly_markets, events_map, dry_run=dry_run
    )

    # ── 9. Persist ────────────────────────────────────────────────────────────
    pf.save_portfolio(portfolio)

    if new_entries:
        # Append to trades_log.json
        from trade_executor import _append_trades_log
        _append_trades_log(new_entries)
        _push_trades_to_strata(new_entries)

    # ── 10. Final P&L after decisions ─────────────────────────────────────────
    pf.sync_prices(portfolio, poly_markets)
    strata_bridge.log("")
    strata_bridge.log("  ── AFTER REVIEW ──")
    print_pnl_report(portfolio)
    pf.save_portfolio(portfolio)

    strata_bridge.log("")
    strata_bridge.log("  Review complete.")

    try:
        if strata_proc:
            input("\n  Press Enter to close Strata TUI...")
            strata_proc.terminate()
    except KeyboardInterrupt:
        if strata_proc:
            strata_proc.terminate()


if __name__ == "__main__":
    main()
