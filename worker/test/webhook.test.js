import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import worker from "../index.js";

// The Resend webhook is the only unauthenticated-by-token entry point in the
// worker — its signature check IS its auth. It shipped with zero coverage, so
// these tests exist mainly to pin the security properties before the code is
// published to the public template, where anyone can deploy it.

let calls;
let originalFetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  calls = [];
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

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

const SECRET = `whsec_${b64("supersecretkey")}`;
const ENV = {
  GITHUB_REPO: "example/morning-digest",
  GITHUB_TOKEN: "tok",
  RESEND_WEBHOOK_SECRET: SECRET,
};

// A successful webhook does four GitHub calls: GET+PUT feedback-log.md, then
// GET+PUT the event shard. Queue enough OK responses to let both writes land.
function okWrites() {
  const get = { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 0, content: "", sha: "s1" }) };
  const put = { ok: true, status: 200, json: async () => ({}) };
  return [get, put, get, put];
}

// Independent implementation of the Svix signing scheme, so a bug in the
// worker's version can't make a test pass by agreeing with itself.
async function svixSign(secret, msgId, timestamp, body) {
  const raw = Uint8Array.from(atob(secret.replace(/^whsec_/, "")), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${msgId}.${timestamp}.${body}`));
  return btoa(String.fromCharCode(...new Uint8Array(sig)));
}

const OPENED = JSON.stringify({
  type: "email.opened",
  data: { subject: "Morning Digest — 2026-08-19" },
});

// Builds a POST to /webhook/resend. Every field is overridable so a test can
// corrupt exactly one thing and leave the rest valid.
async function webhookRequest(overrides = {}) {
  const {
    body = OPENED,
    msgId = "msg_2abc",
    timestamp = String(Math.floor(Date.now() / 1000)),
    signingSecret = SECRET,
    signedBody = body,
    signature,
    headers = {},
    path = "/webhook/resend",
  } = overrides;

  const sig = signature ?? `v1,${await svixSign(signingSecret, msgId, timestamp, signedBody)}`;
  return new Request(`https://worker.example.com${path}`, {
    method: "POST",
    headers: {
      "svix-id": msgId,
      "svix-timestamp": timestamp,
      "svix-signature": sig,
      ...headers,
    },
    body,
  });
}

// --- the happy path ---

test("a validly signed email.opened event is accepted and recorded", async () => {
  mockFetch(okWrites());
  const res = await worker.fetch(await webhookRequest(), ENV);
  assert.equal(res.status, 200);
  assert.equal(calls.length, 4, "expected both the feedback and event writes");
});

// --- forgery must fail ---

test("a tampered body is rejected even though the signature is otherwise valid", async () => {
  mockFetch(okWrites());
  // Sign the real payload, then send a different one.
  const tampered = JSON.stringify({ type: "email.opened", data: { subject: "Morning Digest — 1999-01-01" } });
  const req = await webhookRequest({ body: tampered, signedBody: OPENED });
  const res = await worker.fetch(req, ENV);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0, "a rejected webhook must not write");
});

