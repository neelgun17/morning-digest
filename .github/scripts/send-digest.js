const fs = require("fs");
const { marked } = require("marked");

const file = process.argv[2];
const date = process.argv[3];

if (!file || !date) {
  console.error("Usage: node send-digest.js <file> <date>");
  process.exit(1);
}

const RESEND_API_KEY = process.env.RESEND_API_KEY;
const EMAIL_TO = process.env.EMAIL_TO;
const EMAIL_FROM = process.env.EMAIL_FROM || "Morning Digest <digest@resend.dev>";
const FEEDBACK_URL = process.env.FEEDBACK_URL;
const FEEDBACK_SECRET = process.env.FEEDBACK_SECRET;

if (!RESEND_API_KEY || !EMAIL_TO) {
  console.error("Missing RESEND_API_KEY or EMAIL_TO");
  process.exit(1);
}

const markdown = fs.readFileSync(file, "utf-8");

// Parse sections for feedback buttons
const sections = [];
const sectionRegex = /^### \d+\.\s+(.+?)(?:\s*\(.*?\))?\s*$/gm;
let match;
while ((match = sectionRegex.exec(markdown)) !== null) {
  sections.push({
    title: match[1].trim(),
    position: match.index + match[0].length,
  });
}

// Build feedback buttons HTML for a section
function feedbackButtons(sectionTitle) {
  if (!FEEDBACK_URL) return "";

  const reactions = [
    { key: "more_like_this", label: "More like this", emoji: "👍" },
    { key: "go_deeper", label: "Go deeper", emoji: "🔬" },
    { key: "too_basic", label: "Too basic", emoji: "📈" },
    { key: "too_advanced", label: "Too advanced", emoji: "📉" },
    { key: "not_interested", label: "Not interested", emoji: "👋" },
  ];

  const links = reactions
    .map((r) => {
      const url = `${FEEDBACK_URL}/click?token=${encodeURIComponent(FEEDBACK_SECRET || "")}&date=${encodeURIComponent(date)}&section=${encodeURIComponent(sectionTitle)}&reaction=${r.key}`;
      return `<a href="${url}" style="display:inline-block;padding:6px 12px;margin:3px;border-radius:6px;background:#f0f0f0;color:#333;text-decoration:none;font-size:13px;">${r.emoji} ${r.label}</a>`;
    })
    .join(" ");

  const label = `<p style="margin:16px 0 4px 0;font-size:12px;color:#999;font-weight:600;text-transform:uppercase;">Feedback: ${sectionTitle}</p>`;
  return `${label}<div style="margin:0 0 24px 0;">${links}</div>`;
}

// Convert markdown to HTML and inject feedback buttons after each section
let htmlBody = "";
const lines = markdown.split("\n");
let currentSection = null;
let sectionBuffer = [];

function flushSection() {
  if (sectionBuffer.length === 0) return;
  const sectionMd = sectionBuffer.join("\n");
  htmlBody += marked.parse(sectionMd);
  if (currentSection) {
    htmlBody += feedbackButtons(currentSection);
  }
  sectionBuffer = [];
}

let inNewsBlock = false;

for (const line of lines) {
  const sectionMatch = line.match(/^### \d+\.\s+(.+?)(?:\s*\(.*?\))?\s*$/);
  if (sectionMatch) {
    flushSection();
    inNewsBlock = false;
    currentSection = sectionMatch[1].trim();
    sectionBuffer.push(line);
  } else if (line.match(/^## News/)) {
    flushSection();
    currentSection = null;
    inNewsBlock = true;
    sectionBuffer.push(line);
  } else if (line.startsWith("## Feedback")) {
    // Stop before the markdown feedback section — we replace it
    flushSection();
    if (inNewsBlock) {
      htmlBody += feedbackButtons("News");
    }
    break;
  } else {
    sectionBuffer.push(line);
  }
}
flushSection();
if (inNewsBlock) {
  htmlBody += feedbackButtons("News");
}

// Add freeform feedback section
if (FEEDBACK_URL) {
  htmlBody += `
    <div style="margin:32px 0;padding:20px;border-radius:8px;background:#f8f8f8;">
      <p style="margin:0 0 8px 0;font-weight:600;">Anything else on your mind?</p>
      <p style="margin:0;font-size:14px;color:#666;">
        <a href="${FEEDBACK_URL}/form?token=${encodeURIComponent(FEEDBACK_SECRET || "")}&date=${encodeURIComponent(date)}" style="color:#0066cc;">Open feedback form →</a>
      </p>
    </div>
  `;
}

// Wrap in email template
const html = `
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {
      font-family: Georgia, 'Times New Roman', serif;
      line-height: 1.7;
      color: #1a1a1a;
      max-width: 640px;
      margin: 0 auto;
      padding: 20px;
      background: #fff;
    }
    h1 { font-size: 24px; border-bottom: 2px solid #333; padding-bottom: 8px; }
    h2 { font-size: 20px; color: #333; margin-top: 32px; }
    h3 { font-size: 17px; color: #444; }
    a { color: #0066cc; }
    hr { border: none; border-top: 1px solid #e0e0e0; margin: 24px 0; }
    blockquote { border-left: 3px solid #ccc; margin: 16px 0; padding: 4px 16px; color: #555; }
    em { color: #666; }
    table { border-collapse: collapse; width: 100%; margin: 12px 0; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 14px; }
    th { background: #f5f5f5; }
    ul, ol { padding-left: 24px; }
    li { margin-bottom: 8px; }
  </style>
</head>
<body>
  ${htmlBody}
  <hr>
  <p style="font-size:12px;color:#999;text-align:center;">
    Morning Digest · <a href="https://github.com/neelgun17/morning-digest" style="color:#999;">How this works</a>
  </p>
</body>
</html>
`;

// Extract title from first H1
const titleMatch = markdown.match(/^# (.+)$/m);
const subject = titleMatch ? titleMatch[1] : `Morning Digest — ${date}`;

// Send via Resend
async function send() {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: EMAIL_FROM,
      to: [EMAIL_TO],
      subject: subject,
      html: html,
      tracking: { clicks: false },
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    console.error(`Resend error (${res.status}): ${err}`);
    process.exit(1);
  }

  const data = await res.json();
  console.log(`Email sent: ${data.id}`);
}

send();
