import asyncio

import pytest

from pipeline import rerank


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
