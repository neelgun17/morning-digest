"""Stage-2 rerank: send each top-30 item + interests to Claude Haiku, get
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
import re
from typing import Awaitable, Callable

RERANK_MODEL = "claude-haiku-4-5"
BODY_CHARS_FOR_PROMPT = 4000
PARALLEL = 8

RerankFn = Callable[[str, str, str], Awaitable[dict]]
_rerank_fn: RerankFn | None = None


def set_rerank_fn(fn: RerankFn) -> None:
    global _rerank_fn
    _rerank_fn = fn


def is_available() -> bool:
    return bool(_rerank_fn) or bool(os.environ.get("ANTHROPIC_API_KEY"))


def _default_rerank_fn() -> RerankFn:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    async def call(system: str, user: str, model: str = RERANK_MODEL) -> dict:
        msg = await client.messages.create(
            model=model,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return _parse_json_block(text)

    return call


def _parse_json_block(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
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
    sem = asyncio.Semaphore(PARALLEL)

    async def one(item: dict) -> dict:
        system, user = _prompt(interests, item, bodies.get(item["id"]))
        try:
            async with sem:
                result = await fn(system, user, RERANK_MODEL)
        except Exception as e:
            return {**item, "rerank_failed": True, "rerank_error": str(e)[:120]}
        score = result.get("relevance")
        summary = result.get("summary")
        if not isinstance(score, (int, float)) or not isinstance(summary, str):
            return {**item, "rerank_failed": True,
                    "rerank_error": "bad shape"}
        return {**item, "rerank_score": float(score), "rerank_summary": summary.strip()}

    return await asyncio.gather(*[one(it) for it in items])
