from datetime import datetime, timezone
from pathlib import Path

from pipeline import db


def test_shard_path_derives_month_from_date():
    p = db.shard_path("2026-08-18", shard_dir=Path("data/events"))
    assert p == Path("data/events/2026-08.jsonl")


def test_shard_path_falls_back_to_current_month_for_malformed_date():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    p = db.shard_path("not-a-date", shard_dir=Path("data/events"), now=now)
    assert p == Path("data/events/2026-08.jsonl")

    p2 = db.shard_path(None, shard_dir=Path("data/events"), now=now)
    assert p2 == Path("data/events/2026-08.jsonl")


def test_shard_path_works_for_any_shard_dir_not_just_events():
    # health.py reuses this for data/health/ — the reason the rule lives in
    # db.py rather than in the event-ingestion module.
    p = db.shard_path("2026-08-18", shard_dir=Path("data/health"))
    assert p == Path("data/health/2026-08.jsonl")


def test_shard_path_defaults_to_the_events_dir():
    assert db.shard_path("2026-08-18").parent == db.EVENTS_DIR
