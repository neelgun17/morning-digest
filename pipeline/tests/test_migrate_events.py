import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import ingest_events
from scripts import migrate_events


def write_legacy(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _events(n_per_month: dict[str, int]) -> list[dict]:
    out = []
    for month, n in n_per_month.items():
        for i in range(n):
            out.append({"ts": f"{month}-01T00:00:00Z", "kind": "open", "date": f"{month}-01"})
    return out


def test_migrate_splits_into_monthly_shards(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_dir = tmp_path / "events"
    events = _events({"2026-03": 2, "2026-04": 3, "2026-05": 1})
    write_legacy(events_path, events)

    result = migrate_events.migrate(events_path, events_dir)

    assert result == {"migrated": 6, "shards": 3}
    assert not events_path.exists(), "original must be removed only after verification"
    assert len((events_dir / "2026-03.jsonl").read_text().splitlines()) == 2
    assert len((events_dir / "2026-04.jsonl").read_text().splitlines()) == 3
    assert len((events_dir / "2026-05.jsonl").read_text().splitlines()) == 1


def test_migrate_falls_back_to_current_month_for_malformed_line(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_dir = tmp_path / "events"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps({"ts": "2026-04-09T07:00:00Z", "kind": "open", "date": "2026-04-09"}) + "\n"
        + "not valid json\n",
        encoding="utf-8",
    )
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)

    result = migrate_events.migrate(events_path, events_dir, now=now)

    assert result["migrated"] == 2
    assert (events_dir / "2026-04.jsonl").exists()
    assert (events_dir / "2026-08.jsonl").read_text().strip() == "not valid json"


def test_migrate_aborts_and_preserves_original_on_count_mismatch(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    events_dir = tmp_path / "events"
    write_legacy(events_path, _events({"2026-04": 5}))

    # Simulate a verification bug that would under-report what actually made
    # it to disk — the migration must refuse to delete the original when its
    # own safety check can't confirm the counts line up.
    monkeypatch.setattr(migrate_events, "_count_shard_lines", lambda shards: 4)

    with pytest.raises(migrate_events.MigrationError):
        migrate_events.migrate(events_path, events_dir)

    assert events_path.exists(), "original must survive a failed verification"


def test_migrate_refuses_non_empty_target_dir(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_dir = tmp_path / "events"
    write_legacy(events_path, _events({"2026-04": 1}))
    events_dir.mkdir(parents=True)
    (events_dir / "2026-04.jsonl").write_text('{"pre":"existing"}\n', encoding="utf-8")

    # A library function must not raise SystemExit — that's a CLI concern, and
    # it makes the failure uncatchable by any caller that isn't a script.
    with pytest.raises(migrate_events.MigrationError):
        migrate_events.migrate(events_path, events_dir)

    assert events_path.exists()


def test_migrate_then_ingest_preserves_event_count(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_dir = tmp_path / "events"
    events = _events({"2026-03": 4, "2026-04": 6, "2026-05": 3})
    write_legacy(events_path, events)

    migrate_events.migrate(events_path, events_dir)

    counts = ingest_events.ingest(events_path, tmp_path / "digest.db")
    assert counts["events"] == len(events)


def test_migration_errors_are_not_systemexit(tmp_path):
    """SystemExit would bypass normal exception handling in any caller."""
    events_path = tmp_path / "events.jsonl"
    events_dir = tmp_path / "events"
    write_legacy(events_path, _events({"2026-04": 1}))
    events_dir.mkdir(parents=True)
    (events_dir / "2026-04.jsonl").write_text('{"pre":"existing"}\n', encoding="utf-8")

    with pytest.raises(Exception) as exc:
        migrate_events.migrate(events_path, events_dir)
    assert not isinstance(exc.value, SystemExit)
