#!/usr/bin/env python3
from __future__ import annotations
"""
Conflict Event Analyzer — library used by run.py.

Exported: load_all_messages, group_into_windows, analyze_message_group,
          load_existing_events, merge_events, save_events, extract_json,
          DATA_DIR, EVENTS_FILE, QUEUE_FILE, WINDOW_MINUTES
"""

import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv
import strata_bridge

load_dotenv()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

DATA_DIR    = Path("data")
EVENTS_DIR  = Path("events")
EVENTS_FILE = EVENTS_DIR  # kept for backwards-compat with run.py imports
QUEUE_FILE  = DATA_DIR / "queue.jsonl"

WINDOW_MINUTES  = 45
MAX_WINDOW_HOURS = 4   # hard cap: never group messages more than 4 hours apart
MAX_PHOTOS      = 0   # text-only mode for speed


# ── system prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a conflict-intelligence analyst and financial strategist specializing in Middle East events.
You receive raw Telegram intelligence and produce clean, precise event records for trading decisions.

═══ WORKFLOW ═══
1. Read all messages carefully.
2. Use web_search to verify the event, confirm the precise city/location, identify affected trade routes, ports, and currently active companies on those routes.
3. Output a JSON ARRAY of events (one object per distinct event). Return [] if nothing is actionable.
4. Output ONLY the raw JSON array — no markdown fences, no commentary.

═══ STRICT RULES ═══

LOCATION — map-displayable city or geographic feature only:
  ✓ "Tehran", "Jerusalem", "Strait of Hormuz", "Red Sea", "Haifa", "Bandar Abbas"
  ✗ "U.S. Embassy Jerusalem", "Iran nuclear facility", "Israeli military base"
  If the only known location is a country, use the capital city.

EVENT_TYPE — exactly one of these four strings:
  "Attack and Hit"     — weapon reached its target (confirmed hit, confirmed damage)
  "Attack and Miss"    — weapon intercepted, shot down, or failed to hit
  "Diplomatic Event"   — evacuations, negotiations, sanctions, statements, deployments without fire
  "Blockade"           — naval blockade, port closure, airspace closure, route interdiction

INVOLVED — countries and independent non-state actors ONLY.
  Merge rule: if a group IS the state's military/government arm, use the country name.
    IDF → "Israel"
    US Army / US Navy / US CENTCOM / US State Dept → "United States"
    IRGC / Iranian Armed Forces → "Iran"
    Russian Armed Forces → "Russia"
  Keep separate if the group operates independently of the state:
    Hezbollah (not Lebanon), Hamas (not Gaza/Palestine), Houthis (not Yemen),
    PMF/Popular Mobilization Forces (not Iraq), Islamic Jihad, etc.
  Result is a flat list of strings: ["Israel", "United States", "Hezbollah"]

PRIMARY_MARKETS — sector names only, from this list (pick the most relevant):
  defense | oil | natural gas | shipping routes | airlines | food commodities |
  steel & metals | insurance | tourism & hotels | gold | cyber security | agriculture

SECONDARY_MARKETS — use web_search to find the TOP 5 most exposed companies/commodities:
  - If a shipping route is affected: find the top 5 shipping companies currently operating on that route
  - If a port is hit: find what commodities flow through that port and top port operators
  - If an oil facility is hit: find top buyers of that crude/gas, pipeline operators
  - If a military base: find the top 5 defense contractors supplying that country's military
  Each entry: {"name": "Company or commodity name", "ticker": "TICKER.EXCHANGE or null", "type": "company|commodity", "signal": "bullish|bearish", "reason": "one sentence"}
  Ticker format: always include exchange suffix — NYSE/NASDAQ: no suffix needed (e.g. RTX, LMT),
    Tel Aviv: .TA (e.g. ESLT.TA), London: .L (e.g. HSX.L), Frankfurt: .DE, Copenhagen: .CO

TRADE_POSITION — the single most actionable hidden opportunity from this event:
  - Look beyond the obvious first-order effect
  - Consider supply chain, insurance, re-routing, safe-haven flows
  - Be specific: name the instrument, direction, and catalyst
  - Max 2 sentences.