test("a signature made with the wrong secret is rejected", async () => {
  mockFetch(okWrites());
  const req = await webhookRequest({ signingSecret: `whsec_${b64("wrongkey")}` });
  const res = await worker.fetch(req, ENV);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

test("a garbage signature is rejected", async () => {
  mockFetch(okWrites());
  const req = await webhookRequest({ signature: "v1,not-a-real-signature" });
  const res = await worker.fetch(req, ENV);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

// --- replay protection ---

test("a signature older than the replay window is rejected", async () => {
  mockFetch(okWrites());
  const stale = String(Math.floor(Date.now() / 1000) - 600);
  const res = await worker.fetch(await webhookRequest({ timestamp: stale }), ENV);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

test("a far-future timestamp is rejected", async () => {
  mockFetch(okWrites());
  const future = String(Math.floor(Date.now() / 1000) + 600);
  const res = await worker.fetch(await webhookRequest({ timestamp: future }), ENV);
  assert.equal(res.status, 401);
});

test("a non-numeric timestamp does not slip past the replay window", async () => {
  // parseInt("later") is NaN, and `Math.abs(now - NaN) > 300` is false — so an
  // unparseable timestamp skipped the window check entirely. The signature
  // still binds the timestamp string, so this was never forgeable; it is the
  // guard failing open, which is not what the code above it claims to do.
  mockFetch(okWrites());
  const res = await worker.fetch(await webhookRequest({ timestamp: "later" }), ENV);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

// --- malformed input must not become an outage ---

test("a signed but unparseable body is rejected without throwing", async () => {
  // verifyResendWebhook JSON.parses inside itself and handleResendWebhook has
  // no try/catch, so an unparseable body threw out of the worker as a 500 —
  // which makes Svix redeliver it forever.
  mockFetch(okWrites());
  const res = await worker.fetch(await webhookRequest({ body: "{not json" }), ENV);
  assert.notEqual(res.status, 500, "must not 500 — Svix would redeliver in a loop");
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

test("a non-base64 webhook secret is reported, not thrown as an opaque 500", async () => {
  // atob() throws on an invalid secret, so a misconfiguration presented as a
  // total outage with nothing in the response to say why.
  mockFetch(okWrites());
  const env = { ...ENV, RESEND_WEBHOOK_SECRET: "whsec_not!valid!base64!" };
  const res = await worker.fetch(await webhookRequest(), env);
  assert.equal(res.status, 500);
  assert.match(await res.text(), /secret/i, "the response should name the misconfiguration");
  assert.equal(calls.length, 0);
});

// --- headers and configuration ---

test("a missing svix header is rejected", async () => {
  for (const missing of ["svix-id", "svix-timestamp", "svix-signature"]) {
    calls = [];
    mockFetch(okWrites());
    const req = await webhookRequest();
    req.headers.delete(missing);
    const res = await worker.fetch(req, ENV);
    assert.equal(res.status, 401, `${missing} absent must not bypass verification`);
    assert.equal(calls.length, 0);
  }
});

test("a rotated key is accepted when it is the second signature in the header", async () => {
  // Svix sends every active key's signature space-separated during rotation.
  mockFetch(okWrites());
  const ts = String(Math.floor(Date.now() / 1000));
  const good = await svixSign(SECRET, "msg_2abc", ts, OPENED);
  const old = await svixSign(`whsec_${b64("retiredkey")}`, "msg_2abc", ts, OPENED);
  const req = await webhookRequest({ timestamp: ts, signature: `v1,${old} v1,${good}` });
  const res = await worker.fetch(req, ENV);
  assert.equal(res.status, 200);
});

test("an unconfigured webhook secret fails closed and writes nothing", async () => {
  mockFetch(okWrites());
  const { RESEND_WEBHOOK_SECRET, ...envWithoutSecret } = ENV;
  const res = await worker.fetch(await webhookRequest(), envWithoutSecret);
  assert.equal(res.status, 500);
  assert.equal(calls.length, 0, "must not write when it cannot verify");
});

// --- routing ---

test("the old /webhook path is no longer handled", async () => {
  // The route moved to /webhook/resend so signature auth could run ahead of
  // the token gate. /webhook now falls through to the token check.
  mockFetch(okWrites());
  const res = await worker.fetch(await webhookRequest({ path: "/webhook" }), ENV);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

// --- payload handling ---

test("a non-open event type is acknowledged without writing", async () => {
  mockFetch(okWrites());
  const body = JSON.stringify({ type: "email.delivered", data: { subject: "Morning Digest — 2026-08-19" } });
  const res = await worker.fetch(await webhookRequest({ body }), ENV);
  assert.equal(res.status, 200);
  assert.equal(calls.length, 0);
});

test("a subject with an unparseable date is acknowledged without writing", async () => {
  // Acknowledged, not retried: redelivering will not make the subject parse.
  mockFetch(okWrites());
  const body = JSON.stringify({ type: "email.opened", data: { subject: "Morning Digest — 9999-99-99" } });
  const res = await worker.fetch(await webhookRequest({ body }), ENV);
  assert.equal(res.status, 200);
  assert.equal(calls.length, 0);
});

test("the long-form subject date is parsed", async () => {
  mockFetch(okWrites());
  const body = JSON.stringify({ type: "email.opened", data: { subject: "Daily Digest — Thursday, April 9, 2026" } });
  const res = await worker.fetch(await webhookRequest({ body }), ENV);
  assert.equal(res.status, 200);
  assert.equal(calls.length, 4);
  assert.match(calls[1].opts.body, /2026-04-09/, "the parsed date should reach the commit message");
});

// --- failure surfacing ---

test("a failed write returns non-2xx so Svix redelivers", async () => {
  const get = { ok: true, status: 200, json: async () => ({ encoding: "base64", size: 0, content: "", sha: "s1" }) };
  const put = { ok: false, status: 500, text: async () => "github exploded" };
  mockFetch([get, put, get, put]);
  const res = await worker.fetch(await webhookRequest(), ENV);
  assert.equal(res.status, 500, "a swallowed write failure would silently lose the open event");
});
