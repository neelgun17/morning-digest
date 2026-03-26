export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/click") return handleClick(url, env);
    if (url.pathname === "/form" && request.method === "GET") return handleFormPage(url);
    if (url.pathname === "/form" && request.method === "POST") return handleFormSubmit(request, env);

    return new Response("Not found", { status: 404 });
  },
};

async function handleClick(url, env) {
  const date = url.searchParams.get("date");
  const section = url.searchParams.get("section");
  const reaction = url.searchParams.get("reaction");

  if (!date || !section || !reaction) {
    return new Response("Missing parameters", { status: 400 });
  }

  const validReactions = ["more_like_this", "go_deeper", "too_basic", "too_advanced", "not_interested"];
  if (!validReactions.includes(reaction)) {
    return new Response("Invalid reaction", { status: 400 });
  }

  const entry = `- **${section}**: ${reaction}`;
  await appendFeedback(env, date, entry);

  const label = {
    more_like_this: "More like this",
    go_deeper: "Go deeper",
    too_basic: "Too basic",
    too_advanced: "Too advanced",
    not_interested: "Not interested",
  }[reaction];

  return new Response(thankYouPage(label, section), {
    headers: { "Content-Type": "text/html" },
  });
}

function handleFormPage(url) {
  const date = url.searchParams.get("date") || "";
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
  <form method="POST" action="/form">
    <input type="hidden" name="date" value="${date}">
    <textarea name="text" placeholder="Want more of something? Less of something? New interest to explore? Anything goes."></textarea>
    <br>
    <button type="submit">Send feedback</button>
  </form>
</body>
</html>`;

  return new Response(html, { headers: { "Content-Type": "text/html" } });
}

async function handleFormSubmit(request, env) {
  const formData = await request.formData();
  const date = formData.get("date") || "unknown";
  const text = formData.get("text") || "";

  if (!text.trim()) {
    return new Response(thankYouPage("(empty)", ""), {
      headers: { "Content-Type": "text/html" },
    });
  }

  const entry = `- **Freeform**: "${text.trim()}"`;
  await appendFeedback(env, date, entry);

  return new Response(thankYouPage("Your feedback", "freeform"), {
    headers: { "Content-Type": "text/html" },
  });
}

async function appendFeedback(env, date, entry) {
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
    // Retry once on SHA conflict
    if (putRes.status === 409) {
      console.log("SHA conflict, retrying...");
      return appendFeedback(env, date, entry);
    }
    console.error("Failed to update feedback-log.md:", await putRes.text());
  }
}

function thankYouPage(label, section) {
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
  <p>Recorded: <strong>${label}</strong>${section ? ` for ${section}` : ""}.</p>
  <p>Tomorrow's digest will take this into account.</p>
</body>
</html>`;
}
