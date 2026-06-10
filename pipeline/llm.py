"""Provider-agnostic LLM completion for the reranker and judge.

Provider selection (first configured key wins):
  1. ANTHROPIC_API_KEY  -> Claude Haiku (default, what the prompts were tuned on)
  2. GEMINI_API_KEY     -> Gemini Flash (REST via httpx, no extra dependency)

Both providers expose the same minimal surface: a system prompt + one user
message in, response text out. Callers parse the JSON themselves.
"""
from __future__ import annotations

import os

ANTHROPIC_MODEL = "claude-haiku-4-5"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def provider() -> str | None:
    """Which provider is configured, or None if neither key is set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return None


def model_name() -> str:
    return ANTHROPIC_MODEL if provider() == "anthropic" else GEMINI_MODEL


def _gemini_payload(system: str, user: str, max_tokens: int) -> dict:
    return {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }


def _gemini_extract(data: dict) -> str:
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


async def acomplete(system: str, user: str, max_tokens: int = 512) -> str:
    """Async completion (used by the reranker's parallel calls)."""
    if provider() == "anthropic":
        from anthropic import AsyncAnthropic
        msg = await AsyncAnthropic().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")
    if provider() == "gemini":
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                GEMINI_URL.format(model=GEMINI_MODEL),
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                json=_gemini_payload(system, user, max_tokens),
            )
            res.raise_for_status()
            return _gemini_extract(res.json())
    raise RuntimeError("no LLM provider configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")


def complete(system: str, user: str, max_tokens: int = 1024) -> str:
    """Sync completion (used by the judge's sequential calls)."""
    if provider() == "anthropic":
        from anthropic import Anthropic
        msg = Anthropic().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")
    if provider() == "gemini":
        import httpx
        res = httpx.post(
            GEMINI_URL.format(model=GEMINI_MODEL),
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            json=_gemini_payload(system, user, max_tokens),
            timeout=60,
        )
        res.raise_for_status()
        return _gemini_extract(res.json())
    raise RuntimeError("no LLM provider configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")
