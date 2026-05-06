"""Rebuild the SQLite event store from data/events.jsonl.

events.jsonl is the durable, human-diffable source of truth. Each line is one
JSON object written by the Cloudflare worker:

    {"ts": "2026-04-09T07:00:00Z", "kind": "click",
     "date": "2026-04-09", "section": "1. LLM Memory",
     "reaction": "more_like_this"}
    {"ts": "2026-04-09T07:30:00Z", "kind": "open", "date": "2026-04-09"}

This module rebuilds digest.db from that log. items rows are NOT created here
(fetch.py owns those); impressions are stubbed with item_id=NULL when we have
no link, so reactions can still be joined by (date, section). Once rank.py
runs, it backfills item_id on the impression row it created.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import db

OPEN_SECTION = "__digest_open__"  # synthetic section for whole-email opens


def ingest(events_path: Path = db.EVENTS_PATH, db_path: Path = db.DB_PATH) -> dict:
    conn = db.reset(db_path)
    cur = conn.cursor()
    counts = {"events": 0, "impressions": 0, "reactions": 0, "skipped": 0}

    if not events_path.exists():
        conn.commit()
        conn.close()
        return counts

    with events_path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                counts["skipped"] += 1
                print(f"warn: bad JSON on line {line_num}", file=sys.stderr)
                continue
            counts["events"] += 1
            _apply(cur, ev, counts)

    conn.commit()
    conn.close()
    return counts


def _apply(cur, ev: dict, counts: dict) -> None:
    kind = ev.get("kind")
    date = ev.get("date")
    if not date:
        counts["skipped"] += 1
        return

    if kind == "impression":
        # Ranker-emitted: links a specific item_id to a (date, section) slot.
        item_id = ev.get("item_id")
        section = ev.get("section") or "unknown"
        cur.execute(
            "INSERT OR IGNORE INTO impressions "
            "(item_id, digest_date, section, position, score) VALUES (?, ?, ?, ?, ?)",
            (item_id, date, section, ev.get("position"), ev.get("score")),
        )
        counts["impressions"] = cur.execute(
            "SELECT COUNT(*) FROM impressions"
        ).fetchone()[0]
        return

    section = ev.get("section") or (OPEN_SECTION if kind == "open" else None)
    if section is None:
        counts["skipped"] += 1
        return

    imp_id = _ensure_impression(cur, date, section)
    counts["impressions"] = cur.execute(
        "SELECT COUNT(*) FROM impressions"
    ).fetchone()[0]

    reaction = ev.get("reaction") or ("opened" if kind == "open" else None)
    if reaction is None:
        return

    cur.execute(
        "INSERT OR IGNORE INTO reactions (impression_id, reaction, ts) VALUES (?, ?, ?)",
        (imp_id, reaction, ev.get("ts", "")),
    )
    counts["reactions"] = cur.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]


def _ensure_impression(cur, date: str, section: str) -> int:
    row = cur.execute(
        "SELECT id FROM impressions WHERE digest_date = ? AND section = ? AND item_id IS NULL",
        (date, section),
    ).fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO impressions (item_id, digest_date, section) VALUES (NULL, ?, ?)",
        (date, section),
    )
    return cur.lastrowid


if __name__ == "__main__":
    result = ingest()
    print(json.dumps(result, indent=2))
