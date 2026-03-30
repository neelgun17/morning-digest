# Morning Digest

**Wake up to a personalized daily learning digest, delivered to your inbox by AI.**

Every morning, a Claude Code agent searches curated sources and the web for high-quality content matched to your interests, renders it as a clean HTML email, and sends it to you. You read it with your coffee. You tap a reaction button. The next day's digest gets better.

**30 minutes of learning + 15 minutes of news, zero effort to curate.**

See what a digest looks like: [`daily/sample-digest.md`](daily/sample-digest.md)

---

## How It Works

```
┌──────────────────────────────────────────────────┐
│  SOURCES                                          │
│  Curated RSS feeds + Hacker News + Web Search     │
└──────────────────┬───────────────────────────────┘
                   │ 7am daily
                   ▼
┌──────────────────────────────────────────────────┐
│  AI AGENT (Claude Code, scheduled)                │
│  Reads your interests + past feedback             │
│  Generates personalized digest                    │
│  Commits to your private repo                     │
└──────────────────┬───────────────────────────────┘
                   │ triggers GitHub Action
                   ▼
┌──────────────────────────────────────────────────┐
│  EMAIL                                            │
│  Rendered HTML with one-click feedback buttons    │
│  Arrives in your inbox, ready to read             │
└──────────────────┬───────────────────────────────┘
                   │ you tap a button
                   ▼
┌──────────────────────────────────────────────────┐
│  FEEDBACK (Cloudflare Worker)                     │
│  Logs your reaction to the repo                   │
│  Tomorrow's digest adapts                         │
└──────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites

- [Claude Code](https://claude.ai/code) with scheduled agents
- [GitHub CLI](https://cli.github.com) (`gh`), logged in
- Git

### Step 1 — Create your private repo

```bash
git clone https://github.com/neelgun17/morning-digest.git
cd morning-digest
./setup.sh
```

The script creates a **private** repo, copies templates, and pushes to GitHub.

### Step 2 — Edit your interest profile

```bash
cd ../my-morning-digest   # or whatever you named it
```

Open `interests.md` and replace the examples with your own interests. The file has comments explaining each field. Also customize `sources.yml` with RSS feeds relevant to your interests.

### Step 2.5 — Connect GitHub to Claude Code

The scheduled agent needs access to your private repo. Install the Claude GitHub App:

1. Go to **https://claude.ai/code/onboarding?magic=github-app-setup**
2. Install the app and grant access to your private repo

Without this, the agent can't clone or push to your repo.

### Step 3 — Schedule the daily agent

> **Important:** This project uses **Claude Code scheduled triggers** — the agent runs on Anthropic's cloud for free. You do **not** need a GitHub Actions workflow or an Anthropic API key.

Open Claude Code inside your private repo and run:

```
/schedule
```

Then tell it:

```
Create a scheduled trigger called "morning-digest" that runs daily at 7am my time.
Repo: <your private repo URL>
Tools needed: Bash, Read, Write, Edit, Glob, Grep, WebSearch
Use the prompt from .github/prompts/digest-agent.txt
```

The full agent prompt lives in [`.github/prompts/digest-agent.txt`](.github/prompts/digest-agent.txt) — edit it anytime to change how your digest is generated.

### Step 4 — Set up email delivery (optional but recommended)

This sends your digest as a beautiful HTML email with one-click feedback buttons.

**a) Email via Resend:**

1. Sign up at [resend.com](https://resend.com) (free tier: 3,000 emails/month)
2. Get your API key
3. Add these secrets to your GitHub repo (Settings → Secrets → Actions):
   - `RESEND_API_KEY` — your Resend API key
   - `EMAIL_TO` — your email address
   - `EMAIL_FROM` — (optional) defaults to `Morning Digest <digest@resend.dev>`

The GitHub Action at `.github/workflows/email-digest.yml` fires automatically when the agent pushes a new digest.

**b) One-click feedback via Cloudflare Worker:**

1. Install [Wrangler](https://developers.cloudflare.com/workers/wrangler/install-and-update/): `npm install -g wrangler`
2. Log in: `wrangler login`
3. Edit `worker/wrangler.toml` — set `GITHUB_REPO` to your `username/repo-name`
4. Deploy:

```bash
cd worker
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN
# paste a GitHub personal access token with repo write access
```

5. Copy the Worker URL (e.g., `https://morning-digest-feedback.yourname.workers.dev`)
6. Add it as a GitHub repo secret: `FEEDBACK_URL`

Now each section in your email gets these reaction buttons:

| Button | What it tells the system |
|--------|------------------------|
| 👍 More like this | Topic, depth, and source all worked |
| 🔬 Go deeper | Dedicate more time to this topic |
| 📈 Too basic | Right topic, increase difficulty |
| 📉 Too advanced | Right topic, simplify |
| 👋 Not interested | Deprioritize this topic |

Plus a freeform feedback form for anything else.

---

## What Happens When You Miss a Day

The agent detects whether you've been reading by checking if feedback exists on recent digests:

| Scenario | What you get |
|----------|-------------|
| **Active** (feedback on yesterday's digest) | Full digest — 30 min learning + 15 min news |
| **Missed 1-2 days** | Welcome-back digest — news catch-up prioritized, plus one short learning item |
| **Missed 3+ days** | Catch-up digest — news-only summary of the most significant stories. Learning resumes next day. |

News goes stale, learning content doesn't — so when you've been away, the system gets you current on the world first.

---

## File Structure

```
my-morning-digest/
  interests.md               ← Your interest profile (edit this)
  feedback-log.md             ← Feedback history (auto-updated by Worker)
  sources.yml                 ← RSS feeds and APIs for content sourcing
  daily/
    2026-03-26.md             ← Auto-generated daily digests
    ...
  .github/
    workflows/email-digest.yml  ← Sends email on new digest
    scripts/send-digest.js      ← Markdown → HTML email renderer
  worker/
    index.js                  ← Cloudflare Worker for feedback
    wrangler.toml             ← Worker config
```

---

## Customizing Content Sources

Edit `sources.yml` to add RSS feeds relevant to your interests:

```yaml
rss:
  - url: https://example.com/feed.xml
    name: Example Blog
    category: tech
```

The agent checks these feeds first, then fills gaps with web search. Higher quality sources = better digests.

---

## Updating Your Interests

Review `interests.md` every few months:

- Change **priority** (high/medium/low) to shift what appears more often
- Change **depth** (beginner/intermediate/advanced) as you learn
- Add or remove topics anytime
- Adjust your **preferences** (articles vs podcasts, etc.)

---

## FAQ

**Can I use it without email?**
Yes. Skip Step 4 entirely. The digests still commit to your repo — just `git pull` or read on GitHub.

**Can I use a different AI model?**
Yes. Specify a different model when scheduling (e.g., `claude-opus-4-6` for deeper analysis, `claude-haiku-4-5` for faster/cheaper).

**Can I change the schedule?**
Yes. Run `/schedule` in Claude Code, or manage at [claude.ai/code/scheduled](https://claude.ai/code/scheduled).

**Can I get digests on specific days only?**
Yes. Use a cron like `0 12 * * 1-5` for weekdays, or `0 12 * * 1,3,5` for Mon/Wed/Fri.

**How do I stop it?**
Disable the trigger at [claude.ai/code/scheduled](https://claude.ai/code/scheduled).

**What does it cost?**
- Claude Code scheduled agents: included in your plan
- Resend email: free tier (3,000/month)
- Cloudflare Worker: free tier (100,000 requests/day)
- Total: $0
