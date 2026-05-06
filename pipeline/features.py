"""Feature engineering for the trained ranker.

Each (item, user-context) pair becomes a fixed-length feature vector. Kept
deliberately small so a ~few-hundred-row training set is enough to fit a
LogisticRegression without overfitting.

Features:
  0  max cosine sim across all interest paragraphs
  1  mean cosine sim
  2  std  cosine sim
  3  log1p(age in hours)
  4  smoothed CTR for the item's source     (Beta(α=1, β=1) prior)
  5  smoothed CTR for the item's category
  6  one-hot: category in {ai, tech, finance, news, intellectual} (5 cols)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CATEGORIES = ["ai", "tech", "finance", "news", "intellectual"]
FEATURE_DIM = 6 + len(CATEGORIES)
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0


@dataclass
class Stats:
    """Smoothed CTR per source and per category, computed from training data."""
    source: dict[str, float]
    category: dict[str, float]

    def for_source(self, name: str) -> float:
        return self.source.get(name, _smoothed(0, 0))

    def for_category(self, name: str) -> float:
        return self.category.get((name or "").lower(), _smoothed(0, 0))


def _smoothed(positives: int, total: int) -> float:
    return (positives + PRIOR_ALPHA) / (total + PRIOR_ALPHA + PRIOR_BETA)


def build_stats(rows: list[dict]) -> Stats:
    """rows: [{source, category, label} ...] from the labeled training set."""
    src_pos: dict[str, int] = {}
    src_n:   dict[str, int] = {}
    cat_pos: dict[str, int] = {}
    cat_n:   dict[str, int] = {}
    for r in rows:
        s, c, y = r.get("source", ""), (r.get("category") or "").lower(), int(r["label"])
        src_n[s]   = src_n.get(s, 0) + 1
        src_pos[s] = src_pos.get(s, 0) + y
        cat_n[c]   = cat_n.get(c, 0) + 1
        cat_pos[c] = cat_pos.get(c, 0) + y
    return Stats(
        source={s: _smoothed(src_pos[s], src_n[s]) for s in src_n},
        category={c: _smoothed(cat_pos[c], cat_n[c]) for c in cat_n},
    )


def vectorize(
    item_emb: np.ndarray,
    interest_embs: np.ndarray,
    age_hours: float,
    source: str,
    category: str,
    stats: Stats,
) -> np.ndarray:
    sims = interest_embs @ item_emb  # (I,)
    cat_norm = (category or "").lower()
    onehot = [1.0 if cat_norm == c else 0.0 for c in CATEGORIES]
    return np.array([
        float(sims.max()) if len(sims) else 0.0,
        float(sims.mean()) if len(sims) else 0.0,
        float(sims.std()) if len(sims) else 0.0,
        float(np.log1p(max(age_hours, 0.0))),
        stats.for_source(source),
        stats.for_category(category),
        *onehot,
    ], dtype=np.float32)
