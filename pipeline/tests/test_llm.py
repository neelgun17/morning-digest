import asyncio

import pytest

from pipeline import llm


@pytest.fixture(autouse=True)
def _reset_seams():
    """Every test injects its own transport/sleep; always reset after so one
    test's fake never leaks into the next."""
    yield
    llm.set_transport_fn(None)
    llm.set_transport_fn_sync(None)
    llm.set_sleep_fn(None)
    llm.set_async_sleep_fn(None)


def _fake_async_sleep_calls():
    calls = []

    async def fake(seconds):
        calls.append(seconds)

    return calls, fake


def _fake_sync_sleep_calls():
    calls = []

    def fake(seconds):
        calls.append(seconds)

    return calls, fake


# ---------------------------------------------------------------------------
# acomplete (async path — used by the reranker's parallel calls)
# ---------------------------------------------------------------------------

def test_acomplete_retries_on_429_and_succeeds_later():
    attempts = {"n": 0}

    async def transport(system, user, max_tokens):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise llm.HTTPStatusError(429, message="rate limited")
        return "ok"

    sleep_calls, fake_sleep = _fake_async_sleep_calls()
    llm.set_transport_fn(transport)
    llm.set_async_sleep_fn(fake_sleep)

    result = asyncio.run(llm.acomplete("sys", "user"))
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(sleep_calls) == 2  # slept between attempts 1->2 and 2->3, not after success


def test_acomplete_honors_retry_after_header():
    attempts = {"n": 0}

    async def transport(system, user, max_tokens):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise llm.HTTPStatusError(429, retry_after=7.5, message="rate limited")
        return "ok"

    sleep_calls, fake_sleep = _fake_async_sleep_calls()
    llm.set_transport_fn(transport)
    llm.set_async_sleep_fn(fake_sleep)

    asyncio.run(llm.acomplete("sys", "user"))
    assert sleep_calls == [7.5]


def test_acomplete_gives_up_after_max_attempts_and_raises():
    attempts = {"n": 0}

    async def transport(system, user, max_tokens):
        attempts["n"] += 1
        raise llm.HTTPStatusError(429, message="always rate limited")

    _, fake_sleep = _fake_async_sleep_calls()
    llm.set_transport_fn(transport)
    llm.set_async_sleep_fn(fake_sleep)

    with pytest.raises(llm.HTTPStatusError):
        asyncio.run(llm.acomplete("sys", "user"))
    assert attempts["n"] == llm.MAX_ATTEMPTS


def test_acomplete_retries_5xx_but_not_other_4xx():
    attempts = {"n": 0}

    async def transport(system, user, max_tokens):
        attempts["n"] += 1
        raise llm.HTTPStatusError(500, message="server error")

    _, fake_sleep = _fake_async_sleep_calls()
    llm.set_transport_fn(transport)
    llm.set_async_sleep_fn(fake_sleep)
    with pytest.raises(llm.HTTPStatusError):
        asyncio.run(llm.acomplete("sys", "user"))
    assert attempts["n"] == llm.MAX_ATTEMPTS  # 5xx is retried like 429

    attempts["n"] = 0

    async def transport_403(system, user, max_tokens):
        attempts["n"] += 1
        raise llm.HTTPStatusError(403, message="forbidden")

    llm.set_transport_fn(transport_403)
    with pytest.raises(llm.HTTPStatusError):
        asyncio.run(llm.acomplete("sys", "user"))
    assert attempts["n"] == 1  # non-429 4xx must NOT be retried


def test_backoff_is_bounded_and_sleep_is_never_the_real_one(monkeypatch):
    # If the fake sleep weren't wired in correctly, this test would actually
    # block for the sum of all backoff delays. Assert it returns fast AND
    # that the computed delays never exceed the configured ceiling.
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    async def transport(system, user, max_tokens):
        raise llm.HTTPStatusError(429, message="rate limited")

    llm.set_transport_fn(transport)
    llm.set_async_sleep_fn(fake_sleep)

    with pytest.raises(llm.HTTPStatusError):
        asyncio.run(llm.acomplete("sys", "user"))

    assert calls, "expected at least one backoff sleep"
    assert all(c <= llm.MAX_DELAY for c in calls)


# ---------------------------------------------------------------------------
# complete (sync path — used by the judge's sequential calls)
# ---------------------------------------------------------------------------

def test_complete_retries_on_429_and_succeeds_later():
    attempts = {"n": 0}

    def transport(system, user, max_tokens):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise llm.HTTPStatusError(429, message="rate limited")
        return "ok"

    sleep_calls, fake_sleep = _fake_sync_sleep_calls()
    llm.set_transport_fn_sync(transport)
    llm.set_sleep_fn(fake_sleep)

    result = llm.complete("sys", "user")
    assert result == "ok"
    assert attempts["n"] == 2
    assert len(sleep_calls) == 1


def test_complete_gives_up_after_max_attempts_and_raises():
    def transport(system, user, max_tokens):
        raise llm.HTTPStatusError(500, message="server error")

    _, fake_sleep = _fake_sync_sleep_calls()
    llm.set_transport_fn_sync(transport)
    llm.set_sleep_fn(fake_sleep)

    with pytest.raises(llm.HTTPStatusError):
        llm.complete("sys", "user")


# ---------------------------------------------------------------------------
# Gemini-specific parsing: thinkingConfig + MAX_TOKENS truncation
# ---------------------------------------------------------------------------

def test_gemini_payload_disables_thinking_budget():
    payload = llm._gemini_payload("sys", "user", 1024)
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_gemini_extract_raises_clear_error_on_max_tokens():
    data = {
        "candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"text": '{"relevance": 8, "summ'}]},  # truncated
        }],
    }
    with pytest.raises(llm.MaxTokensError):
        llm._gemini_extract(data)


def test_gemini_extract_returns_text_on_normal_finish():
    data = {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": '{"relevance": 8, "summary": "ok"}'}]},
        }],
    }
    assert llm._gemini_extract(data) == '{"relevance": 8, "summary": "ok"}'


def test_retry_after_is_capped_so_a_provider_cannot_hang_the_pipeline(monkeypatch):
    """Gemini returns long retry delays on daily-quota exhaustion. Honoring
    those verbatim would let one provider stall a 3-minute job for hours."""
    slept = []
    monkeypatch.setattr(llm, "_sleep_fn", lambda s: slept.append(s))

    attempts = {"n": 0}

    def transport(system, user, max_tokens):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise llm.HTTPStatusError(429, retry_after=3600.0)
        return "ok"

    llm.set_transport_fn_sync(transport)
    try:
        assert llm.complete("s", "u") == "ok"
    finally:
        llm.set_transport_fn_sync(None)
        llm.set_sleep_fn(None)

    assert slept, "should have slept between attempts"
    assert slept[0] <= llm.MAX_DELAY, f"slept {slept[0]}s, expected <= {llm.MAX_DELAY}"
