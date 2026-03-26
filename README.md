# Morning Digest

A system that generates a personalized daily learning digest using Claude Code. Wake up to 45 minutes of curated content tailored to your interests — 30 minutes of learning material and 10-15 minutes of news.

## How It Works

1. You fill out an **interest profile** with your professional, intellectual, and physical interests
2. A **scheduled Claude Code agent** runs every morning, searches the web, and generates a digest
3. The digest lands in your repo as a markdown file with direct links to articles, podcasts, and videos
4. You **leave feedback** inline, and the next day's digest adapts

The system improves continuously — it reads your feedback, avoids repeating topics, and rotates across your interests throughout the week.

## Setup

### 1. Clone and create your private repo

```bash
git clone https://github.com/neelgun17/morning-digest.git my-morning-digest
cd my-morning-digest
rm -rf .git
git init
```

Create a **private** repo on GitHub and push:

```bash
gh repo create my-morning-digest --private --source=. --push
```

### 2. Set up your interest profile

```bash
cp interests.template.md interests.md
cp feedback-log.template.md feedback-log.md
```

Edit `interests.md` with your actual interests, priorities, and depth levels. Be specific — the more detail you provide, the better your digests will be.

### 3. Schedule the agent

Open Claude Code and run:

```
/schedule
```

Create a daily trigger with:
- **Name**: `morning-digest`
- **Schedule**: Your preferred morning time (e.g., 7am your timezone)
- **Repo**: Your private repo URL
- **Tools**: `Bash, Read, Write, Edit, Glob, Grep, WebSearch`

Use this prompt for the agent:

```
You are a daily learning digest generator. Your job is to create a personalized morning reading digest.

1. Read `interests.md` for the user's interest profile and priorities.
2. Read `feedback-log.md` for recent feedback.
3. Check the 3 most recent files in `daily/` to avoid repeating topics. Rotate interests across the week.
4. Use WebSearch to find 2-3 high-quality articles from reputable sources matching the interests. Prioritize written content. Total read time ~30 minutes.
5. Use WebSearch to find 3-5 current news items across the user's news categories. Summarize each in 1-2 sentences with links.
6. Write the digest to `daily/YYYY-MM-DD.md` using today's date.
7. Include a feedback table at the bottom for inline reactions.
8. Commit and push with message: "Add daily digest for YYYY-MM-DD"
```

### 4. Daily routine

- Pull or check GitHub each morning for your new digest
- Read with your coffee (~45 min)
- Edit the feedback section at the bottom of the daily file
- The next morning's digest adapts

## File Structure

```
morning-digest/
  interests.md          # Your interest profile (create from template)
  feedback-log.md       # Running feedback history (create from template)
  daily/
    2026-03-26.md       # Daily digests auto-generated here
    2026-03-27.md
    ...
  interests.template.md # Template for new users
  feedback-log.template.md
  README.md
```

## Updating Your Interests

Review and update `interests.md` every few months. The structured format makes it easy to:
- Adjust priority levels as interests shift
- Change depth as you learn more
- Add or remove topics entirely
- Modify content preferences

## Requirements

- [Claude Code](https://claude.ai/code) with scheduled agents enabled
- A GitHub account
