"""Train the ranker on accumulated impressions + reactions.

Joining strategy: impressions written by rank.py have item_id but their
section is "learning"/"news". Reactions written by the worker have human
section names like "1. LLM Memory". The bridge is `daily/digest-manifest-*.json`,
emitted by the Claude agent when it writes the digest:

    {"date": "2026-05-05",
     "items": [{"section": "1. LLM Memory", "item_id": "abc..."}, ...]}

So the join is: reaction.section → manifest item_id → impression row → features.

If <MIN_LABELS labeled rows are available, training is skipped — rank.py
keeps using the cold-start scorer until enough data accumulates.
"""
from __future__ import annotations

import json
import pickle
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import db, embed, features, rank

MODEL_PATH = db.REPO_ROOT / "data" / "ranker.pkl"
MIN_LABELS = 50

POSITIVE_REACTIONS = {"more_like_this", "go_deeper", "opened"}
NEGATIVE_REACTIONS = {"not_interested", "too_basic"}


def load_manifests(daily_dir: Path) -> dict[str, dict[str, str]]:
    """Returns {date: {section_title: item_id}}."""
    out: dict[str, dict[str, str]] = {}
    for p in sorted(daily_dir.glob("digest-manifest-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        date = data.get("date")
        if not date:
            continue
        out[date] = {x["section"]: x["item_id"] for x in data.get("items", [])
                     if x.get("section") and x.get("item_id")}
    return out


def build_dataset(conn, manifests: dict[str, dict[str, str]]) -> list[dict]:
    """One row per (impression, reaction-or-implicit-negative). Returns list of
    dicts ready for vectorization."""
    impressions = conn.execute(
        "SELECT id, item_id, digest_date, section, position, score FROM impressions "
        "WHERE item_id IS NOT NULL"
    ).fetchall()
    items = {r[0]: r for r in conn.execute(
        "SELECT id, source, category, title, summary, published_at, embedding FROM items"
    ).fetchall()}

    # Reactions joined by date+section_title; map back to item_id via manifest.
    raw_reactions = conn.execute(
        "SELECT i.digest_date, i.section, r.reaction "
        "FROM reactions r JOIN impressions i ON i.id = r.impression_id"
    ).fetchall()
    item_reactions: dict[tuple[str, str], list[str]] = defaultdict(list)  # (date, item_id)
    for date, section, reaction in raw_reactions:
        if section == "__digest_open__":
            # Open = weak positive for every item in the manifest that day.
            for item_id in manifests.get(date, {}).values():
                item_reactions[(date, item_id)].append("opened")
        else:
            item_id = manifests.get(date, {}).get(section)
            if item_id:
                item_reactions[(date, item_id)].append(reaction)

    rows = []
    for imp_id, item_id, date, _section, _pos, _score in impressions:
        item = items.get(item_id)
        if not item or item[6] is None:  # no embedding
            continue
        reacts = item_reactions.get((date, item_id), [])
        label = _label(reacts)
        if label is None:
            continue
        rows.append({
            "imp_id": imp_id, "item_id": item_id, "date": date,
            "source": item[1], "category": item[2], "title": item[3],
            "embedding": embed._from_blob(item[6]),
            "published_at": item[5],
            "label": label,
        })
    return rows


def _label(reactions: list[str]) -> int | None:
    if any(r in NEGATIVE_REACTIONS for r in reactions):
        return 0
    if any(r in POSITIVE_REACTIONS for r in reactions):
        return 1
    return None  # neutral / no signal — drop


def fit(rows: list[dict], interests: list[str]) -> dict:
    if len(rows) < MIN_LABELS:
        return {"trained": False, "rows": len(rows), "reason": f"need ≥{MIN_LABELS} labels"}
    if not interests:
        return {"trained": False, "rows": len(rows), "reason": "no interests"}

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    interest_embs = embed.encode(interests)
    stats = features.build_stats(rows)
    X = np.vstack([
        features.vectorize(
            r["embedding"], interest_embs,
            rank._hours_since(r.get("published_at", "")),
            r["source"], r["category"], stats,
        ) for r in rows
    ])
    y = np.array([r["label"] for r in rows], dtype=int)

    model = LogisticRegression(max_iter=200, class_weight="balanced")
    model.fit(X, y)
    auc = float(roc_auc_score(y, model.predict_proba(X)[:, 1])) if len(set(y)) > 1 else None

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MODEL_PATH.open("wb") as f:
        pickle.dump({
            "model": model,
            "stats": stats,
            "interests": interests,
            "feature_dim": features.FEATURE_DIM,
        }, f)
    try:
        path_str = str(MODEL_PATH.relative_to(db.REPO_ROOT))
    except ValueError:
        path_str = str(MODEL_PATH)
    return {"trained": True, "rows": len(rows), "positives": int(y.sum()),
            "train_auc": auc, "path": path_str}


def run() -> dict:
    conn = db.connect()
    db.init_schema(conn)
    manifests = load_manifests(db.REPO_ROOT / "daily")
    rows = build_dataset(conn, manifests)
    interests = rank.parse_interests()
    summary = fit(rows, interests)
    conn.close()
    return summary


def load_model() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    with MODEL_PATH.open("rb") as f:
        return pickle.load(f)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
