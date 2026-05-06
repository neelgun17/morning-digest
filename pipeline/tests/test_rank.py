import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

from pipeline import db, embed, rank


def _stub_encoder():
    """Tiny deterministic encoder: bag-of-words over a fixed vocab.

    Embedding dim = len(vocab). Used so tests don't pull sentence-transformers.
    """
    vocab = ["llm", "ai", "philosophy", "stoicism", "running", "finance", "market"]

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


def _seed_items(conn, items):
    encode = embed._encoder or _stub_encoder()
    embed.set_encoder(encode)
    now = datetime.now(timezone.utc)
    for it in items:
        published = (now - timedelta(hours=it.get("age_hours", 6))).isoformat()
        conn.execute(
            "INSERT INTO items (id, url, source, category, title, summary, "
            "published_at, fetched_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (it["id"], it["url"], it["source"], it["category"], it["title"],
             it.get("summary", ""), published, now.isoformat(),
             embed._to_blob(encode([f"{it['title']}. {it.get('summary','')}"])[0])),
        )
    conn.commit()


def test_parse_interests_strips_examples_and_comments(tmp_path, monkeypatch):
    md = tmp_path / "interests.md"
    md.write_text(
        "# Interests\n\n"
        "<!-- this is a comment -->\n"
        "## Professional\n"
        "| Interest | Priority | Depth |\n"
        "|----------|----------|-------|\n"
        "| LLM systems | high | intermediate |\n"
        "| Example: Rust | medium | beginner |\n"
        "\n## Preferences\n"
        "- Morning format: Reading with coffee\n",
        encoding="utf-8",
    )
    out = rank.parse_interests(md)
    assert any("LLM systems" in s for s in out)
    assert all("Example:" not in s for s in out)
    assert any("Morning format" in s for s in out)


def test_rank_orders_by_interest_match_and_recency(tmp_path, monkeypatch):
    embed.set_encoder(_stub_encoder())
    conn = db.connect(tmp_path / "d.db")
    db.init_schema(conn)

    _seed_items(conn, [
        {"id": "a", "url": "u/a", "source": "S1", "category": "ai",
         "title": "LLM context windows", "age_hours": 5},
        {"id": "b", "url": "u/b", "source": "S2", "category": "ai",
         "title": "running shoes review", "age_hours": 5},  # off-topic
        {"id": "c", "url": "u/c", "source": "S3", "category": "finance",
         "title": "market volatility today", "age_hours": 5},
        {"id": "d", "url": "u/d", "source": "S4", "category": "intellectual",
         "title": "Stoicism and philosophy", "age_hours": 200},  # stale
    ])

    interests = ["LLM and AI systems", "Stoicism philosophy", "finance markets"]
    scored = rank.score_items(conn, interests, today="2026-05-05")
    titles = [s["title"] for s in scored]
    # 'a' (recent + on-topic) beats 'd' (on-topic but old) and 'b' (off-topic)
    assert titles[0] == "LLM context windows"
    assert titles[-1] in {"running shoes review", "Stoicism and philosophy"}


def test_split_sections_separates_news_and_caps_diversity():
    items = [
        {"id": str(i), "url": f"u{i}", "source": "S1", "category": "finance",
         "title": f"f{i}", "summary": "", "published_at": "", "score": 1.0,
         "sim": 1.0, "age_hours": 1, "reason": ""}
        for i in range(5)
    ] + [
        {"id": "x", "url": "ux", "source": "S2", "category": "ai",
         "title": "x", "summary": "", "published_at": "", "score": 0.9,
         "sim": 0.9, "age_hours": 1, "reason": ""},
    ]
    sections = rank.split_sections(items)
    # source-diversity cap: at most 2 from S1 in news
    assert sum(1 for it in sections["news"] if it["source"] == "S1") <= 2
    assert any(it["category"] == "ai" for it in sections["learning"])


def test_run_writes_candidates_and_logs_impressions(tmp_path, monkeypatch):
    embed.set_encoder(_stub_encoder())
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "digest.db")
    monkeypatch.setattr(db, "EVENTS_PATH", tmp_path / "data" / "events.jsonl")

    (tmp_path / "interests.md").write_text(
        "## Tech\n- LLM and AI systems\n- finance markets\n", encoding="utf-8",
    )
    (tmp_path / "daily").mkdir()
    (tmp_path / "data").mkdir()

    conn = db.connect(db.DB_PATH)
    db.init_schema(conn)
    _seed_items(conn, [
        {"id": "a", "url": "u/a", "source": "S1", "category": "ai",
         "title": "LLM systems primer", "age_hours": 3},
        {"id": "b", "url": "u/b", "source": "S2", "category": "finance",
         "title": "market today", "age_hours": 3},
    ])

    summary = rank.run(conn, today="2026-05-05")
    assert summary["learning"] == 1 and summary["news"] == 1

    out = json.loads((tmp_path / "daily" / "candidates-2026-05-05.json").read_text())
    assert out["learning"][0]["id"] == "a"
    assert out["news"][0]["id"] == "b"

    log = (tmp_path / "data" / "events.jsonl").read_text().splitlines()
    kinds = [json.loads(l) for l in log]
    assert all(k["kind"] == "impression" for k in kinds)
    assert {k["item_id"] for k in kinds} == {"a", "b"}
