"""Self-diagnosis for the daily pipeline.

rank.run() already computes a stage2_summary (reranked/rerank_skipped counts)
— until now it just got printed to a log nobody reads, which is the root
cause behind the whole reliability effort: the stage-2 reranker was ~85%
failing on Gemini 429s for months, the judge was erroring out, and a June
email outage ran five days, all silently. This module turns what rank.run()
already computes into a persisted record and a pass/fail check, wired into
pipeline.yml as the final step so a degraded run goes red in GitHub (and
GitHub emails by default) — no new service, just noticing what we compute.

Ordering in pipeline.yml is deliberate: candidates get written and committed
BEFORE this check runs, so a degraded day still produces a digest. Then the
health check can fail the run without blocking that digest from existing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db

RERANK_SUCCESS_FLOOR = 0.5
HEALTH_DIR = db.REPO_ROOT / "data" / "health"
DAILY_DIR = db.REPO_ROOT / "daily"


def record(summary: dict, today: str | None = None) -> dict:
    """Build a health record from rank.run()'s return value."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    stage2 = summary.get("stage2") or {}
    reranked = stage2.get("reranked")
    skipped = stage2.get("rerank_skipped")
    success_ratio = None
    if reranked is not None and skipped is not None and (reranked + skipped) > 0:
        success_ratio = reranked / (reranked + skipped)

    return {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "learning": summary.get("learning", 0),
        "news": summary.get("news", 0),
        "model": summary.get("model"),
        "stage2_available": bool(stage2.get("available")),
        "rerank_reranked": reranked,
        "rerank_skipped": skipped,
        "rerank_success_ratio": success_ratio,
    }


def check(rec: dict) -> list[str]:
    """Human-readable problems with a health record, or [] if healthy."""
    problems: list[str] = []

    # A correctly-disabled LLM (no API key configured) is healthy, not
    # degraded — only flag the rerank ratio when stage 2 actually ran.
    ratio = rec.get("rerank_success_ratio")
    if rec.get("stage2_available") and ratio is not None and ratio < RERANK_SUCCESS_FLOOR:
        reranked, skipped = rec.get("rerank_reranked", 0), rec.get("rerank_skipped", 0)
        problems.append(
            f"rerank success ratio {ratio:.2f} is below the {RERANK_SUCCESS_FLOOR} "
            f"floor ({reranked}/{reranked + skipped} succeeded)"
        )

    if rec.get("learning", 0) == 0:
        problems.append("learning section is empty")
    if rec.get("news", 0) == 0:
        problems.append("news section is empty")

    return problems


def check_digest(daily_dir: Path | None = None, today: str | None = None) -> list[str]:
    """Problems with today's *agent* output, or [] if it looks right.

    Separate from check() because it observes a different actor at a different
    time. check() grades the python pipeline, which runs at 08:00 and reports
    its own health immediately. The Claude agent runs at 12:00 — four hours
    later — so nothing in that run can see whether it did its job. Two ways it
    fails, and neither announced itself before this existed:

      A. It writes nothing. No digest, no push, so email-digest.yml never even
         triggers — silence is indistinguishable from "not yet". The June 2026
         outage ran five days this way.
      B. It improvises instead of reading candidates. The digest looks normal
         and the email sends, but manifest item_ids come back null, so feedback
         can't join to items and the ranker quietly stops learning.
    """
    daily_dir = daily_dir if daily_dir is not None else DAILY_DIR
    today = today or datetime.now(timezone.utc).date().isoformat()
    problems: list[str] = []

    digest = daily_dir / f"{today}.md"
    if not digest.exists():
        problems.append(f"no digest for {today} — the agent produced nothing")
        return problems  # nothing else is meaningful without it

    manifest_path = daily_dir / f"digest-manifest-{today}.json"
    if not manifest_path.exists():
        problems.append(f"digest exists but its manifest is missing ({manifest_path.name})")
        return problems

    try:
        items = json.loads(manifest_path.read_text(encoding="utf-8")).get("items", [])
    except (json.JSONDecodeError, OSError) as e:
        problems.append(f"manifest for {today} is unreadable: {e}")
        return problems

    # News legitimately carries a null item_id, and a 15+ day catch-up digest is
    # News-only by design — so only the learning sections can prove grounding.
    graded = [i for i in items if (i.get("section") or "").strip().lower() != "news"]
    if graded and all(not i.get("item_id") for i in graded):
        problems.append(
            f"digest for {today} is not grounded in ranked candidates — all "
            f"{len(graded)} learning item_ids are null (agent likely improvised "
            "instead of reading the candidates file); feedback cannot be joined "
            "back to items, so the ranker learns nothing from today"
        )

    return problems


def persist(rec: dict, health_dir: Path | None = None) -> Path:
    """Append one health record to its month shard (data/health/YYYY-MM.jsonl)."""
    health_dir = health_dir or HEALTH_DIR
    path = db.shard_path(rec["date"], health_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return path


def latest(health_dir: Path | None = None) -> dict | None:
    """The most recently recorded health record, across all month shards."""
    health_dir = health_dir or HEALTH_DIR
    if not health_dir.exists():
        return None
    # Newest-first: an empty or partially-written newest shard must not hide
    # older records, since main() exits 1 when no record is found at all.
    for shard in sorted(health_dir.glob("*.jsonl"), reverse=True):
        lines = [l for l in shard.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            return json.loads(lines[-1])
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="exit 1 if the latest run is unhealthy")
    p.add_argument("--check-digest", action="store_true",
                   help="exit 1 if today's agent-written digest is missing or ungrounded")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (defaults to today UTC)")
    args = p.parse_args(argv)

    if args.check_digest:
        problems = check_digest(DAILY_DIR, args.date)
        date = args.date or datetime.now(timezone.utc).date().isoformat()
        payload = {"date": date}
        payload.update({"problems": problems} if problems else {"status": "digest ok"})
        print(json.dumps(payload, indent=2))
        return 1 if problems else 0

    rec = latest()
    if rec is None:
        print("no health record found", file=sys.stderr)
        return 1 if args.check else 0

    problems = check(rec)
    payload = {"date": rec.get("date")}
    payload.update({"problems": problems} if problems else {"status": "healthy"})
    print(json.dumps(payload, indent=2))

    # Without --check this is a read-only report; --check is what turns an
    # unhealthy record into a non-zero exit (that's how pipeline.yml fails).
    return 1 if (problems and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
