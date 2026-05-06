from pathlib import Path

import pytest

from pipeline import db, judge


def test_parse_json_block_handles_fenced_response():
    text = "Sure, here you go:\n```json\n{\"a\": 1, \"b\": [2, 3]}\n```\nDone."
    assert judge._parse_json_block(text) == {"a": 1, "b": [2, 3]}


def test_parse_json_block_handles_bare_object():
    text = 'I think {"score": 4, "rationale": "fits well"} is the answer'
    assert judge._parse_json_block(text) == {"score": 4, "rationale": "fits well"}


def test_parse_sections_stops_at_feedback():
    md = (
        "# Daily Digest\n"
        "## Learning Block\n"
        "### 1. LLM Memory (12 min read)\n"
        "Body of section 1.\n"
        "### 2. Stoicism (8 min)\n"
        "Body of section 2.\n"
        "## News Block\n"
        "stuff\n"
        "## Feedback\n"
        "### 3. Should not appear\n"
    )
    sections = judge.parse_sections(md)
    titles = [s["section"] for s in sections]
    assert titles == ["LLM Memory", "Stoicism"]
    assert "Body of section 1" in sections[0]["body"]


def test_run_uses_injected_judge_fn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)
    (tmp_path / "interests.md").write_text("LLMs and philosophy\n", encoding="utf-8")
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-05-04.md").write_text(
        "# d1\n## Learning Block\n### 1. LLM Memory (12 min)\nbody\n## Feedback\n",
        encoding="utf-8",
    )
    (daily / "2026-05-05.md").write_text(
        "# d2\n## Learning Block\n### 1. Stoicism (10 min)\nbody\n## Feedback\n",
        encoding="utf-8",
    )

    calls = []

    def fake_judge(system, user, model):
        calls.append((system, user, model))
        return {
            "sections": [{"section": "LLM Memory", "score": 4,
                          "rationale": "directly relevant"}],
            "overall_score": 4,
        }

    summary = judge.run(n=2, judge_fn=fake_judge)
    assert summary["n_digests"] == 2
    assert summary["mean_overall"] == pytest.approx(4.0)
    assert len(calls) == 2
    # most-recent-first ordering
    assert summary["digests"][0]["digest_date"] == "2026-05-05"

    # report file written
    out = list((tmp_path / "eval").glob("judge-*.json"))
    assert len(out) == 1


def test_list_recent_digests_skips_non_date_files(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-05-05.md").write_text("x")
    (daily / "2026-05-04.md").write_text("x")
    (daily / "sample-digest.md").write_text("x")
    (daily / "candidates-2026-05-05.json").write_text("x")
    files = judge.list_recent_digests(daily, n=10)
    assert [p.name for p in files] == ["2026-05-05.md", "2026-05-04.md"]
