"""Cold-start ranker: cosine-sim against interest paragraphs + recency boost.

Outputs daily/candidates-YYYY-MM-DD.json that the Claude Code agent reads
instead of fetching feeds itself. Also logs impression records to
data/events.jsonl so reactions can be joined to scored items in training.

Section split is intentionally simple: items in 'finance' and 'news' categories
go to the news block; everything else is eligible for learning. The agent picks
which 2-3 to actually feature.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import db, embed, extract, features, rerank

LEARNING_K = 8
NEWS_K = 10
STAGE1_TOP_K = 30                # oversample for stage 2 to re-order
RECENCY_HALF_LIFE_HOURS = 36.0
NEWS_CATEGORIES = {"news", "finance"}

# Anti-repetition knobs. The candidate pool is restricted to items the pipeline
# fetched within MAX_AGE_DAYS, and any item surfaced as a candidate within the
# last SEEN_EXCLUDE_DAYS is dropped so the same article can't recur day after
# day. Items the user explicitly marked "seen this" are excluded permanently.
# If these filters would starve the pool below MIN_POOL, they relax in stages
# (recency window first, then recent-impression exclusion) — but the explicit
# "seen this" exclusion is never relaxed.
SEEN_EXCLUDE_DAYS = 5
MAX_AGE_DAYS = 7
MIN_POOL = 24
LEARNING_PER_INTEREST = 2        # topic-diversity cap within the learning block


def parse_interests(path: Path | None = None) -> list[str]:
    """Pull each non-empty interest line/cell out of interests.md.

    Falls back to interests.example.md so a freshly-cloned template can dry-run.
    """
    if path is None:
        path = db.REPO_ROOT / "interests.md"
    if not path.exists():
        path = db.REPO_ROOT / "interests.example.md"
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)  # strip comments
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "*", "---")):
            continue
        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            # skip header/separator rows
            if not cells or all(set(c) <= {"-", " "} for c in cells):
                continue
            if cells[0].lower() in {"interest", "category", "preference"}:
                continue
            payload = " — ".join(c for c in cells if c)
            if "Example:" in payload:
                continue
            items.append(payload)
        elif line.startswith("- "):
            items.append(line[2:].strip())
    return items


def _hours_since(iso: str) -> float:
    if not iso:
        return 24.0  # unknown → neutral
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 48.0


def _recency_factor(hours: float) -> float:
    return math.pow(0.5, hours / RECENCY_HALF_LIFE_HOURS)


def _days_ago(today: str, days: int) -> str:
    """ISO date `days` before `today` (lexicographically comparable to ISO ts)."""
    return (date.fromisoformat(today) - timedelta(days=days)).isoformat()


def _fresh_item_ids(conn: sqlite3.Connection, today: str, days: int) -> set[str]:
    """Items the pipeline fetched within the last `days` (NULL fetched_at kept)."""
    cutoff = _days_ago(today, days)
    rows = conn.execute(
        "SELECT id FROM items WHERE fetched_at IS NULL OR fetched_at >= ?",
        (cutoff,),
    ).fetchall()
    return {r[0] for r in rows}


def _recent_impression_ids(conn: sqlite3.Connection, today: str, days: int) -> set[str]:
    """Item ids surfaced as candidates within the last `days` (any section)."""
    cutoff = _days_ago(today, days)
    rows = conn.execute(
        "SELECT DISTINCT item_id FROM impressions "
        "WHERE item_id IS NOT NULL AND digest_date >= ?",
        (cutoff,),
    ).fetchall()
    return {r[0] for r in rows}


def _seen_this_item_ids(conn: sqlite3.Connection) -> set[str]:
    """Item ids the user explicitly marked 'seen this'.

    Worker-written click reactions live on impressions keyed by (date, human
    section title) with item_id NULL, so we bridge back to item_id via the
    per-day digest manifests — the same join train.py uses for labels.
    """
    rows = conn.execute(
        "SELECT i.digest_date, i.section FROM reactions r "
        "JOIN impressions i ON i.id = r.impression_id "
        "WHERE r.reaction = 'seen_this'"
    ).fetchall()
    if not rows:
        return set()
    from . import train  # local import avoids the train<->rank import cycle
    manifests = train.load_manifests(db.REPO_ROOT / "daily")
    ids: set[str] = set()
    for digest_date, section in rows:
        item_id = manifests.get(digest_date, {}).get(section)
        if item_id:
            ids.add(item_id)
    return ids


def score_items(
    conn: sqlite3.Connection,
    interests: list[str],
    today: str,
    model_bundle: dict | None = None,
) -> list[dict]:
    all_rows = conn.execute(
        "SELECT id, url, source, category, title, summary, published_at, embedding "
        "FROM items WHERE embedding IS NOT NULL"
    ).fetchall()
    if not all_rows or not interests:
        return []

    # Anti-repetition filtering with graceful degradation. seen_this is always
    # excluded; the recency window and recent-impression exclusion relax in
    # stages if they would starve the pool below MIN_POOL.
    seen_this = _seen_this_item_ids(conn)
    recent = _recent_impression_ids(conn, today, SEEN_EXCLUDE_DAYS)
    fresh = _fresh_item_ids(conn, today, MAX_AGE_DAYS)

    def _keep(r, use_window: bool, use_recent: bool) -> bool:
        if r[0] in seen_this:
            return False
        if use_recent and r[0] in recent:
            return False
        if use_window and r[0] not in fresh:
            return False
        return True

    rows = [r for r in all_rows if _keep(r, True, True)]
    if len(rows) < MIN_POOL:
        rows = [r for r in all_rows if _keep(r, False, True)]   # drop recency window
    if len(rows) < MIN_POOL:
        rows = [r for r in all_rows if _keep(r, False, False)]  # drop recent-impression too
    if not rows:
        return []

    interest_vecs = embed.encode(interests)  # (I, D)
    cand_vecs = np.vstack([embed._from_blob(r[7]) for r in rows])  # (N, D)
    sims = cand_vecs @ interest_vecs.T  # (N, I)
    top_sim = sims.max(axis=1)
    top_interest = sims.argmax(axis=1)

    use_model = bool(model_bundle and model_bundle.get("model"))
    if use_model:
        stats: features.Stats = model_bundle["stats"]
        model = model_bundle["model"]
        feats = []
        for (_, _, source, category, _, _, published_at, _), vec in zip(rows, cand_vecs):
            feats.append(features.vectorize(
                vec, interest_vecs, _hours_since(published_at),
                source, category, stats,
            ))
        probs = model.predict_proba(np.vstack(feats))[:, 1]
    else:
        probs = None

    out = []
    for idx, ((id_, url, source, category, title, summary, published_at, _), s, i) in enumerate(
        zip(rows, top_sim, top_interest)
    ):
        hours = _hours_since(published_at)
        if probs is not None:
            score = float(probs[idx]) * _recency_factor(hours)
            generator = "trained-logreg"
        else:
            score = float(s) * _recency_factor(hours)
            generator = "cold-start-cosine"
        out.append({
            "id": id_,
            "url": url,
            "source": source,
            "category": category,
            "title": title,
            "summary": summary,
            "published_at": published_at,
            "score": round(score, 4),
            "sim": round(float(s), 4),
            "age_hours": round(hours, 1),
            "interest_idx": int(i),
            "reason": f"matches interest: {interests[i][:80]}",
            "_generator": generator,
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def split_sections(scored: list[dict]) -> dict:
    learning, news = [], []
    seen_sources_l: dict[str, int] = {}
    seen_sources_n: dict[str, int] = {}
    seen_interest_l: dict[int, int] = {}
    deferred: list[dict] = []  # learning items that hit the per-interest cap

    for it in scored:
        cat = (it["category"] or "").lower()
        if cat in NEWS_CATEGORIES:
            if seen_sources_n.get(it["source"], 0) >= 2:
                continue  # source diversity cap
            if len(news) >= NEWS_K:
                continue
            news.append(it)
            seen_sources_n[it["source"]] = seen_sources_n.get(it["source"], 0) + 1
            continue

        # Learning block: source cap + per-interest topic-diversity cap.
        if seen_sources_l.get(it["source"], 0) >= 2:
            continue
        if len(learning) >= LEARNING_K:
            continue
        iidx = it.get("interest_idx")
        if iidx is not None and seen_interest_l.get(iidx, 0) >= LEARNING_PER_INTEREST:
            deferred.append(it)  # over-represented topic — only used if list is thin
            continue
        learning.append(it)
        seen_sources_l[it["source"]] = seen_sources_l.get(it["source"], 0) + 1
        if iidx is not None:
            seen_interest_l[iidx] = seen_interest_l.get(iidx, 0) + 1

    # Top up the learning block from deferred items (still source-capped) if the
    # diversity cap left it under capacity.
    for it in deferred:
        if len(learning) >= LEARNING_K:
            break
        if seen_sources_l.get(it["source"], 0) >= 2:
            continue
        learning.append(it)
        seen_sources_l[it["source"]] = seen_sources_l.get(it["source"], 0) + 1

    return {"learning": learning, "news": news}


def log_impressions(events_path: Path, today: str, sections: dict) -> None:
    """Append one impression event per candidate to events.jsonl."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with events_path.open("a", encoding="utf-8") as f:
        for section_name, items in sections.items():
            for pos, it in enumerate(items):
                f.write(json.dumps({
                    "ts": ts,
                    "kind": "impression",
                    "date": today,
                    "section": section_name,
                    "position": pos,
                    "item_id": it["id"],
                    "score": it["score"],
                }, sort_keys=True) + "\n")


