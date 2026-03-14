# STRATA

**Real-time conflict intelligence → market signals → automated trading**

Scrapes Telegram conflict channels, extracts structured events with Claude AI, maps them to Polymarket prediction markets, and executes positions automatically — all visible in a live terminal dashboard.

---

## The Thesis

When a missile hits near a Gulf port, markets take minutes to hours to price in the cascading effects. STRATA closes that gap:

```
"Strike near a major Gulf port"
 → detect from live Telegram feeds
 → extract: location, event type, involved parties, confidence
 → map to active Polymarket questions
 → rank trades by urgency and expected edge
 → execute via Polymarket CLOB API
```

**Speed matters.** By the time a journalist writes the headline, the position is already placed.

---

## Architecture

```
Telegram Channels
      │
      ▼
 scraper.py ──────────────────▶  data/<channel>/<date>/messages.jsonl
      │                                      │
      │                          (queue.jsonl for live mode)
      │                                      │
      ▼                                      ▼
 run.py / resume.py ──────────▶  analyzer.py (Claude Sonnet)
      │                                      │
      │                          events/<event_id>.json
      │                                      │
      ▼                                      ▼
 trade_executor.py ───────────▶  Polymarket CLOB API
      │
      ▼
 portfolio.json  /  trades_log.json

      ↕ HTTP (port 3001)
 strata.js — live terminal TUI (map + feed + trades)
```

---

## All Runnable Commands

### `node strata.js` — Live Terminal Dashboard

The visual layer. Launches automatically via other scripts unless `--no-strata` is passed.
Start it manually for a standalone view:

```bash
node strata.js
```

Shows:
- Left panel: incident feed (collapsible AI events + live Telegram messages)
- Center: ASCII world map with red dots at event locations, click for detail popup
- Bottom: AI trade signals with urgency + market
- Right: Intel stats + trades execution log

---

### `scraper.py` — Telegram Ingestion

Connects to Telegram via MTProto and saves messages to `data/`.

```bash
# Live mode — watch 3 default channels, backfill 100 msgs on start
python scraper.py

# Live mode with more history on first run
python scraper.py --backfill 500

# Fetch a specific date range then exit (no live watch)
python scraper.py --fetch-range 2026-02-27 2026-03-14

# All 10 channels
python scraper.py --fetch-range 2026-02-27 2026-03-14 --channels all

# Specific channels
python scraper.py --fetch-range 2026-02-27 2026-03-14 --channels OSINTdefender IsraelWarRoom
```

**Default channels (3):** `OSINTdefender`, `IsraelWarRoom`, `AmitSegal`

**All channels (10):** + `MiddleEastSpectator`, `IranIntl_En`, `AJEnglish`, `warmonitor3`, `IntelSlava`, `Aurora_Intel`, `elizrael`

Messages are keyword-filtered, auto-translated to English, and saved as JSONL. Photos and videos are downloaded alongside.

---

### `run.py` — Event Analysis Pipeline

Processes scraped messages with Claude → extracts conflict events → ranks Polymarket trades.

```bash
# Process all scraped data (fastest, no web search)
python run.py

# Process from a start date
python run.py --since 2026-02-27

# Process a specific date range
python run.py --since 2026-02-27 --until 2026-03-14

# Real-time watch mode (reads queue.jsonl as scraper writes it)
python run.py --watch

# Enable web search for real-time event verification (slower but richer)
python run.py --since 2026-02-27 --search

# Control parallel workers (default: 5)
python run.py --since 2026-02-27 --workers 10

# Combine flags
python run.py --since 2026-02-27 --search --workers 3
```

**How it works:**
1. Groups messages into 45-minute time windows
2. Sends each window to Claude Sonnet in parallel (5 workers default)
3. Claude extracts structured events: type, location, parties, confidence, secondary market impacts
4. For each new event, ranks top Polymarket trades by urgency
5. Saves to `events/<event_id>.json` and streams to Strata TUI

**Speed:** With 5 workers and no web search, processes ~70 windows in ~7 minutes. Add `--search` for richer analysis at ~3x the time.

---

### `resume.py` — Smart Resume (Skip Completed Work)

Like `run.py` but skips steps already done — safe to re-run after interruption.

```bash
# Resume from a date, skip already-analyzed windows
python resume.py --since 2026-03-01

# With end date
python resume.py --since 2026-03-01 --until 2026-03-14

# Execute real Polymarket trades (default is dry-run)
python resume.py --since 2026-03-01 --live

# Headless (no Strata TUI)
python resume.py --since 2026-03-01 --no-strata
```

**What it skips:**
- Message windows already turned into events (by fingerprint match)
- Events that already have `polymarket_trades` ranked
- Events that already have `executions` recorded

---

### `main.py` — Full Orchestrator

Runs scraper + analyzer + trade executor all in parallel subprocesses.

```bash
# Live mode, dry-run trades
python main.py

# Live mode, execute real trades
python main.py --live

# Batch from date, dry-run
python main.py --since 2026-03-01

# Batch + real trades
python main.py --since 2026-03-01 --live

# All 10 channels
python main.py --channels all

# Skip trade executor
python main.py --no-trade

# Headless (no Strata TUI launched)
python main.py --no-strata

# Backfill messages on startup before going live
python main.py --backfill 500
```

