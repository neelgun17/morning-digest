import json
from pathlib import Path

import numpy as np
import pytest

from pipeline import embed, eval as ev


def _stub_encoder():
    vocab = ["llm", "ai", "philosophy", "stoicism", "running", "finance"]

    def encode(texts):
        out = np.zeros((len(texts), len(vocab)), dtype=np.float32)
        for i, t in enumerate(texts):
            for j, w in enumerate(vocab):
                if w in t.lower():
                    out[i, j] = 1.0
            n = np.linalg.norm(out[i])
            if n > 0:
                out[i] /= n
        return out
    return encode


def test_ndcg_perfect_ranking_is_one():
    assert ev.ndcg_at_k([1, 1, 0, 0, 0], 5) == pytest.approx(1.0)


def test_ndcg_inverted_ranking_is_low():
    perfect = ev.ndcg_at_k([1, 1, 0, 0, 0], 5)
    inverted = ev.ndcg_at_k([0, 0, 0, 1, 1], 5)
    assert inverted < perfect


def test_recall_at_k():
    assert ev.recall_at_k([1, 0, 0, 1, 0, 1], 5) == pytest.approx(2 / 3)
    assert ev.recall_at_k([0, 0, 0, 0], 5) == 0.0


def test_entropy_increases_with_diversity():
    low = ev.entropy(["a", "a", "a", "a"])
    high = ev.entropy(["a", "b", "c", "d"])
    assert low == 0.0 and high > 1.5


def test_lodo_short_circuits_with_too_few_days():
    out = ev.leave_one_day_out([], interests=["x"])
    assert out["ok"] is False
    assert "≥3 days" in out["reason"]


def test_lodo_runs_when_enough_days(tmp_path):
    embed.set_encoder(_stub_encoder())
    interests = ["LLM AI systems", "stoicism philosophy"]
    rows = []
    # 4 days × 6 items each: half are clearly on-topic+positive, half off-topic+negative.
    for d in range(4):
        for i in range(6):
            on = i < 3
            title = "LLM systems" if on else "running shoes"
            rows.append({
                "imp_id": d * 10 + i,
                "item_id": f"{d}-{i}",
                "date": f"2026-05-0{d+1}",
                "source": "S1" if on else "S2",
                "category": "ai" if on else "tech",
                "title": title,
                "embedding": embed.encode([title])[0],
                "published_at": "",
                "label": 1 if on else 0,
            })
    out = ev.leave_one_day_out(rows, interests)
    assert out["ok"] is True
    assert 0.0 <= out["macro"]["ndcg_at_5"] <= 1.0
    assert out["macro"]["ndcg_at_5"] > 0.5  # signal is strong → should rank well


def test_check_against_baseline_flags_regression(tmp_path):
    base = {"ok": True, "macro": {"ndcg_at_5": 0.80}}
    cur_pass = {"ok": True, "macro": {"ndcg_at_5": 0.74}}   # 7.5% drop → ok
    cur_fail = {"ok": True, "macro": {"ndcg_at_5": 0.65}}   # 18.75% drop → fail

    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(base))

    ok, _ = ev.check_against_baseline(cur_pass, p)
    assert ok is True

    ok, msg = ev.check_against_baseline(cur_fail, p)
    assert ok is False and "dropped" in msg


def test_check_against_baseline_accepts_when_no_baseline(tmp_path):
    ok, msg = ev.check_against_baseline(
        {"ok": True, "macro": {"ndcg_at_5": 0.5}},
        baseline_path=tmp_path / "missing.json",
    )
    assert ok is True
    assert "no baseline" in msg
