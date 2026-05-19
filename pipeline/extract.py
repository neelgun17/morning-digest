"""Fetch article bodies for the top-N stage-1 candidates and cache them.

Used by rank.py between stage 1 (cheap cosine recall) and stage 2 (Haiku
rerank + summary). Bodies are interest-independent, so the cache lives in
items.body and survives across runs.

Failures (timeout, 4xx/5xx, paywall, JS-only page) return None for that
item — the caller falls back to title + RSS summary.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import db

USER_AGENT = "morning-digest-pipeline/0.1 (+https://github.com/neelgun17/morning-digest)"
TIMEOUT_S = 12.0
MAX_BODY_CHARS = 8000          # cap stored body; rerank sends a shorter excerpt
PARALLEL = 8


def _trafilatura_extract(html: str) -> str | None:
    """Lazy import so tests can run without trafilatura installed."""
    try:
        import trafilatura
    except ImportError:
        return None
    return trafilatura.extract(html, include_comments=False, include_tables=False)


async def fetch_body(
    client: httpx.AsyncClient,
    url: str,
    extract_fn: Callable[[str], str | None] = _trafilatura_extract,
) -> str | None:
    try:
        r = await client.get(url, timeout=TIMEOUT_S,
                             headers={"User-Agent": USER_AGENT},
                             follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return None
    body = extract_fn(r.text)
    if not body:
        return None
    return body.strip()[:MAX_BODY_CHARS]


async def fetch_bodies_for_items(
    conn: sqlite3.Connection,
    item_ids: list[str],
    extract_fn: Callable[[str], str | None] = _trafilatura_extract,
) -> dict:
    """Fetch + cache bodies for items not already in the DB. Returns counts."""
    cur = conn.cursor()
    rows = cur.execute(
        f"SELECT id, url, body FROM items WHERE id IN ({','.join('?' * len(item_ids))})",
        item_ids,
    ).fetchall() if item_ids else []

    cached = sum(1 for _, _, body in rows if body)
    to_fetch = [(id_, url) for id_, url, body in rows if not body]
    fetched = 0

    if to_fetch:
        sem = asyncio.Semaphore(PARALLEL)

        async def one(client, id_, url):
            async with sem:
                return id_, await fetch_body(client, url, extract_fn)

        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(*[one(client, i, u) for i, u in to_fetch])

        now = datetime.now(timezone.utc).isoformat()
        for id_, body in results:
            if body:
                cur.execute(
                    "UPDATE items SET body = ?, body_fetched_at = ? WHERE id = ?",
                    (body, now, id_),
                )
                fetched += 1
        conn.commit()

    return {"requested": len(item_ids), "cached": cached, "fetched": fetched,
            "failed": len(to_fetch) - fetched}


def get_bodies(conn: sqlite3.Connection, item_ids: list[str]) -> dict[str, str]:
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT id, body FROM items WHERE id IN ({','.join('?' * len(item_ids))}) "
        "AND body IS NOT NULL",
        item_ids,
    ).fetchall()
    return {id_: body for id_, body in rows}