---

### `trade_executor.py` — Polymarket Order Placement

Places orders on Polymarket via the CLOB API.

```bash
# Preview trades for one event (dry-run)
python trade_executor.py events/abc12345.json

# Execute real trades for one event
python trade_executor.py events/abc12345.json --live

# Watch events/ directory and auto-execute new events as they arrive
python trade_executor.py --watch

# Watch + real trades
python trade_executor.py --watch --live

# Only primary or secondary signals
python trade_executor.py events/abc12345.json --section primary
python trade_executor.py events/abc12345.json --section secondary
```

Requires `POLY_PRIVATE_KEY` in `.env`. Defaults: `POLY_ORDER_SIZE=10` USDC per trade, `POLY_MIN_URGENCY=immediate`.

---

### `review.py` — Portfolio Review Agent

Loads current portfolio, fetches live Polymarket prices, shows P&L, and asks Claude to decide HOLD / ADD / SELL on each open position.

```bash
# Review + dry-run decisions
python review.py

# Execute real trades based on Claude's decisions
python review.py --live

# Headless output
python review.py --no-strata

# Also consider new events from this date
python review.py --since 2026-03-01
```

---

### `search.py` — Search Saved Intelligence

Query the scraped message database.

```bash
python search.py missile
python search.py strike --date 2026-03-12
python search.py explosion --channel OSINTdefender
python search.py drone --videos-only
```

---

## Recommended Workflows

### Demo / Replay historical data

```bash
# Terminal 1: launch the TUI
node strata.js

# Terminal 2: run the pipeline (fast, parallel, no web search)
cd telegram_scraper
python resume.py --since 2026-02-27
```

### Live monitoring

```bash
# Terminal 1: TUI
node strata.js

# Terminal 2: scrape live
cd telegram_scraper
python scraper.py --backfill 200

# Terminal 3: analyze in real-time as messages come in
cd telegram_scraper
python run.py --watch
```

### Full auto (one command)

```bash
cd telegram_scraper
python main.py --backfill 200        # dry-run trades
python main.py --backfill 200 --live # real trades
```

---

## Setup

### Prerequisites

- Python 3.10+
- Node.js 18+
- [Telegram API credentials](https://my.telegram.org) → API development tools
- [Anthropic API key](https://console.anthropic.com)
- Polymarket wallet private key (only for live trading)

### Install

```bash
# Node dependencies (for TUI)
npm install

# Python dependencies
cd telegram_scraper
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Fill in: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, ANTHROPIC_API_KEY
# Optional for trading: POLY_PRIVATE_KEY, POLY_ORDER_SIZE, POLY_MIN_URGENCY
```

First run of `scraper.py` will prompt for a Telegram login code sent to your phone.

---

## Data Layout

```
telegram_scraper/
  data/
    <CHANNEL>/
      <YYYY-MM-DD>/
        messages.jsonl     ← scraped messages (text, translation, timestamps)
        photos/            ← downloaded images
        videos/            ← downloaded video clips
    queue.jsonl            ← live message queue (scraper → analyzer)

  events/
    <event_id>.json        ← one file per extracted event
                           ← contains: headline, location, event_type, involved,
                              primary/secondary markets, polymarket_trades, executions

  portfolio.json           ← open/closed positions, cash balance
  trades_log.json          ← full execution history
```

### Event JSON structure

```json
{
  "event_id": "a1b2c3d4",
  "headline": "IDF Kills Hezbollah Intelligence Chief in Beirut",
  "timestamp": "2026-03-02T01:00:00+00:00",
  "location": "Beirut",
  "event_type": "Attack and Hit",
  "involved": ["Israel", "Hezbollah"],
  "summary": "2-3 sentence description of what happened.",
  "primary_markets": ["defense", "insurance"],
  "secondary_markets": [
    { "name": "Elbit Systems", "ticker": "ESLT.TA", "signal": "bullish", "reason": "..." }
  ],
  "trade_position": "Specific actionable position in 1-2 sentences.",
  "confidence": "high",
  "sources": ["OSINTdefender", "IsraelWarRoom"],
  "polymarket_trades": {
    "primary": [
      { "rank": 1, "market": "Will Israel attack Iran by March 31?",
        "trade": "BUY NO", "current_price": 0.14, "urgency": "immediate",
        "url": "https://polymarket.com/..." }
    ],
    "secondary": [...]
  },
  "executions": [
    { "timestamp": "...", "status": "DRY_RUN", "size": 10, "price": 0.14 }
  ]
}
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Terminal UI | Node.js + MapSCII + raw ANSI |
| Scraping | [Telethon](https://github.com/LonamiWebs/Telethon) — Telegram MTProto |
| Translation | [deep-translator](https://github.com/nidhaloff/deep-translator) |
| AI Analysis | Claude Sonnet (parallel workers) |
| Trading | [Polymarket CLOB API](https://docs.polymarket.com) + py-clob-client |
| Runtime | Python 3.10+, Node.js 18+ |

---

*Built at B&B Hacks 2026*
