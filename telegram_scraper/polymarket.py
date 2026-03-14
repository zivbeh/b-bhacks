"""
Polymarket Gamma API client.
Fetches active conflict/geopolitical markets and caches them for 10 minutes.
"""

from __future__ import annotations
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional
import strata_bridge

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
CACHE_TTL   = 600   # 10 minutes
_DISK_CACHE = Path(__file__).parent / ".polymarket_cache.json"


def _get(path: str, params: dict) -> list:
    qs  = urllib.parse.urlencode(params)
    url = f"{GAMMA_API}/{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        strata_bridge.log(f"  [polymarket] fetch error: {e}")
        return []


def load_conflict_markets(force_refresh: bool = False) -> list[dict]:
    """
    Return all active Polymarket markets that are conflict/geopolitics-relevant.
    Results are cached in-memory (CACHE_TTL) and on disk (.polymarket_cache.json).
    Falls back to disk cache when API is unreachable.
    """
    global _cache
    if not force_refresh and time.time() - _cache["ts"] < CACHE_TTL and _cache["markets"]:
        return _cache["markets"]

    # Try live API
    all_markets: list[dict] = []
    try:
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
    except Exception as e:
        strata_bridge.log(f"  [polymarket] fetch failed: {e}")

    if all_markets:
        relevant = []
        for m in all_markets:
            q = m.get("question", "").lower()
            if any(kw in q for kw in CONFLICT_KEYWORDS):
                relevant.append(_clean(m))
        relevant.sort(key=lambda x: x["volume_usd"], reverse=True)
        # Persist to disk cache
        try:
            _DISK_CACHE.write_text(json.dumps(relevant, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        strata_bridge.log(f"  [polymarket] {len(relevant)} conflict markets loaded (live)")
        _cache = {"ts": time.time(), "markets": relevant}
        return relevant

    # Fall back to disk cache
    if _DISK_CACHE.exists():
        try:
            relevant = json.loads(_DISK_CACHE.read_text(encoding="utf-8"))
            if relevant:
                strata_bridge.log(f"  [polymarket] {len(relevant)} markets from disk cache (API down)")
                _cache = {"ts": time.time(), "markets": relevant}
                return relevant
        except Exception:
            pass

    # Simulate synthetic markets based on known conflict keywords
    strata_bridge.log("  [polymarket] API down + no cache — using simulated markets")
    _cache = {"ts": time.time(), "markets": _simulated_markets()}
    return _cache["markets"]


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


def _simulated_markets() -> list[dict]:
    """Return a minimal set of synthetic markets for offline/simulation mode."""
    synthetic = [
        {"question": "Will Israel attack Iran by April 2026?",        "outcomes": ["Yes","No"], "prices": ["0.72","0.28"], "volume_usd": 4200000, "liquidity": 300000, "end_date": "2026-04-30", "url": "https://polymarket.com", "condition_id": "sim001", "tokens": [{"token_id":"sim001y","outcome":"Yes"},{"token_id":"sim001n","outcome":"No"}]},
        {"question": "Will Iran strike Israel in March 2026?",         "outcomes": ["Yes","No"], "prices": ["0.35","0.65"], "volume_usd": 3100000, "liquidity": 250000, "end_date": "2026-03-31", "url": "https://polymarket.com", "condition_id": "sim002", "tokens": [{"token_id":"sim002y","outcome":"Yes"},{"token_id":"sim002n","outcome":"No"}]},
        {"question": "Will there be a ceasefire in Gaza by May 2026?", "outcomes": ["Yes","No"], "prices": ["0.45","0.55"], "volume_usd": 2800000, "liquidity": 180000, "end_date": "2026-05-31", "url": "https://polymarket.com", "condition_id": "sim003", "tokens": [{"token_id":"sim003y","outcome":"Yes"},{"token_id":"sim003n","outcome":"No"}]},
        {"question": "Will Houthis resume Red Sea attacks in Q2 2026?","outcomes": ["Yes","No"], "prices": ["0.61","0.39"], "volume_usd": 1500000, "liquidity": 120000, "end_date": "2026-06-30", "url": "https://polymarket.com", "condition_id": "sim004", "tokens": [{"token_id":"sim004y","outcome":"Yes"},{"token_id":"sim004n","outcome":"No"}]},
        {"question": "Will crude oil exceed $90 by end of March 2026?","outcomes": ["Yes","No"], "prices": ["0.28","0.72"], "volume_usd": 1200000, "liquidity": 90000,  "end_date": "2026-03-31", "url": "https://polymarket.com", "condition_id": "sim005", "tokens": [{"token_id":"sim005y","outcome":"Yes"},{"token_id":"sim005n","outcome":"No"}]},
        {"question": "Will Hezbollah attack Israel in March 2026?",    "outcomes": ["Yes","No"], "prices": ["0.22","0.78"], "volume_usd": 980000,  "liquidity": 75000,  "end_date": "2026-03-31", "url": "https://polymarket.com", "condition_id": "sim006", "tokens": [{"token_id":"sim006y","outcome":"Yes"},{"token_id":"sim006n","outcome":"No"}]},
        {"question": "Will US/Israel strike Yemen by March 31?",       "outcomes": ["Yes","No"], "prices": ["0.88","0.12"], "volume_usd": 870000,  "liquidity": 65000,  "end_date": "2026-03-31", "url": "https://polymarket.com", "condition_id": "sim007", "tokens": [{"token_id":"sim007y","outcome":"Yes"},{"token_id":"sim007n","outcome":"No"}]},
        {"question": "Will Iran nuclear deal be signed in 2026?",      "outcomes": ["Yes","No"], "prices": ["0.18","0.82"], "volume_usd": 750000,  "liquidity": 55000,  "end_date": "2026-12-31", "url": "https://polymarket.com", "condition_id": "sim008", "tokens": [{"token_id":"sim008y","outcome":"Yes"},{"token_id":"sim008n","outcome":"No"}]},
        {"question": "Will Israel strike 4 countries in 2026?",        "outcomes": ["Yes","No"], "prices": ["0.33","0.67"], "volume_usd": 680000,  "liquidity": 48000,  "end_date": "2026-12-31", "url": "https://polymarket.com", "condition_id": "sim009", "tokens": [{"token_id":"sim009y","outcome":"Yes"},{"token_id":"sim009n","outcome":"No"}]},
        {"question": "Will US impose new Iran sanctions in March 2026?","outcomes": ["Yes","No"], "prices": ["0.55","0.45"], "volume_usd": 520000,  "liquidity": 38000,  "end_date": "2026-03-31", "url": "https://polymarket.com", "condition_id": "sim010", "tokens": [{"token_id":"sim010y","outcome":"Yes"},{"token_id":"sim010n","outcome":"No"}]},
    ]
    return synthetic


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
