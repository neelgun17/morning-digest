import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pytest

from pipeline import db, embed, features, rank, train


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


def test_label_priorities():
    assert train._label(["more_like_this"]) == 1
    assert train._label(["opened"]) == 1
    assert train._label(["not_interested"]) == 0
    assert train._label(["too_basic"]) == 0
    # explicit negative wins over open
    assert train._label(["opened", "not_interested"]) == 0
    # nothing meaningful → drop
    assert train._label([]) is None
    assert train._label(["weird"]) is None


def test_build_stats_smoothing():
    rows = [
        {"source": "S1", "category": "ai", "label": 1},
        {"source": "S1", "category": "ai", "label": 1},
        {"source": "S2", "category": "finance", "label": 0},
    ]
    stats = features.build_stats(rows)
    assert stats.for_source("S1") > stats.for_source("S2")
    # unseen → falls back to neutral prior 0.5
    assert stats.for_source("Unknown") == pytest.approx(0.5)
    assert stats.for_category("unseen") == pytest.approx(0.5)


def test_vectorize_shape():
    embed.set_encoder(_stub_encoder())
    item = embed.encode(["LLM systems"])[0]
    interests = embed.encode(["LLM", "philosophy"])
    stats = features.build_stats([
        {"source": "S1", "category": "ai", "label": 1},
    ])
    v = features.vectorize(item, interests, age_hours=4.0,
                           source="S1", category="ai", stats=stats)
    assert v.shape == (features.FEATURE_DIM,)
    # category one-hot fires for ai
    ai_idx = 6 + features.CATEGORIES.index("ai")
    assert v[ai_idx] == 1.0


def _seed(conn, items, encode):
    now = datetime.now(timezone.utc)
    for it in items:
        conn.execute(
            "INSERT INTO items (id, url, source, category, title, summary, "
            "published_at, fetched_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (it["id"], it["url"], it["source"], it["category"], it["title"],
             "", (now - timedelta(hours=2)).isoformat(), now.isoformat(),
             embed._to_blob(encode([it["title"]])[0])),
        )
    conn.commit()


def test_fit_skips_when_too_few_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(train, "MODEL_PATH", tmp_path / "ranker.pkl")
    out = train.fit(rows=[{"label": 1}] * 10, interests=["x"])
    assert out["trained"] is False
    assert "≥50" in out["reason"]


def test_fit_trains_and_saves_model(tmp_path, monkeypatch):
    embed.set_encoder(_stub_encoder())
    monkeypatch.setattr(train, "MODEL_PATH", tmp_path / "ranker.pkl")

    interests = ["LLM AI systems", "stoicism philosophy"]
    interest_embs = embed.encode(interests)
    n = 60
    rows = []
    for i in range(n):
        on_topic = i % 2 == 0
        title = "LLM context" if on_topic else "running shoes"
        emb = embed.encode([title])[0]
        rows.append({
            "imp_id": i, "item_id": f"id{i}", "date": "2026-05-05",
            "source": "S1" if on_topic else "S2",
            "category": "ai" if on_topic else "tech",
            "title": title, "embedding": emb,
            "published_at": "", "label": 1 if on_topic else 0,
        })

    out = train.fit(rows, interests)
    assert out["trained"] is True
    assert (tmp_path / "ranker.pkl").exists()
    assert out["train_auc"] is None or out["train_auc"] > 0.7

    bundle = train.load_model()
    assert bundle is not None
    assert bundle["feature_dim"] == features.FEATURE_DIM
