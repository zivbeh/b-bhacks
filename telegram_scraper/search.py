#!/usr/bin/env python3
"""
Quick search tool — search saved JSONL logs by keyword, date, or channel.

Usage:
  python search.py <keyword>
  python search.py <keyword> --date 2024-04-14
  python search.py <keyword> --channel OSINTdefender
  python search.py <keyword> --videos-only
"""

import argparse
import json
from pathlib import Path

DATA_DIR = Path("data")


def search(keyword: str, date: str = None, channel: str = None, videos_only: bool = False):
    pattern = "**/*.jsonl"
    results = []

    for jsonl_file in sorted(DATA_DIR.glob(pattern)):
        parts = jsonl_file.parts  # data / channel / date / messages.jsonl
        if len(parts) < 4:
            continue
        file_channel = parts[1]
        file_date    = parts[2]

        if channel and file_channel.lower() != channel.lower():
            continue
        if date and file_date != date:
            continue

        with jsonl_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = (rec.get("text_en") or "") + " " + (rec.get("text_orig") or "")
                if keyword.lower() not in text.lower():
                    continue
                if videos_only and not (rec.get("media_file", "") or "").endswith(
                    (".mp4", ".webm", ".gif")
                ):
                    continue
                results.append(rec)

    results.sort(key=lambda r: r["timestamp"])
    print(f"\nFound {len(results)} result(s) for '{keyword}'\n{'─'*60}")
    for r in results:
        media = f"  📎 {r['media_file']}" if r.get("media_file") else ""
        print(f"[{r['timestamp']}] [{r['channel']}]{media}")
        print(f"  {(r['text_en'] or r['text_orig'] or '(no text)')[:300]}")
        print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword", help="Search term")
    ap.add_argument("--date",        help="Filter by date YYYY-MM-DD")
    ap.add_argument("--channel",     help="Filter by channel name")
    ap.add_argument("--videos-only", action="store_true")
    args = ap.parse_args()
    search(args.keyword, args.date, args.channel, args.videos_only)
