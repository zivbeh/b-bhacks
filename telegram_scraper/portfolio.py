"""
Portfolio state management for simulated Polymarket positions.

portfolio.json schema:
{
  "cash_usdc":   1000.0,       # starting/remaining capital
  "positions":   [...],        # open positions
  "history":     [...]         # closed positions
}

Position schema:
{
  "id":              "<event_id>_<rank>_<section>",
  "event_id":        "abc123",
  "event_headline":  "IDF Kills Hezbollah Commander",
  "market":          "Will Israel attack Iran by March 31?",
  "direction":       "YES" | "NO",
  "entry_price":     0.65,
  "size_usdc":       10.0,
  "shares":          15.38,     # size_usdc / entry_price
  "entry_timestamp": "2026-03-02T14:30:00Z",
  "url":             "https://polymarket.com/...",
  "token_id":        "...",
  "current_price":   null,      # filled in by sync_prices()
  "status":          "open" | "closed",
  "exit_price":      null,
  "exit_timestamp":  null,
  "exit_pnl_usdc":   null
}
"""

from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

PORTFOLIO_FILE = Path("portfolio.json")
STARTING_CASH  = 1000.0


# ── persistence ────────────────────────────────────────────────────────────────

def load_portfolio() -> dict:
    if PORTFOLIO_FILE.exists():
        try:
            return json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cash_usdc": STARTING_CASH, "positions": [], "history": []}


def save_portfolio(portfolio: dict) -> None:
    tmp = PORTFOLIO_FILE.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(portfolio, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PORTFOLIO_FILE)


# ── position helpers ───────────────────────────────────────────────────────────

def open_position(
    portfolio:       dict,
    event_id:        str,
    event_headline:  str,
    market:          str,
    trade:           str,   # "BUY YES" | "BUY NO" | etc.
    entry_price:     float,
    size_usdc:       float,
    url:             str,
    token_id:        str,
    entry_timestamp: str | None = None,
    position_id:     str | None = None,
) -> dict:
    """Add a new open position. Returns the position dict."""
    direction = trade.replace("BUY ", "").strip()  # "YES", "NO", "OVER", "UNDER"
    entry_price = max(entry_price, 0.001)
    pos = {
        "id":              position_id or f"{event_id}_{uuid.uuid4().hex[:6]}",
        "event_id":        event_id,
        "event_headline":  event_headline,
        "market":          market,
        "direction":       direction,
        "entry_price":     entry_price,
        "size_usdc":       size_usdc,
        "shares":          round(size_usdc / entry_price, 4),
        "entry_timestamp": entry_timestamp or datetime.now(timezone.utc).isoformat(),
        "url":             url,
        "token_id":        token_id,
        "current_price":   entry_price,   # will be refreshed
        "status":          "open",
        "exit_price":      None,
        "exit_timestamp":  None,
        "exit_pnl_usdc":   None,
    }
    portfolio["positions"].append(pos)
    portfolio["cash_usdc"] = round(portfolio.get("cash_usdc", STARTING_CASH) - size_usdc, 4)
    return pos


def close_position(portfolio: dict, position_id: str, exit_price: float) -> dict | None:
    """Close an open position. Moves it to history. Returns the closed position."""
    for i, pos in enumerate(portfolio["positions"]):
        if pos["id"] == position_id and pos["status"] == "open":
            exit_value = round(pos["shares"] * exit_price, 4)
            pnl        = round(exit_value - pos["size_usdc"], 4)
            pos["status"]          = "closed"
            pos["exit_price"]      = exit_price
            pos["exit_timestamp"]  = datetime.now(timezone.utc).isoformat()
            pos["exit_pnl_usdc"]   = pnl
            pos["current_price"]   = exit_price
            portfolio["cash_usdc"] = round(portfolio.get("cash_usdc", 0) + exit_value, 4)
            portfolio["history"].append(portfolio["positions"].pop(i))
            return pos
    return None


def add_to_position(
    portfolio:   dict,
    position_id: str,
    add_usdc:    float,
    new_price:   float,
) -> dict | None:
    """Buy more shares of an existing open position at new_price."""
    for pos in portfolio["positions"]:
        if pos["id"] == position_id and pos["status"] == "open":
            new_shares  = add_usdc / max(new_price, 0.001)
            total_cost  = pos["size_usdc"] + add_usdc
            total_shares= pos["shares"] + new_shares
            pos["entry_price"] = round(total_cost / total_shares, 6)  # avg cost
            pos["shares"]      = round(total_shares, 4)
            pos["size_usdc"]   = round(total_cost, 4)
            pos["current_price"] = new_price
            portfolio["cash_usdc"] = round(portfolio.get("cash_usdc", 0) - add_usdc, 4)
            return pos
    return None


