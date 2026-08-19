import asyncio

import pytest

from pipeline import llm, rerank


def _mk_items(n):
    return [{"id": f"i{k}", "url": f"u{k}", "source": "S", "category": "ai",
             "title": f"item {k}", "summary": "rss summary", "age_hours": 4.0,
             "score": 0.5} for k in range(n)]


def test_rerank_attaches_score_and_summary():
    async def fake(system, user, model):
        return {"relevance": 8, "summary": "On-topic for LLMs."}

    out = asyncio.run(rerank.rerank(_mk_items(3),
                                    interests=["LLMs"],
                                    bodies={"i0": "body of 0", "i1": "body of 1"},
                                    rerank_fn=fake))
    assert all(it["rerank_score"] == 8.0 for it in out)
    assert all(it["rerank_summary"] == "On-topic for LLMs." for it in out)
    assert all("rerank_failed" not in it for it in out)


def test_rerank_falls_back_on_malformed_json():
    async def fake(system, user, model):
        return {"relevance": "high", "summary": 42}  # wrong types
    out = asyncio.run(rerank.rerank(_mk_items(2),
                                    interests=["x"], bodies={}, rerank_fn=fake))
    assert all(it.get("rerank_failed") for it in out)


def test_rerank_isolates_per_item_failures():
    calls = {"n": 0}

    async def fake(system, user, model):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("haiku timed out")
        return {"relevance": 7, "summary": "ok"}

    out = asyncio.run(rerank.rerank(_mk_items(3),
                                    interests=["x"], bodies={}, rerank_fn=fake))
    failed = [it for it in out if it.get("rerank_failed")]
    succeeded = [it for it in out if "rerank_score" in it]
    assert len(failed) == 1 and len(succeeded) == 2
    assert "haiku timed out" in failed[0]["rerank_error"]


def test_parse_json_block_handles_fenced_response():
    assert rerank._parse_json_block("```json\n{\"relevance\": 5, \"summary\": \"x\"}\n```") == \
        {"relevance": 5, "summary": "x"}


def test_is_available_with_injected_fn():
    rerank.set_rerank_fn(lambda *a, **kw: None)
    assert rerank.is_available()
    rerank.set_rerank_fn(None)  # reset


def test_parallel_defaults_to_8_for_anthropic(monkeypatch):
    monkeypatch.delenv("RERANK_PARALLEL", raising=False)
    monkeypatch.setattr(llm, "provider", lambda: "anthropic")
    assert rerank.parallel() == 8


def test_parallel_defaults_to_2_for_gemini(monkeypatch):
    # Gemini's free-tier RPM is much lower than Anthropic's — firing 8 parallel
    # calls is most of what was causing the ~85% 429 rate.
    monkeypatch.delenv("RERANK_PARALLEL", raising=False)
    monkeypatch.setattr(llm, "provider", lambda: "gemini")
    assert rerank.parallel() == 2


def test_parallel_env_override_wins_over_provider_default(monkeypatch):
    monkeypatch.setattr(llm, "provider", lambda: "gemini")
    monkeypatch.setenv("RERANK_PARALLEL", "5")
    assert rerank.parallel() == 5


def test_parallel_uses_provider_specific_default(monkeypatch):
    monkeypatch.delenv("RERANK_PARALLEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert rerank.parallel() == 8

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "y")
    assert rerank.parallel() == 2


def test_parallel_warns_instead_of_silently_ignoring_unparseable_override(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RERANK_PARALLEL", "not-a-number")

    assert rerank.parallel() == 8  # falls back
    assert "RERANK_PARALLEL" in capsys.readouterr().err


def test_parallel_warns_instead_of_silently_ignoring_non_positive_override(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RERANK_PARALLEL", "0")

    assert rerank.parallel() == 8
    assert "RERANK_PARALLEL" in capsys.readouterr().err


def test_rerank_error_preserves_enough_to_identify_the_quota():
    """Gemini names the exhausted quota (RPM vs RPD) and a retryDelay deep in
    its 429 body. Truncating too early turned every quota failure into an
    undiagnosable 'you exceeded your current quota'."""
    long_err = (
        '{"error": {"code": 429, "message": "You exceeded your current quota, '
        'please check your plan and billing details.", "details": [{"@type": '
        '"type.googleapis.com/google.rpc.QuotaFailure", "violations": [{'
        '"quotaMetric": "generate_content_free_tier_requests", "quotaId": '
        '"GenerateRequestsPerDayPerProjectPerModel"}]}, {"retryDelay": "41s"}]}}'
    )

    async def fake(system, user, model):
        raise RuntimeError(long_err)

    out = asyncio.run(rerank.rerank(_mk_items(1), interests=["x"], bodies={}, rerank_fn=fake))
    err = out[0]["rerank_error"]
    assert "quotaId" in err, f"quota identity lost to truncation: {err}"
    assert "PerDay" in err or "PerMinute" in err
