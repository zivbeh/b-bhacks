"""
Polymarket Gamma API client.
Fetches active conflict/geopolitical markets and caches them for 10 minutes.
"""

from __future__ import annotations
import json
import time
import urllib.request
import urllib.parse
from typing import Optional

GAMMA_API = "https://gamma-api.polymarket.com"

# Keywords that flag a market as potentially conflict/geopolitics relevant
CONFLICT_KEYWORDS = [
    # Middle East specific
    "israel", "iran", "hamas", "hezbollah", "houthi", "idf", "irgc",
    "gaza", "west bank", "beirut", "tehran", "tel aviv",
    "ceasefire", "hostage", "normalization",
    # Conflict / military (specific enough to not match sports)
    "nuclear", "strait of hormuz", "missile", "airstrike", "war crimes",
    "military action", "invasion", "troops deployed", "bombing campaign",
    "assassination",
    # Geopolitics
    "sanctions", "regime change", "nato", "ukraine", "russia war",
    "peace deal", "blockade", "arms deal",
    # Energy (tied to conflict)
    "crude oil", "oil price", "opec", "brent", "strait",
]

_cache: dict = {"ts": 0.0, "markets": []}
CACHE_TTL = 600  # 10 minutes


def _get(path: str, params: dict) -> list:
    qs  = urllib.parse.urlencode(params)
    url = f"{GAMMA_API}/{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [polymarket] fetch error: {e}")
        return []


def load_conflict_markets(force_refresh: bool = False) -> list[dict]:
    """
    Return all active Polymarket markets that are conflict/geopolitics-relevant.
    Results are cached for CACHE_TTL seconds.
    """
    global _cache
    if not force_refresh and time.time() - _cache["ts"] < CACHE_TTL and _cache["markets"]:
        return _cache["markets"]

    print("  [polymarket] refreshing markets...", end="", flush=True)

    # Fetch in pages (500 per page) sorted by volume
    all_markets: list[dict] = []
    for offset in [0, 500, 1000]:
        page = _get("markets", {
            "active": "true",
            "closed": "false",
            "limit": 500,
            "order": "volume",
            "ascending": "false",
            "offset": offset,
        })
        if not page:
            break
        all_markets.extend(page)
        if len(page) < 500:
            break

    # Filter for conflict-relevant — question text only (descriptions are too noisy)
    relevant = []
    for m in all_markets:
        q = m.get("question", "").lower()
        if any(kw in q for kw in CONFLICT_KEYWORDS):
            relevant.append(_clean(m))

    # Sort by volume desc
    relevant.sort(key=lambda x: x["volume_usd"], reverse=True)

    print(f" {len(relevant)} markets")
    _cache = {"ts": time.time(), "markets": relevant}
    return relevant


def _clean(m: dict) -> dict:
    """Extract only the fields we care about."""
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices   = json.loads(m.get("outcomePrices", "[]"))
    except (json.JSONDecodeError, TypeError):
        outcomes, prices = [], []

    # Build URL — prefer the parent event slug
    events = m.get("events", [])
    if events and events[0].get("slug"):
        url = f"https://polymarket.com/event/{events[0]['slug']}"
    elif m.get("slug"):
        url = f"https://polymarket.com/market/{m['slug']}"
    else:
        url = "https://polymarket.com"

    # Token IDs needed for CLOB order placement (one per outcome)
    tokens = []
    for tok in m.get("tokens", []):
        tokens.append({
            "token_id": tok.get("token_id", ""),
            "outcome":  tok.get("outcome", ""),
        })

    return {
        "question":     m.get("question", ""),
        "outcomes":     outcomes,
        "prices":       prices,       # parallel list e.g. ["0.73", "0.27"]
        "volume_usd":   float(m.get("volumeNum", 0) or 0),
        "liquidity":    float(m.get("liquidityNum", 0) or 0),
        "end_date":     m.get("endDateIso") or m.get("endDate", ""),
        "url":          url,
        "condition_id": m.get("conditionId", ""),
        "tokens":       tokens,       # [{"token_id": "...", "outcome": "Yes"}, ...]
    }


def format_for_prompt(markets: list[dict]) -> str:
    """Format markets as a compact table for the Claude prompt."""
    lines = []
    for i, m in enumerate(markets, 1):
        pairs = " | ".join(f"{o}: {p}" for o, p in zip(m["outcomes"], m["prices"]))
        lines.append(
            f"{i:2}. [{pairs}]  Vol ${m['volume_usd']:,.0f}  "
            f"Ends {m['end_date'][:10]}  \"{m['question']}\""
        )
    return "\n".join(lines)
