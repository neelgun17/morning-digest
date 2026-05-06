"""Cold-start ranker: cosine-sim against interest paragraphs + recency boost.

Outputs daily/candidates-YYYY-MM-DD.json that the Claude Code agent reads
instead of fetching feeds itself. Also logs impression records to
data/events.jsonl so reactions can be joined to scored items in training.

Section split is intentionally simple: items in 'finance' and 'news' categories
go to the news block; everything else is eligible for learning. The agent picks
which 2-3 to actually feature.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import db, embed

LEARNING_K = 8
NEWS_K = 10
RECENCY_HALF_LIFE_HOURS = 36.0
NEWS_CATEGORIES = {"news", "finance"}


def parse_interests(path: Path | None = None) -> list[str]:
    """Pull each non-empty interest line/cell out of interests.md.

    Falls back to interests.example.md so a freshly-cloned template can dry-run.
    """
    if path is None:
        path = db.REPO_ROOT / "interests.md"
    if not path.exists():
        path = db.REPO_ROOT / "interests.example.md"
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)  # strip comments
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "*", "---")):
            continue
        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            # skip header/separator rows
            if not cells or all(set(c) <= {"-", " "} for c in cells):
                continue
            if cells[0].lower() in {"interest", "category", "preference"}:
                continue
            payload = " — ".join(c for c in cells if c)
            if "Example:" in payload:
                continue
            items.append(payload)
        elif line.startswith("- "):
            items.append(line[2:].strip())
    return items


def _hours_since(iso: str) -> float:
    if not iso:
        return 24.0  # unknown → neutral
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 48.0


def _recency_factor(hours: float) -> float:
    return math.pow(0.5, hours / RECENCY_HALF_LIFE_HOURS)


def score_items(
    conn: sqlite3.Connection,
    interests: list[str],
    today: str,
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, url, source, category, title, summary, published_at, embedding "
        "FROM items WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows or not interests:
        return []

    interest_vecs = embed.encode(interests)  # (I, D)
    cand_vecs = np.vstack([embed._from_blob(r[7]) for r in rows])  # (N, D)
    sims = cand_vecs @ interest_vecs.T  # (N, I)
    top_sim = sims.max(axis=1)
    top_interest = sims.argmax(axis=1)

    out = []
    for (id_, url, source, category, title, summary, published_at, _), s, i in zip(
        rows, top_sim, top_interest
    ):
        hours = _hours_since(published_at)
        score = float(s) * _recency_factor(hours)
        out.append({
            "id": id_,
            "url": url,
            "source": source,
            "category": category,
            "title": title,
            "summary": summary,
            "published_at": published_at,
            "score": round(score, 4),
            "sim": round(float(s), 4),
            "age_hours": round(hours, 1),
            "reason": f"matches interest: {interests[i][:80]}",
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def split_sections(scored: list[dict]) -> dict:
    learning, news = [], []
    seen_sources_l: dict[str, int] = {}
    seen_sources_n: dict[str, int] = {}
    for it in scored:
        cat = (it["category"] or "").lower()
        bucket = news if cat in NEWS_CATEGORIES else learning
        seen = seen_sources_n if bucket is news else seen_sources_l
        if seen.get(it["source"], 0) >= 2:
            continue  # source diversity cap
        if bucket is learning and len(learning) >= LEARNING_K:
            continue
        if bucket is news and len(news) >= NEWS_K:
            continue
        bucket.append(it)
        seen[it["source"]] = seen.get(it["source"], 0) + 1
    return {"learning": learning, "news": news}


def log_impressions(events_path: Path, today: str, sections: dict) -> None:
    """Append one impression event per candidate to events.jsonl."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with events_path.open("a", encoding="utf-8") as f:
        for section_name, items in sections.items():
            for pos, it in enumerate(items):
                f.write(json.dumps({
                    "ts": ts,
                    "kind": "impression",
                    "date": today,
                    "section": section_name,
                    "position": pos,
                    "item_id": it["id"],
                    "score": it["score"],
                }, sort_keys=True) + "\n")


def write_candidates(out_path: Path, today: str, sections: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": today,
        "generator": "cold-start-cosine-v1",
        "learning": sections["learning"],
        "news": sections["news"],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run(conn: sqlite3.Connection, today: str | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date().isoformat()
    interests = parse_interests()
    scored = score_items(conn, interests, today)
    sections = split_sections(scored)
    out_path = db.REPO_ROOT / "daily" / f"candidates-{today}.json"
    write_candidates(out_path, today, sections)
    log_impressions(db.REPO_ROOT / "data" / "events.jsonl", today, sections)
    return {
        "interests": len(interests),
        "scored": len(scored),
        "learning": len(sections["learning"]),
        "news": len(sections["news"]),
        "out": str(out_path.relative_to(db.REPO_ROOT)),
    }
