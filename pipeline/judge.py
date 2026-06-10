"""LLM-as-judge quality scoring for past digests.

Picks the N most recent digests under daily/, sends each one to an LLM
(Claude Haiku by default, Gemini Flash if only GEMINI_API_KEY is set — see
llm.py) along with interests.md, and asks for a 1–5 quality score per section
plus an overall score. Results land in eval/judge-YYYY-MM-DD.json.

The judge call is injectable (`set_judge_fn`) so tests don't need an API key.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db, llm, rank

JUDGE_MODEL = llm.model_name()
DEFAULT_N = 7

JudgeFn = Callable[[str, str, str], dict]
_judge_fn: JudgeFn | None = None


SECTION_HEADER = re.compile(r"^###\s+\d+\.\s+(.+?)(?:\s*\(.*\))?\s*$")


def set_judge_fn(fn: JudgeFn) -> None:
    global _judge_fn
    _judge_fn = fn


def _default_judge_fn() -> JudgeFn:
    def call(system: str, user: str, model: str = JUDGE_MODEL) -> dict:
        text = llm.complete(system, user, max_tokens=1024)
        return _parse_json_block(text)

    return call


def _parse_json_block(text: str) -> dict:
    """Tolerant JSON extraction — Haiku sometimes wraps in ```json fences."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in judge response: {text[:200]}")
    return json.loads(text[start : end + 1])


def list_recent_digests(daily_dir: Path, n: int) -> list[Path]:
    digests = sorted(
        (p for p in daily_dir.glob("*.md") if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", p.name)),
        reverse=True,
    )
    return digests[:n]


def parse_sections(markdown: str) -> list[dict]:
    """Extract `### N. Title` sections from a digest. Stops at `## Feedback`."""
    out = []
    current = None
    for line in markdown.splitlines():
        if line.startswith("## Feedback"):
            break
        m = SECTION_HEADER.match(line)
        if m:
            if current:
                out.append(current)
            current = {"section": m.group(1).strip(), "body": []}
        elif current is not None:
            current["body"].append(line)
    if current:
        out.append(current)
    return [{"section": s["section"], "body": "\n".join(s["body"]).strip()} for s in out]


def build_prompt(interests_md: str, digest_md: str) -> tuple[str, str]:
    system = (
        "You are evaluating a daily learning digest against a user's interest profile. "
        "For each Learning Block section, score relevance and quality on a 1–5 scale "
        "(1 = poor fit / low quality, 5 = excellent fit / high quality). Skip the News "
        "Block. Reply ONLY with a JSON object, no prose, in this exact shape:\n"
        '{"sections": [{"section": "<exact section title>", "score": <1-5>, '
        '"rationale": "<one sentence>"}], "overall_score": <1-5>}'
    )
    user = (
        "<interests>\n" + interests_md.strip() + "\n</interests>\n\n"
        "<digest>\n" + digest_md.strip() + "\n</digest>"
    )
    return system, user


def judge_one(path: Path, interests_md: str, judge_fn: JudgeFn) -> dict:
    digest_md = path.read_text(encoding="utf-8")
    system, user = build_prompt(interests_md, digest_md)
    result = judge_fn(system, user, JUDGE_MODEL)
    return {
        "digest_date": path.stem,
        "sections": result.get("sections", []),
        "overall_score": result.get("overall_score"),
        "model": JUDGE_MODEL,
    }


def run(n: int = DEFAULT_N, judge_fn: JudgeFn | None = None) -> dict:
    global _judge_fn
    fn = judge_fn or _judge_fn or _default_judge_fn()
    interests_path = db.REPO_ROOT / "interests.md"
    if not interests_path.exists():
        interests_path = db.REPO_ROOT / "interests.example.md"
    interests_md = interests_path.read_text(encoding="utf-8") if interests_path.exists() else ""

    digests = list_recent_digests(db.REPO_ROOT / "daily", n)
    judged = [judge_one(p, interests_md, fn) for p in digests]

    overalls = [j["overall_score"] for j in judged
                if isinstance(j.get("overall_score"), (int, float))]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": JUDGE_MODEL,
        "n_digests": len(judged),
        "mean_overall": (sum(overalls) / len(overalls)) if overalls else None,
        "digests": judged,
    }

    out_dir = db.REPO_ROOT / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    out = out_dir / f"judge-{today}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=DEFAULT_N)
    args = p.parse_args()
    if llm.provider() is None:
        raise SystemExit("no LLM key set (need ANTHROPIC_API_KEY or GEMINI_API_KEY)")
    summary = run(n=args.n)
    print(json.dumps({k: v for k, v in summary.items() if k != "digests"}, indent=2))