def write_candidates(out_path: Path, today: str, sections: dict,
                     generator: str = "cold-start-cosine-v1") -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": today,
        "generator": generator,
        "learning": sections["learning"],
        "news": sections["news"],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run(conn: sqlite3.Connection, today: str | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date().isoformat()
    interests = parse_interests()
    model_bundle = _try_load_model()

    # Stage 1 — score everything with the cheap ranker.
    scored = score_items(conn, interests, today, model_bundle=model_bundle)

    # Stage 2 — fetch bodies + Haiku rerank for the top STAGE1_TOP_K only.
    stage2_summary: dict = {"available": rerank.is_available()}
    if scored and rerank.is_available():
        coarse_top = scored[:STAGE1_TOP_K]
        fetch_counts, bodies = asyncio.run(
            extract.fetch_bodies_for_items(conn, [it["id"] for it in coarse_top])
        )
        reranked = asyncio.run(rerank.rerank(coarse_top, interests, bodies))
        stage2_summary.update(fetch_counts)
        stage2_summary["reranked"] = sum(1 for it in reranked if "rerank_score" in it)
        stage2_summary["rerank_skipped"] = sum(1 for it in reranked if it.get("rerank_failed"))
        scored = _apply_stage2(reranked) + scored[STAGE1_TOP_K:]
        scored.sort(key=lambda x: x["score"], reverse=True)

    sections = split_sections(scored)
    out_path = db.REPO_ROOT / "daily" / f"candidates-{today}.json"
    write_candidates(out_path, today, sections, generator=_generator_tag(scored))
    log_impressions(db.REPO_ROOT / "data" / "events.jsonl", today, sections)
    return {
        "interests": len(interests),
        "scored": len(scored),
        "learning": len(sections["learning"]),
        "news": len(sections["news"]),
        "model": _generator_tag(scored),
        "stage2": stage2_summary,
        "out": str(out_path.relative_to(db.REPO_ROOT)),
    }


def _apply_stage2(reranked: list[dict]) -> list[dict]:
    """Replace stage-1 score with rerank_score × recency; swap in LLM summary."""
    out = []
    for it in reranked:
        if "rerank_score" in it:
            score = (it["rerank_score"] / 10.0) * _recency_factor(it["age_hours"])
            out.append({**it,
                        "score": round(score, 4),
                        "summary": it["rerank_summary"],
                        "_generator": "haiku-rerank"})
        else:
            out.append(it)  # keep stage-1 fallback unchanged
    return out


def _try_load_model() -> dict | None:
    # Lazy import to avoid a circular import (train imports rank).
    try:
        from . import train
        return train.load_model()
    except Exception:
        return None


def _generator_tag(scored: list[dict]) -> str:
    if scored and scored[0].get("_generator") == "haiku-rerank":
        return "haiku-rerank-v1"
    if scored and scored[0].get("_generator") == "trained-logreg":
        return "trained-logreg-v1"
    return "cold-start-cosine-v1"
