"""One-shot: seed data/events.jsonl from a legacy feedback-log.md.

Run this once in the private repo if you have historical feedback that pre-dates
the structured event log. Idempotent — appends only events not already present
(matched on ts+date+section+reaction).

Format expected in feedback-log.md (matches what the worker writes today):

    ## 2026-04-09 (email)
    - **Read**: email opened
    - **1. LLM Memory**: more_like_this

The "Read" lines become {"kind":"open"} events; the bullet entries with a
known reaction become {"kind":"click"} events.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import db

DATE_HEADER = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b")
ENTRY = re.compile(r"^-\s+\*\*(.+?)\*\*:\s*(.+?)\s*$")
KNOWN_REACTIONS = {
    "more_like_this", "go_deeper", "too_basic", "too_advanced", "not_interested",
}


def parse(md_path: Path) -> list[dict]:
    if not md_path.exists():
        return []
    events: list[dict] = []
    current_date: str | None = None
    for line in md_path.read_text(encoding="utf-8").splitlines():
        m = DATE_HEADER.match(line)
        if m:
            current_date = m.group(1)
            continue
        if not current_date:
            continue
        em = ENTRY.match(line)
        if not em:
            continue
        section, value = em.group(1).strip(), em.group(2).strip()
        if section.lower() == "read":
            events.append({
                "ts": f"{current_date}T00:00:00Z",
                "kind": "open",
                "date": current_date,
            })
        elif value in KNOWN_REACTIONS:
            events.append({
                "ts": f"{current_date}T00:00:00Z",
                "kind": "click",
                "date": current_date,
                "section": section,
                "reaction": value,
            })
    return events


def merge(events: list[dict], jsonl_path: Path = db.EVENTS_PATH) -> int:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    existing.add(line)

    written = 0
    with jsonl_path.open("a", encoding="utf-8") as f:
        for ev in events:
            line = json.dumps(ev, sort_keys=True)
            if line in existing:
                continue
            f.write(line + "\n")
            existing.add(line)
            written += 1
    return written


if __name__ == "__main__":
    md = db.REPO_ROOT / (sys.argv[1] if len(sys.argv) > 1 else "feedback-log.md")
    parsed = parse(md)
    n = merge(parsed)
    print(f"parsed {len(parsed)} events from {md.name}, wrote {n} new lines")
