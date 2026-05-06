"""Fetch candidate items from RSS feeds and Hacker News in parallel.

Reads sources.yml from the repo root. RSS via feedparser, HN via httpx.
Items get a stable id = sha1(url) so re-fetches are idempotent. Returns the
list of newly-inserted items so the caller can pipe them to embed.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

from . import db

USER_AGENT = "morning-digest-pipeline/0.1 (+https://github.com/neelgun17/morning-digest)"
HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_LIMIT = 30


def item_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def load_sources(path: Path = db.REPO_ROOT / "sources.yml") -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _fetch_rss(client: httpx.AsyncClient, source: dict) -> list[dict]:
    try:
        r = await client.get(source["url"], timeout=15.0,
                             headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"warn: rss fetch failed {source['url']}: {e}")
        return []
    feed = feedparser.parse(r.content)
    out = []
    for entry in feed.entries[:25]:
        url = getattr(entry, "link", None)
        if not url:
            continue
        out.append({
            "id": item_id(url),
            "url": url,
            "source": source.get("name", ""),
            "category": source.get("category", ""),
            "title": getattr(entry, "title", "")[:300],
            "summary": _extract_summary(entry),
            "published_at": _extract_published(entry),
        })
    return out


def _extract_summary(entry) -> str:
    for key in ("summary", "description"):
        val = getattr(entry, key, None)
        if val:
            return str(val)[:1000]
    return ""


def _extract_published(entry) -> str:
    for key in ("published", "updated", "created"):
        val = getattr(entry, key, None)
        if val:
            return str(val)
    return ""


async def _fetch_hn(client: httpx.AsyncClient) -> list[dict]:
    try:
        r = await client.get(HN_TOP, timeout=15.0)
        r.raise_for_status()
        ids = r.json()[:HN_LIMIT]
    except Exception as e:
        print(f"warn: HN top failed: {e}")
        return []

    async def one(hn_id: int) -> dict | None:
        try:
            rr = await client.get(HN_ITEM.format(id=hn_id), timeout=10.0)
            rr.raise_for_status()
            d = rr.json()
        except Exception:
            return None
        if not d or d.get("type") != "story":
            return None
        url = d.get("url") or f"https://news.ycombinator.com/item?id={hn_id}"
        return {
            "id": item_id(url),
            "url": url,
            "source": "Hacker News",
            "category": "tech",
            "title": (d.get("title") or "")[:300],
            "summary": "",
            "published_at": datetime.fromtimestamp(d.get("time", 0), tz=timezone.utc).isoformat()
                            if d.get("time") else "",
        }

    results = await asyncio.gather(*[one(i) for i in ids])
    return [x for x in results if x]


async def fetch_all(sources: dict) -> list[dict]:
    rss_sources = sources.get("rss", []) or []
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_rss(client, s) for s in rss_sources] + [_fetch_hn(client)]
        results = await asyncio.gather(*tasks)
    items: dict[str, dict] = {}
    for batch in results:
        for it in batch:
            items.setdefault(it["id"], it)  # de-dupe by url hash
    return list(items.values())


def insert(conn: sqlite3.Connection, items: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    inserted = 0
    for it in items:
        cur.execute(
            "INSERT OR IGNORE INTO items "
            "(id, url, source, category, title, summary, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (it["id"], it["url"], it["source"], it["category"],
             it["title"], it["summary"], it["published_at"], now),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return inserted


def run(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    if own:
        conn = db.connect()
        db.init_schema(conn)
    try:
        sources = load_sources()
        items = asyncio.run(fetch_all(sources))
        return insert(conn, items)
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    n = run()
    print(f"fetched and inserted {n} new items")
