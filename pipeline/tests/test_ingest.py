import json
from pathlib import Path

from pipeline import db, ingest_events
from pipeline.backfill_markdown import parse, merge


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_ingest_creates_impression_and_reaction(tmp_path):
    events_path = tmp_path / "events.jsonl"
    db_path = tmp_path / "digest.db"
    write_jsonl(events_path, [
        {"ts": "2026-04-09T07:00:00Z", "kind": "click",
         "date": "2026-04-09", "section": "1. LLM Memory", "reaction": "more_like_this"},
    ])

    counts = ingest_events.ingest(events_path, db_path)
    assert counts["events"] == 1
    assert counts["impressions"] == 1
    assert counts["reactions"] == 1

    conn = db.connect(db_path)
    rows = conn.execute(
        "SELECT i.digest_date, i.section, r.reaction "
        "FROM impressions i JOIN reactions r ON r.impression_id = i.id"
    ).fetchall()
    assert rows == [("2026-04-09", "1. LLM Memory", "more_like_this")]


def test_ingest_open_event_uses_synthetic_section(tmp_path):
    events_path = tmp_path / "events.jsonl"
    db_path = tmp_path / "digest.db"
    write_jsonl(events_path, [
        {"ts": "2026-04-09T08:00:00Z", "kind": "open", "date": "2026-04-09"},
    ])

    ingest_events.ingest(events_path, db_path)
    conn = db.connect(db_path)
    rows = conn.execute(
        "SELECT i.section, r.reaction FROM impressions i "
        "JOIN reactions r ON r.impression_id = i.id"
    ).fetchall()
    assert rows == [(ingest_events.OPEN_SECTION, "opened")]


def test_ingest_dedupes_repeated_events(tmp_path):
    events_path = tmp_path / "events.jsonl"
    db_path = tmp_path / "digest.db"
    ev = {"ts": "2026-04-09T07:00:00Z", "kind": "click",
          "date": "2026-04-09", "section": "1. X", "reaction": "more_like_this"}
    write_jsonl(events_path, [ev, ev, ev])

    counts = ingest_events.ingest(events_path, db_path)
    assert counts["events"] == 3
    assert counts["impressions"] == 1
    assert counts["reactions"] == 1  # deduped via UNIQUE constraint


def test_backfill_parses_and_merges(tmp_path):
    md = tmp_path / "feedback-log.md"
    md.write_text(
        "# Feedback Log\n\n"
        "## 2026-04-09 (email)\n"
        "- **Read**: email opened\n"
        "- **1. LLM Memory**: more_like_this\n"
        "- **Freeform**: \"some note\"\n"   # ignored — not a known reaction
        "\n## 2026-04-08 (email)\n"
        "- **2. Stoicism**: go_deeper\n",
        encoding="utf-8",
    )
    events = parse(md)
    kinds = sorted((e["kind"], e.get("section"), e.get("reaction")) for e in events)
    assert kinds == [
        ("click", "1. LLM Memory", "more_like_this"),
        ("click", "2. Stoicism", "go_deeper"),
        ("open", None, None),
    ]

    out = tmp_path / "events.jsonl"
    n1 = merge(events, out)
    n2 = merge(events, out)  # idempotent
    assert n1 == 3 and n2 == 0
