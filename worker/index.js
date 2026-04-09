export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

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
    if (url.pathname === "/open") return handleOpen(url, env, ctx);
    if (url.pathname === "/webhook" && request.method === "POST") return handleResendWebhook(request, env);

    return new Response("Not found", { status: 404 });
  },
};

// Tracking pixel endpoint — logs an email open when the image is loaded
async function handleOpen(url, env, ctx) {
  const date = url.searchParams.get("date");
  if (date && isValidDate(date)) {
    // Log the open without blocking the pixel response
    ctx.waitUntil(appendFeedback(env, date, "- **Read**: email opened"));
  }
  // Return a 1x1 transparent PNG
  const pixel = new Uint8Array([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d,
    0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00,
    0x0a, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x62, 0x00, 0x00, 0x00, 0x02,
    0x00, 0x01, 0xe2, 0x21, 0xbc, 0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
    0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
  ]);
  return new Response(pixel, {
    headers: {
      "Content-Type": "image/png",
      "Cache-Control": "no-store, no-cache, must-revalidate",
    },
  });
}

// Resend webhook endpoint — handles email.opened events
async function handleResendWebhook(request, env) {
  try {
    const body = await request.json();
    const eventType = body.type;

    if (eventType === "email.opened") {
      // Extract date from subject line
      // Supports both "Daily Digest — Thursday, April 9, 2026" and "Morning Digest — 2026-04-02"
      const subject = body.data?.subject || "";
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

      if (date) {
        await appendFeedback(env, date, "- **Read**: email opened");
      }
    }

    return new Response("OK", { status: 200 });
  } catch (e) {
    console.error("Webhook error:", e);
    return new Response("Bad request", { status: 400 });
  }
}

// Strip markdown/HTML formatting to prevent injection into feedback-log.md
function sanitize(input, maxLength = 200) {
  return input
    .replace(/[[\](){}|`*_~#<>!]/g, "")
    .replace(/\n/g, " ")
    .trim()
    .slice(0, maxLength);
}

// Validate date format (YYYY-MM-DD)
function isValidDate(date) {
  return /^\d{4}-\d{2}-\d{2}$/.test(date);
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

  const validReactions = ["more_like_this", "go_deeper", "too_basic", "too_advanced", "not_interested"];
  if (!validReactions.includes(reaction)) {
    return new Response("Invalid reaction", { status: 400 });
  }

  const cleanSection = sanitize(section, 100);
  const entry = `- **${cleanSection}**: ${reaction}`;
  await appendFeedback(env, date, entry);

  const label = {
    more_like_this: "More like this",
    go_deeper: "Go deeper",
    too_basic: "Too basic",
    too_advanced: "Too advanced",
    not_interested: "Not interested",
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
    <input type="hidden" name="date" value="${sanitize(date, 10)}">
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
  await appendFeedback(env, cleanDate, entry);

  return new Response(thankYouPage("Your feedback", "freeform"), {
    headers: { "Content-Type": "text/html" },
  });
}

async function appendFeedback(env, date, entry, retries = 2) {
  const repo = env.GITHUB_REPO;
  const token = env.GITHUB_TOKEN;
  const path = "feedback-log.md";
  const apiBase = `https://api.github.com/repos/${repo}/contents/${path}`;
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github.v3+json",
    "User-Agent": "morning-digest-feedback",
  };

  // Get current file
  const getRes = await fetch(apiBase, { headers });
  if (!getRes.ok) {
    console.error("Failed to get feedback-log.md:", await getRes.text());
    return;
  }

  const fileData = await getRes.json();
  const currentContent = atob(fileData.content.replace(/\n/g, ""));
  const sha = fileData.sha;

  // Check for duplicate entry
  if (currentContent.includes(entry)) {
    console.log("Duplicate feedback entry, skipping");
    return;
  }

  // Check if there's already a section for this date
  const dateHeader = `## ${date} (email)`;
  let newContent;

  if (currentContent.includes(dateHeader)) {
    // Append to existing date section
    newContent = currentContent.replace(dateHeader, `${dateHeader}\n${entry}`);
  } else {
    // Insert new date section after the header comment
    const insertPoint = currentContent.indexOf("<!-- New entries are added at the top -->");
    if (insertPoint !== -1) {
      const after = insertPoint + "<!-- New entries are added at the top -->".length;
      newContent =
        currentContent.slice(0, after) +
        `\n\n${dateHeader}\n${entry}\n` +
        currentContent.slice(after);
    } else {
      newContent = currentContent + `\n\n${dateHeader}\n${entry}\n`;
    }
  }

  // Update file
  const putRes = await fetch(apiBase, {
    method: "PUT",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `Add feedback for ${date}`,
      content: btoa(unescape(encodeURIComponent(newContent))),
      sha: sha,
    }),
  });

  if (!putRes.ok) {
    if (putRes.status === 409 && retries > 0) {
      console.log(`SHA conflict, retrying (${retries} left)...`);
      return appendFeedback(env, date, entry, retries - 1);
    }
    console.error("Failed to update feedback-log.md:", await putRes.text());
  }
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

  await appendSource(env, sourceUrl, cleanName, cleanCategory);

  return new Response(thankYouPage(`${cleanName}`, "your sources"), {
    headers: { "Content-Type": "text/html" },
  });
}

async function appendSource(env, sourceUrl, name, category, retries = 2) {
  const repo = env.GITHUB_REPO;
  const token = env.GITHUB_TOKEN;
  const path = "sources.yml";
  const apiBase = `https://api.github.com/repos/${repo}/contents/${path}`;
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github.v3+json",
    "User-Agent": "morning-digest-feedback",
  };

  const getRes = await fetch(apiBase, { headers });
  if (!getRes.ok) {
    console.error("Failed to get sources.yml:", await getRes.text());
    return;
  }

  const fileData = await getRes.json();
  const currentContent = atob(fileData.content.replace(/\n/g, ""));
  const sha = fileData.sha;

  // Check for duplicate URL
  if (currentContent.includes(sourceUrl)) {
    console.log("Source already exists, skipping");
    return;
  }

  // Append new source before the apis: section
  const entry = `\n  # User-suggested\n  - url: ${sourceUrl}\n    name: ${name}\n    category: ${category}\n`;
  let newContent;
  const apisIndex = currentContent.indexOf("\napis:");
  if (apisIndex !== -1) {
    newContent = currentContent.slice(0, apisIndex) + entry + currentContent.slice(apisIndex);
  } else {
    newContent = currentContent + entry;
  }

  const putRes = await fetch(apiBase, {
    method: "PUT",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `Add user-suggested source: ${name}`,
      content: btoa(unescape(encodeURIComponent(newContent))),
      sha: sha,
    }),
  });

  if (!putRes.ok) {
    if (putRes.status === 409 && retries > 0) {
      console.log(`SHA conflict, retrying (${retries} left)...`);
      return appendSource(env, sourceUrl, name, category, retries - 1);
    }
    console.error("Failed to update sources.yml:", await putRes.text());
  }
}

function thankYouPage(label, section) {
  // Escape HTML in label/section to prevent XSS
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const safeLabel = esc(label);
  const safeSection = esc(section);
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
</body>
</html>`;
}
