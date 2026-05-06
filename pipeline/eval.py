"""Offline eval harness for the ranker.

Two modes:
  --metrics    Leave-one-day-out CV → nDCG@5, recall@5, topic/source entropy.
  --llm-judge  Sample recent digests, ask Claude Haiku to score them against
               interests.md. (Implementation lives in pipeline/judge.py.)

Output goes to eval/metrics-YYYY-MM-DD.json (per-run) and eval/baseline.json
(latest-on-main result, used by CI as the regression bar).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import db, embed, features, rank, train

EVAL_DIR = db.REPO_ROOT / "eval"
BASELINE_PATH = EVAL_DIR / "baseline.json"
MIN_DAYS = 3
MIN_DAILY_ROWS = 4
DEFAULT_K = 5
REGRESSION_TOLERANCE = 0.10  # nDCG@5 may drop at most 10% vs baseline


# ---------- metrics primitives ----------

def dcg(relevances: list[float]) -> float:
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(relevances))


def ndcg_at_k(relevances_in_rank_order: list[float], k: int = DEFAULT_K) -> float:
    top = relevances_in_rank_order[:k]
    if not top:
        return 0.0
    ideal = sorted(relevances_in_rank_order, reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(top) / idcg if idcg > 0 else 0.0


def recall_at_k(relevances_in_rank_order: list[float], k: int = DEFAULT_K) -> float:
    total_positives = sum(1 for r in relevances_in_rank_order if r > 0)
    if total_positives == 0:
        return 0.0
    in_top = sum(1 for r in relevances_in_rank_order[:k] if r > 0)
    return in_top / total_positives


def entropy(values: list[str]) -> float:
    if not values:
        return 0.0
    counts = Counter(values)
    total = sum(counts.values())
    return -sum((c / total) * math.log2(c / total) for c in counts.values() if c > 0)


# ---------- leave-one-day-out ----------

def leave_one_day_out(rows: list[dict], interests: list[str]) -> dict:
    """For each held-out day, train on the other days then rank that day's items.

    Returns per-day metrics + macro means.
    """
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    days = [d for d, items in by_day.items() if len(items) >= MIN_DAILY_ROWS]
    if len(days) < MIN_DAYS:
        return {
            "ok": False,
            "reason": f"need ≥{MIN_DAYS} days with ≥{MIN_DAILY_ROWS} rows each "
                      f"(have {len(days)})",
            "days_seen": len(by_day),
        }

    interest_embs = embed.encode(interests)
    per_day = []
    for held in days:
        train_rows = [r for d in days if d != held for r in by_day[d]]
        test_rows = by_day[held]
        if not any(r["label"] == 1 for r in test_rows):
            continue  # nothing to recall

        from sklearn.linear_model import LogisticRegression
        stats = features.build_stats(train_rows)
        Xtr = np.vstack([
            features.vectorize(r["embedding"], interest_embs,
                               rank._hours_since(r.get("published_at", "")),
                               r["source"], r["category"], stats)
            for r in train_rows
        ])
        ytr = np.array([r["label"] for r in train_rows], dtype=int)
        if len(set(ytr)) < 2:
            continue  # need both classes to train
        model = LogisticRegression(max_iter=200, class_weight="balanced")
        model.fit(Xtr, ytr)

        Xte = np.vstack([
            features.vectorize(r["embedding"], interest_embs,
                               rank._hours_since(r.get("published_at", "")),
                               r["source"], r["category"], stats)
            for r in test_rows
        ])
        scores = model.predict_proba(Xte)[:, 1]
        order = np.argsort(-scores)
        ranked = [test_rows[i] for i in order]
        rels = [float(r["label"]) for r in ranked]

        per_day.append({
            "date": held,
            "n": len(test_rows),
            "positives": int(sum(rels)),
            "ndcg_at_5": ndcg_at_k(rels, 5),
            "recall_at_5": recall_at_k(rels, 5),
            "category_entropy_top5": entropy([r["category"] or "" for r in ranked[:5]]),
            "source_entropy_top5": entropy([r["source"] or "" for r in ranked[:5]]),
        })

    if not per_day:
        return {"ok": False, "reason": "no day produced a usable train/test split"}

    macro = {
        "ndcg_at_5": float(np.mean([d["ndcg_at_5"] for d in per_day])),
        "recall_at_5": float(np.mean([d["recall_at_5"] for d in per_day])),
        "category_entropy_top5": float(np.mean([d["category_entropy_top5"] for d in per_day])),
        "source_entropy_top5": float(np.mean([d["source_entropy_top5"] for d in per_day])),
    }
    return {"ok": True, "macro": macro, "per_day": per_day, "days_used": len(per_day)}


# ---------- orchestration ----------

def run_metrics(write: bool = True) -> dict:
    conn = db.connect()
    db.init_schema(conn)
    manifests = train.load_manifests(db.REPO_ROOT / "daily")
    rows = train.build_dataset(conn, manifests)
    interests = rank.parse_interests()
    result = leave_one_day_out(rows, interests)
    conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows_total": len(rows),
        "interests": len(interests),
        **result,
    }
    if write:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).date().isoformat()
        out = EVAL_DIR / f"metrics-{today}.json"
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {out.relative_to(db.REPO_ROOT)}")
    return payload


def check_against_baseline(current: dict, baseline_path: Path = BASELINE_PATH) -> tuple[bool, str]:
    if not baseline_path.exists():
        return True, "no baseline yet — accepting"
    if not current.get("ok"):
        return True, f"current eval not ok ({current.get('reason')}) — accepting"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not baseline.get("ok"):
        return True, "baseline not ok — accepting"
    base = baseline["macro"]["ndcg_at_5"]
    cur = current["macro"]["ndcg_at_5"]
    drop = (base - cur) / base if base > 0 else 0
    if drop > REGRESSION_TOLERANCE:
        return False, f"nDCG@5 dropped {drop*100:.1f}% (baseline {base:.3f}, current {cur:.3f})"
    return True, f"nDCG@5 ok (baseline {base:.3f}, current {cur:.3f}, drop {drop*100:.1f}%)"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--metrics", action="store_true")
    p.add_argument("--update-baseline", action="store_true",
                   help="overwrite eval/baseline.json with the current result")
    p.add_argument("--check", action="store_true",
                   help="exit non-zero if nDCG@5 regressed beyond tolerance")
    p.add_argument("--llm-judge", action="store_true",
                   help="score recent digests with Claude Haiku, write eval/judge-DATE.json")
    p.add_argument("--n", type=int, default=7,
                   help="how many recent digests to judge (default 7)")
    args = p.parse_args(argv)

    if not (args.metrics or args.check or args.update_baseline or args.llm_judge):
        p.error("specify at least one of --metrics, --check, --update-baseline, --llm-judge")

    if args.llm_judge:
        from . import judge
        summary = judge.run(n=args.n)
        print(json.dumps({k: v for k, v in summary.items() if k != "digests"}, indent=2))

    if args.metrics or args.check or args.update_baseline:
        result = run_metrics(write=args.metrics)
        print(json.dumps(result.get("macro", {}), indent=2))

        if args.update_baseline:
            EVAL_DIR.mkdir(parents=True, exist_ok=True)
            BASELINE_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(f"updated baseline: {BASELINE_PATH.relative_to(db.REPO_ROOT)}")

        if args.check:
            ok, msg = check_against_baseline(result)
            print(f"::notice::{msg}" if ok else f"::error::{msg}")
            return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