# ── price sync ─────────────────────────────────────────────────────────────────

def sync_prices(portfolio: dict, poly_markets: list[dict]) -> None:
    """Update current_price on all open positions from latest Polymarket data."""
    # Build lookup: question text (lowercase) → YES price
    price_map: dict[str, float] = {}
    for m in poly_markets:
        q = m.get("question", "").lower().strip()
        outcomes = m.get("outcomes", [])
        prices   = m.get("prices", [])
        if outcomes and prices:
            for outcome, price in zip(outcomes, prices):
                key = (q, outcome.upper())
                try:
                    price_map[key] = float(price)
                except (ValueError, TypeError):
                    pass

    for pos in portfolio["positions"]:
        if pos["status"] != "open":
            continue
        q   = pos["market"].lower().strip()
        dir = pos["direction"].upper()
        key = (q, dir)
        if key in price_map:
            pos["current_price"] = price_map[key]


# ── P&L calculations ───────────────────────────────────────────────────────────

def compute_pnl(portfolio: dict) -> dict:
    """
    Returns a summary dict with unrealized + realized P&L.
    """
    open_positions  = [p for p in portfolio["positions"] if p["status"] == "open"]
    closed_positions= portfolio.get("history", [])

    unrealized_usdc = 0.0
    total_invested  = 0.0

    for pos in open_positions:
        current = pos.get("current_price") or pos["entry_price"]
        value   = pos["shares"] * current
        pnl     = value - pos["size_usdc"]
        pos["_unrealized_pnl"]   = round(pnl, 4)
        pos["_current_value"]    = round(value, 4)
        pos["_pnl_pct"]          = round(pnl / pos["size_usdc"] * 100, 2) if pos["size_usdc"] else 0
        unrealized_usdc += pnl
        total_invested  += pos["size_usdc"]

    realized_usdc = sum(p.get("exit_pnl_usdc", 0) or 0 for p in closed_positions)

    return {
        "open_count":       len(open_positions),
        "closed_count":     len(closed_positions),
        "total_invested":   round(total_invested, 2),
        "unrealized_usdc":  round(unrealized_usdc, 2),
        "realized_usdc":    round(realized_usdc, 2),
        "total_pnl_usdc":   round(unrealized_usdc + realized_usdc, 2),
        "cash_usdc":        round(portfolio.get("cash_usdc", 0), 2),
        "pnl_pct":          round(unrealized_usdc / total_invested * 100, 2) if total_invested else 0,
    }


# ── formatting for Claude ──────────────────────────────────────────────────────

def format_portfolio_for_prompt(portfolio: dict) -> str:
    """Return a compact text block for Claude's context."""
    pnl    = compute_pnl(portfolio)
    lines  = []
    lines.append(f"PORTFOLIO SUMMARY")
    lines.append(f"  Cash available: ${pnl['cash_usdc']:.2f} USDC")
    lines.append(f"  Open positions: {pnl['open_count']}  |  Total invested: ${pnl['total_invested']:.2f}")
    lines.append(f"  Unrealized P&L: ${pnl['unrealized_usdc']:+.2f} ({pnl['pnl_pct']:+.1f}%)")
    lines.append(f"  Realized P&L:   ${pnl['realized_usdc']:+.2f}")
    lines.append(f"  Total P&L:      ${pnl['total_pnl_usdc']:+.2f}")
    lines.append("")

    open_pos = [p for p in portfolio["positions"] if p["status"] == "open"]
    if open_pos:
        lines.append("OPEN POSITIONS")
        lines.append(f"  {'ID':<16} {'DIR':<4} {'ENTRY':>6} {'NOW':>6} {'PNL%':>6}  {'MARKET'}")
        lines.append("  " + "─" * 80)
        for pos in open_pos:
            pnl_pct = pos.get("_pnl_pct", 0)
            pnl_str = f"{pnl_pct:+.1f}%"
            lines.append(
                f"  {pos['id']:<16} {pos['direction']:<4} "
                f"{pos['entry_price']:>6.3f} {(pos.get('current_price') or pos['entry_price']):>6.3f} "
                f"{pnl_str:>6}  {pos['market'][:65]}"
            )
    else:
        lines.append("OPEN POSITIONS: none")

    closed_pos = portfolio.get("history", [])
    if closed_pos:
        lines.append("")
        lines.append(f"CLOSED POSITIONS ({len(closed_pos)} total, last 5)")
        for pos in closed_pos[-5:]:
            lines.append(
                f"  {pos['id']:<16} {pos['direction']:<4} "
                f"entry:{pos['entry_price']:.3f} exit:{pos.get('exit_price',0):.3f} "
                f"pnl:${pos.get('exit_pnl_usdc',0):+.2f}  {pos['market'][:50]}"
            )

    return "\n".join(lines)
