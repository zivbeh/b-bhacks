# ⚡ STRATA

**Real-time conflict intelligence → market signals → automated trading**

Scrapes social media, Telegram, and news channels to detect Middle East conflict events *faster than humans can react* — maps them through supply networks to tradable instruments and executes on [Polymarket](https://polymarket.com) via API.

---

## The Thesis

When a missile hits near a Gulf port, markets take minutes to hours to price in the cascading effects. This system aims to close that gap:

```
"Strike near a major Gulf port"
 → identify the port / nearby logistics node
 → infer shipping disruption, insurance costs, rerouting risk
 → infer oil flow, tanker rates, refinery throughput, defense risk premium
 → map to tradable instruments and prediction markets
 → execute with explanation and source evidence
```

**Speed matters.** By the time a journalist writes the headline, the position is already placed.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA INGESTION                           │
│                                                                 │
│  Telegram Channels ──┐                                          │
│  News APIs ──────────┼──▶  scraper.py  ──▶  data/*.jsonl        │
│  Social Media ───────┘     (live + backfill)   (text + media)   │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     AI EVENT ANALYSIS                           │
│                                                                 │
│  analyzer.py                                                    │
│  ├─ Claude Opus + web search verification                       │
│  ├─ Structured event extraction (location, weapons, parties)    │
│  ├─ Casualty & damage assessment                                │
│  ├─ Supply chain impact mapping                                 │
│  ├─ Polymarket market identification                            │
│  └─ Trading signal generation (sector, direction, tickers)      │
│                                                                 │
│  Output: events.json                                            │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    TRADING EXECUTION                            │
│                                                                 │
│  Polymarket API  ──▶  Automated position entry                  │
│  Supply Graph    ──▶  Cascade analysis (oil, defense, shipping) │
│  Risk Engine     ──▶  Position sizing & confidence scoring      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Pipeline Detail

### 1. 📡 Ingestion — `scraper.py`

Real-time Telegram scraper watching 10 conflict-focused channels:

| Channel | Focus |
|---------|-------|
| `@OSINTdefender` | OSINT aggregator — strikes, explosions, live updates |
| `@GeoConfirmed` | Geolocated conflict events with maps |
| `@MiddleEastSpectator` | ME news aggregator (English) |
| `@iranintl` | Iran/Israel/region coverage |
| `@IsraelWarRoom` | IDF operations, Gaza, Lebanon |
| `@AlJazeera` | Al Jazeera breaking news |
| `@warmonitor3` | Multi-front military coverage |
| `@Conflicts` | Conflict news aggregator |
| `@IntelSlava` | Intel/military ops tracker |
| `@menaconflict` | MENA conflict reporting |

**Features:**
- Keyword filtering (missiles, strikes, drones, etc.)
- Auto-translation to English (Arabic, Hebrew, Farsi, Russian → EN)
- Media download (photos, videos, documents)
- Backfill history on first run
- Live listener for new messages as they arrive

### 2. 🧠 Analysis — `analyzer.py`

Uses **Claude Opus** with web search to process raw intelligence into structured events:

```json
{
  "event_id": "a1b2c3d4",
  "timestamp": "2026-03-12T14:30:00+00:00",
  "confirmed": true,
  "event_type": "airstrike",
  "summary": "IDF airstrike targets Hezbollah weapons depot near Baalbek, Lebanon",
  "location": {
    "name": "Baalbek industrial zone",
    "country": "Lebanon",
    "facility_type": "military_base",
    "precision": "high"
  },
  "groups_involved": [...],
  "weapons_used": ["F-35"],
  "casualties": {"killed": 3, "injured": 12, "confidence": "estimated"},
  "suppliers": [
    {"company": "Lockheed Martin", "country": "US", "product": "F-35", "relevance": "direct"}
  ],
  "polymarket_markets": [
    {
      "market": "Israel-Lebanon ceasefire by April 2026",
      "direction": "no",
      "impact": "high",
      "reasoning": "Continued strikes indicate escalation, not de-escalation"
    }
  ],
  "trading_signals": [
    {
      "sector": "defense",
      "signal": "bullish",
      "magnitude": "medium",
      "tickers": ["LMT", "RTX", "ELBIT"],
      "reasoning": "Confirmed use of advanced munitions increases procurement outlook"
    },
    {
      "sector": "oil_gas",
      "signal": "bullish",
      "magnitude": "low",
      "tickers": ["XOM", "CVX"],
      "reasoning": "Regional escalation adds risk premium to oil futures"
    }
  ]
}
```

**Modes:**
- **Batch:** Process historical data — `python3 analyzer.py --since 2026-02-27`
- **Real-time:** Watch for new messages — `python3 analyzer.py --watch`

### 3. 🔍 Search — `search.py`

Query saved intelligence by keyword, date, channel, or media type:

```bash
python3 search.py missile
python3 search.py strike --date 2026-03-12
python3 search.py explosion --channel OSINTdefender
python3 search.py drone --videos-only
```

### 4. 🏗️ Supply Network Graph *(in progress)*

Maps conflict events to economic ripple effects:

```
Missile strike on port
 └─▶ Shipping disruption
      ├─▶ Tanker rates ↑  (ZIM, INSW)
      ├─▶ Insurance costs ↑  (shipping insurers)
      └─▶ Rerouting via longer routes
           ├─▶ Oil delivery delays → spot price ↑  (XOM, CVX, CL futures)
           └─▶ Refinery throughput ↓
                └─▶ Gasoline/petrochemical supply ↓
```

### 5. 💰 Polymarket Execution *(in progress)*

Automated trading on prediction markets:
- Match detected events to active Polymarket questions
- Score confidence based on source count, confirmation status, and historical accuracy
- Execute positions via Polymarket API with position sizing based on signal magnitude

---

## Demo

**Replay February 28th → present:**

- 📺 Live terminal tracking of all events (100s of incidents)
- 🖱️ Click to view the video/source itself
- 📊 Real-time trading signal dashboard
- ⚡ Polymarket execution log

---

## Quick Start

### Prerequisites
- Python 3.9+
- [Telegram API credentials](https://my.telegram.org) (API development tools)
- [Anthropic API key](https://console.anthropic.com) (for Claude-powered analysis)

### Setup

```bash
cd telegram_scraper

# Configure credentials
cp .env.example .env
# Edit .env with your TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, ANTHROPIC_API_KEY

# Install dependencies
pip3 install -r requirements.txt
pip3 install anthropic

# Run the scraper (first run will prompt for Telegram login code)
python3 scraper.py                    # backfill 100 msgs + live watch
python3 scraper.py --backfill 500     # backfill more history on first run

# Run the analyzer
python3 analyzer.py --since 2026-02-27              # batch process from date
python3 analyzer.py --since 2026-02-27 --until 2026-03-14  # specific range
python3 analyzer.py --watch                          # real-time mode

# Search saved intelligence
python3 search.py missile
python3 search.py strike --date 2026-03-12 --videos-only
```

### Data Layout

```
data/
  OSINTdefender/
    2026-03-12/
      messages.jsonl          ← structured message records
      videos/
        143000_12345.mp4      ← downloaded media
      photos/
        143001_12346.jpg
  IsraelWarRoom/
    ...
events.json                   ← AI-extracted structured events with trading signals
```

---

## Roadmap

- [x] Telegram multi-channel scraper with keyword filtering
- [x] Auto-translation (AR/HE/FA/RU → EN)
- [x] Media download (photos + videos)
- [x] Claude-powered event analysis with web search verification
- [x] Structured event extraction (location, weapons, parties, casualties)
- [x] Trading signal generation (sector, direction, magnitude, tickers)
- [x] Polymarket market matching
- [x] Batch + real-time analysis modes
- [ ] Supply network graph visualization
- [ ] Polymarket API integration (automated execution)
- [ ] Live terminal dashboard with event replay
- [ ] WhatsApp / X (Twitter) / news RSS ingestion
- [ ] Confidence scoring & backtesting framework
- [ ] Multi-market execution (Polymarket + traditional brokers)

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Scraping | [Telethon](https://github.com/LonamiWebs/Telethon) (Telegram MTProto) |
| Translation | [deep-translator](https://github.com/nidhaloff/deep-translator) (Google Translate) |
| Language Detection | [langdetect](https://github.com/Mimino666/langdetect) |
| AI Analysis | [Claude Opus](https://anthropic.com) + web search |
| Trading | Polymarket API *(in progress)* |
| Runtime | Python 3.9+ |

---

*Built at B&B Hacks 2026* 🏴‍☠️
