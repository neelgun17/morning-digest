"""Stage-2 rerank: send each top-30 item + interests to an LLM (Claude Haiku
by default, Gemini Flash if only GEMINI_API_KEY is set — see llm.py), get
back a {relevance: 1-10, summary: "..."} pair.

The summary is personalized to the user's interests by virtue of the prompt,
not a generic article summary. That summary flows through to the email so
the user reads it verbatim.

The Anthropic client is injectable (`set_rerank_fn`) so tests don't burn
API quota. Falls back to None for any single item that fails — the caller
keeps the stage-1 score and original RSS summary for that one.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Awaitable, Callable

from . import llm

RERANK_MODEL = llm.model_name()
BODY_CHARS_FOR_PROMPT = 4000

# Anthropic's rate limits comfortably handle 8 parallel rerank calls; Gemini's
# free tier does not — firing 8 at once was most of what caused the ~85% 429
# rate on Gemini. RERANK_PARALLEL env var overrides either default.
DEFAULT_PARALLEL = {"anthropic": 8, "gemini": 2}
FALLBACK_PARALLEL = 8


def parallel() -> int:
    """Concurrency for the rerank fan-out, provider-aware.

    An invalid RERANK_PARALLEL warns rather than silently falling back: a
    typo'd env var quietly changing concurrency is the same silent-degradation
    class this module's 429 handling exists to eliminate.
    """
    override = os.environ.get("RERANK_PARALLEL")
    if override:
        try:
            n = int(override)
        except ValueError:
            print(f"warn: ignoring RERANK_PARALLEL={override!r} (not an integer)",
                  file=sys.stderr)
        else:
            if n > 0:
                return n
            print(f"warn: ignoring RERANK_PARALLEL={override!r} (must be > 0)",
                  file=sys.stderr)
    return DEFAULT_PARALLEL.get(llm.provider(), FALLBACK_PARALLEL)

RerankFn = Callable[[str, str, str], Awaitable[dict]]
_rerank_fn: RerankFn | None = None


def set_rerank_fn(fn: RerankFn) -> None:
    global _rerank_fn
    _rerank_fn = fn


def is_available() -> bool:
    return bool(_rerank_fn) or llm.provider() is not None


def _default_rerank_fn() -> RerankFn:
    async def call(system: str, user: str, model: str = RERANK_MODEL) -> dict:
        text = await llm.acomplete(system, user, max_tokens=512)
        return _parse_json_block(text)

    return call


def _parse_json_block(text: str) -> dict:
    """Extract the JSON object from the LLM response.

    Spans first `{` to last `}` so it handles bare responses, fenced
    responses (```json ... ```), and nested objects uniformly — non-greedy
    regex would prematurely terminate on inner braces.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in rerank response: {text[:200]}")
    return json.loads(text[start : end + 1])


def _prompt(interests: list[str], item: dict, body: str | None) -> tuple[str, str]:
    system = (
        "You are reranking articles for a personalized daily learning digest. "
        "Given the user's interests and an article excerpt, return ONLY a JSON object "
        "with these exact keys, no other prose:\n"
        '{"relevance": <integer 1-10, how well it matches the interests>, '
        '"summary": "<one or two sentences (under 60 words) summarizing the article '
        'through the lens of the user\'s interests — plain prose, no markdown, '
        'no \\"this article\\" preamble>"}\n'
        "Be honest: low scores for off-topic items. The summary should make the "
        "specific connection to the user's interests clear."
    )
    user = (
        "<interests>\n" + "\n".join(f"- {i}" for i in interests) + "\n</interests>\n\n"
        f"<article source=\"{item.get('source','')}\" title=\"{item.get('title','')}\">\n"
        + (body[:BODY_CHARS_FOR_PROMPT] if body else item.get("summary", "")[:1500] or "(no body available)")
        + "\n</article>"
    )
    return system, user


async def rerank(
    items: list[dict],
    interests: list[str],
    bodies: dict[str, str],
    rerank_fn: RerankFn | None = None,
) -> list[dict]:
    """Returns each item with `rerank_score` (0-10) and `rerank_summary` set
    when the call succeeds, or `rerank_failed=True` when it doesn't."""
    fn = rerank_fn or _rerank_fn or _default_rerank_fn()
    sem = asyncio.Semaphore(parallel())

    async def one(item: dict) -> dict:
        system, user = _prompt(interests, item, bodies.get(item["id"]))
        try:
            async with sem:
                result = await fn(system, user, RERANK_MODEL)
        except Exception as e:
            # 400, not 120: Gemini names the exhausted quota (RPM vs RPD) and
            # a retryDelay deep in its 429 body, and 120 chars cut it off right
            # before that — making every quota failure look identical and
            # undiagnosable.
            return {**item, "rerank_failed": True, "rerank_error": str(e)[:400]}
        score = result.get("relevance")
        summary = result.get("summary")
        if not isinstance(score, (int, float)) or not isinstance(summary, str):
            return {**item, "rerank_failed": True,
                    "rerank_error": "bad shape"}
        return {**item, "rerank_score": float(score), "rerank_summary": summary.strip()}

    return await asyncio.gather(*[one(it) for it in items])
