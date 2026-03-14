#!/usr/bin/env python3
"""
Telegram Middle East Conflict Scraper

Commands:
  python scraper.py                          # watch default 3 channels live
  python scraper.py --backfill 500           # pull more history first, then watch live
  python scraper.py --fetch-range 2026-02-27 2026-03-14   # pull a date range, then exit
  python scraper.py --fetch-range 2026-02-27 2026-03-14 --channels all  # all 10 channels
"""

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
import strata_bridge
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from deep_translator import GoogleTranslator
from langdetect import detect, LangDetectException

load_dotenv()

# ── credentials ────────────────────────────────────────────────────────────────
API_ID   = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE    = os.environ["TELEGRAM_PHONE"]

# ── channel registry ───────────────────────────────────────────────────────────
DEFAULT_CHANNELS = [
    "OSINTdefender",   # OSINT aggregator — strikes, explosions, live updates
    "IsraelWarRoom",   # IDF ops, Gaza, Lebanon
    "AmitSegal",       # Amit Segal — Israeli journalist & political reporter
]

ALL_CHANNELS = DEFAULT_CHANNELS + [
    "MiddleEastSpectator", # ME news aggregator (English)
    "IranIntl_En",         # Iran International English
    "AJEnglish",           # Al Jazeera English
    "warmonitor3",         # War monitor — multi-front coverage
    "IntelSlava",          # Intel/military ops tracker
    "Aurora_Intel",        # Aurora Intelligence — OSINT geolocated events
    "elizrael",            # Eli Levi — Israeli military reporter
]

# Private channels / groups — invite links (https://t.me/+HASH or t.me/joinchat/HASH)
# The scraper will auto-join if not already a member and bypass the keyword filter.
PRIVATE_CHANNELS = [
    "https://t.me/+kKFnaadzxSUzNTRh",   # test channel
]

# Keywords — only save messages containing at least one of these
KEYWORDS = [
    "iran", "israel", "idf", "irgc", "hezbollah", "hamas", "gaza",
    "strike", "attack", "missile", "drone", "explosion", "hit", "killed",
    "airstrike", "operation", "rocket", "west bank", "lebanon", "syria",
    "yemen", "houthi", "rafah", "tel aviv", "tehran", "beirut", "baghdad",
    "war", "conflict", "military", "troops", "tank", "bombing", "shelling",
]

DATA_DIR   = Path("data")
translator = GoogleTranslator(source="auto", target="en")


# ── helpers ────────────────────────────────────────────────────────────────────

def channel_dir(channel: str, ts: datetime) -> Path:
    d = DATA_DIR / channel / ts.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_relevant(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in KEYWORDS)


def translate(text: str) -> str:
    if not text or not text.strip():
        return text
    try:
        lang = detect(text)
    except LangDetectException:
        lang = "unknown"
    if lang == "en":
        return text
    try:
        return translator.translate(text)
    except Exception:
        return text


def media_ext(media) -> str:
    if isinstance(media, MessageMediaPhoto):
        return ".jpg"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                return Path(attr.file_name).suffix or ".bin"
        return {
            "video/mp4": ".mp4", "video/webm": ".webm",
            "image/jpeg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "audio/ogg": ".ogg",
            "audio/mpeg": ".mp3",
        }.get(getattr(doc, "mime_type", ""), ".bin")
    return ".bin"


def is_video(media) -> bool:
    if not isinstance(media, MessageMediaDocument):
        return False
    mime = getattr(media.document, "mime_type", "")
    return mime.startswith("video/") or mime == "image/gif"


