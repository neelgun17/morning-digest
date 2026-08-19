import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import worker, { shardPath, appendEvent, appendFeedback } from "../index.js";

let calls;
let originalFetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  calls = [];
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

// Queue up mock responses in call order. Each entry is either a response
// object ({ok, status, json, text}) or a function(url, opts) -> response.
function mockFetch(responses) {
  let i = 0;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    const entry = responses[i++];
    return typeof entry === "function" ? entry(url, opts) : entry;
  };
}

function b64(str) {
  return Buffer.from(str, "utf-8").toString("base64");
}

function decodeBody(opts) {
  const body = JSON.parse(opts.body);
  return { ...body, decodedContent: Buffer.from(body.content, "base64").toString("utf-8") };
}

const ENV = { GITHUB_REPO: "example/morning-digest", GITHUB_TOKEN: "tok" };

test("shardPath derives the month shard from an ISO date", () => {
  assert.equal(shardPath("2026-08-18"), "data/events/2026-08.jsonl");
});

test("shardPath falls back to the current month for a malformed or missing date", () => {
  const now = new Date("2026-08-18T00:00:00Z");
  assert.equal(shardPath("not-a-date", now), "data/events/2026-08.jsonl");
  assert.equal(shardPath(null, now), "data/events/2026-08.jsonl");
  assert.equal(shardPath(undefined, now), "data/events/2026-08.jsonl");
});

test("appendEvent aborts when GitHub returns encoding: none (>1MB file)", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "none", size: 1_200_000, content: "", sha: "abc" }) },
  ]);
  const result = await appendEvent(ENV, { ts: "2026-08-18T00:00:00Z", kind: "open", date: "2026-08-18" });
  assert.equal(result.error, "encoding guard");
  assert.equal(calls.length, 1, "must not attempt the PUT after the guard trips");
});

test("appendEvent aborts when size > 0 but content is empty", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 500, content: "", sha: "abc" }) },
  ]);
  const result = await appendEvent(ENV, { ts: "2026-08-18T00:00:00Z", kind: "open", date: "2026-08-18" });
  assert.equal(result.error, "encoding guard");
  assert.equal(calls.length, 1);
});

test("appendEvent appends to existing content rather than replacing it", async () => {
  const existing = JSON.stringify({ kind: "open", date: "2026-08-01" }) + "\n";
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: existing.length, content: b64(existing), sha: "sha-existing" }) },
    { ok: true, status: 200, json: async () => ({ content: { sha: "sha-new" } }) },
  ]);
  const event = { ts: "2026-08-18T00:00:00Z", kind: "click", date: "2026-08-18" };
  await appendEvent(ENV, event);

  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /data\/events\/2026-08\.jsonl/);
  assert.match(calls[1].url, /data\/events\/2026-08\.jsonl/);

  const putBody = decodeBody(calls[1].opts);
  assert.equal(calls[1].opts.method, "PUT");
  assert.equal(putBody.sha, "sha-existing");
  assert.equal(putBody.decodedContent, existing + JSON.stringify(event) + "\n");
});

test("appendEvent creates the shard on a genuine 404 (new file)", async () => {
  mockFetch([
    { ok: false, status: 404, text: async () => "Not Found" },
    { ok: true, status: 201, json: async () => ({ content: { sha: "sha-new" } }) },
  ]);
  const event = { ts: "2026-08-18T00:00:00Z", kind: "open", date: "2026-08-18" };
  await appendEvent(ENV, event);

  assert.equal(calls.length, 2);
  const putBody = decodeBody(calls[1].opts);
  assert.equal(putBody.sha, undefined, "no sha on a brand-new file");
  assert.equal(putBody.decodedContent, JSON.stringify(event) + "\n");
  assert.match(calls[1].url, /data\/events\/2026-08\.jsonl/);
});

test("appendFeedback also aborts on the encoding guard", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "none", size: 1_200_000, content: "", sha: "abc" }) },
  ]);
  const result = await appendFeedback(ENV, "2026-08-18", "- **Read**: email opened");
  assert.equal(result.error, "encoding guard");
  assert.equal(calls.length, 1);
});

