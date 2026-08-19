export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Resend webhook uses its own signature auth — handle before token check
    if (url.pathname === "/webhook/resend" && request.method === "POST") {
      return handleResendWebhook(request, env);
    }

    // Authenticate all requests with a shared secret token
    const token = url.searchParams.get("token");
    if (!env.FEEDBACK_SECRET || token !== env.FEEDBACK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    if (url.pathname === "/click") return handleClick(url, env);
    if (url.pathname === "/form" && request.method === "GET") return handleFormPage(url, env);
    if (url.pathname === "/form" && request.method === "POST") return handleFormSubmit(request, url, env);
    if (url.pathname === "/source" && request.method === "GET") return handleSourcePage(url, env);
    if (url.pathname === "/source" && request.method === "POST") return handleSourceSubmit(request, url, env);

    return new Response("Not found", { status: 404 });
  },
};

// Strip markdown/HTML formatting to prevent injection into feedback-log.md
function sanitize(input, maxLength = 200) {
  return input
    .replace(/[[\](){}|`*_~#<>!]/g, "")
    .replace(/\n/g, " ")
    .trim()
    .slice(0, maxLength);
}

// Validate a YYYY-MM-DD date. The shape check alone accepted 9999-99-99 and
// 2026-13-01, which then reached shardPath and created phantom month shards
// (data/events/9999-99.jsonl) that the pipeline joins against nothing. Round-
// tripping through Date is what rejects a well-shaped but non-existent day,
// leap years included.
function isValidDate(date) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const parsed = new Date(`${date}T00:00:00Z`);
  return !isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date;
}

// Return the slice of content belonging to a given date's section,
// from the header through (but not including) the next "## " heading or EOF.
// Returns "" if the header is not found.
function sectionSlice(content, dateHeader) {
  const start = content.indexOf(dateHeader);
  if (start === -1) return "";
  const afterHeader = start + dateHeader.length;
  const rest = content.slice(afterHeader);
  const nextIdx = rest.search(/\n## /);
  return nextIdx === -1 ? content.slice(start) : content.slice(start, afterHeader + nextIdx);
}

// Month shard for a given ISO date, e.g. "2026-08-18" -> "data/events/2026-08.jsonl".
// Mirrors pipeline/ingest_events.py's shard_path exactly: a malformed or
// missing date falls back to the current month rather than raising, so a bad
// event still lands somewhere findable.
export function shardPath(dateStr, now = new Date()) {
  if (typeof dateStr === "string" && /^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
    return `data/events/${dateStr.slice(0, 7)}.jsonl`;
  }
  return `data/events/${now.toISOString().slice(0, 7)}.jsonl`;
}

// Guards the GitHub Contents API's undocumented behavior past its 1MB inline
// limit: it returns encoding: "none" with an empty content field but a VALID
// sha. Unguarded, that decodes to "" ("empty file") and the PUT below would
// silently overwrite months of history with a single line. Treat that case,
// and any other size>0-but-empty-content response, as a hard abort rather
// than a "the file happens to be empty" read.
function encodingGuardError(fileData) {
  if (fileData.encoding !== "base64") {
    return `unexpected encoding "${fileData.encoding}" (file likely exceeds the GitHub API's 1MB inline limit)`;
  }
  if (fileData.size > 0 && !fileData.content) {
    return "size > 0 but content is empty";
  }
  return null;
}

// Decode a Svix "whsec_<base64>" secret to raw key bytes. Returns null when
// the secret isn't valid base64 — atob() throws on that, and an unguarded
// throw here surfaced as a bare 500 with nothing to say the secret was the
// problem, so a one-character typo in `wrangler secret put` read as an outage.
function decodeWebhookSecret(secret) {
  try {
    return Uint8Array.from(atob(secret.replace(/^whsec_/, "")), (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

// Compare two base64 signatures without an early exit. Both are fixed-length
// HMAC digests, so the length check leaks nothing an attacker doesn't have.
function signaturesMatch(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function verifyResendWebhook(request, secretBytes) {
  const msgId = request.headers.get("svix-id");
  const msgTimestamp = request.headers.get("svix-timestamp");
  const msgSignature = request.headers.get("svix-signature");

  if (!msgId || !msgTimestamp || !msgSignature) return null;

  // Reject timestamps outside a 5 minute window to limit replay. parseInt on
  // a non-numeric header yields NaN, and every comparison against NaN is
  // false — so the bare `Math.abs(...) > 300` check passed anything
  // unparseable straight through. The signature still binds the timestamp
  // string, so this was never forgeable, but the guard has to actually guard.
  const sentAt = Number.parseInt(msgTimestamp, 10);
  if (!Number.isFinite(sentAt)) return null;
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - sentAt) > 300) return null;

  const body = await request.text();

  // Svix signs: "{msg_id}.{timestamp}.{body}"
  const toSign = `${msgId}.${msgTimestamp}.${body}`;
  const encoder = new TextEncoder();

  const key = await crypto.subtle.importKey("raw", secretBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(toSign));
  const computed = btoa(String.fromCharCode(...new Uint8Array(sig)));

  // Svix sends multiple signatures separated by spaces: "v1,<base64> v1,<base64>"
  const signatures = msgSignature.split(" ");
  const valid = signatures.some((s) => {
    const [version, value] = s.split(",");
    return version === "v1" && typeof value === "string" && signaturesMatch(value, computed);
  });

  if (!valid) return null;

  // A signed body should always be JSON, but an unguarded parse threw out of
  // here and past handleResendWebhook (which has no try/catch) as a 500 —
  // which Svix treats as retryable, redelivering the same bad body forever.
  try {
    return JSON.parse(body);
  } catch {
    console.error("Webhook signature valid but body is not JSON");
    return null;
  }
}

async function handleResendWebhook(request, env) {
  if (!env.RESEND_WEBHOOK_SECRET) {
    return new Response("Webhook not configured", { status: 500 });
  }

  const secretBytes = decodeWebhookSecret(env.RESEND_WEBHOOK_SECRET);
  if (!secretBytes) {
    console.error("RESEND_WEBHOOK_SECRET is not valid base64 — cannot verify webhooks");
    return new Response("Webhook secret misconfigured", { status: 500 });
  }

  const payload = await verifyResendWebhook(request, secretBytes);
  if (!payload) {
    return new Response("Invalid signature", { status: 401 });
  }

  // Only handle email.opened events
  if (payload.type !== "email.opened") {
    return new Response("OK", { status: 200 });
  }

  // Extract date from subject line
  // Supports both "Daily Digest — Thursday, April 9, 2026" and "Morning Digest — 2026-04-02"
  const subject = payload.data?.subject || "";
  let date = null;

  // Try YYYY-MM-DD first
  const isoMatch = subject.match(/(\d{4}-\d{2}-\d{2})/);
  if (isoMatch) {
    date = isoMatch[1];
  } else {
    // Try "Month Day, Year" format (e.g. "April 9, 2026")
    const longMatch = subject.match(/(\w+)\s+(\d{1,2}),\s+(\d{4})/);
    if (longMatch) {
      const monthNames = {
        January: "01", February: "02", March: "03", April: "04",
        May: "05", June: "06", July: "07", August: "08",
        September: "09", October: "10", November: "11", December: "12",
      };
      const month = monthNames[longMatch[1]];
      if (month) {
        date = `${longMatch[3]}-${month}-${longMatch[2].padStart(2, "0")}`;
      }
    }
  }

  if (!date || !isValidDate(date)) {
    console.log("Could not extract date from subject:", subject);
    return new Response("OK", { status: 200 });
  }

  const entry = `- **Read**: email opened`;
  const failures = [
    await appendFeedback(env, date, entry),
    await appendEvent(env, { ts: new Date().toISOString(), kind: "open", date }),
  ].filter(Boolean);

  if (failures.length) {
    // Non-2xx makes Svix redeliver, so surfacing the failure also retries it.
    console.error("Resend webhook write failed:", JSON.stringify(failures));
    return new Response("Write failed", { status: 500 });
  }

  return new Response("OK", { status: 200 });
}

async function handleClick(url, env) {
  const date = url.searchParams.get("date");
  const section = url.searchParams.get("section");
  const reaction = url.searchParams.get("reaction");

  if (!date || !section || !reaction) {
    return new Response("Missing parameters", { status: 400 });
  }

  if (!isValidDate(date)) {
    return new Response("Invalid date format", { status: 400 });
  }

  const validReactions = ["more_like_this", "go_deeper", "too_basic", "too_advanced", "not_interested", "seen_this"];
  if (!validReactions.includes(reaction)) {
    return new Response("Invalid reaction", { status: 400 });
  }

  const cleanSection = sanitize(section, 100);
  const entry = `- **${cleanSection}**: ${reaction}`;
  // Both writes are attempted even if the first fails — they're independent
  // records and a partial write is better than none — but any failure is
  // surfaced rather than papered over with a thank-you page.
  const failures = [
    await appendFeedback(env, date, entry),
    await appendEvent(env, {
      ts: new Date().toISOString(),
      kind: "click",
      date,
      section: cleanSection,
      reaction,
    }),
  ].filter(Boolean);

  if (failures.length) return writeFailureResponse(failures);

  const label = {
    more_like_this: "More like this",
    go_deeper: "Go deeper",
    too_basic: "Too basic",
    too_advanced: "Too advanced",
    not_interested: "Not interested",
    seen_this: "Seen this",
  }[reaction];

  return new Response(thankYouPage(label, cleanSection), {
    headers: { "Content-Type": "text/html" },
  });
}

function handleFormPage(url, env) {
  const date = url.searchParams.get("date") || "";
  const tokenParam = encodeURIComponent(env.FEEDBACK_SECRET || "");
  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Digest Feedback</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 40px auto; padding: 20px; color: #1a1a1a; }
    h2 { font-size: 20px; }
    textarea { width: 100%; height: 120px; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; font-family: inherit; resize: vertical; }
    button { margin-top: 12px; padding: 10px 24px; background: #0066cc; color: white; border: none; border-radius: 8px; font-size: 15px; cursor: pointer; }
    button:hover { background: #0052a3; }
  </style>
</head>
<body>
  <h2>What's on your mind?</h2>
  <p>This goes directly into your feedback log. The next digest will take it into account.</p>
  <form method="POST" action="/form?token=${tokenParam}">
    <input type="hidden" name="date" value="${escapeHtml(sanitize(date, 10))}">
    <textarea name="text" placeholder="Want more of something? Less of something? New interest to explore? Anything goes." maxlength="500"></textarea>
    <br>
    <button type="submit">Send feedback</button>
  </form>
</body>
</html>`;

  return new Response(html, { headers: { "Content-Type": "text/html" } });
}

async function handleFormSubmit(request, url, env) {
  const formData = await request.formData();
  const date = formData.get("date") || "unknown";
  const text = formData.get("text") || "";

  if (!text.trim()) {
    return new Response(thankYouPage("(empty)", ""), {
      headers: { "Content-Type": "text/html" },
    });
  }

  const cleanDate = isValidDate(date) ? date : "unknown";
  const cleanText = sanitize(text, 500);
  const entry = `- **Freeform**: "${cleanText}"`;
  const failure = await appendFeedback(env, cleanDate, entry);
  if (failure) return writeFailureResponse([failure]);

  return new Response(thankYouPage("Your feedback", "freeform"), {
    headers: { "Content-Type": "text/html" },
  });
}

// Read a file from the repo, transform its contents, and write it back.
//
// appendFeedback, appendEvent and appendSource were three copies of this same
// GET -> guard -> decode -> PUT -> 409-retry skeleton, differing only in how
// they build the new content. The copies drifted twice: `sha: sha` was fixed
// in two of three, and the >1MB encoding guard, 404-as-create and error
// returns landed on two of three. One skeleton, one place to fix.
//
// `transform(currentContent)` returns the new content, or null to mean "no
// write needed" (a duplicate entry, an already-recorded event) — which is a
// success, not a failure. Returns null on success, or an {error, ...} object.
async function updateRepoFile(env, path, { message, transform, retries = 2, createIfMissing = true }) {
  const apiBase = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${path}`;
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github.v3+json",
    "User-Agent": "morning-digest-feedback",
  };

  // A 404 means "not created yet" — create it. Any other non-OK is a real
  // failure the caller surfaces to the user rather than silently swallowing.
  let currentContent = "";
  let sha = undefined;
  const getRes = await fetch(apiBase, { headers });
  if (getRes.ok) {
    const fileData = await getRes.json();
    const guardError = encodingGuardError(fileData);
    if (guardError) {
      console.error(`Aborting ${path} write — ${guardError}`);
      return { error: "encoding guard", detail: guardError, status: getRes.status };
    }
    currentContent = decodeURIComponent(escape(atob(fileData.content.replace(/\n/g, ""))));
    sha = fileData.sha;
  } else if (getRes.status === 404) {
    // Not every file can be conjured from an empty string. A transform that
    // *inserts into* an existing document (sources.yml) produces a structurally
    // different one when handed "", so those callers opt out and report the
    // missing file instead of writing something the pipeline cannot read.
    if (!createIfMissing) {
      console.error(`Refusing to create ${path} from nothing — it should already exist`);
      return { error: "missing file", status: 404 };
    }
  } else {
    const body = await getRes.text();
    console.error(`Failed to get ${path}:`, body);
    return { error: "GET failed", status: getRes.status, body };
  }

  const newContent = transform(currentContent);
  if (newContent === null) return null;

  // sha is omitted (not undefined) when the file does not exist yet — the
  // GitHub API reads its absence as "create".
  const putBody = {
    message,
    content: btoa(unescape(encodeURIComponent(newContent))),
  };
  if (sha) putBody.sha = sha;

  const putRes = await fetch(apiBase, {
    method: "PUT",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify(putBody),
  });

  if (!putRes.ok) {
    if (putRes.status === 409 && retries > 0) {
      // Re-fetch and re-transform against the content that actually won.
      console.log(`${path} SHA conflict, retrying (${retries} left)...`);
      return updateRepoFile(env, path, { message, transform, retries: retries - 1 });
    }
    const body = await putRes.text();
    console.error(`Failed to update ${path}:`, body);
    return { error: "PUT failed", status: putRes.status, body };
  }

  return null;
}

export async function appendFeedback(env, date, entry, retries = 2) {
  const dateHeader = `## ${date} (email)`;
  const marker = "<!-- New entries are added at the top -->";

  return updateRepoFile(env, "feedback-log.md", {
    message: `Add feedback for ${date}`,
    retries,
    transform: (current) => {
      // Duplicate check is scoped to this date's section only.
      if (sectionSlice(current, dateHeader).includes(entry)) {
        console.log("Duplicate feedback entry, skipping");
        return null;
      }
      if (current.includes(dateHeader)) {
        return current.replace(dateHeader, `${dateHeader}\n${entry}`);
      }
      const insertPoint = current.indexOf(marker);
      if (insertPoint !== -1) {
        const after = insertPoint + marker.length;
        return current.slice(0, after) + `\n\n${dateHeader}\n${entry}\n` + current.slice(after);
      }
      return current + `\n\n${dateHeader}\n${entry}\n`;
    },
  });
}

// Append one JSON line to the event's month shard (data/events/YYYY-MM.jsonl)
// in the configured GitHub repo. Mirrors the markdown feedback log so the
// python pipeline has a structured, machine-readable source of truth without
// changing the human-readable log. Sharding by month keeps each file ~10x
// under the GitHub Contents API's 1MB inline-read limit permanently — see
// encodingGuardError for what happens if a file ever crosses it anyway.
export async function appendEvent(env, event, retries = 2) {
  const line = JSON.stringify(event);

  return updateRepoFile(env, shardPath(event.date), {
    message: `Append event for ${event.date} (${event.kind})`,
    retries,
    transform: (current) => {
      // Idempotent on the event itself, ignoring ts: the webhook returns 500
      // on failure so Svix redelivers, and a redelivery carries a fresh
      // timestamp. Comparing only against the last line would let that
      // through whenever any other event landed in between.
      const trimmed = current.replace(/\n+$/, "");
      const alreadyRecorded = trimmed.split("\n").some((prev) => {
        if (!prev.trim()) return false;
        try {
          const p = JSON.parse(prev);
          return p.kind === event.kind && p.date === event.date &&
                 p.section === event.section && p.reaction === event.reaction;
        } catch {
          return false;
        }
      });
      if (alreadyRecorded) return null;
      return trimmed ? `${trimmed}\n${line}\n` : `${line}\n`;
    },
  });
}

function isValidUrl(str) {
  return /^https?:\/\/.+\..+/.test(str);
}

function handleSourcePage(url, env) {
  const tokenParam = encodeURIComponent(env.FEEDBACK_SECRET || "");
  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Suggest a Source</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 40px auto; padding: 20px; color: #1a1a1a; }
    h2 { font-size: 20px; }
    label { display: block; margin-top: 16px; font-size: 14px; font-weight: 600; color: #444; }
    input, select { width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; font-family: inherit; box-sizing: border-box; margin-top: 4px; }
    button { margin-top: 20px; padding: 10px 24px; background: #0066cc; color: white; border: none; border-radius: 8px; font-size: 15px; cursor: pointer; }
    button:hover { background: #0052a3; }
    .hint { font-size: 13px; color: #888; margin-top: 2px; }
  </style>
</head>
<body>
  <h2>Suggest a Source</h2>
  <p>Add an RSS feed or website to your digest. It'll be included in future feed rotation.</p>
  <form method="POST" action="/source?token=${tokenParam}">
    <label>Feed URL <span style="color:#c00;">*</span></label>
    <input type="url" name="url" placeholder="https://example.com/feed.xml" required>
    <p class="hint">RSS or Atom feed URL works best.</p>

    <label>Name</label>
    <input type="text" name="name" placeholder="e.g. Example Blog" maxlength="100">

    <label>Category</label>
    <select name="category">
      <option value="tech">Tech</option>
      <option value="ai">AI</option>
      <option value="finance">Finance</option>
      <option value="news">News</option>
      <option value="intellectual">Intellectual</option>
    </select>

    <br>
    <button type="submit">Add source</button>
  </form>
</body>
</html>`;

  return new Response(html, { headers: { "Content-Type": "text/html" } });
}

async function handleSourceSubmit(request, url, env) {
  const formData = await request.formData();
  const sourceUrl = (formData.get("url") || "").trim();
  const name = formData.get("name") || "";
  const category = formData.get("category") || "tech";

  if (!sourceUrl || !isValidUrl(sourceUrl)) {
    return new Response("Invalid URL. Must start with http:// or https://", { status: 400 });
  }

  const cleanName = sanitize(name, 100) || new URL(sourceUrl).hostname;
  const cleanCategory = sanitize(category, 30);

  const failure = await appendSource(env, sourceUrl, cleanName, cleanCategory);
  if (failure) return writeFailureResponse([failure]);

  return new Response(thankYouPage(`${cleanName}`, "your sources"), {
    headers: { "Content-Type": "text/html" },
  });
}

async function appendSource(env, sourceUrl, name, category, retries = 2) {
  return updateRepoFile(env, "sources.yml", {
    message: `Add user-suggested source: ${name}`,
    retries,
    // The transform below inserts into an existing document. Against "" it
    // would emit a bare indented list with no top-level key — valid YAML, but
    // a list, and pipeline/fetch.py does sources.get("rss", []).
    createIfMissing: false,
    transform: (current) => {
      if (current.includes(sourceUrl)) {
        console.log("Source already exists, skipping");
        return null;
      }
      const entry = `\n  # User-suggested\n  - url: ${sourceUrl}\n    name: ${name}\n    category: ${category}\n`;
      // Keep user-suggested feeds above the apis: block so the YAML stays valid.
      const apisIndex = current.indexOf("\napis:");
      return apisIndex !== -1
        ? current.slice(0, apisIndex) + entry + current.slice(apisIndex)
        : current + entry;
    },
  });
}

// Escape for HTML text and double-quoted attribute contexts. Not single-quoted
// attributes — add ' to the set before using it in one.
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// The counterpart to thankYouPage: shown when a write actually failed, so the
// user isn't told "tomorrow's digest will take this into account" when nothing
// was recorded. 502 (not 200) so uptime checks and webhook senders see it too.
function writeFailureResponse(failures) {
  console.error("Feedback write failed:", JSON.stringify(failures));
  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Couldn't save that</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 80px auto; padding: 20px; text-align: center; color: #1a1a1a; }
    .mark { font-size: 48px; margin-bottom: 16px; }
    h2 { font-size: 20px; margin-bottom: 8px; }
    p { color: #666; font-size: 15px; }
    code { font-size: 13px; color: #888; }
  </style>
</head>
<body>
  <div class="mark">\u26a0\ufe0f</div>
  <h2>Couldn't save that</h2>
  <p>Your reaction wasn't recorded \u2014 the digest repo rejected the write.</p>
  <p><code>${escapeHtml(failures.map((f) => f.error).join(", "))}</code></p>
</body>
</html>`;
  return new Response(html, { status: 502, headers: { "Content-Type": "text/html" } });
}

function thankYouPage(label, section) {
  // Escape HTML in label/section to prevent XSS
  const safeLabel = escapeHtml(label);
  const safeSection = escapeHtml(section);
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Thanks!</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 80px auto; padding: 20px; text-align: center; color: #1a1a1a; }
    .check { font-size: 48px; margin-bottom: 16px; }
    h2 { font-size: 20px; margin-bottom: 8px; }
    p { color: #666; font-size: 15px; }
  </style>
</head>
<body>
  <div class="check">✓</div>
  <h2>Got it!</h2>
  <p>Recorded: <strong>${safeLabel}</strong>${safeSection ? ` for ${safeSection}` : ""}.</p>
  <p>Tomorrow's digest will take this into account.</p>
  <p style="font-size:13px;color:#999;">This tab will close automatically…</p>
  <script>setTimeout(function(){ window.close(); }, 1500);</script>
</body>
</html>`;
}
