"""Provider-agnostic LLM completion for the reranker and judge.

Provider selection (first configured key wins):
  1. ANTHROPIC_API_KEY  -> Claude Haiku (default, what the prompts were tuned on)
  2. GEMINI_API_KEY     -> Gemini Flash (REST via httpx, no extra dependency)

Both providers expose the same minimal surface: a system prompt + one user
message in, response text out. Callers parse the JSON themselves.

Retry/backoff: both acomplete and complete wrap the actual provider call in a
retry loop that retries 429s and 5xxs with exponential backoff + jitter
(honoring a Retry-After header when the provider sends one), and gives up
after MAX_ATTEMPTS. Other 4xx errors are not retried. The provider call
itself is injectable (`set_transport_fn` / `set_transport_fn_sync`) and so is
the sleep (`set_async_sleep_fn` / `set_sleep_fn`) so tests can simulate
transient failures without burning API quota or actually waiting — mirrors
the `set_rerank_fn` / `set_judge_fn` seam pattern used elsewhere.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Awaitable, Callable

ANTHROPIC_MODEL = "claude-haiku-4-5"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MAX_ATTEMPTS = 5
BASE_DELAY = 1.0    # seconds, before jitter
MAX_DELAY = 30.0    # backoff ceiling regardless of attempt count


class LLMError(Exception):
    """Base class for structured LLM transport errors."""


class HTTPStatusError(LLMError):
    """A provider call failed with an HTTP-style status code.

    Raised by the default transports (translated from httpx for Gemini, from
    the Anthropic SDK's APIStatusError for Anthropic) and usable directly by
    injected test transports.
    """

    def __init__(self, status_code: int, retry_after: float | None = None, message: str = ""):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message or f"HTTP {status_code}")


class MaxTokensError(LLMError):
    """The provider truncated its response because of the token budget.

    Gemini 2.5 Flash counts *thinking* tokens against maxOutputTokens, so a
    modest budget can be entirely consumed by reasoning before any output
    text is written — the caller would otherwise get a silently-truncated
    string that fails to parse as JSON several calls downstream.
    """


AsyncTransportFn = Callable[[str, str, int], Awaitable[str]]
SyncTransportFn = Callable[[str, str, int], str]

_async_transport_fn: AsyncTransportFn | None = None
_sync_transport_fn: SyncTransportFn | None = None
_async_sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep
_sleep_fn: Callable[[float], None] = time.sleep


def set_transport_fn(fn: AsyncTransportFn | None) -> None:
    global _async_transport_fn
    _async_transport_fn = fn


def set_transport_fn_sync(fn: SyncTransportFn | None) -> None:
    global _sync_transport_fn
    _sync_transport_fn = fn


def set_async_sleep_fn(fn: Callable[[float], Awaitable[None]] | None) -> None:
    global _async_sleep_fn
    _async_sleep_fn = fn or asyncio.sleep


def set_sleep_fn(fn: Callable[[float], None] | None) -> None:
    global _sleep_fn
    _sleep_fn = fn or time.sleep


def provider() -> str | None:
    """Which provider is configured, or None if neither key is set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return None


def model_name() -> str:
    return ANTHROPIC_MODEL if provider() == "anthropic" else GEMINI_MODEL


def _is_retryable(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Delay before the next attempt (0-indexed attempt just made).

    Honors an explicit Retry-After when the provider sent one; otherwise
    exponential backoff with full jitter. Both are capped at MAX_DELAY —
    Gemini returns long retry delays on daily-quota exhaustion, and honoring
    those verbatim would let one provider stall the whole pipeline for hours.
    """
    if retry_after is not None:
        return min(retry_after, MAX_DELAY)
    ceiling = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
    return ceiling * (0.5 + random.random() * 0.5)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form isn't handled; falls back to computed backoff


def _gemini_request(system: str, user: str, max_tokens: int) -> tuple[str, dict, dict]:
    """URL, headers and JSON body for one Gemini generateContent call.

    Shared by the async and sync transports: the async/sync split itself is
    unavoidable in Python, but the request shape shouldn't be written twice.
    """
    return (
        GEMINI_URL.format(model=GEMINI_MODEL),
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        _gemini_payload(system, user, max_tokens),
    )


def _anthropic_request(system: str, user: str, max_tokens: int) -> dict:
    """Keyword args for one Anthropic messages.create call."""
    return {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }


def _anthropic_text(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")


def _gemini_payload(system: str, user: str, max_tokens: int) -> dict:
    return {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            # This is structured extraction (rerank/judge JSON), not open-ended
            # reasoning — thinking tokens buy nothing here and silently eat the
            # whole maxOutputTokens budget, truncating the JSON we actually want.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }


def _gemini_extract(data: dict) -> str:
    candidate = data["candidates"][0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise MaxTokensError(
            "Gemini response was truncated at maxOutputTokens before finishing "
            "(finishReason=MAX_TOKENS) — the caller would otherwise receive "
            "unparseable partial JSON. Raise max_tokens or check thinkingConfig."
        )
    parts = candidate["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def _httpx_raise_for_status(res) -> None:
    if res.status_code < 400:
        return
    retry_after = _parse_retry_after(res.headers.get("retry-after"))
    raise HTTPStatusError(res.status_code, retry_after=retry_after, message=res.text[:600])


def _translate_anthropic_error(e: Exception) -> Exception:
    status_code = getattr(e, "status_code", None)
    if status_code is None:
        return e
    retry_after = None
    response = getattr(e, "response", None)
    if response is not None:
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
    return HTTPStatusError(status_code, retry_after=retry_after, message=str(e)[:200])


def _default_async_transport() -> AsyncTransportFn:
    async def call(system: str, user: str, max_tokens: int) -> str:
        if provider() == "anthropic":
            from anthropic import AsyncAnthropic
            # max_retries=0: our retry loop owns backoff/jitter/Retry-After
            # handling so it's uniform across providers and testable.
            client = AsyncAnthropic(max_retries=0)
            try:
                msg = await client.messages.create(**_anthropic_request(system, user, max_tokens))
            except Exception as e:
                raise _translate_anthropic_error(e) from e
            return _anthropic_text(msg)
        if provider() == "gemini":
            import httpx
            url, headers, payload = _gemini_request(system, user, max_tokens)
            async with httpx.AsyncClient(timeout=60) as client:
                res = await client.post(url, headers=headers, json=payload)
                _httpx_raise_for_status(res)
                return _gemini_extract(res.json())
        raise RuntimeError("no LLM provider configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")

    return call


def _default_sync_transport() -> SyncTransportFn:
    def call(system: str, user: str, max_tokens: int) -> str:
        if provider() == "anthropic":
            from anthropic import Anthropic
            client = Anthropic(max_retries=0)
            try:
                msg = client.messages.create(**_anthropic_request(system, user, max_tokens))
            except Exception as e:
                raise _translate_anthropic_error(e) from e
            return _anthropic_text(msg)
        if provider() == "gemini":
            import httpx
            url, headers, payload = _gemini_request(system, user, max_tokens)
            res = httpx.post(url, headers=headers, json=payload, timeout=60)
            _httpx_raise_for_status(res)
            return _gemini_extract(res.json())
        raise RuntimeError("no LLM provider configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")

    return call


async def acomplete(system: str, user: str, max_tokens: int = 512) -> str:
    """Async completion (used by the reranker's parallel calls)."""
    fn = _async_transport_fn or _default_async_transport()
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await fn(system, user, max_tokens)
        except HTTPStatusError as e:
            if not _is_retryable(e.status_code) or attempt == MAX_ATTEMPTS - 1:
                raise
            await _async_sleep_fn(_backoff_delay(attempt, e.retry_after))
    raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover


def complete(system: str, user: str, max_tokens: int = 1024) -> str:
    """Sync completion (used by the judge's sequential calls)."""
    fn = _sync_transport_fn or _default_sync_transport()
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fn(system, user, max_tokens)
        except HTTPStatusError as e:
            if not _is_retryable(e.status_code) or attempt == MAX_ATTEMPTS - 1:
                raise
            _sleep_fn(_backoff_delay(attempt, e.retry_after))
    raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover
