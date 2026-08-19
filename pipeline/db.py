"""SQLite schema, on-disk paths, and connection helper for the digest pipeline.

The DB is rebuilt from the event log on every pipeline run, so this file owns
both the schema definition and every path the pipeline reads or writes. items
rows are populated by fetch.py; impressions are written by rank.py; reactions
come from the worker via the monthly event shards.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "digest.db"
EVENTS_PATH = REPO_ROOT / "data" / "events.jsonl"  # legacy, pre-sharding; still read
EVENTS_DIR = REPO_ROOT / "data" / "events"         # monthly shards, e.g. 2026-08.jsonl

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def shard_path(date_str: str | None, shard_dir: Path = EVENTS_DIR,
               now: datetime | None = None) -> Path:
    """Month shard for an ISO date, e.g. '2026-08-18' -> shard_dir/2026-08.jsonl.

    Lives here rather than in ingest_events because it's a path rule, not an
    ingestion concern — rank.py, health.py (for data/health/) and the migration
    script all need it, and all of them already import this module.

    A malformed or missing date falls back to the current month (UTC) so a bad
    event still lands somewhere findable instead of raising mid-pipeline.
    """
    if date_str and _DATE_RE.match(date_str):
        month = date_str[:7]
    else:
        month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    return shard_dir / f"{month}.jsonl"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    source          TEXT,
    category        TEXT,
    title           TEXT,
    summary         TEXT,
    published_at    TEXT,
    fetched_at      TEXT,
    embedding       BLOB,
    cluster_id      INTEGER,
    body            TEXT,
    body_fetched_at TEXT
);

-- impressions.item_id is intentionally NOT a FOREIGN KEY: an event-log
-- replay can reference items that were dedupe'd out or haven't been
-- re-fetched yet. The join is done lazily in train.py with a NULL check.
CREATE TABLE IF NOT EXISTS impressions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT,
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
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLEs for schema changes against existing DBs."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "body" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN body TEXT")
    if "body_fetched_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN body_fetched_at TEXT")


def reset(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Drop and recreate the DB. Used by ingest_events at pipeline start."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = connect(path)
    init_schema(conn)
    return conn
