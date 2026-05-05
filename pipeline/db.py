"""SQLite schema and connection helper for the digest pipeline.

The DB is rebuilt from data/events.jsonl on every pipeline run, so this file
owns the schema definition. items rows are populated by fetch.py; impressions
are written by rank.py; reactions come from the worker via events.jsonl.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "digest.db"
EVENTS_PATH = REPO_ROOT / "data" / "events.jsonl"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    source       TEXT,
    category     TEXT,
    title        TEXT,
    summary      TEXT,
    published_at TEXT,
    fetched_at   TEXT,
    embedding    BLOB,
    cluster_id   INTEGER
);

CREATE TABLE IF NOT EXISTS impressions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT REFERENCES items(id),
    digest_date  TEXT NOT NULL,
    section      TEXT NOT NULL,
    position     INTEGER,
    score        REAL,
    exploration  INTEGER DEFAULT 0,
    UNIQUE(digest_date, section, item_id)
);

CREATE TABLE IF NOT EXISTS reactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    impression_id  INTEGER REFERENCES impressions(id),
    reaction       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    UNIQUE(impression_id, reaction, ts)
);

CREATE INDEX IF NOT EXISTS idx_impressions_date    ON impressions(digest_date);
CREATE INDEX IF NOT EXISTS idx_reactions_imp       ON reactions(impression_id);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Drop and recreate the DB. Used by ingest_events at pipeline start."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = connect(path)
    init_schema(conn)
    return conn