def append_jsonl(path: Path, record: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def invite_hash(link: str) -> str:
    """Extract the hash from a t.me/+ or t.me/joinchat/ invite link."""
    return re.split(r"[/+]+", link.rstrip("/"))[-1]


async def resolve_channels(client: TelegramClient, public: list[str], private: list[str]) -> tuple[list, set[int]]:
    """
    Join any private channels not yet joined. Returns (entity_list, private_ids)
    where private_ids is the set of channel IDs that bypass keyword filtering.
    """
    entities = []
    private_ids: set[int] = set()

    # Public channels — resolve by username
    for ch in public:
        try:
            entities.append(await client.get_entity(ch))
        except Exception as e:
            strata_bridge.log(f"  [!] could not resolve @{ch}: {e}")

    # Private channels — join if needed, then resolve
    for link in private:
        hash_ = invite_hash(link)
        try:
            result = await client(ImportChatInviteRequest(hash_))
            entity = result.chats[0]
            strata_bridge.log(f"  [joined] {getattr(entity, 'title', link)}")
        except UserAlreadyParticipantError:
            entity = await client.get_entity(link)
        except Exception as e:
            strata_bridge.log(f"  [!] could not join {link}: {e}")
            continue
        entities.append(entity)
        private_ids.add(entity.id)

    return entities, private_ids


# ── message processor ──────────────────────────────────────────────────────────

async def process_message(client: TelegramClient, message, channel: str, *, live: bool = False, skip_filter: bool = False):
    ts      = message.date.replace(tzinfo=timezone.utc)
    raw_txt = message.text or message.message or ""
    eng_txt = translate(raw_txt)

    if not skip_filter and not is_relevant(raw_txt + " " + eng_txt):
        return False  # filtered out

    cdir   = channel_dir(channel, ts)
    prefix = f"{ts.strftime('%H%M%S')}_{message.id}"

    record = {
        "id":         message.id,
        "channel":    channel,
        "timestamp":  ts.isoformat(),
        "text_orig":  raw_txt,
        "text_en":    eng_txt,
        "has_media":  message.media is not None,
        "media_file": None,
    }

    if message.media:
        mtype     = "videos" if is_video(message.media) else "photos"
        media_dir = cdir / mtype
        media_dir.mkdir(exist_ok=True)
        fpath     = media_dir / f"{prefix}{media_ext(message.media)}"
        try:
            await client.download_media(message.media, file=str(fpath))
            record["media_file"] = str(fpath.relative_to(DATA_DIR))
        except Exception as e:
            strata_bridge.log(f"  [!] media error [{channel}]: {e}")

    append_jsonl(cdir / "messages.jsonl", record)

    # emit to real-time queue so analyzer.py --watch can pick it up
    append_jsonl(DATA_DIR / "queue.jsonl", record)

    if live:
        media_type = (
            "video" if (message.media and is_video(message.media)) else
            "photo" if isinstance(message.media, MessageMediaPhoto) else
            None
        )
        strata_bridge.log_telegram(
            ts          = ts.strftime("%H:%M"),
            channel     = channel,
            text        = eng_txt or "(no text)",
            media_path  = record.get("media_file"),
            media_type  = media_type,
        )
    return True


# ── fetch modes ────────────────────────────────────────────────────────────────

async def backfill(client: TelegramClient, channel: str, limit: int):
    """Pull the N most recent messages from a channel."""
    strata_bridge.log(f"  backfilling @{channel} (last {limit} msgs)...")
    count = 0
    try:
        async for msg in client.iter_messages(channel, limit=limit):
            if await process_message(client, msg, channel):
                count += 1
        strata_bridge.log(f"  backfill @{channel}: {count} saved")
    except Exception as e:
        strata_bridge.log(f"  [!] backfill @{channel} FAILED: {e}")


async def fetch_range(client: TelegramClient, channel: str, since: datetime, until: datetime):
    """Pull all messages from a channel between since (inclusive) and until (inclusive)."""
    strata_bridge.log(f"  fetching @{channel} from {since.date()} to {until.date()}...")
    count = 0
    try:
        # offset_date + reverse=True starts iteration forward from `since`
        async for msg in client.iter_messages(
            channel,
            offset_date=since,
            reverse=True,       # oldest → newest
            limit=None,
        ):
            msg_ts = msg.date.replace(tzinfo=timezone.utc)
            if msg_ts > until:
                break
            if await process_message(client, msg, channel):
                count += 1
        strata_bridge.log(f"  fetch @{channel}: {count} saved")
    except Exception as e:
        strata_bridge.log(f"  [!] fetch @{channel} FAILED: {e}")


# ── entrypoints ────────────────────────────────────────────────────────────────

async def run_live(client: TelegramClient, channels: list, backfill_limit: int):
    entities, private_ids = await resolve_channels(client, channels, PRIVATE_CHANNELS)
    if not entities:
        strata_bridge.log("  [!] no channels resolved — check usernames and network")
        return

    if backfill_limit > 0:
        for entity in entities:
            name = getattr(entity, "username", None) or getattr(entity, "title", str(entity.id))
            await backfill(client, entity, limit=backfill_limit)

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        entity  = event.chat
        chat_id = getattr(entity, "id", 0)
        # Use username for public channels, title for private
        ch = getattr(entity, "username", None) or getattr(entity, "title", str(chat_id))
        # Private channels bypass keyword filter — accept all messages
        skip_filter = chat_id in private_ids
        await process_message(client, event.message, ch, live=True, skip_filter=skip_filter)

    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        strata_bridge.log("  scraper stopped.")


async def run_fetch_range(client: TelegramClient, channels: list, since: datetime, until: datetime):
    strata_bridge.log(f"{'='*70}")
    strata_bridge.log(f"  Telegram ME Conflict Scraper  |  date range fetch")
    strata_bridge.log(f"  Range:    {since.date()} -> {until.date()}")
    strata_bridge.log(f"  Channels: {', '.join(channels)}")
    strata_bridge.log(f"  Saving to: ./data/")
    strata_bridge.log(f"{'='*70}")

    for ch in channels:
        await fetch_range(client, ch, since, until)

    strata_bridge.log(f"Done. Data saved to ./data/")


async def main():
    ap = argparse.ArgumentParser(
        description="ME conflict Telegram scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scraper.py
      Watch default channels (OSINTdefender, IsraelWarRoom, AmitSegal) live.

  python scraper.py --backfill 500
      Pull last 500 msgs from default channels, then watch live.

  python scraper.py --fetch-range 2026-02-27 2026-03-14
      Fetch everything from Feb 27 to today from default channels, then exit.

  python scraper.py --fetch-range 2026-02-27 2026-03-14 --channels all
      Same but across all 10 channels.

  python scraper.py --fetch-range 2026-02-27 2026-03-14 --channels OSINTdefender IsraelWarRoom
      Same but for specific channels.
        """,
    )
    ap.add_argument(
        "--backfill", type=int, default=100, metavar="N",
        help="messages to backfill per channel on startup in live mode (default: 100)",
    )
    ap.add_argument(
        "--fetch-range", nargs=2, metavar=("SINCE", "UNTIL"),
        help="fetch all messages between two dates YYYY-MM-DD and exit (no live mode)",
    )
    ap.add_argument(
        "--channels", nargs="+", metavar="CHANNEL",
        help="channel list: 'all' for all 10, or space-separated usernames (default: 3 defaults)",
    )
    args = ap.parse_args()

    # resolve channel list
    if not args.channels:
        channels = DEFAULT_CHANNELS
    elif args.channels == ["all"]:
        channels = ALL_CHANNELS
    else:
        channels = args.channels

    client = TelegramClient("me_watcher", API_ID, API_HASH)
    await client.start(phone=PHONE)

    if args.fetch_range:
        since = parse_date(args.fetch_range[0])
        # make `until` end-of-day inclusive
        until = parse_date(args.fetch_range[1]).replace(hour=23, minute=59, second=59)
        await run_fetch_range(client, channels, since, until)
    else:
        await run_live(client, channels, backfill_limit=args.backfill)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