// --- call sites must not report success when the write failed -------------
// The guard above is only half the fix: appendEvent/appendFeedback returning
// an error object is useless if the handler ignores it and still renders
// "Got it! Tomorrow's digest will take this into account."

const CLICK_ENV = { ...ENV, FEEDBACK_SECRET: "s3cret" };

function clickRequest() {
  return new Request(
    "https://w.example/click?token=s3cret&date=2026-08-18" +
      "&section=1.%20LLM%20Memory&reaction=more_like_this",
  );
}

test("/click returns 200 and the thank-you page when both writes land", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 10, content: b64("# Feedback Log\n"), sha: "a" }) },
    { ok: true, status: 200, json: async () => ({}) },
    { ok: false, status: 404, text: async () => "not found" },
    { ok: true, status: 200, json: async () => ({}) },
  ]);

  const res = await worker.fetch(clickRequest(), CLICK_ENV);
  assert.equal(res.status, 200);
  assert.match(await res.text(), /Got it!/);
});

test("/click reports failure instead of a false 'Got it!' when the guard aborts", async () => {
  mockFetch([
    // feedback-log.md is past the 1MB inline limit — guard aborts the write
    { ok: true, status: 200, json: async () => ({ encoding: "none", size: 1200000, content: "", sha: "a" }) },
    { ok: false, status: 404, text: async () => "not found" },
    { ok: true, status: 200, json: async () => ({}) },
  ]);

  const res = await worker.fetch(clickRequest(), CLICK_ENV);
  assert.notEqual(res.status, 200);
  const body = await res.text();
  assert.doesNotMatch(body, /Got it!/);
  assert.doesNotMatch(body, /Tomorrow's digest will take this into account/);
});

test("/click reports failure when the PUT itself fails", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 10, content: b64("# Feedback Log\n"), sha: "a" }) },
    { ok: false, status: 500, text: async () => "github exploded" },
    { ok: false, status: 404, text: async () => "not found" },
    { ok: true, status: 200, json: async () => ({}) },
  ]);

  const res = await worker.fetch(clickRequest(), CLICK_ENV);
  assert.notEqual(res.status, 200);
});

test("appendFeedback creates feedback-log.md on a genuine 404 instead of failing", async () => {
  mockFetch([
    { ok: false, status: 404, text: async () => "not found" },
    { ok: true, status: 200, json: async () => ({}) },
  ]);

  const res = await appendFeedback(ENV, "2026-08-18", "- **1. X**: more_like_this");
  assert.equal(res, null, "a missing log is a file to create, not an error");
  const body = decodeBody(calls[1].opts);
  assert.match(body.decodedContent, /- \*\*1\. X\*\*: more_like_this/);
  assert.equal(body.sha, undefined, "no sha when creating a new file");
});

test("appendEvent does not duplicate an event that is present but not the last line", async () => {
  const existing =
    '{"date":"2026-08-18","kind":"open"}\n' +
    '{"date":"2026-08-18","kind":"click","section":"1. X","reaction":"go_deeper"}\n';
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: existing.length, content: b64(existing), sha: "a" }) },
  ]);

  // Same open event, redelivered by Svix with a fresh timestamp.
  const res = await appendEvent(ENV, { ts: "2026-08-18T09:00:00Z", kind: "open", date: "2026-08-18" });
  assert.equal(res, null);
  assert.equal(calls.length, 1, "should not PUT — the event is already recorded");
});

test("appendEvent still records a genuinely different reaction for the same date", async () => {
  const existing = '{"date":"2026-08-18","kind":"click","section":"1. X","reaction":"go_deeper"}\n';
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: existing.length, content: b64(existing), sha: "a" }) },
    { ok: true, status: 200, json: async () => ({}) },
  ]);

  const res = await appendEvent(ENV, { ts: "t", kind: "click", date: "2026-08-18", section: "1. X", reaction: "seen_this" });
  assert.equal(res, null);
  assert.equal(calls.length, 2, "a different reaction is a new event");
});