CONFIDENCE — calibrate carefully:
  "high"   — 2+ independent Telegram sources OR single source with official confirmation via web_search
  "medium" — single Telegram source, unverified but plausible given context
  "low"    — unverified rumor, single source, contradicted by other reports

═══ OUTPUT SCHEMA ═══
[
  {
    "event_id": "<8-char hash you generate>",
    "headline": "<newspaper-style headline, max 12 words>",
    "timestamp": "<ISO 8601 UTC — time of event, not when reported>",
    "location": "<map-displayable city or geographic feature>",
    "event_type": "<Attack and Hit | Attack and Miss | Diplomatic Event | Blockade>",
    "involved": ["<country or independent group>"],
    "summary": "<2-3 sentences: what happened, confirmed outcome, casualties if known>",
    "primary_markets": ["<sector name>"],
    "secondary_markets": [
      {
        "name": "<company or commodity name>",
        "ticker": "<TICKER.EXCHANGE or null>",
        "type": "<company|commodity>",
        "signal": "<bullish|bearish>",
        "reason": "<one sentence why this is affected>"
      }
    ],
    "trade_position": "<specific actionable position in max 2 sentences>",
    "confidence": "<high|medium|low>",
    "sources": ["<Telegram channel names>"]
  }
]"""


# ── helpers ────────────────────────────────────────────────────────────────────

def event_fingerprint(ev: dict) -> str:
    """
    Content-based deduplication fingerprint.
    Same event_type + location + hour-bucket + sorted involved → same ID.
    """
    ts = ev.get("timestamp", "")[:13]  # "2026-03-01T04" — hour bucket
    etype = ev.get("event_type", "").lower().strip()
    loc = ev.get("location", "").lower().strip()
    involved = "|".join(sorted(i.lower().strip() for i in ev.get("involved", [])))
    raw = f"{etype}|{loc}|{ts}|{involved}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def load_existing_events() -> list:
    """Load all events from the events/ directory (one file per event)."""
    EVENTS_DIR.mkdir(exist_ok=True)
    events = []
    for f in sorted(EVENTS_DIR.glob("*.json")):
        try:
            events.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return events


def save_events(events: list):
    """Write each event as its own file: events/{event_id}.json"""
    EVENTS_DIR.mkdir(exist_ok=True)
    for ev in events:
        eid = ev.get("event_id", "unknown")
        path = EVENTS_DIR / f"{eid}.json"
        tmp  = path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def merge_events(existing: list, new_events: list) -> tuple[list, list]:
    """
    Merge new_events into existing using content fingerprints.
    Returns (merged_list, list_of_actually_added_events).
    """
    # Re-fingerprint all existing events on the content-based scheme
    existing_fps = {event_fingerprint(e) for e in existing}
    added = []
    for ev in new_events:
        # Override whatever event_id the LLM generated with our deterministic one
        fp = event_fingerprint(ev)
        ev["event_id"] = fp
        if fp not in existing_fps:
            existing.append(ev)
            existing_fps.add(fp)
            added.append(ev)
    return existing, added


def load_photo_b64(path: str) -> tuple[str, str] | None:
    fpath = DATA_DIR / path
    if not fpath.exists():
        return None
    ext = fpath.suffix.lower()
    media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".png": "image/png", ".gif": "image/gif",
                  ".webp": "image/webp"}.get(ext)
    if not media_type:
        return None
    try:
        data = base64.standard_b64encode(fpath.read_bytes()).decode("utf-8")
        return data, media_type
    except Exception:
        return None


def extract_json(text: str) -> list:
    """Extract a JSON array from Claude's response, tolerating surrounding text."""
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else [obj]
    except json.JSONDecodeError:
        pass
    for pattern in [r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", r"(\[[\s\S]*\])"]:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    strata_bridge.log(f"  [!] JSON parse failed:\n{text[:300]}")
    return []


def build_raw_input_record(messages: list) -> dict:
    """Build the raw_input block stored alongside each event. Uses relative paths."""
    raw_msgs = []
    media_urls = []
    for m in messages:
        raw_msgs.append({
            "timestamp": m.get("timestamp"),
            "channel":   m.get("channel"),
            "text_en":   m.get("text_en") or m.get("text_orig") or "",
            "text_orig": m.get("text_orig") or "",
        })
        mf = m.get("media_file")
        if mf:
            # Store relative path from project root (portable across machines)
            media_urls.append(str(DATA_DIR / mf))
    return {"messages": raw_msgs, "media_urls": media_urls}


# ── Claude analysis ────────────────────────────────────────────────────────────

def analyze_message_group(messages: list) -> list:
    """Send a group of messages (+ photos) to Claude. Returns list of event dicts."""
    if not messages:
        return []

    content = []

    # Attach up to MAX_PHOTOS photos
    photos_added = 0
    for msg in messages:
        mf = msg.get("media_file", "")
        if mf and "/photos/" in mf and photos_added < MAX_PHOTOS:
            result = load_photo_b64(mf)
            if result:
                b64, mtype = result
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mtype, "data": b64},
                })
                photos_added += 1

    # Build text block with all messages
    lines = []
    for m in messages:
        ts   = m.get("timestamp", "?")
        ch   = m.get("channel", "?")
        text = m.get("text_en") or m.get("text_orig") or "(no text)"
        line = f"[{ts}] @{ch}: {text}"
        mf   = m.get("media_file")
        if mf:
            line += f"\n  [media: {DATA_DIR / mf}]"
        lines.append(line)

    content.append({
        "type": "text",
        "text": (
            "Analyze these intelligence reports. Use web_search to verify, locate, "
            "and find currently active companies on affected routes/ports.\n\n"
            + "\n\n".join(lines)
        ),
    })

    loop_messages = [{"role": "user", "content": content}]
    continuations = 0
    max_cont      = 6

    while continuations < max_cont:
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=6000,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20260209", "name": "web_search"}],
                messages=loop_messages,
            )
        except anthropic.RateLimitError:
            strata_bridge.log("  [!] rate limited — waiting 60s...")
            time.sleep(60)
            continue
        except anthropic.APIError as e:
            strata_bridge.log(f"  [!] API error: {e}")
            return []

        if response.stop_reason == "end_turn":
            text = "\n".join(b.text for b in response.content if b.type == "text")
            events = extract_json(text)
            raw = build_raw_input_record(messages)
            for ev in events:
                ev["raw_input"] = raw
            return events

        if response.stop_reason in ("pause_turn", "tool_use"):
            loop_messages.append({"role": "assistant", "content": response.content})
            continuations += 1
            continue

        strata_bridge.log(f"  [!] unexpected stop_reason: {response.stop_reason}")
        break

    return []


