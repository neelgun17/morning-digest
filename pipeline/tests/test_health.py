import json
import pytest

from pipeline import health


def _stage2(reranked, skipped, available=True):
    return {"available": available, "reranked": reranked, "rerank_skipped": skipped}


def test_record_computes_rerank_success_ratio():
    summary = {"learning": 3, "news": 4, "model": "haiku-rerank-v1",
               "stage2": _stage2(6, 2)}
    rec = health.record(summary, today="2026-08-18")
    assert rec["rerank_success_ratio"] == pytest.approx(0.75)
    assert rec["learning"] == 3 and rec["news"] == 4
    assert rec["date"] == "2026-08-18"


def test_check_flags_rerank_below_floor():
    rec = health.record({"learning": 3, "news": 4, "stage2": _stage2(2, 8)}, today="2026-08-18")
    problems = health.check(rec)
    assert any("rerank" in p.lower() for p in problems)


def test_check_does_not_flag_rerank_when_stage2_unavailable():
    # A correctly-disabled LLM (no API key set) is healthy, not degraded —
    # this is the distinction the plan calls out explicitly.
    rec = health.record({"learning": 3, "news": 4,
                          "stage2": {"available": False}}, today="2026-08-18")
    problems = health.check(rec)
    assert not any("rerank" in p.lower() for p in problems)


def test_check_flags_empty_learning_and_news():
    rec = health.record({"learning": 0, "news": 0, "stage2": _stage2(6, 0)}, today="2026-08-18")
    problems = health.check(rec)
    assert any("learning" in p.lower() for p in problems)
    assert any("news" in p.lower() for p in problems)


def test_check_passes_healthy_record_cleanly():
    rec = health.record({"learning": 3, "news": 5, "stage2": _stage2(8, 1)}, today="2026-08-18")
    assert health.check(rec) == []


def test_persist_and_latest_roundtrip(tmp_path):
    health_dir = tmp_path / "health"
    rec1 = health.record({"learning": 2, "news": 3, "stage2": _stage2(5, 0)}, today="2026-08-17")
    rec2 = health.record({"learning": 1, "news": 1, "stage2": _stage2(1, 9)}, today="2026-08-18")
    health.persist(rec1, health_dir)
    health.persist(rec2, health_dir)

    latest_rec = health.latest(health_dir)
    assert latest_rec["date"] == "2026-08-18"


def test_latest_finds_most_recent_across_month_shards(tmp_path):
    health_dir = tmp_path / "health"
    rec_july = health.record({"learning": 1, "news": 1, "stage2": _stage2(1, 0)}, today="2026-07-30")
    rec_aug = health.record({"learning": 1, "news": 1, "stage2": _stage2(1, 0)}, today="2026-08-01")
    health.persist(rec_july, health_dir)
    health.persist(rec_aug, health_dir)

    latest_rec = health.latest(health_dir)
    assert latest_rec["date"] == "2026-08-01"


def test_cli_check_exits_1_on_problems(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path / "health")
    rec = health.record({"learning": 0, "news": 3, "stage2": _stage2(1, 9)}, today="2026-08-18")
    health.persist(rec)

    exit_code = health.main(["--check"])
    assert exit_code == 1
    assert "learning" in capsys.readouterr().out.lower()


def test_cli_check_exits_0_when_healthy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path / "health")
    rec = health.record({"learning": 3, "news": 4, "stage2": _stage2(8, 1)}, today="2026-08-18")
    health.persist(rec)

    exit_code = health.main(["--check"])
    assert exit_code == 0


def test_latest_falls_back_to_an_older_shard_when_the_newest_is_empty(tmp_path):
    """`latest()` must not report "no records" just because the newest month's
    shard exists but happens to be empty — the CLI exits 1 on a missing record,
    so an empty newest shard would fail the pipeline for the wrong reason."""
    (tmp_path / "2026-07.jsonl").write_text(
        json.dumps({"date": "2026-07-31", "learning": 3, "news": 3}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "2026-08.jsonl").write_text("", encoding="utf-8")

    rec = health.latest(tmp_path)
    assert rec is not None, "an empty newest shard must not hide older records"
    assert rec["date"] == "2026-07-31"
