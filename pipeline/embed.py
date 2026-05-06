"""Embed items with sentence-transformers MiniLM and dedupe near-duplicates.

The encoder is loaded lazily so unit tests can inject a stub via `set_encoder`.
Embeddings are float32[384], stored as a BLOB on the items row. Dedupe is a
simple pairwise cosine threshold over recent items — for the volume morning-digest
runs at (~200 items/day), a full O(N²) pass is fine and saves the MinHash dep.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Iterable

import numpy as np

EMBED_DIM = 384
DEDUPE_THRESHOLD = 0.92

_encoder: Callable[[list[str]], np.ndarray] | None = None


def set_encoder(fn: Callable[[list[str]], np.ndarray]) -> None:
    """Inject an encoder. Tests use this; production calls _default_encoder."""
    global _encoder
    _encoder = fn


def _default_encoder() -> Callable[[list[str]], np.ndarray]:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    def encode(texts: list[str]) -> np.ndarray:
        return np.asarray(model.encode(texts, normalize_embeddings=True), dtype=np.float32)

    return encode


def encode(texts: list[str]) -> np.ndarray:
    global _encoder
    if _encoder is None:
        _encoder = _default_encoder()
    return _encoder(texts)


def _to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def embed_unembedded(conn: sqlite3.Connection, batch_size: int = 64) -> int:
    rows = conn.execute(
        "SELECT id, title, summary FROM items WHERE embedding IS NULL"
    ).fetchall()
    if not rows:
        return 0
    texts = [f"{title}. {summary}".strip() for _, title, summary in rows]
    vecs = encode(texts)
    cur = conn.cursor()
    for (id_, _, _), vec in zip(rows, vecs):
        cur.execute("UPDATE items SET embedding = ? WHERE id = ?", (_to_blob(vec), id_))
    conn.commit()
    return len(rows)


def dedupe_recent(conn: sqlite3.Connection, days: int = 30) -> int:
    """Remove near-duplicate items from the last `days` of fetches.

    Keeps the earliest-fetched item in each cluster. Returns the number deleted.
    """
    rows = conn.execute(
        "SELECT id, embedding, fetched_at FROM items "
        "WHERE embedding IS NOT NULL "
        "AND fetched_at >= datetime('now', ?) "
        "ORDER BY fetched_at ASC",
        (f"-{days} days",),
    ).fetchall()
    if len(rows) < 2:
        return 0

    keep_ids: list[str] = []
    keep_vecs: list[np.ndarray] = []
    drop: list[str] = []
    for id_, blob, _ in rows:
        v = _from_blob(blob)
        if keep_vecs:
            sims = np.dot(np.vstack(keep_vecs), v)
            if sims.max() >= DEDUPE_THRESHOLD:
                drop.append(id_)
                continue
        keep_ids.append(id_)
        keep_vecs.append(v)

    if drop:
        conn.executemany("DELETE FROM items WHERE id = ?", [(d,) for d in drop])
        conn.commit()
    return len(drop)


def run(conn: sqlite3.Connection) -> dict:
    embedded = embed_unembedded(conn)
    deduped = dedupe_recent(conn)
    return {"embedded": embedded, "deduped": deduped}


def cosine_matrix(query: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    """Cosine sim assuming both inputs are L2-normalized (MiniLM output is)."""
    return candidates @ query.T
