# Setup

## 1. Get Telegram API credentials

1. Go to https://my.telegram.org
2. Log in with your phone number
3. Click **API development tools**
4. Create a new app (any name/description)
5. Copy **App api_id** and **App api_hash**

## 2. Configure

```bash
cd telegram_scraper
cp .env.example .env
# Edit .env and fill in your API_ID, API_HASH, and phone number (+1234567890 format)
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Run

```bash
# Fetch last 100 messages from each channel, then watch live:
python scraper.py

# Fetch last 500 messages (first run):
python scraper.py 500
```

On first run Telegram will send you a login code via SMS or the Telegram app.
The session is saved to `me_watcher.session` so you only log in once.

## 5. Search saved data

```bash
python search.py missile
python search.py strike --date 2024-04-14
python search.py explosion --channel OSINTdefender
python search.py drone --videos-only
```

## Data layout

```
data/
  OSINTdefender/
    2024-04-14/
      messages.jsonl      ← one JSON record per message
      videos/
        143000_12345.mp4
      photos/
        143001_12346.jpg
  iranintl/
    ...
```

Each `messages.jsonl` line:
```json
{
  "id": 12345,
  "channel": "OSINTdefender",
  "timestamp": "2024-04-14T14:30:00+00:00",
  "text_orig": "...",          // original language
  "text_en":   "...",          // translated to English
  "has_media": true,
  "media_file": "OSINTdefender/2024-04-14/videos/143000_12345.mp4"
}
```

## Add more channels

Edit `channels.py` and add channel usernames to `CHANNELS`.
You can also adjust `KEYWORDS` to filter for specific topics.