// --- HTML sinks ---

test("the form page escapes the date it echoes into a hidden input", async () => {
  // `date` comes straight from the query string. sanitize() strips angle
  // brackets and markdown, but not the double quote — so the value could
  // close its own attribute. Only the 10-char truncation kept that from
  // becoming a working event handler, which is not a guarantee worth relying
  // on the next time someone widens the limit.
  const url = new URL('https://w.example.com/form?token=s3cret&date=2026" onx');
  const res = await worker.fetch(
    new Request(url, { method: "GET" }),
    { ...ENV, FEEDBACK_SECRET: "s3cret" },
  );
  const html = await res.text();
  assert.doesNotMatch(html, /value="2026" onx/, "the date must not break out of its attribute");
  assert.match(html, /value="2026&quot;/, "the quote should be escaped, not stripped");
});

// --- appendSource must behave like its two siblings ----------------------
// appendFeedback and appendEvent both got the encoding guard, 404-as-create,
// and error returns; appendSource had none of the three, and its caller
// rendered a thank-you page regardless. Three near-identical functions, fixes
// applied to two — the divergence that shared scaffolding is meant to prevent.

const SOURCE_ENV = { ...ENV, FEEDBACK_SECRET: "s3cret" };

function sourceRequest(url = "https://example.com/feed.xml") {
  const form = new FormData();
  form.set("url", url);
  form.set("name", "Example Blog");
  form.set("category", "tech");
  return new Request("https://w.example/source?token=s3cret", { method: "POST", body: form });
}

const SOURCES_YML = "sources:\n  - url: https://a.example/feed\n\napis:\n  hn: true\n";

test("/source aborts instead of clobbering sources.yml past the 1MB limit", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "none", size: 1200000, content: "", sha: "a" }) },
  ]);

  const res = await worker.fetch(sourceRequest(), SOURCE_ENV);
  assert.notEqual(res.status, 200);
  assert.equal(calls.length, 1, "must not PUT after the guard trips");
});

test("/source reports failure rather than a thank-you page when the write fails", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 60, content: b64(SOURCES_YML), sha: "a" }) },
    { ok: false, status: 500, text: async () => "github exploded" },
  ]);

  const res = await worker.fetch(sourceRequest(), SOURCE_ENV);
  assert.notEqual(res.status, 200);
  assert.doesNotMatch(await res.text(), /Got it!/);
});

test("/source refuses to create sources.yml from nothing rather than corrupting it", async () => {
  // appendSource's transform inserts into an existing document. Against "" it
  // emits a bare indented list with no top-level key, which YAML happily parses
  // as a list — and pipeline/fetch.py then dies on sources.get("rss", []) with
  // AttributeError. Writing a corrupt config is strictly worse than refusing.
  mockFetch([
    { ok: false, status: 404, text: async () => "not found" },
  ]);

  const res = await worker.fetch(sourceRequest(), SOURCE_ENV);
  assert.notEqual(res.status, 200, "a missing sources.yml is a failure to report");
  assert.equal(calls.length, 1, "must not PUT a fabricated sources.yml");
});

test("/source still inserts before the apis: block and skips duplicates", async () => {
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 60, content: b64(SOURCES_YML), sha: "a" }) },
    { ok: true, status: 200, json: async () => ({}) },
  ]);
  await worker.fetch(sourceRequest(), SOURCE_ENV);
  const written = decodeBody(calls[1].opts).decodedContent;
  assert.ok(written.indexOf("example.com/feed.xml") < written.indexOf("\napis:"),
    "new source must land before the apis: block");

  calls.length = 0;
  mockFetch([
    { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 60, content: b64(SOURCES_YML), sha: "a" }) },
  ]);
  const res = await worker.fetch(sourceRequest("https://a.example/feed"), SOURCE_ENV);
  assert.equal(res.status, 200, "a duplicate is success, not failure");
  assert.equal(calls.length, 1, "must not PUT a duplicate");
});
