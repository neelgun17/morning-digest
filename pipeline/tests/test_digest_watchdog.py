"""The one silent-failure path left: the agent runs 4h after the pipeline's
health check, so nothing currently notices when it produces no digest at all
(the June 2026 outage ran five days) or produces one that isn't grounded in
the ranker (looks like success, but feedback can't join to items)."""
import json

import pytest

from pipeline import health

TODAY = "2026-08-19"


def write_digest(daily, date=TODAY):
    (daily / f"{date}.md").write_text("# Digest\n\n### 1. A Thing (10 min read)\n", encoding="utf-8")


def write_manifest(daily, items, date=TODAY):
    (daily / f"digest-manifest-{date}.json").write_text(
        json.dumps({"date": date, "items": items}), encoding="utf-8")


def test_flags_a_completely_missing_digest(tmp_path):
    problems = health.check_digest(tmp_path, TODAY)
    assert problems
    assert any("no digest" in p.lower() for p in problems)


def test_passes_a_digest_grounded_in_ranked_candidates(tmp_path):
    write_digest(tmp_path)
    write_manifest(tmp_path, [
        {"section": "1. A Thing", "item_id": "abc123"},
        {"section": "News", "item_id": None},
    ])
    assert health.check_digest(tmp_path, TODAY) == []


def test_flags_a_digest_with_no_manifest(tmp_path):
    write_digest(tmp_path)
    problems = health.check_digest(tmp_path, TODAY)
    assert any("manifest" in p.lower() for p in problems)


def test_flags_an_ungrounded_digest_where_every_learning_item_id_is_null(tmp_path):
    """Mode B: the agent improvised instead of using candidates. The email
    still sends and looks fine, but the training join is broken."""
    write_digest(tmp_path)
    write_manifest(tmp_path, [
        {"section": "1. A Thing", "item_id": None},
        {"section": "2. Another", "item_id": None},
        {"section": "News", "item_id": None},
    ])
    problems = health.check_digest(tmp_path, TODAY)
    assert any("grounded" in p.lower() for p in problems)


def test_does_not_false_alarm_on_a_news_only_catch_up_digest(tmp_path):
    """15+ days away means News-only by design — a null News item_id is
    specified behaviour, not an ungrounded digest."""
    write_digest(tmp_path)
    write_manifest(tmp_path, [{"section": "News", "item_id": None}])
    assert health.check_digest(tmp_path, TODAY) == []


def test_does_not_flag_when_only_some_item_ids_are_null(tmp_path):
    write_digest(tmp_path)
    write_manifest(tmp_path, [
        {"section": "1. A Thing", "item_id": "abc"},
        {"section": "2. Another", "item_id": None},
        {"section": "News", "item_id": None},
    ])
    assert health.check_digest(tmp_path, TODAY) == []


def test_flags_a_malformed_manifest_rather_than_crashing(tmp_path):
    write_digest(tmp_path)
    (tmp_path / f"digest-manifest-{TODAY}.json").write_text("{not json", encoding="utf-8")
    problems = health.check_digest(tmp_path, TODAY)
    assert any("manifest" in p.lower() for p in problems)


def test_cli_check_digest_exits_1_when_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, "DAILY_DIR", tmp_path)
    assert health.main(["--check-digest", "--date", TODAY]) == 1


def test_cli_check_digest_exits_0_when_present_and_grounded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, "DAILY_DIR", tmp_path)
    write_digest(tmp_path)
    write_manifest(tmp_path, [{"section": "1. A Thing", "item_id": "abc"}])
    assert health.main(["--check-digest", "--date", TODAY]) == 0