# ── data loading + windowing ───────────────────────────────────────────────────

def load_all_messages(since: datetime | None = None, until: datetime | None = None) -> list:
    messages = []
    for jsonl_file in sorted(DATA_DIR.glob("**/*.jsonl")):
        if jsonl_file.name == "queue.jsonl":
            continue
        with jsonl_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
                rec["_ts"] = ts
                messages.append(rec)
    messages.sort(key=lambda m: m["_ts"])
    return messages


def group_into_windows(messages: list, window_minutes: int = WINDOW_MINUTES) -> list[list]:
    """
    Group messages into windows split by gaps > window_minutes.
    Also enforces a MAX_WINDOW_HOURS absolute cap so low-traffic days don't
    produce a single massive window spanning multiple calendar days.
    """
    if not messages:
        return []
    windows, current = [], [messages[0]]
    window_start = messages[0]["_ts"]
    for msg in messages[1:]:
        gap_minutes = (msg["_ts"] - current[-1]["_ts"]).total_seconds() / 60
        window_span = (msg["_ts"] - window_start).total_seconds() / 3600
        if gap_minutes > window_minutes or window_span > MAX_WINDOW_HOURS:
            windows.append(current)
            current = [msg]
            window_start = msg["_ts"]
        else:
            current.append(msg)
    windows.append(current)
    return windows
