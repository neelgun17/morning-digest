import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from pipeline import db, extract


def test_fetch_body_returns_extracted_text(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, text="<html><body><article>The main thing.</article></body></html>",
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    extracted = []

    def stub_extract(html):
        extracted.append(html)
        return "The main thing."

    async def go():
        async with httpx.AsyncClient() as c:
            return await extract.fetch_body(c, "https://x.test/a", extract_fn=stub_extract)
    body = asyncio.run(go())
    assert body == "The main thing."
    assert "main thing" in extracted[0]


def test_fetch_body_returns_none_on_http_error(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(500, text="boom", request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async def go():
        async with httpx.AsyncClient() as c:
            return await extract.fetch_body(c, "https://x.test/a", extract_fn=lambda h: "x")
    assert asyncio.run(go()) is None


def test_fetch_body_returns_none_when_extractor_returns_nothing(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, text="<html></html>",
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async def go():
        async with httpx.AsyncClient() as c:
            return await extract.fetch_body(c, "https://x.test/a", extract_fn=lambda h: None)
    assert asyncio.run(go()) is None


def test_fetch_bodies_for_items_caches_and_writes(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "d.db")
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO items (id, url, fetched_at) VALUES (?, ?, ?)",
                 ("a", "https://x.test/a", now))
    conn.execute("INSERT INTO items (id, url, fetched_at, body) VALUES (?, ?, ?, ?)",
                 ("b", "https://x.test/b", now, "already cached body"))
    conn.commit()

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, text="<html><body>fresh fetch for " + url + "</body></html>",
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    counts, bodies = asyncio.run(extract.fetch_bodies_for_items(
        conn, ["a", "b"], extract_fn=lambda h: h[:30]))
    assert counts == {"requested": 2, "cached": 1, "fetched": 1, "failed": 0}
    assert bodies["b"] == "already cached body"
    assert "fresh fetch" in bodies["a"]

    # get_bodies (still used elsewhere) returns the same view from disk
    persisted = extract.get_bodies(conn, ["a", "b"])
    assert persisted["b"] == "already cached body"
    assert "fresh fetch" in persisted["a"]


def test_get_bodies_skips_items_without_body(tmp_path):
    conn = db.connect(tmp_path / "d.db")
    db.init_schema(conn)
    conn.execute("INSERT INTO items (id, url, fetched_at) VALUES (?, ?, ?)", ("a", "u", "t"))
    conn.commit()
    assert extract.get_bodies(conn, ["a"]) == {}
