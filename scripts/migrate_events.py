"""One-time migration: split the legacy data/events.jsonl into monthly shards
under data/events/ (see pipeline/db.py's shard_path).

Why this exists: the GitHub Contents API only returns inline base64 content up
to 1MB — past that it returns encoding: "none" with an EMPTY content field but
a *valid* sha. Unguarded, the worker's append logic decoded that to "" ("empty
file") and overwrote the whole history with one line. events.jsonl was on pace
to cross that limit in ~8 months. Sharding by month keeps every file ~10x
under the limit permanently (see worker/index.js's encodingGuardError for the
belt-and-suspenders guard on top of this).

This script only ever WRITES the split shards and DELETES the original after
verifying — by re-reading the shards back off disk — that the total line count
matches exactly. If that verification fails, the original is left untouched
and the script errors out. 1,500 events here represent months of irreplaceable
training signal; the whole point of this file is to not lose any of it.

Usage:
    python3 -m scripts.migrate_events
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from pipeline import db


class MigrationError(Exception):
    """A migration refused to proceed, or its own safety check failed.

    A plain exception rather than SystemExit: migrate() is a library function
    that tests and other callers import, and SystemExit bypasses ordinary
    exception handling. __main__ below translates it to an exit code.
    """


def _read_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _split_lines(lines: list[str], events_dir: Path,
                  now: datetime | None = None) -> dict[Path, list[str]]:
    """Group lines by the month shard they belong to. A line that isn't valid
    JSON (or has no `date`) still gets a home — shard_path's own fallback to
    the current month — nothing is dropped on the floor."""
    shards: dict[Path, list[str]] = {}
    for line in lines:
        date = None
        try:
            date = json.loads(line).get("date")
        except json.JSONDecodeError:
            date = None
        path = db.shard_path(date, events_dir, now=now)
        shards.setdefault(path, []).append(line)
    return shards


def _count_shard_lines(shards: dict[Path, list[str]]) -> int:
    """Re-read each shard from disk rather than trusting the in-memory split,
    so the safety check reflects what was actually written."""
    total = 0
    for path in shards:
        if path.exists():
            total += len(_read_lines(path))
    return total


def migrate(events_path: Path = db.EVENTS_PATH, events_dir: Path = db.EVENTS_DIR,
            now: datetime | None = None) -> dict:
    if not events_path.exists():
        print(f"no legacy file at {events_path}; nothing to migrate")
        return {"migrated": 0, "shards": 0}

    if events_dir.exists() and any(events_dir.glob("*.jsonl")):
        raise MigrationError(
            f"{events_dir} already has shard files — refusing to run the migration "
            "against a non-empty target (could silently merge or clobber unrelated "
            "data). Investigate before re-running."
        )

    lines = _read_lines(events_path)
    shards = _split_lines(lines, events_dir, now=now)

    events_dir.mkdir(parents=True, exist_ok=True)
    for path, shard_lines in shards.items():
        path.write_text("\n".join(shard_lines) + "\n", encoding="utf-8")

    written = _count_shard_lines(shards)
    if written != len(lines):
        raise MigrationError(
            f"migration line-count mismatch: read {len(lines)} lines from "
            f"{events_path}, but shards on disk contain {written}. Leaving the "
            "original file untouched — investigate before retrying."
        )

    events_path.unlink()
    return {"migrated": len(lines), "shards": len(shards)}


if __name__ == "__main__":
    try:
        result = migrate()
    except MigrationError as e:
        print(f"migration aborted: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, indent=2))
